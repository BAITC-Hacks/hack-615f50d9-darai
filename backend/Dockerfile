# DARAI backend: FastAPI + local speech stack (CPU build by default).
# Model weights are NOT baked in: they are mounted read-only at /models.
FROM python:3.11-slim-bookworm

ARG INSTALL_ML=true
# CPU wheels by default; for CUDA use e.g. https://download.pytorch.org/whl/cu121
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PYANNOTE_METRICS_ENABLED=0 \
    DO_NOT_TRACK=1 \
    DATA_DIR=/data \
    MODELS_DIR=/models

# ffmpeg: normalization; LibreOffice writer: DOCX->PDF; DejaVu: Cyrillic + Kazakh glyphs offline.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libsndfile1 libreoffice-writer-nogui \
        fonts-dejavu-core fonts-dejavu-extra fonts-liberation2 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-ml.txt ./
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_ML" = "true" ]; then \
        pip install --extra-index-url "$TORCH_INDEX_URL" -r requirements-ml.txt -r requirements.txt; \
    fi

COPY . .
RUN useradd --system --uid 10001 --home /app darai \
 && mkdir -p /data && chown darai /data
USER darai

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"
# One worker: in-process job semaphore and reminder scheduler assume a single process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]
