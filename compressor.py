import os
import subprocess
import re
from utils import get_video_metadata

OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

class VideoCompressor:
    def __init__(self, output_dir=OUTPUT_DIR):
        self.output_dir = output_dir

    def compress(self, input_path, target_mb, remove_audio, start_time, end_time, progress_callback):
        
        # 1. Analyze Video
        progress_callback(0, desc="Analyzing Metadata...")
        meta = get_video_metadata(input_path)
        
        if not meta:
            raise Exception("Could not read video metadata.")

        # Handle Trimming Inputs
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

        # Reserve space for MP4 container overhead
        if target_mb <= 20:
            safety_buffer = 0.90 
        else:
            safety_buffer = 0.95

        effective_target_bytes = target_bytes_strict * safety_buffer
        total_bits_allowed = effective_target_bytes * 8
        
        # Calculate Audio Bitrate
        audio_bitrate = 128 * 1024 # Standard 128k
        should_process_audio = meta["has_audio"] and not remove_audio

        if not should_process_audio:
            audio_bitrate = 0
            audio_args = ["-an"]
        else:
            # Check if video bitrate would be too low
            naive_video_bitrate = (total_bits_allowed / target_duration) - audio_bitrate
            
            # If video allows < 200kbps, drop audio to 64k to save space
            if naive_video_bitrate < (200 * 1024):
                audio_bitrate = 64 * 1024 
            
            audio_args = ["-c:a", "aac", "-b:a", str(int(audio_bitrate))]

        # Calculate Video Bitrate
        target_total_bitrate = total_bits_allowed / target_duration
        video_bitrate = target_total_bitrate - audio_bitrate

        # Don't upscale bitrate if the source is already lower quality
        if video_bitrate > meta["bitrate"]:
            video_bitrate = meta["bitrate"]

        # Safety floor (10kbps minimum to prevent FFmpeg errors)
        if video_bitrate < 10000:
            video_bitrate = 10000

        # Construct FFmpeg Arguments
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        ext = "mp4"
        output_path = os.path.join(self.output_dir, f"{base_name}_compressed.{ext}")
        pass_log_prefix = os.path.join(self.output_dir, f"ffmpeg2pass_{base_name}")

        trim_args = ["-ss", str(s_time), "-to", str(e_time)] if is_trimmed else []

        codec_args = ["-c:v", "libx264"]

        # Helper to stringify bitrate
        v_bitrate_str = str(int(video_bitrate))

        common_args = [
            "-y",
            *trim_args,
            *codec_args,
            "-preset", "medium",
            "-b:v", v_bitrate_str,
            # Constrain bitrate to prevent massive spikes that overshoot size
            "-maxrate", str(int(video_bitrate * 1.5)),
            "-bufsize", str(int(video_bitrate * 2)), 
            "-passlogfile", pass_log_prefix
        ]

        # PASS 1 (Analysis)
        cmd_pass1 = [
            "ffmpeg", "-i", input_path,
            *common_args,
            "-pass", "1",
            "-an", 
            "-f", "mp4", os.devnull # Windows-safe null output
        ]
        
        self._run_ffmpeg_with_progress(
            cmd_pass1, 
            progress_callback, 
            target_duration, 
            progress_start=0.0, 
            progress_end=0.25, 
            description="Analyzing metadata..."
        )

        # PASS 2 (Encoding)
        cmd_pass2 = [
            "ffmpeg", "-i", input_path,
            *common_args,
            "-pass", "2",
            *audio_args,
            output_path
        ]
        
        self._run_ffmpeg_with_progress(
            cmd_pass2, 
            progress_callback, 
            target_duration, 
            progress_start=0.25, 
            progress_end=1.0, 
            description="Compressing..."
        )

        self._cleanup_logs(pass_log_prefix)
        return output_path

    def _run_ffmpeg_with_progress(self, cmd, progress_callback, total_duration, progress_start, progress_end, description):
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            universal_newlines=True
        )

        stderr_buffer = []
        time_pattern = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d+)")

        for line in process.stderr: # type:ignore
            stderr_buffer.append(line)
            if len(stderr_buffer) > 50:
                stderr_buffer.pop(0)

            match = time_pattern.search(line)
            if match:
                hours, minutes, seconds = map(float, match.groups())
                current_time = hours * 3600 + minutes * 60 + seconds
                fraction_complete = min(current_time / total_duration, 1.0)
                global_progress = progress_start + (fraction_complete * (progress_end - progress_start))
                progress_callback(global_progress, desc=f"{description}")

        process.wait()
        if process.returncode != 0:
            error_log = "".join(stderr_buffer)
            raise Exception(f"FFmpeg Error (Exit Code {process.returncode}):\n{error_log}")

    def _cleanup_logs(self, prefix):
        try:
            for ext in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
                p = prefix + ext
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass