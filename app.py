import os
import uuid

import gradio as gr
from compressor import CompressionCancelled, VideoCompressor
from utils import (
    compute_bitrate_plan,
    estimate_video_bitrate,
    get_video_metadata,
    pick_auto_resolution,
)

compressor = VideoCompressor()

PRESETS = ["8 MB", "10 MB", "25 MB", "50 MB", "Custom"]
RESOLUTION_CHOICES = ["Auto", "Original", "720p", "480p", "360p"]
RESOLUTION_HEIGHTS = {"720p": 720, "480p": 480, "360p": 360}
LOW_BITRATE_WARN_KBPS = 200


def _resolve_target_mb(preset, custom_mb):
    if preset == "Custom":
        if custom_mb is None:
            raise gr.Error("Enter a custom size in MB.")
        try:
            target_mb = float(custom_mb)
        except (TypeError, ValueError):
            raise gr.Error("Invalid custom size. Please enter a valid number.")
        if target_mb <= 0:
            raise gr.Error("Custom size must be greater than 0.")
        return target_mb
    return float(preset.lower().replace("mb", "").strip())


def _effective_height(output_resolution, source_height, source_width, source_fps, video_bitrate_bps):
    """Mirror of compressor._resolve_target_height for the UI's summary line.

    Returns the height that will actually be encoded (capped at source).
    """
    if not source_height:
        return 0
    if output_resolution == "Auto":
        return pick_auto_resolution(source_height, source_width, source_fps, video_bitrate_bps or 0)
    if output_resolution in RESOLUTION_HEIGHTS:
        return min(RESOLUTION_HEIGHTS[output_resolution], source_height)
    return source_height  # "Original"


def prepare_job():
    """Generate the job id before compress runs so the cancel button can target it."""
    return uuid.uuid4().hex[:12]


def processing_function(job_id, video_file, preset, custom_mb, remove_audio, speed_mode, output_resolution, start_time, end_time, progress=gr.Progress()):
    if video_file is None:
        return None

    target_mb = _resolve_target_mb(preset, custom_mb)

    try:
        return compressor.compress(
            job_id=job_id,
            input_path=video_file,
            target_mb=target_mb,
            remove_audio=remove_audio,
            start_time=start_time,
            end_time=end_time,
            speed_mode=speed_mode,
            output_resolution=output_resolution,
            progress_callback=progress,
        )
    except CompressionCancelled:
        return None
    except Exception as e:
        raise gr.Error(f"Compression failed: {str(e)}")


def make_result_message(output_path, original_path, preset, custom_mb):
    """Compute the post-encode size summary. Runs in a separate .then() after
    processing_function so it isn't an output of the long-running compress
    event — otherwise Gradio paints a second progress overlay on result_md."""
    if not output_path:
        return ""
    try:
        target_mb = _resolve_target_mb(preset, custom_mb)
    except gr.Error:
        return ""

    if output_path == original_path:
        return "**Source was already under target — returned unchanged.**"

    try:
        size_mb = os.path.getsize(output_path) / 1024 / 1024
    except OSError:
        return ""

    delta = size_mb - target_mb
    if delta > 0.5:
        status = f" (overshot by {delta:.1f} MB)"
    elif delta < -2:
        status = f" (well under, {abs(delta):.1f} MB headroom)"
    else:
        status = " (on target)"
    return f"**Output:** {size_mb:.2f} MB / {target_mb:g} MB target{status}"


def cancel_active_job(job_id):
    if job_id:
        compressor.cancel(job_id)
    return gr.update(visible=True), gr.update(visible=False), None, ""


def _format_summary(meta, preset, custom_mb, remove_audio, output_resolution, start_time, end_time):
    if not meta:
        return ""

    duration = meta["duration"]
    s_time = float(start_time) if start_time else 0.0
    e_time = float(end_time) if end_time else duration
    s_time = max(s_time, 0.0)
    e_time = min(e_time, duration)
    effective_duration = max(e_time - s_time, 0.0)
    is_trimmed = (s_time > 0 or e_time < duration)

    try:
        target_mb = _resolve_target_mb(preset, custom_mb)
    except gr.Error:
        target_mb = None

    res_str = f"{meta['width']}x{meta['height']}" if meta.get("width") and meta.get("height") else "unknown resolution"
    lines = [
        f"**Source:** {meta['size_bytes'] / 1024 / 1024:.1f} MB &middot; "
        f"{duration:.1f}s &middot; "
        f"{res_str} &middot; "
        f"{'audio present' if meta['has_audio'] else 'no audio'}"
    ]

    if effective_duration <= 0:
        lines.append("_Trim range is empty._")
        return "\n\n".join(lines)

    if target_mb is None:
        return "\n\n".join(lines)

    source_under_target = meta["size_bytes"] < target_mb * 1024 * 1024
    if source_under_target and not is_trimmed and not remove_audio:
        lines.append(
            f"**Source is already under {target_mb:g} MB** &mdash; Compress will return "
            "the original file unchanged."
        )
        return "\n\n".join(lines)

    plan = compute_bitrate_plan(
        target_mb=target_mb,
        duration=effective_duration,
        has_audio=meta["has_audio"],
        remove_audio=remove_audio,
        source_bitrate_cap=meta.get("bitrate"),
    )
    bitrate = estimate_video_bitrate(meta, target_mb, remove_audio, effective_duration)
    if bitrate is None or plan is None:
        return "\n\n".join(lines)

    kbps = bitrate / 1000
    video_bps, audio_bps = plan
    estimated_mb = (video_bps + audio_bps) * effective_duration / 8 / 1024 / 1024

    range_note = f" (trim: {s_time:.1f}s &ndash; {e_time:.1f}s)" if is_trimmed else ""
    quality_warn = "  &mdash; **warning: very low quality**" if kbps < LOW_BITRATE_WARN_KBPS else ""
    lines.append(
        f"**Estimated output:** ~{estimated_mb:.1f} MB &middot; "
        f"{kbps:.0f} kbps video{quality_warn}{range_note}"
    )

    source_height = meta.get("height") or 0
    source_width = meta.get("width") or 0
    source_fps = meta.get("fps") or 0
    if source_height:
        effective_h = _effective_height(output_resolution, source_height, source_width, source_fps, bitrate)
        auto_pick = pick_auto_resolution(source_height, source_width, source_fps, bitrate)
        if output_resolution == "Auto":
            if effective_h < source_height:
                lines.append(f"**Resolution:** Auto &rarr; {effective_h}p (source is {source_height}p)")
            else:
                lines.append(f"**Resolution:** Auto &rarr; original ({source_height}p)")
        elif output_resolution == "Original":
            tip = ""
            if auto_pick < source_height:
                tip = f"  &mdash; **tip:** {auto_pick}p would look better at this bitrate"
            lines.append(f"**Resolution:** Original ({source_height}p){tip}")
        else:
            tip = ""
            if effective_h > auto_pick:
                tip = f"  &mdash; **tip:** {auto_pick}p would look better at this bitrate"
            lines.append(f"**Resolution:** {effective_h}p{tip}")

    return "\n\n".join(lines)


def on_video_upload(video_path, preset, custom_mb, remove_audio, output_resolution, _start_time, _end_time):
    # _start_time/_end_time are bound by the event but ignored: a new upload
    # resets trim to (0, full_duration), so we recompute the summary against
    # the source duration rather than carrying over the previous trim values.
    if not video_path:
        return None, "", gr.update(value=0), gr.update(value=None)
    meta = get_video_metadata(video_path)
    if not meta:
        return None, "_Could not read video metadata._", gr.update(value=0), gr.update(value=None)
    duration = meta["duration"]
    start_update = gr.update(
        value=0,
        maximum=duration,
        label=f"Start (sec) — source is {duration:.1f}s",
    )
    end_update = gr.update(
        value=None,
        maximum=duration,
        label=f"End (sec) — source is {duration:.1f}s",
    )
    summary = _format_summary(meta, preset, custom_mb, remove_audio, output_resolution, 0, None)
    return meta, summary, start_update, end_update


def on_settings_change(meta, preset, custom_mb, remove_audio, output_resolution, start_time, end_time):
    if not meta:
        return ""
    return _format_summary(meta, preset, custom_mb, remove_audio, output_resolution, start_time, end_time)


def on_preset_change(preset):
    return gr.update(visible=(preset == "Custom"))


with gr.Blocks(title="Smart Video Compressor") as demo:
    gr.Markdown("# 📼 Smart Video Compressor")
    gr.Markdown("Compress videos to a specific size, inspired by 8mb.video.")

    meta_state = gr.State(value=None)
    active_job = gr.State(value=None)

    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="Upload Video", sources=["upload"])
            summary_md = gr.Markdown("")

            with gr.Row():
                target_preset = gr.Dropdown(
                    choices=PRESETS,
                    value="10 MB",
                    label="Target File Size",
                    filterable=False,
                    allow_custom_value=False,
                    scale=2,
                    min_width=130,
                )
                target_custom = gr.Number(
                    label="Custom size (MB)",
                    value=10,
                    minimum=1,
                    maximum=2000,
                    visible=False,
                    scale=2,
                    min_width=120,
                )
                speed_mode = gr.Dropdown(
                    choices=["Prioritize Speed", "Prioritize Quality"],
                    value="Prioritize Speed",
                    label="Encoding Preset",
                    filterable=False,
                    allow_custom_value=False,
                    scale=2,
                    min_width=140,
                )

            with gr.Row():
                resolution = gr.Dropdown(
                    choices=RESOLUTION_CHOICES,
                    value="Auto",
                    label="Output Resolution",
                    filterable=False,
                    allow_custom_value=False,
                    scale=3,
                    min_width=150,
                )
                remove_audio = gr.Checkbox(
                    label="Remove Audio",
                    value=False,
                    scale=2,
                    min_width=140,
                )

            with gr.Accordion("Trimming Options", open=True):
                with gr.Row():
                    start_t = gr.Number(label="Start (sec)", value=0)
                    end_t = gr.Number(label="End (sec)", value=None)

            with gr.Row():
                btn = gr.Button("Compress", variant="primary", scale=3)
                cancel_btn = gr.Button("Cancel", variant="stop", scale=1, visible=False)

        with gr.Column():
            video_output = gr.Video(label="Result", interactive=False)
            result_md = gr.Markdown("")

    target_preset.change(
        fn=on_preset_change,
        inputs=target_preset,
        outputs=target_custom,
    )

    summary_inputs = [meta_state, target_preset, target_custom, remove_audio, resolution, start_t, end_t]

    video_input.change(
        fn=on_video_upload,
        inputs=[video_input, target_preset, target_custom, remove_audio, resolution, start_t, end_t],
        outputs=[meta_state, summary_md, start_t, end_t],
    )

    for component in (target_preset, target_custom, remove_audio, resolution, start_t, end_t):
        trigger = component.input if hasattr(component, "input") else component.change
        trigger(
            fn=on_settings_change,
            inputs=summary_inputs,
            outputs=summary_md,
        )

    # Generate the job id, clear any stale result message, and flip the UI to
    # "running" before the encode starts so the cancel button can call
    # compressor.cancel(job_id) at any time.
    prep_event = btn.click(
        fn=prepare_job,
        outputs=[active_job],
    ).then(
        fn=lambda: (gr.update(visible=False), gr.update(visible=True), ""),
        outputs=[btn, cancel_btn, result_md],
    )
    # Capture this dependency explicitly so cancels= targets the encode step.
    # result_md is computed in a separate .then() (below) so Gradio doesn't
    # paint a second progress overlay on it during the long compress.
    compress_event = prep_event.then(
        fn=processing_function,
        inputs=[active_job, video_input, target_preset, target_custom, remove_audio, speed_mode, resolution, start_t, end_t],
        outputs=video_output,
    )
    compress_event.then(
        fn=make_result_message,
        inputs=[video_output, video_input, target_preset, target_custom],
        outputs=result_md,
    ).then(
        fn=lambda: (gr.update(visible=True), gr.update(visible=False), None),
        outputs=[btn, cancel_btn, active_job],
    )

    cancel_btn.click(
        fn=cancel_active_job,
        inputs=[active_job],
        outputs=[btn, cancel_btn, active_job, result_md],
        cancels=[compress_event],
    )

if __name__ == "__main__":
    demo.queue(max_size=8).launch(server_name="0.0.0.0", server_port=7860, inbrowser=True)
