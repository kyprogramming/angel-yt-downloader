# ── Build stage ───────────────────────────────────────────────────────────
FROM python:3.12-slim

# Install FFmpeg (required for video+audio merging)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY app.py .
COPY index.html .

# Render sets PORT env var; default to 10000 (Render's default)
ENV PORT=10000

# Downloads go to /tmp/downloads (writable on Render free tier)
ENV DOWNLOAD_DIR=/tmp/downloads

# Expose port
EXPOSE $PORT

# Run with gunicorn — production WSGI server
# --workers 1        : keep 1 worker so progress_store is shared in memory
# --threads 4        : handle concurrent requests (downloads) within the worker
# --timeout 600      : allow up to 10 min per request (big video downloads)
# --worker-class gthread : thread-based worker needed for --threads > 1
CMD gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --threads 4 \
    --worker-class gthread \
    --timeout 600 \
    --access-logfile - \
    --error-logfile -