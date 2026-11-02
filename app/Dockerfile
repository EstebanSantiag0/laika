# Laika app: conversation engine, PWA, channels. CPU only.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Madrid

# git: the vault keeps its own local history (Phase 3, vault.py)
# ffmpeg: audio of saved videos (SB4); tesseract: text in scanned PDF pages and screenshots (SB3/SB5)
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa tesseract-ocr-rus tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# yt-dlp is not pinned on purpose: sites (Instagram above all) change often and only the newest release
# keeps up. Each build takes the newest; `python -m laika.admin update-ytdlp` updates a running container.
RUN pip install -U yt-dlp

COPY laika ./laika
COPY web ./web

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"

CMD ["uvicorn", "laika.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
