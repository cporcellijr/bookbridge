# [START FILE: abs-kosync-enhanced/Dockerfile]
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=web_server.py \
    PYTHONPATH="/app" \
    LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib"

WORKDIR /app

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# 1. Install System Dependencies
# FFmpeg with full codec support for audio conversion
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    libavcodec-extra \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
COPY requirements-ctc.txt /app/requirements-ctc.txt

# 2. Install Python Dependencies
ARG INSTALL_GPU=false
# CTC forced alignment (issue #426) is opt-in: torch/torchaudio are large and stay
# out of the standard image. Build with --build-arg INSTALL_CTC=true (the -ctc tag).
ARG INSTALL_CTC=false
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    if [ "$INSTALL_GPU" = "true" ]; then \
    pip install --no-cache-dir nvidia-cublas-cu12 nvidia-cudnn-cu12; \
    fi && \
    if [ "$INSTALL_CTC" = "true" ]; then \
    pip install --no-cache-dir -r /app/requirements-ctc.txt; \
    fi

# 3. Create directories
RUN mkdir -p /app/src /app/templates /app/static /app/scripts /data/audio_cache /data/logs /data/transcripts

# 4. Copy Application Code
COPY src/ /app/src/
COPY templates/ /app/templates/
COPY static/ /app/static/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
COPY plugins/ /app/plugins/
COPY scripts/backup_db.sh /app/scripts/backup_db.sh

COPY start.sh /app/start.sh
RUN sed -i 's/\r$//' /app/start.sh /app/scripts/backup_db.sh && \
    chmod +x /app/start.sh /app/scripts/backup_db.sh

EXPOSE 5757

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5757/ || exit 1

CMD ["/app/start.sh"]
# [END FILE]
