FROM python:3.11-slim

WORKDIR /app

# System deps for Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and its OS dependencies
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application code
COPY config.py monitor.py ./

# Session volume — mount your whatsapp_session/ from the host
VOLUME ["/app/whatsapp_session"]

ENV PYTHONUNBUFFERED=1

CMD ["python", "monitor.py"]
