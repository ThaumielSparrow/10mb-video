import math
import re
import subprocess
import json
import os


def available_cpus(default=2):
    """CPUs this container may actually use, for ffmpeg's -threads.

    Inside a CFS-quota container (HF Spaces), os.cpu_count() and ffmpeg's own
    auto-detect both report the *host's* cores, so "auto" threading spawns far
    more threads than the 2 vCPUs we get. FFMPEG_THREADS overrides; otherwise
    use the cgroup v2/v1 quota; otherwise `default` (HF cpu-basic's 2 vCPUs).
    """
    env = os.environ.get("FFMPEG_THREADS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            quota, period = f.read().split()[:2]
        if quota != "max":
            return max(1, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as f:
            quota = int(f.read())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
            period = int(f.read())
        if quota > 0 and period > 0:
            return max(1, math.ceil(quota / period))
    except (OSError, ValueError):
        pass
    return default


def _rotation(stream):
    """Display rotation in degrees from the display-matrix side data (or the
    legacy 'rotate' tag older muxers wrote). Phones record portrait video as
    landscape frames plus a 90/-90 rotation, which ffmpeg applies on decode."""
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                return int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                return 0
    try:
        return int((stream.get("tags") or {}).get("rotate", 0))
    except (TypeError, ValueError):
        return 0

def _parse_fps(value):
    """Parse ffprobe's avg_frame_rate ('30000/1001', '30/1', '24') into float."""
    if not value or value == "0/0":
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return None
            return float(num) / den_f
        return float(value)
    except (TypeError, ValueError):
        return None


def get_video_metadata(input_path):
    """
    Returns a dictionary containing duration, audio presence,
    original bitrate, file size, the video stream's codec, width/height, and
    fps, plus the file extension.

    width/height are *display* dimensions (rotation applied), because ffmpeg
    autorotates on decode — every filter we build sees the rotated frame.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration,size,bit_rate"
            ":stream=codec_type,codec_name,width,height,avg_frame_rate"
            ":stream_side_data=rotation:stream_tags=rotate",
            "-of", "json",
            input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)

        fmt = data["format"]
        duration = float(fmt.get("duration", 0))
        size_bytes = float(fmt.get("size", 0))

        streams = data.get("streams", [])
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        width = video_stream.get("width") if video_stream else None
        height = video_stream.get("height") if video_stream else None
        fps = _parse_fps(video_stream.get("avg_frame_rate")) if video_stream else None
        if video_stream and abs(_rotation(video_stream)) % 180 == 90:
            width, height = height, width

        if "bit_rate" in fmt and fmt["bit_rate"] != "N/A":
            bitrate = float(fmt["bit_rate"])
        else:
            bitrate = (size_bytes * 8) / duration if duration > 0 else 0

        return {
            "duration": duration,
            "size_bytes": size_bytes,
            "bitrate": bitrate,
            "has_audio": has_audio,
            "video_codec": video_stream.get("codec_name") if video_stream else None,
            "container_ext": os.path.splitext(input_path)[1].lower(),
            "width": width,
            "height": height,
            "fps": fps,
        }
    except Exception as e:
        print(f"Error probing video: {e}")
        return None


# Standard resolution ladder (height), descending. We always scale on the
# height axis and let ffmpeg's -vf scale=-2:H pick a width that preserves
# aspect ratio and stays divisible by 2.
RESOLUTION_LADDER = [2160, 1440, 1080, 720, 480, 360]

# Bits per pixel per frame target. ~0.05 is roughly where x264 stops
# producing obvious blocking on typical content. Above this the output
# looks "good"; below it, downscaling to the next lower resolution gives a
# better-looking result at the same bitrate.
QUALITY_BPP_TARGET = 0.05
DEFAULT_FPS = 30.0


def pick_auto_resolution(source_height, source_width, source_fps, video_bitrate_bps, bpp_target=QUALITY_BPP_TARGET):
    """
    Compute the largest standard resolution height where the per-pixel-per-frame
    bitrate budget meets bpp_target (QUALITY_BPP_TARGET for x264; encoders that
    need more bits per pixel pass a higher one). Caps at source dimensions — we
    never upscale.

    bpp = bitrate / (width * height * fps). Higher = better visual quality.
    For each candidate height we compute width = height * (source aspect ratio)
    rounded to an even number (x264 requirement), then bpp at that resolution.

    Falls back to a duration/bitrate-based heuristic when fps or dimensions
    aren't available (degraded sources, missing streams, etc.).
    """
    if not source_height or video_bitrate_bps is None or video_bitrate_bps <= 0:
        return source_height or 0

    # Fallback path when we can't compute bpp accurately.
    if not source_width or not source_fps or source_fps <= 0:
        kbps = video_bitrate_bps / 1000
        if kbps >= 2500:
            return min(1080, source_height)
        if kbps >= 1200:
            return min(720, source_height)
        if kbps >= 500:
            return min(480, source_height)
        return min(360, source_height)

    aspect = source_width / source_height
    candidates = [h for h in RESOLUTION_LADDER if h <= source_height]
    if not candidates:
        return source_height  # source is below the smallest preset; leave alone

    for h in candidates:
        w = int(round(h * aspect))
        if w % 2:
            w += 1
        pixels_per_sec = w * h * source_fps
        if pixels_per_sec <= 0:
            continue
        bpp = video_bitrate_bps / pixels_per_sec
        if bpp >= bpp_target:
            return h

    # Every candidate is below target — pick the smallest (which has the
    # highest bpp given the same bitrate budget).
    return candidates[-1]

def pick_auto_fps_cap(source_fps, source_width, source_height, video_bitrate_bps, cap=30):
    """
    Decide whether to cap framerate. Returns the cap (e.g. 30) or 0 for no cap.

    Auto-cap fires only when bits-per-pixel-per-frame at source dimensions is
    below half of QUALITY_BPP_TARGET — i.e., the encoder is so starved for bits
    that trading half the frames for twice the per-frame budget is clearly a
    win. At healthier bitrates we keep source fps and let Auto resolution
    handle quality.

    This runs *before* pick_auto_resolution so resolution decisions reflect the
    post-cap effective fps. Order matters: capping first means Auto resolution
    sees a doubled per-frame budget and can keep a higher resolution.

    Never caps below 30 fps — below that motion looks choppy regardless of
    content. Never caps when the source is already at or below the cap.
    """
    if not source_fps or source_fps <= cap:
        return 0
    if not source_width or not source_height or video_bitrate_bps <= 0:
        return 0
    bpp = video_bitrate_bps / (source_width * source_height * source_fps)
    return cap if bpp < QUALITY_BPP_TARGET / 2 else 0


# Final stats line ffmpeg prints after a run, e.g. (5.1) "video:77986kB audio:591kB
# subtitle:0kB other streams:0kB ..." or (7.x+) the same with "KiB". Both are 1024-byte units.
_STREAM_SIZES_RE = re.compile(
    r"video:\s*(\d+)\s*Ki?B\s+audio:\s*(\d+)\s*Ki?B\s+subtitle:\s*(\d+)\s*Ki?B"
    r"\s+other streams:\s*(\d+)\s*Ki?B",
    re.IGNORECASE,
)


def get_trim_bitrate(input_path, start, end):
    """
    Performs a Stream Copy trim to measure the EXACT bitrate
    of the specific section the user selected.
    This prevents inflating a simple scene (like a black screen) to a high bitrate.

    The copy goes to ffmpeg's null muxer and we read the per-stream byte totals
    from its final stats line, so nothing is written to disk — the old temp-file
    approach wrote up to the whole (500 MB) source to the Space's disk per trim.
    """
    duration = float(end) - float(start)
    if duration <= 0:
        return None

    try:
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-nostats",
            "-ss", str(start),
            "-to", str(end),
            "-i", input_path,
            "-c", "copy",
            "-map", "0",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=True)
        matches = _STREAM_SIZES_RE.findall(result.stderr)
        if not matches:
            return None
        size_bytes = sum(int(kib) for kib in matches[-1]) * 1024
        return (size_bytes * 8) / duration if size_bytes else None

    except Exception:
        return None


def compute_bitrate_plan(target_mb, duration, has_audio, remove_audio, source_bitrate_cap):
    """
    Single source of truth for size-targeting math. Returns
    (video_bitrate_bps, audio_bitrate_bps) or None if inputs are invalid.

    Both VideoCompressor.compress and the UI's estimate call this so the
    pre-encode summary always matches what the encoder will actually target.
    """
    if duration is None or duration <= 0 or target_mb is None or target_mb <= 0:
        return None

    safety_buffer = 0.90 if target_mb <= 20 else 0.95
    total_bits_allowed = target_mb * 1024 * 1024 * safety_buffer * 8

    if remove_audio or not has_audio:
        audio_bitrate = 0
    else:
        audio_bitrate = 128 * 1024
        naive_video = (total_bits_allowed / duration) - audio_bitrate
        if naive_video < (200 * 1024):
            audio_bitrate = 64 * 1024

    video_bitrate = (total_bits_allowed / duration) - audio_bitrate

    if source_bitrate_cap and video_bitrate > source_bitrate_cap:
        video_bitrate = source_bitrate_cap
    if video_bitrate < 10000:
        video_bitrate = 10000

    return video_bitrate, audio_bitrate


# (container, video codec) pairs gr.Video treats as browser-playable — mirrors
# gradio.processing_utils.video_is_playable. Any other file handed to the output
# component gets silently re-encoded by Gradio (libx264 CRF 23, unthrottled
# threads, ignoring our size target), so we never return one.
_BROWSER_PLAYABLE = {
    (".mp4", "h264"), (".mp4", "av1"),
    (".webm", "vp9"), (".webm", "vp8"), (".webm", "av1"),
    (".ogg", "theora"),
}
# Codecs that can be stream-copied into .mp4 and still pass the check above.
_MP4_REMUXABLE = {"h264", "av1"}


def pick_passthrough(meta, target_mb, is_trimmed, remove_audio):
    """Decide whether a source already under target can skip re-encoding.

    Returns (mode, ext): ("unchanged", None) to hand the source back as-is,
    ("remux", ext) to stream-copy it into a new file with extension ext
    (dropping audio if asked), or (None, None) when it needs a real encode.
    """
    if not meta or is_trimmed or target_mb is None:
        return None, None
    if meta["size_bytes"] >= target_mb * 1024 * 1024:
        return None, None
    ext, codec = meta.get("container_ext"), meta.get("video_codec")
    if (ext, codec) in _BROWSER_PLAYABLE:
        return ("remux", ext) if remove_audio else ("unchanged", None)
    if codec in _MP4_REMUXABLE:
        return "remux", ".mp4"
    return None, None


def estimate_video_bitrate(meta, target_mb, remove_audio, duration):
    """Thin wrapper for the UI summary; returns the video bitrate only."""
    plan = compute_bitrate_plan(
        target_mb=target_mb,
        duration=duration,
        has_audio=bool(meta and meta.get("has_audio")),
        remove_audio=remove_audio,
        source_bitrate_cap=meta.get("bitrate") if meta else None,
    )
    return plan[0] if plan else None