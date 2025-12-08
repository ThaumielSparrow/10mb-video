import os
import subprocess
import re
import signal
from utils import get_video_metadata

OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

class VideoCompressor:
    def __init__(self, output_dir=OUTPUT_DIR):
        self.output_dir = output_dir

    def compress(self, input_path, target_mb, remove_audio, start_time, end_time, use_h265, progress_callback):
        
        # Analyze Video
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

        target_bytes = target_mb * 1024 * 1024
        if (meta["size_bytes"] < target_bytes) and not is_trimmed and not remove_audio and not use_h265:
            print("File is already below target size. Skipping encoding.")
            return input_path

        # Calculate Target Bitrate
        audio_bitrate = 128 * 1024 # Standard 128k
        should_process_audio = meta["has_audio"] and not remove_audio

        if not should_process_audio:
            audio_bitrate = 0
            audio_args = ["-an"]
        else:
            total_available_bits = target_bytes * 8
            naive_video_bitrate = (total_available_bits / target_duration) - audio_bitrate
            if naive_video_bitrate < (200 * 1024):
                audio_bitrate = 64 * 1024 
            audio_args = ["-c:a", "aac", "-b:a", str(int(audio_bitrate))]

        total_bits_allowed = target_bytes * 8
        target_total_bitrate = total_bits_allowed / target_duration
        video_bitrate = target_total_bitrate - audio_bitrate

        if video_bitrate > meta["bitrate"]:
            video_bitrate = meta["bitrate"] * 0.95
        if video_bitrate < 10000:
            video_bitrate = 10000

        # Construct FFmpeg Arguments
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        ext = "mp4"
        output_path = os.path.join(self.output_dir, f"{base_name}_compressed.{ext}")
        pass_log_prefix = os.path.join(self.output_dir, f"ffmpeg2pass_{base_name}")

        trim_args = ["-ss", str(s_time), "-to", str(e_time)] if is_trimmed else []

        # Codec Selection
        if use_h265:
            codec_args = ["-c:v", "libx265", "-tag:v", "hvc1"] # hvc1 tag helps Apple compatibility
        else:
            codec_args = ["-c:v", "libx264"]

        common_args = [
            "-y",
            *trim_args,
            *codec_args,
            "-preset", "medium",
            "-b:v", str(int(video_bitrate)),
            "-passlogfile", pass_log_prefix
        ]

        # PASS 1 (Analysis) - Weights 0% -> 25% of progress bar
        cmd_pass1 = [
            "ffmpeg", "-i", input_path,
            *common_args,
            "-pass", "1",
            "-an", 
            "-f", "mp4", "/dev/null"
        ]
        
        self._run_ffmpeg_with_progress(
            cmd_pass1, 
            progress_callback, 
            target_duration, 
            progress_start=0.0, 
            progress_end=0.25, 
            description="Encoding Pass 1/2 (Analysis)"
        )

        # PASS 2 (Encoding) - Weights 25% -> 100% of progress bar
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
            description="Encoding Pass 2/2 (Compression)"
        )

        self._cleanup_logs(pass_log_prefix)
        return output_path

    def _run_ffmpeg_with_progress(self, cmd, progress_callback, total_duration, progress_start, progress_end, description):
        """
        Runs FFmpeg and parses stderr for 'time=...' to update Gradio progress.
        """
        # Start subprocess, capturing stderr where FFmpeg writes stats
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            universal_newlines=True
        )

        # Regex to match "time=00:00:00.00"
        time_pattern = re.compile(r"time=(\d{2}):(\d{2}):(\d{2}\.\d+)")

        # Read line by line
        for line in process.stderr: # type:ignore
            match = time_pattern.search(line)
            if match:
                hours, minutes, seconds = map(float, match.groups())
                current_time = hours * 3600 + minutes * 60 + seconds
                
                # Calculate percentage of THIS pass
                fraction_complete = min(current_time / total_duration, 1.0)
                
                # Map to global progress bar
                global_progress = progress_start + (fraction_complete * (progress_end - progress_start))
                
                progress_callback(global_progress, desc=f"{description}")

        process.wait()
        if process.returncode != 0:
            raise Exception("FFmpeg encountered an error.")

    def _cleanup_logs(self, prefix):
        try:
            for ext in ["-0.log", "-0.log.mbtree", ".log", ".log.mbtree"]:
                p = prefix + ext
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass