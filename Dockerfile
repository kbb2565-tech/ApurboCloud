FROM python:3.11-slim

# Node.js + npm (node runtime bot er jonno), git, build tools, curl (cloudflared download er jonno)
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm git build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Cloudflare Tunnel client (cloudflared). Architecture onujayi download hoy.
ARG TARGETARCH
RUN curl -fsSL -o /usr/local/bin/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${TARGETARCH:-amd64}" \
    && chmod +x /usr/local/bin/cloudflared \
    && cloudflared --version

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/start.sh

# Railway volume ta /data e mount korbo; data.json ar servers/ ekhane thakbe
ENV DATA_DIR=/data
RUN mkdir -p /data

CMD ["sh", "/app/start.sh"]
