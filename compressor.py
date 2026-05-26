import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from utils import (
    compute_bitrate_plan,
    get_trim_bitrate,
    get_video_metadata,
    pick_auto_fps_cap,
    pick_auto_resolution,
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


def _resolve_target_height(output_resolution, source_height, source_width, source_fps, video_bitrate_bps):
    """Translate the UI's output_resolution choice into an ffmpeg target height.

    Returns 0 when no scale filter should be applied (Original, or when the
    requested height equals/exceeds source).
    """
    if not source_height:
        return 0
    if output_resolution == "Auto":
        picked = pick_auto_resolution(source_height, source_width, source_fps, video_bitrate_bps)
        return picked if picked < source_height else 0
    if output_resolution in _RESOLUTION_HEIGHTS:
        height = _RESOLUTION_HEIGHTS[output_resolution]
        return height if height < source_height else 0
    # "Original" or anything unrecognised: no downscale.
    return 0


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

        s_time = float(start_time) if start_time else 0.0
        e_time = float(end_time) if end_time else meta["duration"]
        if s_time < 0: s_time = 0
        if e_time > meta["duration"]: e_time = meta["duration"]

        target_duration = e_time - s_time
        if target_duration <= 0:
            raise Exception("Invalid start/end time.")

        is_trimmed = (s_time > 0 or e_time < meta["duration"])

        target_bytes_strict = target_mb * 1024 * 1024
        if (meta["size_bytes"] < target_bytes_strict) and not is_trimmed and not remove_audio:
            print("File is already below target size. Skipping encoding.")
            return input_path

        # Per-request working directory keeps output, two-pass logs, and the trim
        # probe file isolated from concurrent jobs sharing the same Space.
        job_dir = os.path.join(self.output_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)

        source_bitrate_cap = meta["bitrate"]
        if is_trimmed:
            progress_callback(0, desc="Analyzing trimmed section...")
            trim_bitrate = get_trim_bitrate(input_path, s_time, e_time, job_dir)
            if trim_bitrate:
                source_bitrate_cap = trim_bitrate
                print(f"Trim detected. Using local bitrate cap: {int(trim_bitrate/1024)}k (Global was {int(meta['bitrate']/1024)}k)")

        plan = compute_bitrate_plan(
            target_mb=target_mb,
            duration=target_duration,
            has_audio=meta["has_audio"],
            remove_audio=remove_audio,
            source_bitrate_cap=source_bitrate_cap,
        )
        if plan is None:
            raise Exception("Invalid bitrate plan (check target and duration).")
        video_bitrate, audio_bitrate = plan

        if remove_audio or not meta["has_audio"]:
            audio_args = ["-an"]
        else:
            audio_args = ["-c:a", "aac", "-b:a", str(int(audio_bitrate))]

        # Resolve fps cap first so resolution picker sees the post-cap effective
        # fps. Order matters: capping fps doubles bits-per-frame, which means
        # Auto resolution can keep a higher resolution than it would otherwise.
        source_height = meta.get("height") or 0
        source_width = meta.get("width") or 0
        source_fps = meta.get("fps") or 0
        target_fps = _resolve_target_fps(
            fps_mode, source_fps, source_width, source_height, video_bitrate,
        )
        effective_fps = target_fps if target_fps else source_fps

        # Resolve output resolution. "Auto" picks the height that gives decent
        # quality at the chosen bitrate; "Original" leaves it alone; fixed
        # presets ("720p" etc.) are honored as-is. We never upscale.
        target_height = _resolve_target_height(
            output_resolution, source_height, source_width, effective_fps, video_bitrate,
        )

        vf_parts = []
        if target_height and target_height < source_height:
            vf_parts.append(f"scale=-2:{target_height}")
        if target_fps:
            vf_parts.append(f"fps={target_fps}")
        scale_args = ["-vf", ",".join(vf_parts)] if vf_parts else []

        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(job_dir, f"{base_name}_compressed.mp4")

        trim_args = ["-ss", str(s_time), "-to", str(e_time)] if is_trimmed else []
        v_bitrate_str = str(int(video_bitrate))

        # -threads matches the HF Spaces Free tier vCPU count; libx264's auto-detect
        # can over-subscribe on shared infra and slow encoding down.
        # NOTE: two-pass *requires* the same -preset on both passes. A pass-1
        # preset that strips features (e.g. ultrafast disables mbtree/cabac)
        # produces a stats file that pass 2 refuses to consume ("Could not open
        # encoder before EOF" / EINVAL). libx264 already optimizes pass 1
        # internally via --slow-firstpass=0 (the default), so don't try to
        # speed it up further by overriding the preset.
        common_args = [
            "-y",
            *trim_args,
            *scale_args,
            "-c:v", "libx264",
            "-preset", ffmpeg_preset,
            "-threads", "2",
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

        cmd_pass1 = [
            "ffmpeg", "-i", input_path,
            *common_args,
            *pass_args,
            "-pass", "1",
            "-an",
            "-f", "mp4", os.devnull
        ]
        self._run_ffmpeg_with_progress(
            job_id, cmd_pass1, progress_callback, target_duration,
            progress_start=0.0, progress_end=0.25,
            description="Analyzing metadata..."
        )

        cmd_pass2 = [
            "ffmpeg", "-i", input_path,
            *common_args,
            *pass_args,
            "-pass", "2",
            *audio_args,
            output_path
        ]
        self._run_ffmpeg_with_progress(
            job_id, cmd_pass2, progress_callback, target_duration,
            progress_start=0.25, progress_end=1.0,
            description="Compressing..."
        )
        self._cleanup_logs(pass_log_prefix)

        return output_path

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
