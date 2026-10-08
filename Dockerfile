FROM python:3.12-slim

# freezedetect and frame extraction shell out to ffmpeg. The free grouping rules
# (used when AI isn't available) read screen text with tesseract, and use the
# system word list (wamerican) to ignore OCR garble.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr wamerican \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# video_processing.py lives at the repo root and is imported by the backend.
COPY video_processing.py ./
COPY backend ./backend

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

EXPOSE 8080

# --proxy-headers so request.base_url (used to build frame image URLs) comes
# out as https://<app>.fly.dev rather than http://, behind Fly's proxy.
CMD ["sh", "-c", "exec uvicorn backend.app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'"]
