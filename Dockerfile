FROM python:3.12-slim

# Pull the uv binary from the official image — no pip-install indirection,
# and matches the version used locally.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps in a separate layer so source edits don't bust the cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

EXPOSE 7860

CMD ["uv", "run", "--no-sync", "python", "app.py"]
