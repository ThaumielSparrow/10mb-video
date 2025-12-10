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
        
        streams = data.get("streams", [])
        has_audio = any(s["codec_type"] == "audio" for s in streams)

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

def get_trim_bitrate(input_path, start, end, temp_dir="outputs"):
    """
    Performs a temporary Stream Copy trim to measure the EXACT bitrate 
    of the specific section the user selected. 
    This prevents inflating a simple scene (like a black screen) to a high bitrate.
    """
    temp_check_file = os.path.join(temp_dir, "temp_bitrate_check.mp4")
    
    try:
        # Stream copy for speed
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", input_path,
            "-c", "copy",
            "-map", "0", # Copy all streams
            "-avoid_negative_ts", "make_zero",
            temp_check_file
        ]
        # We don't care about logs here
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        # Analyze the chunk
        meta = get_video_metadata(temp_check_file)
        
        # Cleanup
        if os.path.exists(temp_check_file):
            os.remove(temp_check_file)
            
        if meta and meta["duration"] > 0:
            return meta["bitrate"]
        return None
        
    except Exception:
        # If stream copy fails (rare codec issues), fallback to None
        if os.path.exists(temp_check_file):
            os.remove(temp_check_file)
        return None

# Thin wrapper around `open` to load js files
def load_js(filename:str) -> str:
    with open(filename, "r") as f:
        return f.read()