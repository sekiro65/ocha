# ==========================================
# Stage 1: Build Explo Go Binary
# ==========================================
FROM golang:1.25-alpine AS builder

WORKDIR /build

RUN apk add --no-cache git build-base

COPY explo/go.mod explo/go.sum ./
RUN go mod download

COPY explo/src ./src
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags "-X explo/src/config.Version=release" -o /build/explo ./src/main/

# ==========================================
# Stage 2: Runtime Environment
# ==========================================
FROM python:3.12-slim-bookworm

LABEL maintainer="OCHA" \
      description="All-in-one automated music download, tagging, and playlist sync pipeline"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    BEETSDIR=/config/beets \
    WATCH_DIR=/downloads \
    MUSIC_DIR=/downloads \
    DATA_DIR=/config/telegram \
    LOG_DIR=/config/logs \
    YTDLP_PATH=/usr/local/bin/yt-dlp \
    FFMPEG_PATH=/usr/bin/ffmpeg \
    UI_PORT=8090 \
    EXPLO_PROXY_PORT=8091

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    flac \
    supervisor \
    curl \
    ca-certificates \
    bash \
    && rm -rf /var/lib/apt/lists/*

# YouTube needs a JavaScript runtime; Deno is the one yt-dlp enables by default
# (Debian's Node is too old for it).
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno

WORKDIR /app

COPY telegram_bot/requirements.txt /tmp/tg_reqs.txt
COPY music_sorter/requirements.txt /tmp/sorter_reqs.txt
COPY control_panel/requirements.txt /tmp/panel_reqs.txt
RUN pip install --no-cache-dir -r /tmp/tg_reqs.txt -r /tmp/sorter_reqs.txt -r /tmp/panel_reqs.txt "yt-dlp[default]" && \
    rm /tmp/*_reqs.txt

COPY music_sorter/patch_beets.py /tmp/patch_beets.py
RUN python3 /tmp/patch_beets.py && rm /tmp/patch_beets.py

# YouTube's native audio: no re-encoding, plays everywhere.
# Runtime writes go to /config, /downloads, /music or the /tmp tmpfs: the root
# file system can be read-only, and services run as PUID:PGID (see entrypoint).
ENV TRACK_EXTENSION=m4a \
    COOKIES_PATH=/config/cookies.txt \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/home \
    XDG_CACHE_HOME=/tmp/cache \
    PUID=1000 \
    PGID=1000

COPY --from=builder /build/explo /app/explo/explo

COPY telegram_bot /app/telegram_bot
COPY music_sorter /app/music_sorter
COPY control_panel /app/control_panel

COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /app/explo/explo

# Control panel, and the Explo dashboard behind the panel's login
EXPOSE 8090 8091

VOLUME ["/music", "/downloads", "/config"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fs "http://localhost:${UI_PORT}/api/session" > /dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
