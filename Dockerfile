FROM python:3.12-bookworm-slim

# Deno is yt-dlp's default JavaScript runtime for YouTube's solver.
ARG DENO_VERSION=2.9.8

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip ffmpeg gosu \
    && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && chmod 0755 /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && apt-get purge -y unzip curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server /app/server
COPY web /app/web
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    PORT=8095 \
    TZ=America/Toronto

EXPOSE 8095
VOLUME ["/config"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/api/health')"

ENTRYPOINT ["/entrypoint.sh"]
