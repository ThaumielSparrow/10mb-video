import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from utils import (
    QUALITY_BPP_TARGET,
    available_cpus,
    compute_bitrate_plan,
    get_trim_bitrate,
    get_video_metadata,
    pick_auto_fps_cap,
    pick_auto_resolution,
    pick_passthrough,
)

# Encode artifacts live in the platform tempdir so the working directory stays
# clean. On Docker (production) this maps to /tmp, which is already ephemeral.
OUTPUT_DIR = os.path.join(tempfile.gettempdir(), "10mb_video_outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Per-job subdirs in OUTPUT_DIR older than this get pruned at the start of each
# compress() call. Long enough that a user has time to download; short enough that
# the free-tier Space disk doesn't fill up.
OUTPUT_TTL_SECONDS = 3600

# Free-tier HF Spaces have limited RAM and the ffmpeg decode path can balloon
# quickly on large inputs. Reject sources above this size before we burn any
# encode time — users with bigger sources should trim/downscale locally first.
MAX_INPUT_MB = 500
MAX_INPUT_BYTES = MAX_INPUT_MB * 1024 * 1024

# Thread count for every ffmpeg stage (decoder, filters, x264). Left on "auto",
# ffmpeg sizes its decoder/filter pools from the *host's* core count, not the
# Space's 2-vCPU quota. See utils.available_cpus.
THREADS = str(available_cpus())
print(f"ffmpeg threads: {THREADS}")

# Decode-once: when we downscale or drop frames, pass 1 also writes its filtered
# frames to a lossless ffvhuff intermediate, and pass 2 encodes from that instead
# of decoding + scaling the full-res source a second time. On 2 vCPUs decode+scale
# is most of a superfast encode, so this cut a 60s 1080p60 H.264 job from 55.0s
# to 38.6s and a 30s 1080p60 HEVC job from 28.7s to 19.8s, with identical frames
# going into x264. ffvhuff came out ~1/3 of raw yuv420p on noisy footage; budget
# it at 1/2 and fall back to two full decodes when it wouldn't fit.
INTERMEDIATE_RATIO = 0.5
INTERMEDIATE_MAX_BYTES = 3 * 1024 ** 3

_FFMPEG = ["ffmpeg", "-nostdin", "-hide_banner"]

_RESOLUTION_HEIGHTS = {"720p": 720, "480p": 480, "360p": 360}

# Hard fps cap when the user picks "On" or when "Auto" judges the source
# starved enough. We never cap below 30 — choppier than that becomes noticeable
# on all content, not just sports/fast-motion.
_FPS_CAP = 30


def _resolve_target_fps(fps_mode, source_fps, source_width, source_height, video_bitrate_bps):
    """Translate fps_mode (Auto/On/Off) into the cap to apply (e.g. 30) or 0 for no cap.

    Called *before* _resolve_target_height so the resolution picker sees the
    post-cap effective fps. See pick_auto_fps_cap docstring for the math.
    """
    if not source_fps or source_fps <= _FPS_CAP:
        return 0
    if fps_mode == "Off":
        return 0
    if fps_mode == "On":
        return _FPS_CAP
    # "Auto" or anything unrecognized → defer to the bpp-based heuristic.
    return pick_auto_fps_cap(source_fps, source_width, source_height, video_bitrate_bps, cap=_FPS_CAP)


def _resolve_target_height(output_resolution, source_height, source_width, source_fps, video_bitrate_bps, bpp_target=QUALITY_BPP_TARGET):
    """Translate the UI's output_resolution choice into a target short-side length.

    Callers pass the short side as source_height and the long side as
    source_width, so "720p" means 720 on the short side for portrait video too.
    Returns 0 when no scale filter should be applied (Original, or when the
    requested height equals/exceeds source).
    """
    if not source_height:
        return 0
    if output_resolution == "Auto":
        picked = pick_auto_resolution(source_height, source_width, source_fps, video_bitrate_bps, bpp_target)
        return picked if picked < source_height else 0
    if output_resolution in _RESOLUTION_HEIGHTS:
        height = _RESOLUTION_HEIGHTS[output_resolution]
        return height if height < source_height else 0
    # "Original" or anything unrecognised: no downscale.
    return 0


def build_plan(meta, target_mb, remove_audio, start_time, end_time, output_resolution, fps_mode,
               source_bitrate_cap=None, bpp_target=QUALITY_BPP_TARGET):
    """Every encode decision for one job, made in one place.

    The server pipeline (VideoCompressor) and the browser's on-device encoder
    (via app.plan_job) both encode from this dict, so the summary the user sees,
    the x264 encode and the WebCodecs encode can't drift apart. Raises
    ValueError for an empty trim range or an impossible bitrate.

    width/height in meta are display dimensions; resolution choices apply to
    the short side so portrait phone video isn't squashed (e.g. to 270x480).
    """
    duration = meta["duration"]
    s_time = max(float(start_time) if start_time else 0.0, 0.0)
    e_time = min(float(end_time) if end_time else duration, duration)
    clip_duration = e_time - s_time
    if clip_duration <= 0:
        raise ValueError("Invalid start/end time.")
    is_trimmed = s_time > 0 or e_time < duration

    passthrough, remux_ext = pick_passthrough(meta, target_mb, is_trimmed, remove_audio)
    bitrates = compute_bitrate_plan(
        target_mb=target_mb,
        duration=clip_duration,
        has_audio=meta["has_audio"],
        remove_audio=remove_audio,
        source_bitrate_cap=source_bitrate_cap or meta["bitrate"],
    )
    if bitrates is None:
        raise ValueError("Invalid bitrate plan (check target and duration).")
    video_bitrate, audio_bitrate = bitrates

    source_width = meta.get("width") or 0
    source_height = meta.get("height") or 0
    source_short = min(source_width, source_height) if source_width else source_height
    source_long = max(source_width, source_height)
    source_fps = meta.get("fps") or 0

    # Resolve fps cap first so resolution picker sees the post-cap effective
    # fps. Order matters: capping fps doubles bits-per-frame, which means
    # Auto resolution can keep a higher resolution than it would otherwise.
    target_fps = _resolve_target_fps(fps_mode, source_fps, source_width, source_height, video_bitrate)
    effective_fps = target_fps or source_fps

    # "Auto" picks the height that gives decent quality at the chosen bitrate;
    # "Original" leaves it alone; fixed presets ("720p" etc.) are honored
    # as-is. We never upscale.
    target_short = _resolve_target_height(
        output_resolution, source_short, source_long, effective_fps, video_bitrate, bpp_target,
    )
    # What Auto would have picked, for the UI's "would look better" hint.
    auto_short = pick_auto_resolution(source_short, source_long, effective_fps, video_bitrate, bpp_target)

    # Exact output size, even on both axes (yuv420p). The server's ffmpeg scale
    # derives the same thing from scale=-2:N; the browser encoder needs it spelled out.
    out_short = (target_short or source_short) // 2 * 2
    out_long = round(out_short * source_long / source_short / 2) * 2 if source_short else 0
    portrait = source_width < source_height

    keep_audio = meta["has_audio"] and not remove_audio
    return {
        "start": s_time,
        "end": e_time,
        "duration": clip_duration,
        "is_trimmed": is_trimmed,
        "passthrough": passthrough,
        "remux_ext": remux_ext,
        "video_bitrate": video_bitrate,
        "audio_bitrate": audio_bitrate if keep_audio else 0,
        "keep_audio": keep_audio,
        "target_fps": target_fps,
        "target_short": target_short,
        "auto_short": auto_short,
        "source_short": source_short,
        "portrait": portrait,
        "out_width": out_short if portrait else out_long,
        "out_height": out_long if portrait else out_short,
        "est_bytes": (video_bitrate + (audio_bitrate if keep_audio else 0)) * clip_duration / 8,
    }


_ERROR_HINTS = ("Error", "Invalid", "not found", "Conversion failed", "No such")


def _summarize_ffmpeg_error(stderr_buffer):
    """Pull the most informative line out of ffmpeg's stderr tail.

    ffmpeg emits a wall of progress lines and then a one-line root cause
    near the bottom. We surface that single line so the gr.Error toast in
    the UI doesn't display 50 lines of noise.
    """
    informative = None
    for line in stderr_buffer:
        stripped = line.strip()
        if not stripped:
            continue
        if any(hint in stripped for hint in _ERROR_HINTS):
            informative = stripped
    if informative:
        return informative
    for line in reversed(stderr_buffer):
        stripped = line.strip()
        if stripped:
            return stripped
    return "ffmpeg failed (no stderr output)."


class CompressionCancelled(Exception):
    """Raised when a running encode is terminated by VideoCompressor.cancel()."""


class VideoCompressor:
    def __init__(self, output_dir=OUTPUT_DIR):
        for tool in ("ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                raise RuntimeError(
                    f"Required executable not found on PATH: {tool}. "
                    "Install ffmpeg (the Dockerfile does this in production)."
                )
        self.output_dir = output_dir
        # Per-job subprocess tracking so cancel() can free CPU mid-encode.
        # Keyed by the job_id passed into compress(). Guarded by _lock for
        # safety against concurrent requests on the shared HF Space.
        self._active = {}
        self._cancelled = set()
        self._lock = threading.Lock()

    def _prune_old_outputs(self):
        cutoff = time.time() - OUTPUT_TTL_SECONDS
        try:
            for entry in os.scandir(self.output_dir):
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            pass

    def cancel(self, job_id):
        """Terminate the active ffmpeg subprocess for job_id, if any."""
        if not job_id:
            return
        with self._lock:
            self._cancelled.add(job_id)
            proc = self._active.get(job_id)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    def compress(self, job_id, input_path, target_mb, remove_audio, start_time, end_time, speed_mode, output_resolution, fps_mode, progress_callback):
        self._prune_old_outputs()

        if not job_id:
            job_id = uuid.uuid4().hex[:12]

        try:
            return self._compress_inner(
                job_id, input_path, target_mb, remove_audio,
                start_time, end_time, speed_mode, output_resolution, fps_mode, progress_callback,
            )
        finally:
            with self._lock:
                self._cancelled.discard(job_id)
                self._active.pop(job_id, None)

    def _compress_inner(self, job_id, input_path, target_mb, remove_audio, start_time, end_time, speed_mode, output_resolution, fps_mode, progress_callback):
        try:
            input_size = os.path.getsize(input_path)
        except OSError as e:
            raise Exception(f"Could not read input file: {e}")
        if input_size > MAX_INPUT_BYTES:
            size_mb = input_size / 1024 / 1024
            raise Exception(
                f"Upload is {size_mb:.0f} MB; the limit is {MAX_INPUT_MB} MB. "
                "Trim or downscale the source locally first."
            )

        preset_map = {
            "Prioritize Speed": "superfast",
            "Prioritize Quality": "medium"
        }
        ffmpeg_preset = preset_map.get(speed_mode, "medium")

        progress_callback(0, desc="Analyzing Metadata...")
        meta = get_video_metadata(input_path)
        if not meta:
            raise Exception("Could not read video metadata.")

        # First pass without the trim probe: it decides passthrough and the
        # trim range, and passthrough never applies to a trimmed clip.
        plan = build_plan(meta, target_mb, remove_audio, start_time, end_time, output_resolution, fps_mode)
        s_time, e_time = plan["start"], plan["end"]
        target_duration = plan["duration"]
        is_trimmed = plan["is_trimmed"]
        passthrough, remux_ext = plan["passthrough"], plan["remux_ext"]
        if passthrough == "unchanged":
            print("File is already below target size. Skipping encoding.")
            return input_path

        # Per-request working directory keeps output, two-pass logs, and the
        # intermediate isolated from concurrent jobs sharing the same Space.
        job_dir = os.path.join(self.output_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(input_path))[0]

        if passthrough == "remux":
            remuxed = self._try_remux(
                job_id, input_path, os.path.join(job_dir, f"{base_name}_compressed{remux_ext}"),
                remove_audio, meta["duration"], target_mb * 1024 * 1024, progress_callback,
            )
            if remuxed:
                return remuxed
            # Remux fails on streams the target container can't hold (e.g. PCM
            # audio into mp4); a real encode always works, so fall through.

        if is_trimmed:
            progress_callback(0, desc="Analyzing trimmed section...")
            trim_bitrate = get_trim_bitrate(input_path, s_time, e_time)
            if trim_bitrate:
                print(f"Trim detected. Using local bitrate cap: {int(trim_bitrate/1024)}k (Global was {int(meta['bitrate']/1024)}k)")
                plan = build_plan(meta, target_mb, remove_audio, start_time, end_time, output_resolution,
                                  fps_mode, source_bitrate_cap=trim_bitrate)

        video_bitrate, audio_bitrate = plan["video_bitrate"], plan["audio_bitrate"]
        keep_audio = plan["keep_audio"]
        if keep_audio:
            audio_args = ["-c:a", "aac", "-b:a", str(int(audio_bitrate))]
        else:
            audio_args = ["-an"]

        source_short = plan["source_short"]
        source_long = max(meta.get("width") or 0, meta.get("height") or 0)
        target_fps, target_short = plan["target_fps"], plan["target_short"]
        effective_fps = target_fps or meta.get("fps") or 0

        # fps before scale: dropping 60->30 first halves the frames the scaler
        # touches. format=yuv420p forces 8-bit 4:2:0: a 10-bit source (iPhone
        # HDR HEVC) otherwise comes out as H.264 High 10, which browsers and
        # Discord won't play and Gradio's playability check doesn't catch.
        vf_parts = []
        if target_fps:
            vf_parts.append(f"fps={target_fps}")
        if target_short:
            if plan["portrait"]:
                vf_parts.append(f"scale={target_short}:-2")
            else:
                vf_parts.append(f"scale=-2:{target_short}")
        vf_parts.append("format=yuv420p")
        vf = ",".join(vf_parts)

        output_path = os.path.join(job_dir, f"{base_name}_compressed.mp4")

        # Input-side seek (-ss/-t before -i): ffmpeg jumps to the keyframe before
        # the trim start and decodes from there, still frame-accurate. Output-side
        # -ss decoded and discarded everything before the trim start on both
        # passes (trimming 40-50s of a 60s clip: 38s -> 12s).
        trim_args = ["-ss", str(s_time), "-t", str(target_duration)] if is_trimmed else []
        source_input = ["-threads", THREADS, *trim_args, "-i", input_path]
        v_bitrate_str = str(int(video_bitrate))

        # NOTE: two-pass *requires* the same -preset on both passes. A pass-1
        # preset that strips features (e.g. ultrafast disables mbtree/cabac)
        # produces a stats file that pass 2 refuses to consume ("Could not open
        # encoder before EOF" / EINVAL). libx264 already optimizes pass 1
        # internally via --slow-firstpass=0 (the default), so don't try to
        # speed it up further by overriding the preset.
        x264_args = [
            "-c:v", "libx264",
            "-preset", ffmpeg_preset,
            "-threads", THREADS,
            "-b:v", v_bitrate_str,
            "-maxrate", str(int(video_bitrate * 1.5)),
            "-bufsize", str(int(video_bitrate * 2)),
        ]

        # Both Speed and Quality run two-pass; only the preset differs.
        # Bench data (see bench.py) showed -tune fastdecode is a free quality
        # loss across every metric and every content type (animation, gameplay,
        # natural), and that single-pass at low target sizes gives up
        # noticeable quality on varied content (e.g. ~+2 VMAF on animation
        # when we switch to two-pass at the same superfast preset).
        pass_log_prefix = os.path.join(job_dir, "ffmpeg2pass")
        pass_args = ["-passlogfile", pass_log_prefix]
        # -sn/-dn: never carry subtitle/data streams into mp4 (bitmap subs can't
        # be converted and would fail the whole encode). +faststart puts the
        # moov atom up front so the result plays before it fully downloads.
        out_args = ["-sn", "-dn", "-movflags", "+faststart"]

        intermediate_path = None
        if target_fps or target_short:
            intermediate_path = self._plan_intermediate(
                job_dir, source_short, source_long, target_short, effective_fps, target_duration,
            )

        if intermediate_path:
            cmd_pass1 = [
                *_FFMPEG, "-y", *source_input,
                "-filter_complex_threads", THREADS,
                "-filter_complex", f"[0:v]{vf},split=2[enc][keep]",
                "-map", "[enc]", *x264_args, *pass_args, "-pass", "1", "-f", "null", "-",
                "-map", "[keep]", "-c:v", "ffvhuff", "-threads", "1", intermediate_path,
            ]
            # Video from the (already trimmed + filtered) intermediate; audio
            # straight from the source with the same input-side trim.
            if keep_audio:
                pass2_inputs = ["-i", intermediate_path, *trim_args, "-i", input_path,
                                "-map", "0:v:0", "-map", "1:a:0"]
            else:
                pass2_inputs = ["-i", intermediate_path]
            cmd_pass2 = [
                *_FFMPEG, "-y", "-threads", THREADS, *pass2_inputs,
                *x264_args, *pass_args, "-pass", "2",
                *audio_args, *out_args, output_path,
            ]
            # Pass 1 now carries the whole decode + scale; pass 2 is mostly x264.
            pass1_share = 0.75 if ffmpeg_preset == "superfast" else 0.5
        else:
            filter_args = ["-filter_threads", THREADS, "-vf", vf]
            cmd_pass1 = [
                *_FFMPEG, "-y", *source_input, *filter_args,
                *x264_args, *pass_args, "-pass", "1",
                "-an", "-sn", "-dn", "-f", "null", "-",
            ]
            cmd_pass2 = [
                *_FFMPEG, "-y", *source_input, *filter_args,
                *x264_args, *pass_args, "-pass", "2",
                *audio_args, *out_args, output_path,
            ]
            pass1_share = 0.45 if ffmpeg_preset == "superfast" else 0.25

        try:
            self._run_ffmpeg_with_progress(
                job_id, cmd_pass1, progress_callback, target_duration,
                progress_start=0.0, progress_end=pass1_share,
                description="Analyzing (pass 1 of 2)..."
            )
            self._run_ffmpeg_with_progress(
                job_id, cmd_pass2, progress_callback, target_duration,
                progress_start=pass1_share, progress_end=1.0,
                description="Compressing (pass 2 of 2)..."
            )
        finally:
            # Don't leave a multi-GB intermediate on the Space's disk until the
            # TTL prune when the encode fails or is cancelled.
            if intermediate_path and os.path.exists(intermediate_path):
                try:
                    os.remove(intermediate_path)
                except OSError:
                    pass
            self._cleanup_logs(pass_log_prefix)

        return output_path

    def _plan_intermediate(self, job_dir, source_short, source_long, target_short, fps, duration):
        """Path for the decode-once intermediate, or None when its estimated size
        exceeds INTERMEDIATE_MAX_BYTES or half the free disk."""
        if not source_short or not source_long or not fps:
            return None
        out_short = target_short or source_short
        out_long = out_short * source_long / source_short
        estimate = out_short * out_long * 1.5 * fps * duration * INTERMEDIATE_RATIO  # yuv420p
        try:
            free = shutil.disk_usage(job_dir).free
        except OSError:
            return None
        if estimate > min(INTERMEDIATE_MAX_BYTES, free / 2):
            return None
        return os.path.join(job_dir, "intermediate.nut")

    def _try_remux(self, job_id, input_path, output_path, remove_audio, duration, max_bytes, progress_callback):
        """Stream-copy a source that's already under target into a browser-playable
        container (dropping audio if asked). Returns output_path, or None when
        ffmpeg can't do it or the result isn't under max_bytes."""
        cmd = [
            *_FFMPEG, "-y", "-i", input_path,
            "-c", "copy", "-sn", "-dn",
            *(["-an"] if remove_audio else []),
            *(["-movflags", "+faststart"] if output_path.endswith(".mp4") else []),
            output_path,
        ]
        try:
            self._run_ffmpeg_with_progress(
                job_id, cmd, progress_callback, duration,
                progress_start=0.0, progress_end=1.0,
                description="Repackaging without re-encoding..."
            )
        except CompressionCancelled:
            raise
        except Exception as e:
            print(f"Remux failed, falling back to a full encode: {e}")
            return None
        try:
            if os.path.getsize(output_path) < max_bytes:
                return output_path
        except OSError:
            pass
        return None

    def _run_ffmpeg_with_progress(self, job_id, cmd, progress_callback, total_duration, progress_start, progress_end, description):
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

        with self._lock:
            # Check for cancellation that arrived between job_id registration
            # and now (the user could in theory click Cancel before Popen).
            already_cancelled = job_id in self._cancelled
            if already_cancelled:
                process.terminate()
            else:
                self._active[job_id] = process

        if already_cancelled:
            process.wait()
            raise CompressionCancelled("Compression cancelled.")

        stderr_buffer = []
        time_pattern = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d+)")
        # stderr is always a pipe because we pass stderr=subprocess.PIPE above;
        # the assert lets pyright narrow the Optional type.
        assert process.stderr is not None

        try:
            for line in process.stderr:
                stderr_buffer.append(line)
                if len(stderr_buffer) > 50:
                    stderr_buffer.pop(0)

                match = time_pattern.search(line)
                if match:
                    hours, minutes, seconds = map(float, match.groups())
                    current_time = hours * 3600 + minutes * 60 + seconds
                    fraction_complete = min(current_time / total_duration, 1.0)
                    global_progress = progress_start + (fraction_complete * (progress_end - progress_start))
                    progress_callback(global_progress, desc=description)

            process.wait()
        finally:
            with self._lock:
                self._active.pop(job_id, None)

        with self._lock:
            was_cancelled = job_id in self._cancelled

        if was_cancelled:
            raise CompressionCancelled("Compression cancelled.")
        if process.returncode != 0:
            summary = _summarize_ffmpeg_error(stderr_buffer)
            raise Exception(f"FFmpeg Error (Exit Code {process.returncode}): {summary}")

    def _cleanup_logs(self, prefix):
        try:
            for ext in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
                p = prefix + ext
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass
