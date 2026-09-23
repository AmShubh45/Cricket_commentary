FROM python:3.11-slim

WORKDIR /app

# System dependencies: ffmpeg for audio processing, fonts for Indic/Devanagari, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-indic \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 1. Install dependencies first for fast layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. Copy project metadata and source code
COPY pyproject.toml README.md ./
COPY src/ src/
COPY templates/ templates/
COPY config/ config/
COPY scripts/ scripts/

# 3. Install package in editable mode
RUN pip install --no-cache-dir -e .

# 4. Create output directory for live audio and broadcast media
RUN mkdir -p /app/output/audio /app/output/frames /app/output/video /app/output/live

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src:/app/config:/app
ENV PORT=8000

EXPOSE 8000

# Start live stream broadcast server, binding to Render's dynamic $PORT
CMD ["sh", "-c", "python scripts/run_live_stream_server.py --port ${PORT:-8000}"]
