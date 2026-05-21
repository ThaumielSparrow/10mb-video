import subprocess
import json
import os

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
    original bitrate, file size, the video stream's width/height, and fps.
    """
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration,size,bit_rate:stream=codec_type,width,height,avg_frame_rate",
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

        if "bit_rate" in fmt and fmt["bit_rate"] != "N/A":
            bitrate = float(fmt["bit_rate"])
        else:
            bitrate = (size_bytes * 8) / duration if duration > 0 else 0

        return {
            "duration": duration,
            "size_bytes": size_bytes,
            "bitrate": bitrate,
            "has_audio": has_audio,
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


def pick_auto_resolution(source_height, source_width, source_fps, video_bitrate_bps):
    """
    Compute the largest standard resolution height where the per-pixel-per-frame
    bitrate budget meets QUALITY_BPP_TARGET. Caps at source dimensions — we
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
        if bpp >= QUALITY_BPP_TARGET:
            return h

    # Every candidate is below target — pick the smallest (which has the
    # highest bpp given the same bitrate budget).
    return candidates[-1]

def get_trim_bitrate(input_path, start, end, work_dir):
    """
    Performs a temporary Stream Copy trim to measure the EXACT bitrate
    of the specific section the user selected.
    This prevents inflating a simple scene (like a black screen) to a high bitrate.

    work_dir must be a per-job directory so concurrent calls don't clobber each other.

    Bitrate is computed from the trimmed file's size and the requested duration
    (size * 8 / duration). This is an approximation — file size includes container
    overhead — but well within the precision needed to drive the size cap, and
    saves a redundant ffprobe call.
    """
    temp_check_file = os.path.join(work_dir, "temp_bitrate_check.mp4")
    duration = float(end) - float(start)
    if duration <= 0:
        return None

    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", input_path,
            "-c", "copy",
            "-map", "0",
            "-avoid_negative_ts", "make_zero",
            temp_check_file
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        if not os.path.exists(temp_check_file):
            return None
        size_bytes = os.path.getsize(temp_check_file)
        return (size_bytes * 8) / duration

    except Exception:
        return None
    finally:
        if os.path.exists(temp_check_file):
            try:
                os.remove(temp_check_file)
            except OSError:
                pass

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