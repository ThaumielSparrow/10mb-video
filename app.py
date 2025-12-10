import gradio as gr
from compressor import VideoCompressor
from utils import load_js

# Initialize Logic
compressor = VideoCompressor()

def processing_function(video_file, strict_val, editable_val, remove_audio, speed_mode, start_time, end_time, progress=gr.Progress()):
    if video_file is None:
        return None

    # Parse Target Size
    if strict_val == "Custom":
        try:
            # Clean user input
            clean_val = str(editable_val).lower().replace("mb", "").strip()
            target_mb = float(clean_val)
        except ValueError:
            raise gr.Error("Invalid custom size. Please enter a valid number.")
        
    else:
        target_mb = float(strict_val.lower().replace("mb", "").strip())

    try:
        output_path = compressor.compress(
            input_path=video_file,
            target_mb=target_mb,
            remove_audio=remove_audio,
            start_time=start_time,
            end_time=end_time,
            speed_mode=speed_mode,
            progress_callback=progress
        )
        return output_path
    except Exception as e:
        raise gr.Error(f"Compression failed: {str(e)}")

# Custom CSS injection as HTML styles
css_html = """
<style>
.status-tracker .eta { display: none !important; }
.status-tracker .time { display: none !important; }
.progress-level .time { display: none !important; }
.meta-text { display: none !important; } 

.vertical-align-fix {
    padding-top: 34px !important;
    margin-top: 0px !important; 
}

@media (max-width: 768px) {
    .vertical-align-fix {
        padding-top: 0px !important;
    }
}
</style>
"""

# UI Layout
with gr.Blocks(title="Smart Video Compressor") as demo:
    gr.HTML(css_html)
    gr.Markdown("# 📼 Smart Video Compressor")
    gr.Markdown("Compress videos to a specific size, inspired by 8mb.video.")
    
    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="Upload Video", sources=["upload"])

            with gr.Row():
                presets = ["8 MB", "10 MB", "25 MB", "50 MB"]
                target_strict = gr.Dropdown(
                    choices=presets + ["Custom"], 
                    value="10 MB",
                    label="Target File Size",
                    filterable=False,
                    allow_custom_value=False,
                    scale=1,
                    visible=True
                )

                target_editable = gr.Dropdown(
                    choices=presets,
                    value=None,
                    label = "Target File Size (MB)",
                    filterable=True,
                    allow_custom_value=True,
                    scale=1,
                    visible=False
                )

                speed_mode = gr.Dropdown(
                    choices=["Prioritize Speed", "Prioritize Quality"],
                    value="Prioritize Speed",
                    label="Encoding Preset",
                    filterable=False,
                    allow_custom_value=False,
                    scale=2
                )

                remove_audio = gr.Checkbox(
                    label="Remove Audio",
                    value=False,
                    elem_classes=["vertical-align-fix"],
                    scale=1
                )
            
            with gr.Accordion("Trimming Options", open=False):
                with gr.Row():
                    start_t = gr.Number(label="Start (sec)", value=0)
                    end_t = gr.Number(label="End (sec)", value=None)

            btn = gr.Button("Compress", variant="primary")

        with gr.Column():
            video_output = gr.Video(label="Result")

    # Events 
    target_strict.change(
        fn=None, 
        inputs=target_strict, 
        outputs=[target_strict, target_editable],
        js=load_js("js/to_custom.js")
    )

    target_editable.input(
        fn=None,
        inputs=target_editable,
        outputs=[target_strict, target_editable],
        js=load_js("js/to_presets.js")
    )

    btn.click(
        fn=processing_function,
        inputs=[video_input, target_strict, target_editable, remove_audio, speed_mode, start_t, end_t],
        outputs=video_output
    )

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7860, inbrowser=True)