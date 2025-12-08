import subprocess
import json
import os

def get_video_metadata(input_path):
    """
    Returns a dictionary containing duration, audio presence, 
    original bitrate, and file size.
    """
    try:
        cmd = [
            "ffprobe", 
            "-v", "error", 
            "-show_entries", "format=duration,size,bit_rate:stream=codec_type", 
            "-of", "json", 
            input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        
        fmt = data["format"]
        duration = float(fmt.get("duration", 0))
        size_bytes = float(fmt.get("size", 0))
        
        # Check for audio streams
        streams = data.get("streams", [])
        has_audio = any(s["codec_type"] == "audio" for s in streams)

        # Get bitrate. If metadata is missing, calculate it manually: (Size * 8) / Duration
        if "bit_rate" in fmt and fmt["bit_rate"] != "N/A":
            bitrate = float(fmt["bit_rate"])
        else:
            bitrate = (size_bytes * 8) / duration if duration > 0 else 0

        return {
            "duration": duration,
            "size_bytes": size_bytes,
            "bitrate": bitrate,
            "has_audio": has_audio
        }
    except Exception as e:
        print(f"Error probing video: {e}")
        return None