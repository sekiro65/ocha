from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
import hmac
import os
import sqlite3
import logging
import time
import re
import asyncio
import uuid
import aiohttp
from aiohttp import web

from fast_telethon import download_file as fast_download_file

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Credentials for the Telegram userbot
api_id = int(os.environ.get('TG_API_ID', 0))
api_hash = os.environ.get('TG_API_HASH', '')
phone_number = os.environ.get('TG_PHONE', '')

# Admin Telegram user ID - messages from this ID can execute /status and /logs
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
PING_INTERVAL = int(os.environ.get('PING_INTERVAL_SECONDS', 60))

# The account/bot that delivers FLAC files pulled from your owned music DB
# (e.g. the Explo -> Spotify-link -> FLAC-lookup pipeline). Messages from
# this sender are downloaded, sorted, and logged exactly like anything else,
# but we never reply or edit status messages back at it - a chatty reply
# mid-transfer can stall whatever's driving that bot on the other end.
# Set via env; 0 disables quiet mode entirely.
# Numeric ID or @username. A username is turned into the ID at startup (resolve_music_bot).
MUSIC_BOT = os.environ.get('MUSIC_BOT_ID', '').strip()
MUSIC_BOT_ID = int(MUSIC_BOT) if MUSIC_BOT.lstrip('-').isdigit() else 0


def resolve_music_bot(client):
    """Look up a @username once; this also caches what Telegram needs to message it."""
    global MUSIC_BOT_ID
    if MUSIC_BOT_ID or not MUSIC_BOT:
        return
    try:
        MUSIC_BOT_ID = client.loop.run_until_complete(client.get_entity(MUSIC_BOT)).id
        logging.info(f"Music bot {MUSIC_BOT} is user {MUSIC_BOT_ID}")
    except Exception as e:
        logging.error(f"Could not find the music bot {MUSIC_BOT!r}: {e}")


def is_music_bot(sender_id):
    return bool(MUSIC_BOT_ID) and sender_id == MUSIC_BOT_ID


# === Explo integration (HTTP API) ===
# Port this bot's HTTP API listens on (localhost only) - Explo's
# TELEGRAM_BOT_API_URL should point at http://127.0.0.1:EXPLO_API_PORT
EXPLO_API_PORT = int(os.environ.get('EXPLO_API_PORT', 4352))
# Shared secret Explo must send as the X-API-Key header. Must match
# TELEGRAM_BOT_API_KEY in Explo's .env exactly.
EXPLO_API_KEY = os.environ.get('EXPLO_API_KEY', '')
# How long (seconds) an explo_jobs row can sit unmatched before a status
# check reports it as errored instead of leaving Explo polling forever.
EXPLO_JOB_TIMEOUT = int(os.environ.get('EXPLO_JOB_TIMEOUT_SECONDS', 900))
# A Spotify URL that already finished downloading within this many days is
# answered as 'done' without asking the music bot again. Explo deletes
# finished jobs, so this lives in its own table. 0 disables the guard.
EXPLO_DEDUPE_DAYS = int(os.environ.get('EXPLO_DEDUPE_DAYS', 30))

# Parallel-download tuning. Files at or above this size use the multi-
# connection downloader; smaller files aren't worth the extra connections.
# connection_count is capped conservatively - push it higher only if you
# don't see FloodWaitErrors in the log at the current value.
FAST_DOWNLOAD_THRESHOLD = int(os.environ.get('FAST_DOWNLOAD_THRESHOLD_MB', 10)) * 1024 * 1024
FAST_DOWNLOAD_MAX_CONNECTIONS = int(os.environ.get('FAST_DOWNLOAD_MAX_CONNECTIONS', 6))

# THE REAL SAFETY LEVER for batch drops (e.g. a whole season at once): caps
# how many files are actually downloading AT THE SAME TIME, regardless of
# how many arrive together. Extra files wait in a queue instead of all
# hammering Telegram at once. 2 concurrent files x 6 connections each stays
# comfortably under the global connection cap below, while still giving each
# active file the full parallel-connection speed boost.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get('MAX_CONCURRENT_DOWNLOADS', 2))
download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Small pause after grabbing a slot, before opening connections / sending
# messages for that file. Spreads out the burst when several queued files
# free up around the same moment (e.g. two small files finishing back to
# back), instead of everything restarting in the same instant.
DOWNLOAD_START_STAGGER_SECONDS = float(os.environ.get('DOWNLOAD_START_STAGGER_SECONDS', 1.5))

# Directories for storing media
DATA_DIR = os.environ.get('DATA_DIR', '/config/telegram')
DOWNLOADS_DIR = os.environ.get('DOWNLOADS_DIR', '/downloads')
MUSIC_DIR = os.environ.get('MUSIC_DIR', DOWNLOADS_DIR)

# Bot file storage & database
BOT_DIR = DATA_DIR
DB_PATH = os.path.join(DATA_DIR, 'downloads.db')
SESSION_PATH = os.path.join(DATA_DIR, 'bot_session')

for d in (MUSIC_DIR, BOT_DIR):
    os.makedirs(d, exist_ok=True)

log_handlers = [logging.StreamHandler()]
log_file = os.path.join(DATA_DIR, 'telegram_userbot.log')
try:
    log_handlers.append(logging.FileHandler(log_file))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=log_handlers
)

# In-memory tracking of currently-active downloads, keyed by message id.
# {task_id: {display_name, kind, downloaded, total, start_time, sender_name}}
active_downloads = {}

MUSIC_EXTENSIONS = {'.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.alac', '.aiff', '.aif'}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS downloads
                          (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           filename TEXT,
                           timestamp TEXT,
                           dest_path TEXT,
                           kind TEXT,
                           sorted INTEGER DEFAULT 0,
                           sender_id TEXT,
                           sender_name TEXT)''')
        # migrate older DBs that predate the extra columns
        existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(downloads)")}
        for col, col_def in (
            ('dest_path', 'TEXT'), ('kind', 'TEXT'), ('sorted', 'INTEGER DEFAULT 0'),
            ('sender_id', 'TEXT'), ('sender_name', 'TEXT'),
        ):
            if col not in existing_cols:
                cursor.execute(f"ALTER TABLE downloads ADD COLUMN {col} {col_def}")

        # Explo request tracking - one row per /explo/request call.
        # state: queued -> sent -> done | not_found | error
        cursor.execute('''CREATE TABLE IF NOT EXISTS explo_jobs
                          (job_id TEXT PRIMARY KEY,
                           spotify_url TEXT,
                           title TEXT,
                           artist TEXT,
                           album TEXT,
                           target_user_id TEXT,
                           state TEXT DEFAULT 'queued',
                           filename TEXT,
                           created_at REAL,
                           updated_at REAL)''')
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logging.error(f"Error initializing database: {e}")


def store_file_details(filename, dest_path, kind, is_sorted, sender_id, sender_name):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO downloads (filename, timestamp, dest_path, kind, sorted, sender_id, sender_name) "
            "VALUES (?, datetime('now'), ?, ?, ?, ?, ?)",
            (filename, dest_path, kind, int(is_sorted), str(sender_id) if sender_id else None, sender_name)
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logging.error(f"Error storing file details: {e}")


def fetch_recent_logs(limit=10):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT filename, sender_name, timestamp, kind, sorted FROM downloads ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows
    except sqlite3.Error as e:
        logging.error(f"Error reading logs: {e}")
        return None


# ---------------------------------------------------------------------------
# Explo job tracking (spotify link -> owned FLAC lookup, requested via the
# music-giving bot). One job per /explo/request call.
# ---------------------------------------------------------------------------
def _history_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS explo_history "
                 "(spotify_url TEXT PRIMARY KEY, filename TEXT, done_at REAL)")
    return conn


def record_history(spotify_url, filename):
    try:
        conn = _history_conn()
        conn.execute("INSERT OR REPLACE INTO explo_history VALUES (?, ?, ?)",
                     (spotify_url, filename, time.time()))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logging.error(f"Error recording explo history: {e}")


def recent_history(spotify_url):
    """Filename if this URL was delivered within EXPLO_DEDUPE_DAYS, else None."""
    if EXPLO_DEDUPE_DAYS <= 0:
        return None
    try:
        conn = _history_conn()
        row = conn.execute("SELECT filename FROM explo_history WHERE spotify_url = ? AND done_at > ?",
                           (spotify_url, time.time() - EXPLO_DEDUPE_DAYS * 86400)).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error as e:
        logging.error(f"Error reading explo history: {e}")
        return None


def create_job(spotify_url, title, artist, album, target_user_id):
    job_id = uuid.uuid4().hex
    now = time.time()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO explo_jobs (job_id, spotify_url, title, artist, album, target_user_id, "
            "state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
            (job_id, spotify_url, title, artist, album, target_user_id, now, now)
        )
        conn.commit()
        conn.close()
        return job_id
    except sqlite3.Error as e:
        logging.error(f"Error creating explo job: {e}")
        return None


def update_job(job_id, **fields):
    if not fields:
        return
    fields['updated_at'] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE explo_jobs SET {set_clause} WHERE job_id = ?",
            (*fields.values(), job_id)
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logging.error(f"Error updating explo job {job_id}: {e}")


def get_job(job_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM explo_jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(f"Error reading explo job {job_id}: {e}")
        return None


def delete_job(job_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM explo_jobs WHERE job_id = ?", (job_id,))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logging.error(f"Error deleting explo job {job_id}: {e}")


def _normalize_for_match(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def find_pending_job_for_file(file_name):
    """
    Best-effort correlation between an incoming FLAC from the music-giving
    bot and the /explo/request job that asked for it. Matches on title
    substring first (required), artist substring as a tiebreaker, picks the
    oldest pending job that matches (FIFO, since requests are usually
    resolved in the order they're sent).

    Adjust this once the music-giving bot's actual reply format/protocol is
    known - e.g. if it echoes back metadata or a job reference, prefer an
    exact correlation over fuzzy matching.
    """
    norm_file = _normalize_for_match(file_name)
    if not norm_file:
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM explo_jobs WHERE state IN ('queued','sent') ORDER BY created_at ASC"
        )
        candidates = [dict(row) for row in cursor.fetchall()]
        conn.close()
    except sqlite3.Error as e:
        logging.error(f"Error querying pending explo jobs: {e}")
        return None

    best = None
    for job in candidates:
        norm_title = _normalize_for_match(job.get('title'))
        if norm_title and norm_title in norm_file:
            best = job
            break  # oldest match wins, FIFO
    return best


# Every message to the music bot goes through this one queue, whoever asked
# (Explo's playlists or the panel's Request page): at most one per interval, and
# a Telegram FloodWait pauses the whole queue for as long as Telegram asks.
DISPATCH_INTERVAL = int(os.environ.get('TELEGRAM_DISPATCH_INTERVAL_SECONDS', 20))
MAX_FLOOD_WAIT = int(os.environ.get('TELEGRAM_MAX_FLOOD_WAIT_SECONDS', 3600))
_dispatch_lock = asyncio.Lock()
_last_dispatch = 0.0


async def _send_once(client, text):
    try:
        await client.send_message(MUSIC_BOT_ID, text)
    except ValueError:
        # Telegram needs the bot's access hash, which a fresh sign-in only
        # learns from the chat list. Load it once and retry.
        await client.get_dialogs()
        await client.send_message(MUSIC_BOT_ID, text)


async def send_track_request(client, job):
    """Send the Spotify link to the music bot, paced and FloodWait-safe."""
    global _last_dispatch
    if not MUSIC_BOT_ID:
        update_job(job['job_id'], state='error')
        logging.error("[explo] music bot not configured, cannot dispatch request")
        return False
    async with _dispatch_lock:
        wait = _last_dispatch + DISPATCH_INTERVAL - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            try:
                await _send_once(client, job['spotify_url'])
            except FloodWaitError as e:
                if e.seconds > MAX_FLOOD_WAIT:
                    raise
                logging.warning(f"[explo] Telegram asked to slow down: pausing all requests for {e.seconds}s")
                await asyncio.sleep(e.seconds + 1)
                await _send_once(client, job['spotify_url'])
            update_job(job['job_id'], state='sent')
            logging.info(f"[explo] dispatched job {job['job_id']} ({job['title']} - {job['artist']}) "
                         f"to music bot")
            return True
        except ValueError:
            logging.error(f"[explo] failed to dispatch job {job['job_id']}: this account has no chat with the "
                          f"music bot ({MUSIC_BOT}). Open the bot in Telegram with this account, press Start, retry.")
        except FloodWaitError as e:
            logging.error(f"[explo] Telegram requires a {e.seconds}s pause (over the {MAX_FLOOD_WAIT}s limit); "
                          f"job {job['job_id']} dropped")
        except Exception as e:
            logging.error(f"[explo] failed to dispatch job {job['job_id']}: {e}")
        finally:
            _last_dispatch = time.time()
        update_job(job['job_id'], state='error')
        return False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def sanitize_filename(name):
    if not name:
        return name
    cleaned = re.sub(r'[<>:"/\\|?*]', '', name)
    return cleaned.strip()


def human_bytes(n):
    if not n:
        return "0 B"
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_eta(seconds):
    if seconds is None or seconds < 0 or seconds != seconds:  # None, negative, or NaN
        return "?"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def render_bar(pct, width=20):
    pct = max(0.0, min(pct, 1.0))
    filled = int(round(width * pct))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct * 100:5.1f}%"


def get_display_name(sender):
    """Best-effort human-readable name for a Telegram sender object."""
    if not sender:
        return "Unknown"
    username = getattr(sender, 'username', None)
    if username:
        return f"@{username}"
    first = getattr(sender, 'first_name', '') or ''
    last = getattr(sender, 'last_name', '') or ''
    full = f"{first} {last}".strip()
    return full or f"ID {getattr(sender, 'id', '?')}"


# ---------------------------------------------------------------------------
# Where an incoming file lands (decided before the download starts)
# ---------------------------------------------------------------------------
def determine_destination(file_name):
    """Audio files land in MUSIC_DIR (the sorter's watch folder) under their own
    name; Beets does the tagging and filing. Anything else returns None and is
    not downloaded: OCHA is music only."""
    if os.path.splitext(file_name)[1].lower() not in MUSIC_EXTENSIONS:
        return None
    return {'kind': 'music', 'dest_dir': MUSIC_DIR, 'final_name': sanitize_filename(file_name)}


# ---------------------------------------------------------------------------
# Status / progress rendering
# ---------------------------------------------------------------------------
def build_single_progress(info, done=False):
    """The self-editing message that lives in the sender's own chat."""
    total = info.get('total') or 0
    downloaded = info.get('downloaded') or 0
    pct = 1.0 if done else ((downloaded / total) if total else 0.0)

    lines = [f"Download progress: {info['display_name']}", render_bar(pct)]

    if done:
        lines.append(f"{human_bytes(total or downloaded)}  |  complete")
    else:
        elapsed = max(time.time() - info['start_time'], 0.01)
        speed = downloaded / elapsed
        eta = ((total - downloaded) / speed) if speed > 0 and total else None
        lines.append(
            f"{human_bytes(downloaded)} / {human_bytes(total) if total else '?'}"
            f"  |  {human_bytes(speed)}/s  |  ETA {format_eta(eta)}"
        )

    return "```\n" + "\n".join(lines) + "\n```"


def build_status_message():
    """Admin-only overview of every currently-active download, from anyone."""
    if not active_downloads:
        return "No active downloads right now."

    now = time.time()
    lines = [f"ACTIVE DOWNLOADS ({len(active_downloads)})", "-" * 34]

    for info in active_downloads.values():
        total = info.get('total') or 0
        downloaded = info.get('downloaded') or 0
        pct = (downloaded / total) if total else 0.0
        elapsed = max(now - info['start_time'], 0.01)
        speed = downloaded / elapsed
        eta = ((total - downloaded) / speed) if speed > 0 and total else None

        kind_tag = info['kind'].upper()
        lines.append(f"[{kind_tag}] {info['display_name'][:42]}")
        lines.append(f"From: {info.get('sender_name', 'Unknown')}")
        lines.append(render_bar(pct))
        lines.append(
            f"{human_bytes(downloaded)} / {human_bytes(total) if total else '?'}"
            f"  |  {human_bytes(speed)}/s  |  ETA {format_eta(eta)}"
        )
        lines.append("")

    lines.append("-" * 34)
    return "```\n" + "\n".join(lines).rstrip() + "\n```"


def build_logs_message(limit=10):
    """Admin-only: last N downloads, who sent them, and whether they got sorted."""
    rows = fetch_recent_logs(limit)
    if rows is None:
        return "Couldn't read the log database."
    if not rows:
        return "No download logs yet."

    lines = [f"LAST {len(rows)} DOWNLOADS", "-" * 34]
    for filename, sender_name, timestamp, kind, sorted_flag in rows:
        tag = "SORTED" if sorted_flag else "UNSORTED"
        lines.append(f"{timestamp}  [{tag}]")
        lines.append(f"From: {sender_name or 'Unknown'}")
        lines.append(f"File: {filename}")
        lines.append("")
    lines.append("-" * 34)
    return "```\n" + "\n".join(lines).rstrip() + "\n```"


async def progress_updater(status_msg, task_id):
    """Edits the per-download progress message every 5s until it's removed."""
    try:
        while task_id in active_downloads:
            await asyncio.sleep(5)
            if task_id not in active_downloads:
                break
            try:
                await status_msg.edit(build_single_progress(active_downloads[task_id]))
            except Exception as e:
                logging.debug(f"Progress edit skipped: {e}")
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Main download handler
# ---------------------------------------------------------------------------
async def handle_new_message(event):
    if not (event.file and event.file.name):
        return

    sender_id = event.sender_id
    # Only the music bot (and you, if ADMIN_ID is set) can make this account
    # download anything. Files from anyone else are ignored without a reply.
    if not (is_music_bot(sender_id) or (ADMIN_ID and sender_id == ADMIN_ID)):
        return

    file_name = event.file.name
    task_id = event.id
    logging.info(f"New file received: {file_name}")

    sender = await event.get_sender()
    sender_name = get_display_name(sender)

    quiet = is_music_bot(sender_id)

    # If every slot is already taken (e.g. mid-way through a season drop),
    # let the sender know it's queued rather than going silent until it
    # eventually starts. Skipped for the music bot - logged instead.
    if download_slots.locked():
        if quiet:
            logging.info(f"[music-bot] '{file_name}' queued - all download slots busy")
        else:
            await event.reply(
                f"Queued '{file_name}' - {MAX_CONCURRENT_DOWNLOADS} download(s) already "
                f"running, this will start once a slot frees up."
            )

    async with download_slots:
        # Smooths out the moment several queued files free up back-to-back
        # so their opening messages/connections don't land in the same instant.
        await asyncio.sleep(DOWNLOAD_START_STAGGER_SECONDS)
        await _process_download(event, file_name, task_id, sender_id, sender_name, quiet)


async def _process_download(event, file_name, task_id, sender_id, sender_name, quiet=False):
    decision = determine_destination(file_name)
    if decision is None:
        logging.info(f"Ignored non-audio file '{file_name}'")
        if not quiet:
            await event.reply(f"Skipped '{file_name}': only audio files are downloaded.")
        return
    os.makedirs(decision['dest_dir'], exist_ok=True)

    final_path = os.path.join(decision['dest_dir'], decision['final_name'])
    # Download to a hidden, clearly-partial filename so Jellyfin never scans
    # a half-written file. Once complete we just rename it in place.
    partial_path = os.path.join(decision['dest_dir'], f".{decision['final_name']}.partial")

    active_downloads[task_id] = {
        'display_name': decision['final_name'],
        'kind': decision['kind'],
        'downloaded': 0,
        'total': event.file.size or 0,
        'start_time': time.time(),
        'sender_name': sender_name,
    }

    status_msg = None
    updater_task = None
    if quiet:
        logging.info(f"[music-bot] downloading '{file_name}' -> {final_path}")
    else:
        await event.reply(f"Downloading '{file_name}'...")
        status_msg = await event.reply(build_single_progress(active_downloads[task_id]))
        updater_task = asyncio.create_task(progress_updater(status_msg, task_id))

    def progress(downloaded, total):
        if task_id in active_downloads:
            active_downloads[task_id]['downloaded'] = downloaded
            active_downloads[task_id]['total'] = total

    use_fast = bool(event.document) and (event.file.size or 0) >= FAST_DOWNLOAD_THRESHOLD

    try:
        if use_fast:
            try:
                with open(partial_path, 'wb') as out:
                    await fast_download_file(
                        event.client, event.document, out,
                        progress_callback=progress,
                        connection_count=FAST_DOWNLOAD_MAX_CONNECTIONS,
                    )
            except Exception as e:
                logging.warning(f"Parallel download failed for {file_name} ({e}), "
                                 f"falling back to standard download")
                await event.download_media(file=partial_path, progress_callback=progress)
        else:
            await event.download_media(file=partial_path, progress_callback=progress)

        os.rename(partial_path, final_path)
        logging.info(f"Downloaded & placed: {file_name} -> {final_path}")
        store_file_details(decision['final_name'], final_path, decision['kind'], True, sender_id, sender_name)

        if quiet:
            job = find_pending_job_for_file(decision['final_name'])
            if job:
                update_job(job['job_id'], state='done', filename=decision['final_name'])
                record_history(job['spotify_url'], decision['final_name'])
                logging.info(f"[explo] job {job['job_id']} matched -> '{decision['final_name']}'")
            else:
                logging.warning(f"[explo] no pending job matched incoming file '{decision['final_name']}' "
                                 f"- Explo will time out on this one, check /explo/status manually if needed")

        if quiet:
            if updater_task:
                updater_task.cancel()
            logging.info(f"[music-bot] '{decision['final_name']}' ready for the sorter")
        else:
            updater_task.cancel()
            await status_msg.edit(build_single_progress(active_downloads[task_id], done=True))
            await event.reply(f"'{decision['final_name']}' downloaded. The sorter files it next.")

    except Exception as e:
        logging.error(f"Error downloading/sorting file {file_name}: {e}")
        if updater_task:
            updater_task.cancel()
        if quiet:
            logging.error(f"[music-bot] failed handling '{file_name}': {e}")
        else:
            try:
                await status_msg.edit(f"Download failed: {file_name}\n{e}")
            except Exception:
                pass
            await event.reply(f"Error while handling '{file_name}': {e}")
        if os.path.exists(partial_path):
            try:
                os.remove(partial_path)
            except OSError:
                pass
    finally:
        active_downloads.pop(task_id, None)


# ---------------------------------------------------------------------------
# Admin commands (only acted on when the sender is you, ADMIN_ID)
# ---------------------------------------------------------------------------
async def handle_status_command(event):
    if event.sender_id != ADMIN_ID:
        return
    await event.reply(build_status_message())


async def handle_logs_command(event):
    if event.sender_id != ADMIN_ID:
        return
    await event.reply(build_logs_message(limit=10))


# ---------------------------------------------------------------------------
# Explo HTTP API
# ---------------------------------------------------------------------------
@web.middleware
async def explo_auth_middleware(request, handler):
    if not EXPLO_API_KEY:
        logging.warning("[explo] EXPLO_API_KEY is unset - refusing all API requests. "
                         "Set it in the bot's .env to enable the Explo integration.")
        return web.json_response({"error": "API not configured"}, status=503)
    if not hmac.compare_digest(request.headers.get('X-API-Key', '').encode(), EXPLO_API_KEY.encode()):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def handle_explo_request(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    spotify_url = payload.get('spotify_url')
    if not spotify_url:
        return web.json_response({"error": "spotify_url is required"}, status=400)

    job_id = create_job(
        spotify_url=spotify_url,
        title=payload.get('title', ''),
        artist=payload.get('artist', ''),
        album=payload.get('album', ''),
        target_user_id=payload.get('target_user_id', ''),
    )
    if not job_id:
        return web.json_response({"error": "failed to create job"}, status=500)

    already = recent_history(spotify_url)
    if already:
        update_job(job_id, state='done', filename=already)
        logging.warning(f"[explo] skipped repeat request {spotify_url} "
                        f"(delivered as '{already}' within {EXPLO_DEDUPE_DAYS}d) - check the library matcher")
        return web.json_response({"job_id": job_id})

    job = get_job(job_id)
    client = request.app['client']
    asyncio.create_task(send_track_request(client, job))

    return web.json_response({"job_id": job_id})


async def handle_explo_status(request):
    job_id = request.match_info['job_id']
    job = get_job(job_id)
    if not job:
        return web.json_response({"error": "job not found"}, status=404)

    # Time out stale jobs so Explo doesn't poll forever if the music bot
    # never responds.
    if job['state'] == 'sent' and (time.time() - (job['updated_at'] or job['created_at'])) > EXPLO_JOB_TIMEOUT:
        update_job(job_id, state='error')
        job['state'] = 'error'

    return web.json_response({
        "job_id": job['job_id'],
        "state": job['state'],
        "filename": job['filename'] or "",
    })


async def handle_explo_delete(request):
    job_id = request.match_info['job_id']
    delete_job(job_id)
    return web.json_response({"deleted": job_id})


async def start_explo_api(client):
    app = web.Application(middlewares=[explo_auth_middleware])
    app['client'] = client
    app.router.add_post('/explo/request', handle_explo_request)
    app.router.add_get('/explo/status/{job_id}', handle_explo_status)
    app.router.add_delete('/explo/job/{job_id}', handle_explo_delete)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', EXPLO_API_PORT)
    await site.start()
    logging.info(f"[explo] API listening on :{EXPLO_API_PORT}")


# ---------------------------------------------------------------------------
# Connectivity watchdog
# ---------------------------------------------------------------------------
async def ping_and_notify(client):
    was_offline = False
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get('http://www.google.com', timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        if was_offline:
                            await client.send_message(ADMIN_ID, "Server is back online!")
                            logging.info("Server is back online, notification sent")
                            was_offline = False
                    else:
                        was_offline = True
                        logging.warning("Ping failed: Non-200 status")
            except Exception as e:
                was_offline = True
                logging.warning(f"Ping failed: {str(e)}")
            await asyncio.sleep(PING_INTERVAL)


# ---------------------------------------------------------------------------
# Bot bootstrap
# ---------------------------------------------------------------------------
def start_bot():
    init_db()
    client = TelegramClient(SESSION_PATH, api_id, api_hash, connection_retries=-1, auto_reconnect=True)
    client.parse_mode = 'markdown'  # needed so the ``` code blocks render monospaced

    @client.on(events.NewMessage(incoming=True))
    async def file_handler(event):
        await handle_new_message(event)

    # These match on incoming text because the bot runs on a separate
    # dedicated account - your own /status or /logs message arrives here
    # as an incoming message FROM you (ADMIN_ID), not as an outgoing one.
    @client.on(events.NewMessage(incoming=True, pattern=r'^/status$'))
    async def status_handler(event):
        await handle_status_command(event)

    @client.on(events.NewMessage(incoming=True, pattern=r'^/logs$'))
    async def logs_handler(event):
        await handle_logs_command(event)

    client.start(phone=phone_number)
    try:
        client.loop.run_until_complete(client.get_dialogs())  # cache chats, incl. the music bot
    except Exception as e:
        logging.warning(f"Could not load the chat list: {e}")
    resolve_music_bot(client)
    if ADMIN_ID:
        client.loop.create_task(ping_and_notify(client))
    client.loop.create_task(start_explo_api(client))
    logging.info("Bot started and running...")
    async def _run_with_reconnect():
        import asyncio as _aio
        while True:
            try:
                await client.run_until_disconnected()
            except Exception as _e:
                logging.error('Telegram connection lost: %s - reconnecting in 10s', _e)
                await _aio.sleep(10)
                try:
                    await client.connect()
                    logging.info('Telegram reconnected OK')
                except Exception as _ce:
                    logging.error('Reconnect failed: %s - retrying in 30s', _ce)
                    await _aio.sleep(30)

    client.loop.run_until_complete(_run_with_reconnect())


if __name__ == "__main__":
    start_bot()