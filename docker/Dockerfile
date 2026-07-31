# WebUI / orchestrator only. No model, no GPU — it just talks HTTP to SearXNG,
# LM Studio and the Irodori-TTS servers, so it runs anywhere, including x86 Linux.
# The TTS servers are NOT part of this image; they need a GPU (or Apple MPS) and
# stay on their own machine.
FROM python:3.12-slim

# ffmpeg stands in for macOS's afconvert when converting uploaded reference
# audio to wav. backend/voices.py picks whichever of the two is present.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install --no-cache-dir -e .

COPY static ./static

# Written at runtime; mount volumes over logs/ and data/ to keep them across
# restarts. audio_cache is scratch — clips are disposable once played.
RUN mkdir -p /app/audio_cache /app/logs /app/data
ENV AUTOSPEAK_AUDIO_DIR=/app/audio_cache \
    AUTOSPEAK_LOGS_DIR=/app/logs \
    AUTOSPEAK_SETTINGS_FILE=/app/data/settings.local.json \
    AUTOSPEAK_HOST=0.0.0.0 \
    AUTOSPEAK_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8000/api/state || exit 1

CMD ["python", "-m", "uvicorn", "backend.server:app", "--host", "0.0.0.0", "--port", "8000"]
