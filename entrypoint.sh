#!/bin/bash
set -e

# Runs as root only for this setup, then drops to PUID:PGID (see the end).
PUID=${PUID:-1000}
PGID=${PGID:-1000}
mkdir -p /music /downloads /config/beets /config/explo /config/telegram /config/logs /config/ui /tmp/home /tmp/cache
[ -e /config/explo/.env ] || : > /config/explo/.env   # Explo's dashboard keeps its playlist schedules here

# Seed default beets config if not present in /config/beets
if [ ! -e /config/beets/config.yaml ] && [ ! -L /config/beets/config.yaml ]; then
    cp /app/music_sorter/config.yaml /config/beets/config.yaml
fi
export BEETSDIR="/config/beets"

# /config/settings.env is what the control panel edits. On first start, seed it
# from the container environment (.env) so existing setups keep working.
SETTINGS=/config/settings.env
if [ ! -f "$SETTINGS" ]; then
    echo "[entrypoint] creating $SETTINGS from environment"
    for key in DOWNLOAD_SERVICES EXPLO_SYSTEM SYSTEM_URL LIBRARY_NAME API_KEY SYSTEM_USERNAME SYSTEM_PASSWORD \
               LISTENBRAINZ_USER LISTENBRAINZ_USER_TOKEN LISTENBRAINZ_DISCOVERY SPOTIFY_CLIENT_ID SPOTIFY_CLIENT_SECRET \
               TRACK_EXTENSION SLSKD_URL SLSKD_API_KEY TG_API_ID TG_API_HASH TG_PHONE MUSIC_BOT_ID ADMIN_ID \
               EXPLO_API_KEY TELEGRAM_DISPATCH_INTERVAL_SECONDS EXPLO_JOB_TIMEOUT_SECONDS PERSIST; do
        [ -n "${!key:-}" ] && printf '%s=%s\n' "$key" "${!key}" >> "$SETTINGS"
    done
    grep -q '^DOWNLOAD_SERVICES=' "$SETTINGS" 2>/dev/null || echo "DOWNLOAD_SERVICES=youtube" >> "$SETTINGS"
    chmod 600 "$SETTINGS"
fi
# Shared secret between Explo and the Telegram bot's local API
if ! grep -qE '^EXPLO_API_KEY=.+' "$SETTINGS"; then
    echo "EXPLO_API_KEY=$(python3 -I -s -c 'import secrets; print(secrets.token_hex(16))')" >> "$SETTINGS"
fi

# Fixed internal wiring (not user settings)
export WEB_UI=true
export WEB_ADDR=127.0.0.1:7288          # only the panel's proxy can reach it
export UI_AUTH_DISABLED=true            # the panel does the auth
export WEB_DATA_PATH=/config/explo
export WEB_ENV_PATH=/config/explo/.env
export DOWNLOAD_DIR=/downloads
export TELEGRAM_BOT_API_URL=http://127.0.0.1:4352
export PERSIST=${PERSIST:-false}

# Give the service user its folders. Only files with the wrong owner are touched,
# so this is quick after the first start, even with a large library.
find /config /downloads /music /tmp/home /tmp/cache \! \( -user "$PUID" -group "$PGID" \)     -exec chown -h "$PUID:$PGID" {} + 2>/dev/null || true
chmod 600 "$SETTINGS"
# logins, cookies and password hash (-type f: never follow a symlink as root)
find /config/telegram /config/ui /config -maxdepth 1 -type f \( -name '*.session' -o -name '*.json' -o -name cookies.txt \)     -exec chmod 600 {} + 2>/dev/null || true

echo "=== Starting OCHA as $PUID:$PGID: panel on :${UI_PORT:-8090}, Explo on :${EXPLO_PROXY_PORT:-8091} ==="
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all     /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
