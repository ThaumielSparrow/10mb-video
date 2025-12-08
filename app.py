import gradio as gr
from compressor import VideoCompressor

# Initialize Logic
compressor = VideoCompressor()

def processing_function(video_file, target_size_type, custom_size_mb, remove_audio, use_h265, start_time, end_time, progress=gr.Progress()):
    if video_file is None:
        return None

    # Parse Target Size
    if target_size_type == "Custom":
        target_mb = float(custom_size_mb)
    else:
        target_mb = float(target_size_type.lower().replace("mb", ""))

    try:
        output_path = compressor.compress(
            input_path=video_file,
            target_mb=target_mb,
            remove_audio=remove_audio,
            start_time=start_time,
            end_time=end_time,
            use_h265=use_h265,
            progress_callback=progress
        )
        return output_path
    except Exception as e:
        raise gr.Error(f"Compression failed: {str(e)}")

# UI Layout
with gr.Blocks(title="Smart Video Compressor") as demo:
    gr.Markdown("# 📼 Smart Video Compressor")
    gr.Markdown("Compress videos to a specific size, inspired by 8mb.video.")
    
    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="Upload Video", sources=["upload"])
            
            with gr.Group():
                target_size = gr.Dropdown(
                    choices=["8MB", "10MB", "25MB", "50MB", "Custom"], 
                    value="8MB", 
                    label="Target File Size",
                    filterable=False,
                    allow_custom_value=False
                )
                custom_size = gr.Number(
                    value=15, 
                    label="Custom Size (MB)", 
                    visible=False
                )
            
            with gr.Row():
                remove_audio = gr.Checkbox(label="Remove Audio", value=False)
                use_h265 = gr.Checkbox(
                    label="Use H.265 (HEVC)", 
                    value=False
                )
            
            with gr.Accordion("Trimming Options", open=False):
                with gr.Row():
                    start_t = gr.Number(label="Start (sec)", value=0)
                    end_t = gr.Number(label="End (sec)", value=None)

            btn = gr.Button("Compress", variant="primary")

        with gr.Column():
            video_output = gr.Video(label="Result")

    # Events
    target_size.change(
        fn=lambda x: gr.Number(visible=(x == "Custom")), 
        inputs=target_size, 
        outputs=custom_size
    )

    btn.click(
        fn=processing_function,
        # Updated inputs list
        inputs=[video_input, target_size, custom_size, remove_audio, use_h265, start_t, end_t],
        outputs=video_output
    )

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=7860)