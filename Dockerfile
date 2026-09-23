FROM python:3.11-slim

WORKDIR /app

# System dependencies: ffmpeg (with Pango/Cairo for Devanagari), Playwright browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpango1.0-dev \
    libcairo2-dev \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-indic \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Install Playwright browsers
RUN playwright install --with-deps chromium

# Copy application code
COPY src/ src/
COPY templates/ templates/
COPY config/ config/

# Create output directory for rendered media
RUN mkdir -p /app/output/audio /app/output/frames /app/output/video

EXPOSE 8000

CMD ["uvicorn", "cricket.main:app", "--host", "0.0.0.0", "--port", "8000"]
