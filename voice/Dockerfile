# Voice service: Qwen3-TTS on the GPU (CUDA 12.4 wheels, matches driver 550) + faster-whisper on CPU.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf-cache \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg sox libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=600s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
