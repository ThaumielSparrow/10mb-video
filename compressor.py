import os
import subprocess
import math
from utils import get_video_metadata

OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

class VideoCompressor:
    def __init__(self, output_dir=OUTPUT_DIR):
        self.output_dir = output_dir

    def compress(self, input_path, target_mb, remove_audio, start_time, end_time, progress_callback):
        
        # 1. Analyze Video
        progress_callback(0.05, desc="Analyzing...")
        meta = get_video_metadata(input_path)
        
        if not meta:
            raise Exception("Could not read video metadata.")

        # Handle Trimming Inputs
        s_time = float(start_time) if start_time else 0.0
        e_time = float(end_time) if end_time else meta["duration"]
        
        # Clamps
        if s_time < 0: s_time = 0
        if e_time > meta["duration"]: e_time = meta["duration"]
        
        target_duration = e_time - s_time
        if target_duration <= 0:
            raise Exception("Invalid start/end time.")

        is_trimmed = (s_time > 0 or e_time < meta["duration"])

        # If the file is ALREADY smaller than target, AND we aren't modifying it 
        # (trimming or removing audio), just return the original.
        target_bytes = target_mb * 1024 * 1024
        
        # We assume 1MB margin of error for "already fits" to be safe
        if (meta["size_bytes"] < target_bytes) and not is_trimmed and not remove_audio:
            print("File is already below target size. Skipping encoding.")
            return input_path

        # 2. Calculate Target Bitrate
        # Available bits = Target Size - Audio Size (approx)
        
        # Determine Audio Settings
        audio_bitrate = 128 * 1024 # Standard 128k
        should_process_audio = meta["has_audio"] and not remove_audio

        if not should_process_audio:
            audio_bitrate = 0
            audio_args = ["-an"]
        else:
            # Logic: If video bitrate is squeezed too tight, lower audio quality
            # Calculate naive video bitrate
            total_available_bits = target_bytes * 8
            naive_video_bitrate = (total_available_bits / target_duration) - audio_bitrate
            
            if naive_video_bitrate < (200 * 1024):
                audio_bitrate = 64 * 1024 # Drop to 64k AAC
            
            audio_args = ["-c:a", "aac", "-b:a", str(int(audio_bitrate))]

        # Calculate Video Bitrate
        total_bits_allowed = target_bytes * 8
        target_total_bitrate = total_bits_allowed / target_duration
        video_bitrate = target_total_bitrate - audio_bitrate

        # If the calculated target bitrate is HIGHER than the source bitrate,
        # cap it at the source bitrate. This prevents making a 5MB file into a 50MB file.
        # We add a slight safety buffer (95% of original) to ensure it definitely fits.
        if video_bitrate > meta["bitrate"]:
            print(f"Target bitrate ({int(video_bitrate)}) > Source ({int(meta['bitrate'])}). Clamping.")
            video_bitrate = meta["bitrate"] * 0.95

        # Safety floor (10kbps)
        if video_bitrate < 10000:
            video_bitrate = 10000

        # Construct FFmpeg Arguments
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        output_path = os.path.join(self.output_dir, f"{base_name}_compressed.mp4")
        pass_log_prefix = os.path.join(self.output_dir, f"ffmpeg2pass_{base_name}")

        trim_args = ["-ss", str(s_time), "-to", str(e_time)] if is_trimmed else []

        common_args = [
            "-y",
            *trim_args,
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", str(int(video_bitrate)),
            "-passlogfile", pass_log_prefix
        ]

        # PASS 1
        progress_callback(0.2, desc="Encoding Pass 1/2...")
        cmd_pass1 = [
            "ffmpeg", "-i", input_path,
            *common_args,
            "-pass", "1",
            "-an", # No audio needed for stats pass
            "-f", "mp4", "/dev/null"
        ]
        subprocess.run(cmd_pass1, check=True)

        # PASS 2
        progress_callback(0.6, desc="Encoding Pass 2/2...")
        cmd_pass2 = [
            "ffmpeg", "-i", input_path,
            *common_args,
            "-pass", "2",
            *audio_args,
            output_path
        ]
        subprocess.run(cmd_pass2, check=True)

        # Cleanup Log Files
        self._cleanup_logs(pass_log_prefix)

        return output_path

    def _cleanup_logs(self, prefix):
        try:
            # FFmpeg generates prefix-0.log, prefix-0.log.mbtree, etc.
            for ext in ["-0.log", "-0.log.mbtree"]:
                if os.path.exists(prefix + ext):
                    os.remove(prefix + ext)
        except Exception:
            pass