import os
from pathlib import Path

import gradio as gr
from gradio.utils import get_upload_folder

from compressor import MAX_INPUT_MB, OUTPUT_DIR, OUTPUT_TTL_SECONDS, VideoCompressor, build_plan
from jobs import JobQueue, QueueFull
from utils import QUALITY_BPP_TARGET

compressor = VideoCompressor()
jobs = JobQueue(compressor)

WEB_DIR = Path(__file__).parent / "web"

RESOLUTION_CHOICES = ("Auto", "Original", "720p", "480p", "360p")
FPS_CHOICES = ("Auto", "On", "Off")
# "Fast" falls back to the server only when this browser can't encode; it then
# uses the speed preset. "Best quality" always runs x264 medium two-pass.
SPEED_MODES = {"fast": "Prioritize Speed", "quality": "Prioritize Quality"}
LOW_BITRATE_WARN_BPS = 200_000

# Scales the bits-per-pixel target Auto resolution uses for the on-device
# (WebCodecs hardware H.264) encode. Benched on Chrome/AMD: one ladder step
# lower helped a grainy 1080p60 clip (SSIM 0.921 -> 0.931) but hurt a 720p
# HEVC clip (0.965 -> 0.940), so there's no consistent case for moving it
# off x264's target yet. Re-tune with real phone footage via webbench/.
DEVICE_BPP_SCALE = 1.0


def _number(value, name, minimum=None, maximum=None, default=None):
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"Missing {name}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number.") from None
    if number != number or (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise ValueError(f"{name} is out of range.")
    return number


def _settings(raw):
    """Validate the page's settings dict (it comes straight from the browser)."""
    raw = raw or {}
    resolution = raw.get("resolution", "Auto")
    fps_mode = raw.get("fps_mode", "Auto")
    mode = raw.get("mode", "fast")
    if resolution not in RESOLUTION_CHOICES or fps_mode not in FPS_CHOICES or mode not in SPEED_MODES:
        raise ValueError("Unknown option.")
    start = _number(raw.get("start"), "Start", minimum=0, default=0.0)
    end = raw.get("end")
    return {
        "target_mb": _number(raw.get("target_mb"), "Target size", minimum=0.1, maximum=2000),
        "remove_audio": bool(raw.get("remove_audio")),
        "start": start,
        "end": None if end in (None, "") else _number(end, "End", minimum=0),
        "resolution": resolution,
        "fps_mode": fps_mode,
        "mode": mode,
    }


def _client_meta(raw):
    """Metadata the browser probed from the file (Mediabunny), in get_video_metadata's shape."""
    raw = raw or {}
    duration = _number(raw.get("duration"), "Duration", minimum=0.01)
    size = _number(raw.get("size_bytes"), "File size", minimum=1)
    ext = str(raw.get("container_ext") or "").lower()[:8]
    codec = raw.get("video_codec")
    return {
        "duration": duration,
        "size_bytes": size,
        "bitrate": size * 8 / duration,
        "has_audio": bool(raw.get("has_audio")),
        "video_codec": str(codec)[:16] if codec else None,
        "container_ext": ext,
        "width": int(_number(raw.get("width"), "Width", minimum=0, default=0)) or None,
        "height": int(_number(raw.get("height"), "Height", minimum=0, default=0)) or None,
        "fps": _number(raw.get("fps"), "Frame rate", minimum=0, default=0.0) or None,
    }


def plan_job(req):
    """Plan for the summary and, when the browser encodes, for its encoder.

    Same build_plan the server pipeline uses, fed the browser's own probe of
    the file, so nothing about the file has to be uploaded to plan it.
    """
    try:
        settings = _settings(req.get("settings"))
        meta = _client_meta(req.get("meta"))
        bpp = QUALITY_BPP_TARGET * (DEVICE_BPP_SCALE if req.get("device") else 1.0)
        plan = build_plan(
            meta, settings["target_mb"], settings["remove_audio"], settings["start"], settings["end"],
            settings["resolution"], settings["fps_mode"], bpp_target=bpp,
        )
    except ValueError as e:
        return {"error": str(e)}
    plan["target_bytes"] = settings["target_mb"] * 1024 * 1024
    plan["low_quality"] = plan["video_bitrate"] < LOW_BITRATE_WARN_BPS
    return plan


def start_job(req):
    """Queue a server encode of a file the page already uploaded."""
    try:
        settings = _settings(req.get("settings"))
    except ValueError as e:
        return {"error": str(e)}
    # The path comes from the browser: only accept files Gradio's upload
    # endpoint wrote, never an arbitrary path on the server.
    upload_dir = os.path.realpath(get_upload_folder())
    path = os.path.realpath(str(req.get("path") or ""))
    if os.path.commonpath([upload_dir, path]) != upload_dir or not os.path.isfile(path):
        return {"error": "Upload not found. Choose the file again."}
    try:
        job_id = jobs.submit(path, {
            "target_mb": settings["target_mb"],
            "remove_audio": settings["remove_audio"],
            "start_time": settings["start"],
            "end_time": settings["end"],
            "speed_mode": SPEED_MODES[settings["mode"]],
            "output_resolution": settings["resolution"],
            "fps_mode": settings["fps_mode"],
        })
    except QueueFull as e:
        return {"error": str(e)}
    return {"job_id": job_id}


def job_status(req):
    status = jobs.status(str(req.get("job_id") or ""))
    if status is None:
        return {"state": "error", "error": "This job has expired. Compress the file again."}
    output = status.pop("output")
    if output:
        status["url"] = f"gradio_api/file={output}"
        status["size"] = os.path.getsize(output)
    return status


def cancel_job(req):
    jobs.cancel(str(req.get("job_id") or ""))
    return {"ok": True}


def _asset(name):
    return (WEB_DIR / name).read_text(encoding="utf-8")


with gr.Blocks(title="10mb video", delete_cache=(900, OUTPUT_TTL_SECONDS)) as demo:
    gr.HTML(
        html_template=_asset("compressor.html"),
        css_template=_asset("compressor.css"),
        js_on_load=_asset("compressor.js"),
        head=(
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<link href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@500;600;700'
            '&display=swap" rel="stylesheet">'
        ),
        server_functions=[plan_job, start_job, job_status, cancel_job],
        apply_default_css=False,
        worker_src=_asset("encoder_worker.js"),
        max_mb=MAX_INPUT_MB,
    )

if __name__ == "__main__":
    # max_file_size rejects oversized uploads before they're transferred and
    # written to disk. allowed_paths lets the page download server results.
    demo.queue(max_size=8).launch(
        server_name="0.0.0.0", server_port=7860, inbrowser=True,
        max_file_size=f"{MAX_INPUT_MB}mb",
        allowed_paths=[OUTPUT_DIR],
        footer_links=["gradio"],
        # Gradio still renders the "·" separators around the links it hides.
        css="footer .divider { display: none; }",
    )
