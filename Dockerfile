FROM python:3.12-slim

# Pull the uv binary from the official image — no pip-install indirection,
# and matches the version used locally. Pinned so rebuilds are reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.10.5 /uv /uvx /bin/

# --no-install-recommends skips the VA-API/VDPAU/Mesa drivers, X11 libs and
# fonts apt pulls in by default; a CPU-only encoder never loads them, and a
# smaller image means a faster cold start when the Space wakes from sleep.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# UV_COMPILE_BYTECODE: ship .pyc in the image. Every container start is a fresh
# filesystem, so otherwise Python recompiles gradio and its deps on each boot
# (import gradio: ~14s cold vs ~4s with bytecode, measured locally).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    GRADIO_ANALYTICS_ENABLED=False \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install deps in a separate layer so source edits don't bust the cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

EXPOSE 7860

# Run the venv's python directly rather than through `uv run`: one less process
# between the Space's SIGTERM and the app.
CMD ["python", "app.py"]
