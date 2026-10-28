#!/usr/bin/env python3
"""OCHA control panel.

One password-protected UI for settings, Telegram login, song requests, live
download status and logs. Also reverse-proxies the Explo dashboard (which
listens on localhost with its own auth off) on EXPLO_PROXY_PORT, so a single
login covers both.

Env: UI_PORT (8090), UI_PASSWORD (initial password, default "Melovepassword";
the panel makes you change the default on first sign-in), EXPLO_PROXY_PORT
(UI_PORT + 1). Behind a domain, set UI_PUBLIC_URL and EXPLO_PUBLIC_URL
(e.g. https://ocha.example.com and https://ocha-explo.example.com).
ALLOWED_HOSTS adds extra hostnames; TRUSTED_IP_HEADER (e.g. CF-Connecting-IP)
names the header your proxy sets with the visitor's IP.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import mimetypes
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlsplit

import bcrypt
from aiohttp import ClientError, ClientSession, ClientTimeout, web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings_file  # noqa: E402

UI_PORT = int(os.environ.get("UI_PORT", 8090))
EXPLO_PROXY_PORT = int(os.environ.get("EXPLO_PROXY_PORT", UI_PORT + 1))
EXPLO_UPSTREAM = os.environ.get("EXPLO_UPSTREAM", "http://127.0.0.1:7288")
STATIC = Path(__file__).parent / "static"
mimetypes.add_type("application/manifest+json", ".webmanifest")
UI_DIR = Path(os.environ.get("UI_DATA_DIR", "/config/ui"))
AUTH_FILE = UI_DIR / "auth.json"
TG_INFO = UI_DIR / "telegram.json"
BACKGROUND = UI_DIR / "background"  # user-uploaded wallpaper (any of webp/jpeg/png)
TG_DIR = Path(os.environ.get("DATA_DIR", "/config/telegram"))
TG_SESSION = TG_DIR / "bot_session"
DOWNLOADS = Path(os.environ.get("WATCH_DIR", "/downloads"))
MUSIC = Path(os.environ.get("LIBRARY_DIR", "/music"))  # MUSIC_DIR is the bot's landing dir
LOG_DIR = Path(os.environ.get("LOG_DIR", "/config/logs"))
BEETS_DB = Path(os.environ.get("BEETSDIR", "/config/beets")) / "library.db"
SUPERVISOR_CONF = "/etc/supervisor/conf.d/supervisord.conf"
SESSION_DAYS = 7
COOKIE = "maf_session"
EXPLO_COOKIE = "ocha_explo"  # its own name: on one host, cookies are shared across ports
DEFAULT_PASSWORD = "Melovepassword"

LOGS = {
    "telegram": TG_DIR / "telegram_userbot.log",
    "sorter": LOG_DIR / "watcher-daemon.log",
    "explo": LOG_DIR / "explo.log",
    "runs": LOG_DIR / "explo-runs.log",
    "scheduler": LOG_DIR / "scheduler.log",
    "panel": LOG_DIR / "control_panel.log",
}
SERVICES = ["telegram_bot", "music_sorter", "explo", "scheduler"]

# Sections in the order people fill them in. key, label, type, extra:
# select -> [(value, label)], text -> placeholder. WHEN hides a field unless
# another setting has one of the given values.
SETTINGS = [
    ("Library", [
        ("EXPLO_SYSTEM", "Media server", "select", [("jellyfin", "Jellyfin"), ("subsonic", "Navidrome / Subsonic")]),
        ("SYSTEM_URL", "Server URL", "text", "http://192.168.1.10:8096"),
        ("LIBRARY_NAME", "Music library", "text", "Music"),
        ("API_KEY", "API key", "secret", None),
        ("SYSTEM_USERNAME", "Username", "text", ""),
        ("SYSTEM_PASSWORD", "Password", "secret", None),
    ]),
    ("Discovery", [
        ("LISTENBRAINZ_USER", "ListenBrainz username", "text", ""),
        ("LISTENBRAINZ_USER_TOKEN", "ListenBrainz token", "secret", None),
        ("SPOTIFY_CLIENT_ID", "Spotify client ID", "text", ""),
        ("SPOTIFY_CLIENT_SECRET", "Spotify client secret", "secret", None),
    ]),
    ("Folders", [
        ("DOWNLOADS_PATH", "Downloads land in", "info", "./downloads"),
        ("MUSIC_PATH", "Sorted music goes to", "info", "./music"),
    ]),
    ("Downloads", [
        ("DOWNLOAD_SERVICES", "Source", "select", [("youtube", "YouTube"), ("slskd", "Soulseek (slskd)"),
                                                   ("telegram", "Telegram music bot")]),
        ("SLSKD_URL", "slskd URL", "text", "http://slskd:5030"),
        ("SLSKD_API_KEY", "slskd API key", "secret", None),
        ("TG_API_ID", "Telegram API ID", "text", "From my.telegram.org/apps"),
        ("TG_API_HASH", "Telegram API hash", "secret", None),
        ("MUSIC_BOT_ID", "Music bot", "text", "@username or numeric ID"),
    ]),
]
WHEN = {
    "API_KEY": ("EXPLO_SYSTEM", ["jellyfin"]),
    "SYSTEM_USERNAME": ("EXPLO_SYSTEM", ["subsonic"]),
    "SYSTEM_PASSWORD": ("EXPLO_SYSTEM", ["subsonic"]),
    "SLSKD_URL": ("DOWNLOAD_SERVICES", ["slskd"]),
    "SLSKD_API_KEY": ("DOWNLOAD_SERVICES", ["slskd"]),
    "TG_API_ID": ("DOWNLOAD_SERVICES", ["telegram"]),
    "TG_API_HASH": ("DOWNLOAD_SERVICES", ["telegram"]),
    "MUSIC_BOT_ID": ("DOWNLOAD_SERVICES", ["telegram"]),
}
SETTING_TYPES = {k: t for _, fields in SETTINGS for k, _, t, _ in fields}
SELECT_VALUES = {k: [v for v, _ in e] for _, fields in SETTINGS for k, _, t, e in fields if t == "select"}
TELEGRAM_KEYS = {"TG_API_ID", "TG_API_HASH", "MUSIC_BOT_ID"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("panel")

# ---------------------------------------------------------------- auth

# token -> {"exp": absolute expiry, "seen": last use, "parent": panel token or None}
sessions: dict[str, dict] = {}
failures: dict[str, deque] = {}
explo_tickets: dict[str, tuple] = {}  # ticket -> (expiry, panel token): signs the Explo host in once
IDLE_SECONDS = 24 * 3600
BCRYPT_ROUNDS = 12


def _pw(password):
    return password.encode()[:72]  # bcrypt only reads 72 bytes; be explicit about it


def env_password():
    return os.environ.get("UI_PASSWORD") or DEFAULT_PASSWORD


def must_change():
    """Still on the published default password: everything waits for a new one."""
    return not AUTH_FILE.exists() and env_password() == DEFAULT_PASSWORD


def _check_password_sync(password):
    if AUTH_FILE.exists():
        data = json.loads(AUTH_FILE.read_text())
        if "bcrypt" in data:
            return bcrypt.checkpw(_pw(password), data["bcrypt"].encode())
        # older installs stored PBKDF2: verify once, then upgrade to bcrypt
        salt = base64.b64decode(data["salt"])
        ok = hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000),
                                 base64.b64decode(data["hash"]))
        if ok:
            _store_password_sync(password, clear_sessions=False)
        return ok
    return hmac.compare_digest(password.encode(), env_password().encode())


def _store_password_sync(password, clear_sessions=True):
    UI_DIR.mkdir(parents=True, exist_ok=True)
    hashed = bcrypt.hashpw(_pw(password), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"bcrypt": hashed}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)
    if clear_sessions:
        sessions.clear()


async def check_password(password):
    return await asyncio.to_thread(_check_password_sync, password)  # bcrypt is slow on purpose


async def store_password(password):
    await asyncio.to_thread(_store_password_sync, password)


def _via_proxy(request):
    """Forwarded headers count only when a private address (Caddy, cloudflared,
    Docker) hands us the request; a direct visitor can't set them."""
    try:
        return ipaddress.ip_address(request.remote or "").is_private
    except ValueError:
        return False


def client_ip(request):
    """Visitor IP for rate limiting. Uses the address the nearest proxy saw
    (last X-Forwarded-For hop), never one the visitor typed in themselves."""
    remote = request.remote or ""
    if _via_proxy(request):
        header = os.environ.get("TRUSTED_IP_HEADER")
        fwd = request.headers.get(header, "") if header else request.headers.get("X-Forwarded-For", "").split(",")[-1]
        try:
            return str(ipaddress.ip_address(fwd.strip()))
        except ValueError:
            pass
    return remote


def _valid(token, now):
    s = sessions.get(token)
    if not s or s["exp"] <= now or now - s["seen"] >= IDLE_SECONDS:
        sessions.pop(token, None)
        return None
    return s


def authed(request, cookie=COOKIE):
    """Panel sessions use COOKIE; Explo sessions use EXPLO_COOKIE and live only
    as long as the panel session that made them."""
    token = request.cookies.get(cookie, "")
    now = time.time()
    s = _valid(token, now)
    explo = cookie == EXPLO_COOKIE
    if s and bool(s["parent"]) == explo and (not explo or _valid(s["parent"], now)):
        s["seen"] = now
        return True
    return False


def end_session(token):
    sessions.pop(token, None)
    for t in [t for t, s in sessions.items() if s["parent"] == token]:
        sessions.pop(t, None)


def _https(request):
    public = os.environ.get("UI_PUBLIC_URL", "")
    if public:
        return public.startswith("https://")
    return request.scheme == "https" or (_via_proxy(request) and request.headers.get("X-Forwarded-Proto") == "https")


def start_session(request, response, parent=None):
    token = secrets.token_urlsafe(32)
    now = time.time()
    exp = min(now + SESSION_DAYS * 86400, sessions[parent]["exp"] if parent else float("inf"))
    sessions[token] = {"exp": exp, "seen": now, "parent": parent}
    response.set_cookie(EXPLO_COOKIE if parent else COOKIE, token, max_age=int(exp - now), httponly=True,
                        samesite="Lax", secure=_https(request), path="/")


def rate_limited(request):
    """Counts the attempt up front, so parallel guesses can't all slip past the check."""
    now = time.time()
    if len(failures) > 1024:
        for k in [k for k, q in failures.items() if not q or q[-1] < now - 600]:
            failures.pop(k, None)
    q = failures.setdefault(client_ip(request), deque())
    while q and q[0] < now - 600:
        q.popleft()
    if len(q) >= 5:
        return True, q, None
    q.append(now)
    return False, q, now


def _scheme(request):
    return "https" if _https(request) else "http"


LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain")


def host_allowed(request):
    """Refuses unknown hostnames so a DNS-rebinding page can't talk to the panel."""
    host = request.host.rsplit(":", 1)[0].strip("[]").lower() if request.host else ""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES):
        return True
    allowed = {urlsplit(os.environ.get(k, "")).hostname for k in ("UI_PUBLIC_URL", "EXPLO_PUBLIC_URL")}
    allowed |= {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",")}
    return host in allowed


@web.middleware
async def check_host(request, handler):
    if not host_allowed(request):
        return web.Response(status=421, text="Unknown host. Add it to ALLOWED_HOSTS or UI_PUBLIC_URL.")
    return await handler(request)


def panel_origin(request):
    url = os.environ.get("UI_PUBLIC_URL", "").rstrip("/")
    return url or f"{_scheme(request)}://{request.host.rsplit(':', 1)[0]}:{UI_PORT}"


def explo_origin(request):
    url = os.environ.get("EXPLO_PUBLIC_URL", "").rstrip("/")
    return url or f"{_scheme(request)}://{request.host.rsplit(':', 1)[0]}:{EXPLO_PROXY_PORT}"


OPEN = {"/api/session", "/api/login"}
WHILE_DEFAULT = OPEN | {"/api/logout", "/api/password"}  # all that works until the default is replaced


@web.middleware
async def guard(request, handler):
    path = request.path
    if path.startswith("/api/"):
        if path not in OPEN and not authed(request):
            return web.json_response({"error": "Sign in first"}, status=401)
        if path not in WHILE_DEFAULT and must_change():
            return web.json_response({"error": "Choose a new password first", "must_change": True}, status=403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # state changes must come from this panel's own pages
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return web.json_response({"error": "Cross-site request refused"}, status=403)
            # JSON-only writes: a cross-site form can't send this content type
            upload = path == "/api/background" and request.content_type.startswith("image/")
            if request.content_type != "application/json" and not upload:
                return web.json_response({"error": "JSON body required"}, status=415)
    resp = await handler(request)
    explo = explo_origin(request)
    resp.headers.update({
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
            f"frame-src {explo}; form-action 'self' {explo}; frame-ancestors 'none'; "
            "object-src 'none'; base-uri 'self'"),
    })
    return resp


async def drop_server_header(request, response):
    response.headers.pop("Server", None)
    if request.path.startswith("/static/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"  # revalidate, so an update shows up at once


async def body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def api_session(request):
    s = settings_file.parse()
    authenticated = authed(request)
    return web.json_response({"authenticated": authenticated, "must_change": authenticated and must_change(),
                              "downloader": s.get("DOWNLOAD_SERVICES", "youtube") if authenticated else "",
                              "background": int(BACKGROUND.stat().st_mtime) if BACKGROUND.exists() else 0})


async def api_login(request):
    limited, q, stamp = rate_limited(request)
    if limited:
        return web.json_response({"error": "Too many attempts. Try again in 10 minutes."}, status=429)
    if not await check_password((await body(request)).get("password", "")):
        await asyncio.sleep(1)
        return web.json_response({"error": "Wrong password"}, status=401)
    q.remove(stamp)  # only failures count
    end_session(request.cookies.get(COOKIE, ""))  # rotate on login
    resp = web.json_response({"ok": True})
    start_session(request, resp)
    return resp


async def api_logout(request):
    end_session(request.cookies.get(COOKIE, ""))  # takes its Explo sessions with it
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_password(request):
    data = await body(request)
    new = data.get("new", "")
    if not must_change():  # on the default, signing in already proved it
        limited, q, stamp = rate_limited(request)
        if limited:
            return web.json_response({"error": "Too many attempts. Try again in 10 minutes."}, status=429)
        if not await check_password(data.get("current", "")):
            return web.json_response({"error": "Current password is wrong"}, status=403)
        q.remove(stamp)
    if len(new) < 8:
        return web.json_response({"error": "Use at least 8 characters"}, status=400)
    if new == DEFAULT_PASSWORD:
        return web.json_response({"error": "Pick something other than the default"}, status=400)
    await store_password(new)
    resp = web.json_response({"ok": True})
    start_session(request, resp)
    return resp


async def api_explo_ticket(request):
    """One-time, 60-second ticket the panel POSTs to the Explo host, so Explo
    (a different hostname or port) gets its own session cookie."""
    now = time.time()
    for t, (exp, _) in list(explo_tickets.items()):
        if exp < now:
            explo_tickets.pop(t, None)
    ticket = secrets.token_urlsafe(32)
    explo_tickets[ticket] = (now + 60, request.cookies.get(COOKIE, ""))
    return web.json_response({"ticket": ticket, "action": f"{explo_origin(request)}/__ocha_auth"})

# ---------------------------------------------------------------- services


async def run(*cmd, timeout=60):
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode, out.decode(errors="replace")


async def supervisor(action, name=None):
    cmd = ["supervisorctl", "-c", SUPERVISOR_CONF, action] + ([name] if name else [])
    return await run(*cmd)


async def service_states():
    _, out = await supervisor("status")
    states = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0] in SERVICES:
            states[parts[0]] = {"state": parts[1], "detail": parts[2] if len(parts) > 2 else ""}
    return states


async def api_service(request):
    name, action = request.match_info["name"], request.match_info["action"]
    if name not in SERVICES or action not in ("start", "stop", "restart"):
        raise web.HTTPNotFound()
    rc, out = await supervisor(action, name)
    return web.json_response({"ok": rc == 0, "output": out.strip()})

# ---------------------------------------------------------------- settings


async def api_settings_get(request):
    values = settings_file.parse()
    groups = []
    for title, fields in SETTINGS:
        out = []
        for key, label, kind, extra in fields:
            f = {"key": key, "label": label, "type": kind}
            if key in WHEN:
                f["when"] = {"key": WHEN[key][0], "values": WHEN[key][1]}
            if kind == "select":
                f["options"] = [{"value": v, "label": lbl} for v, lbl in extra]
                f["value"] = values.get(key) or extra[0][0]
            elif kind == "secret":
                f["set"] = bool(values.get(key))
            elif kind == "info":  # host paths are Docker mounts: shown here, changed in .env
                f["value"] = os.environ.get(key) or extra
            else:
                f["value"] = values.get(key, "")
                f["placeholder"] = extra or ""
            out.append(f)
        groups.append({"title": title, "fields": out})
    return web.json_response({"groups": groups})


async def api_settings_post(request):
    incoming = (await body(request)).get("values", {})
    changes = {}
    for key, val in incoming.items():
        kind = SETTING_TYPES.get(key)
        if kind in (None, "info") or not isinstance(val, str):
            continue
        val = val.strip()
        if kind == "secret" and val == "":
            continue  # blank = keep the stored secret
        if kind == "select":
            if val not in SELECT_VALUES[key]:
                return web.json_response({"error": f"Unknown value for {key}"}, status=400)
        changes[key] = val
    if not changes:
        return web.json_response({"ok": True, "restarted": []})
    settings_file.update(changes)
    restarted = ["explo"]
    if TELEGRAM_KEYS & changes.keys():
        restarted.append("telegram_bot")
    for name in restarted:
        await supervisor("restart", name)
    return web.json_response({"ok": True, "restarted": restarted})

# ---------------------------------------------------------------- telegram login

tg_login = {}  # in-flight login: client, phone, hash


def _tg_client():
    from telethon import TelegramClient
    s = settings_file.parse()
    api_id, api_hash = s.get("TG_API_ID", ""), s.get("TG_API_HASH", "")
    if not api_id.isdigit() or not api_hash:
        raise web.HTTPBadRequest(text=json.dumps({"error": "Save the Telegram API ID and hash first"}),
                                 content_type="application/json")
    TG_DIR.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(TG_SESSION), int(api_id), api_hash)


async def _tg_finish(client, kind):
    me = await client.get_me()
    name = " ".join(filter(None, [me.first_name, me.last_name])) or me.username or str(me.id)
    UI_DIR.mkdir(parents=True, exist_ok=True)
    TG_INFO.write_text(json.dumps({"name": name, "kind": kind, "id": me.id}))
    await client.disconnect()
    # the session file is the Telegram login itself: owner-only
    for p in (Path(str(TG_SESSION) + ".session"), TG_INFO):
        os.chmod(p, 0o600)
    tg_login.clear()
    await supervisor("start", "telegram_bot")
    return web.json_response({"ok": True, "name": name})


async def api_tg_status(request):
    s = settings_file.parse()
    info = json.loads(TG_INFO.read_text()) if TG_INFO.exists() else None
    return web.json_response({
        "configured": bool(s.get("TG_API_ID") and s.get("TG_API_HASH")),
        "authorized": bool(info) and Path(str(TG_SESSION) + ".session").exists(),
        "account": info, "pending": bool(tg_login)})


async def api_tg_code(request):
    phone = re.sub(r"[^\d+]", "", (await body(request)).get("phone", ""))
    if len(phone) < 8:
        return web.json_response({"error": "Enter the number with country code"}, status=400)
    await supervisor("stop", "telegram_bot")  # the session file can't be shared while it runs
    if tg_login.get("client"):
        await tg_login["client"].disconnect()
    client = _tg_client()
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        return web.json_response({"error": str(e)}, status=400)
    tg_login.update(client=client, phone=phone, hash=sent.phone_code_hash)
    settings_file.update({"TG_PHONE": phone})
    return web.json_response({"ok": True})


async def api_tg_verify(request):
    from telethon.errors import SessionPasswordNeededError
    data = await body(request)
    client = tg_login.get("client")
    if not client:
        return web.json_response({"error": "Request a code first"}, status=400)
    try:
        if data.get("password"):
            await client.sign_in(password=data["password"])
        else:
            await client.sign_in(tg_login["phone"], str(data.get("code", "")).strip(),
                                 phone_code_hash=tg_login["hash"])
    except SessionPasswordNeededError:
        return web.json_response({"need_password": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    return await _tg_finish(client, "account")


async def api_tg_logout(request):
    await supervisor("stop", "telegram_bot")
    for p in (Path(str(TG_SESSION) + ".session"), TG_INFO):
        p.unlink(missing_ok=True)
    return web.json_response({"ok": True})

# ---------------------------------------------------------------- status & logs


def staging():
    items = []
    if not DOWNLOADS.exists():
        return items
    paths = list(DOWNLOADS.iterdir())
    tmp = DOWNLOADS / ".ytdlp-tmp"
    if tmp.is_dir():
        paths += list(tmp.iterdir())
    for p in paths:
        if p.name in (".duplicates", ".stale", ".ytdlp-tmp"):
            continue
        try:
            st = p.stat()
            size = st.st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            continue
        partial = p.name.startswith(".") or p.suffix in (".partial", ".part", ".ytdl") or p.parent == tmp
        name = re.sub(r"(\.(webm|m4a|mp4|opus))?\.(partial|part|ytdl)$", "", p.name.lstrip("."))
        items.append({"name": name, "size": size, "mtime": st.st_mtime, "partial": partial})
    return sorted(items, key=lambda i: -i["mtime"])[:50]


def explo_jobs():
    db = TG_DIR / "downloads.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        rows = conn.execute("SELECT title, artist, state, created_at FROM explo_jobs "
                            "ORDER BY created_at DESC LIMIT 25").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    return [{"title": t, "artist": a, "state": s, "at": c} for t, a, s, c in rows]


def library_count():
    try:
        conn = sqlite3.connect(f"file:{BEETS_DB}?mode=ro", uri=True, timeout=2)
        n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        conn.close()
        return n
    except sqlite3.Error:
        return None


async def api_status(request):
    disk = shutil.disk_usage(MUSIC if MUSIC.exists() else "/")
    return web.json_response({
        "services": await service_states(),
        "downloader": settings_file.parse().get("DOWNLOAD_SERVICES", "youtube"),
        "staging": staging(),
        "jobs": explo_jobs(),
        "requests": list(requests_log),
        "library": library_count(),
        "disk": {"free": disk.free, "total": disk.total},
    })


def tail(path, lines=400):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256 * 1024))
            data = f.read().decode(errors="replace")
    except FileNotFoundError:
        return ""
    data = re.sub(r"\x1b\[[0-9;]*m", "", data)  # strip colour codes
    return "\n".join(data.splitlines()[-lines:])


async def api_logs(request):
    name = request.query.get("name", "")
    if name not in LOGS:
        raise web.HTTPNotFound()
    return web.json_response({"name": name, "text": tail(LOGS[name])})

# ---------------------------------------------------------------- song requests

requests_log: deque = deque(maxlen=30)
SPOTIFY_TRACK = re.compile(r"https?://open\.spotify\.com/(?:intl-\w+/)?track/(\w+)")


async def spotify(session, s, query=None, track_id=None):
    """Returns (spotify_url, 'Artist - Title') or None."""
    cid, secret = s.get("SPOTIFY_CLIENT_ID"), s.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    async with session.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                            headers={"Authorization": f"Basic {auth}"}) as r:
        token = (await r.json()).get("access_token")
    h = {"Authorization": f"Bearer {token}"}
    if track_id:
        async with session.get(f"https://api.spotify.com/v1/tracks/{track_id}", headers=h) as r:
            t = await r.json()
    else:
        async with session.get("https://api.spotify.com/v1/search", headers=h,
                               params={"q": query, "type": "track", "limit": "1"}) as r:
            items = (await r.json()).get("tracks", {}).get("items", [])
        if not items:
            return None
        t = items[0]
    return t["external_urls"]["spotify"], f"{t['artists'][0]['name']} - {t['name']}"


async def via_youtube(job, s, session):
    query = job["query"]
    m = SPOTIFY_TRACK.search(query)
    if m:
        found = await spotify(session, s, track_id=m.group(1))
        if not found:
            raise RuntimeError("Spotify links need the Spotify client ID and secret")
        query = found[1]
        job["detail"] = query
    target = query if query.startswith("http") else f"ytsearch1:{query} audio"
    tags = []
    artist, sep, title = query.partition(" - ")
    if sep and not query.startswith("http"):
        # Tag with what the user asked for, not the video's channel/title
        # ("Bacardi Sessions: Ritviz - Liggi [Official Audio]"), so Beets files it right.
        # Copy any field in, then overwrite it: the reliable way to set a constant tag.
        tags = []
        for field, value in (("artist", artist.strip()), ("track", title.strip())):
            tags += ["--parse-metadata", f"title:%({field})s",
                     "--replace-in-metadata", field, "^.*$", value.replace("\\", "\\\\")]
    cmd = [os.environ.get("YTDLP_PATH", "yt-dlp"), "--no-playlist", "-x", *tags,
           "--audio-format", os.environ.get("TRACK_EXTENSION", "m4a"), "--embed-metadata",
           "-P", f"home:{DOWNLOADS}", "-P", f"temp:{DOWNLOADS / '.ytdlp-tmp'}",
           "-o", "%(artist,creator,uploader)s - %(track,title)s.%(ext)s", target]
    cookies = os.environ.get("COOKIES_PATH", "")
    if cookies and os.path.exists(cookies):
        cmd[1:1] = ["--cookies", cookies]
    rc, out = await run(*cmd, timeout=900)
    errors = [l.removeprefix("ERROR: ").strip() for l in out.splitlines() if l.startswith("ERROR:")]
    if "confirm you" in out and "not a bot" in out:
        raise RuntimeError("YouTube blocked this server (common on VPS/datacenter IPs). "
                           "Add a cookies.txt to the config folder, or use another source.")
    if rc != 0 or errors:
        raise RuntimeError(errors[-1] if errors else f"yt-dlp exit {rc}")
    return "Downloaded"


async def via_telegram(job, s, session):
    m = SPOTIFY_TRACK.search(job["query"])
    found = await spotify(session, s, track_id=m.group(1)) if m else await spotify(session, s, query=job["query"])
    if not found:
        raise RuntimeError("No Spotify match. Telegram requests need the Spotify client ID and secret.")
    url, label = found
    job["detail"] = label
    api = os.environ.get("TELEGRAM_BOT_API_URL", "http://127.0.0.1:4352")
    h = {"X-API-Key": s.get("EXPLO_API_KEY", "")}
    artist, _, title = label.partition(" - ")
    async with session.post(f"{api}/explo/request", headers=h,
                            json={"spotify_url": url, "title": title, "artist": artist}) as r:
        if r.status != 200:
            raise RuntimeError(f"Telegram bot answered {r.status}. Is it signed in?")
        job_id = (await r.json())["job_id"]
    for _ in range(180):
        await asyncio.sleep(5)
        async with session.get(f"{api}/explo/status/{job_id}", headers=h) as r:
            state = (await r.json()).get("state")
        if state == "done":
            return "Downloaded"
        if state in ("error", "not_found"):
            raise RuntimeError("The music bot didn't send the file")
    raise RuntimeError("Timed out waiting for the music bot")


async def via_slskd(job, s, session):
    base, key = s.get("SLSKD_URL", "").rstrip("/"), s.get("SLSKD_API_KEY", "")
    if not base:
        raise RuntimeError("Set the slskd URL in Settings")
    h = {"X-API-Key": key}
    query = job["query"]
    m = SPOTIFY_TRACK.search(query)
    if m:
        found = await spotify(session, s, track_id=m.group(1))
        query = found[1] if found else query
    search_text = re.sub(r"\s+-\s+", " ", query)
    async with session.post(f"{base}/api/v0/searches", headers=h, json={"searchText": search_text}) as r:
        sid = (await r.json())["id"]
    for _ in range(20):
        await asyncio.sleep(3)
        async with session.get(f"{base}/api/v0/searches/{sid}", headers=h) as r:
            if "Completed" in (await r.json()).get("state", ""):
                break
    async with session.get(f"{base}/api/v0/searches/{sid}/responses", headers=h) as r:
        responses = await r.json()
    best = None
    for resp in responses:
        for f in resp.get("files", []):
            ext = f["filename"].rsplit(".", 1)[-1].lower()
            if ext not in ("flac", "mp3"):
                continue
            score = (ext == "flac", resp.get("hasFreeUploadSlot", False), resp.get("uploadSpeed", 0))
            if best is None or score > best[0]:
                best = (score, resp["username"], f)
    if not best:
        raise RuntimeError("No FLAC or MP3 found on Soulseek")
    _, user, f = best
    async with session.post(f"{base}/api/v0/transfers/downloads/{quote(user, safe='')}", headers=h,
                            json=[{"filename": f["filename"], "size": f["size"]}]) as r:
        if r.status >= 300:
            raise RuntimeError(f"slskd refused the download ({r.status})")
    job["detail"] = f["filename"].replace("\\", "/").rsplit("/", 1)[-1]
    return "Queued in slskd"


VIA = {"youtube": via_youtube, "telegram": via_telegram, "slskd": via_slskd}


async def process_request(job):
    s = settings_file.parse()
    try:
        async with ClientSession(timeout=ClientTimeout(total=60)) as session:
            job["result"] = await VIA[job["via"]](job, s, session)
        job["state"] = "done"
    except Exception as e:
        log.warning("request %s failed: %s", job["query"], e)
        job["state"], job["result"] = "error", str(e)


async def api_request(request):
    query = (await body(request)).get("query", "").strip()
    if not query or len(query) > 300:
        return web.json_response({"error": "Enter a song, or paste a link"}, status=400)
    via = settings_file.parse().get("DOWNLOAD_SERVICES", "youtube").split(",")[0].strip() or "youtube"
    if via not in VIA:
        return web.json_response({"error": f"Unknown downloader {via}"}, status=400)
    job = {"id": uuid.uuid4().hex[:8], "query": query, "via": via, "state": "working",
           "detail": "", "result": "", "at": time.time()}
    requests_log.appendleft(job)
    asyncio.create_task(process_request(job))
    return web.json_response(job)

# ---------------------------------------------------------------- explo proxy

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-authorization",
       "te", "trailer", "host", "content-length"}


def _explo_page(status, text, request):
    return web.Response(status=status, content_type="text/html",
                        text=f'<p style="font-family:sans-serif">{html.escape(text)} '
                             f'<a href="{html.escape(panel_origin(request))}/" target="_top">'
                             'Open the control panel</a>.</p>',
                        headers={"Content-Security-Policy": f"default-src 'none'; frame-ancestors {panel_origin(request)}"})


async def explo_auth(request):
    """POST /__ocha_auth with a one-time ticket from the panel -> Explo session on this host."""
    form = await request.post()
    entry = explo_tickets.pop(str(form.get("ticket", "")), None)
    if not entry or entry[0] < time.time() or not _valid(entry[1], time.time()) or must_change():
        return _explo_page(401, "This link expired.", request)
    resp = web.Response(status=303, headers={"Location": "/"})
    start_session(request, resp, parent=entry[1])  # ends when the panel session ends
    return resp


def _upstream_cookies(request):
    """Explo gets its own cookies, never OCHA's session tokens."""
    return "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k not in (COOKIE, EXPLO_COOKIE))


async def explo_proxy(request):
    if request.path == "/__ocha_auth" and request.method == "POST":
        return await explo_auth(request)
    if not authed(request, EXPLO_COOKIE):
        return _explo_page(401, "Sign in first.", request)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | {"cookie"}}
    if cookies := _upstream_cookies(request):
        headers["Cookie"] = cookies
    data = await request.read()
    session = request.app["client"]
    try:
        up = await session.request(request.method, EXPLO_UPSTREAM + request.rel_url.path_qs,
                                   headers=headers, data=data or None, allow_redirects=False)
    except ClientError:
        return _explo_page(502, "Explo isn't running. Restart it from Activity.", request)
    try:
        out = {k: v for k, v in up.headers.items() if k.lower() not in HOP | {"server", "x-frame-options"}}
        out.update({
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            # only the OCHA panel may frame Explo
            "Content-Security-Policy": f"frame-ancestors {panel_origin(request)}",
        })
        resp = web.StreamResponse(status=up.status, headers=out)
        await resp.prepare(request)
        async for chunk in up.content.iter_any():  # streams Explo's live run log too
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        up.release()


async def proxy_client(app):
    app["client"] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10), auto_decompress=False)
    yield
    await app["client"].close()

# ---------------------------------------------------------------- background

IMAGE_TYPES = {b"RIFF": "image/webp", b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png"}


def image_type(data):
    for magic, ctype in IMAGE_TYPES.items():
        if data.startswith(magic) and (magic != b"RIFF" or data[8:12] == b"WEBP"):
            return ctype
    return None


async def background(request):
    """Public on purpose: the sign-in screen shows it too."""
    if not BACKGROUND.exists():
        raise web.HTTPNotFound()
    data = BACKGROUND.read_bytes()
    return web.Response(body=data, content_type=image_type(data) or "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def api_background_set(request):
    data = await request.read()
    if not image_type(data):
        return web.json_response({"error": "Use a JPEG, PNG or WebP image"}, status=400)
    UI_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BACKGROUND.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, BACKGROUND)
    return web.json_response({"background": int(BACKGROUND.stat().st_mtime)})


async def api_background_remove(request):
    BACKGROUND.unlink(missing_ok=True)
    return web.json_response({"background": 0})

# ---------------------------------------------------------------- app


# ?v=<hash of the static files>: a CDN (Cloudflare caches .css/.js for hours) can't serve an old copy after an update
ASSET_VERSION = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(STATIC.rglob("*")) if p.is_file())).hexdigest()[:10]
INDEX_HTML = re.sub(r'((?:href|src)="/static/[^"?]+)"', rf'\1?v={ASSET_VERSION}"', (STATIC / "index.html").read_text())


async def index(request):
    return web.Response(text=INDEX_HTML, content_type="text/html", headers={"Cache-Control": "no-cache"})


def build_app():
    app = web.Application(middlewares=[check_host, guard], client_max_size=8 * 1024 ** 2)
    r = app.router
    r.add_get("/", index)
    r.add_get("/api/session", api_session)
    r.add_post("/api/login", api_login)
    r.add_post("/api/logout", api_logout)
    r.add_post("/api/password", api_password)
    r.add_get("/api/status", api_status)
    r.add_get("/api/logs", api_logs)
    r.add_get("/api/settings", api_settings_get)
    r.add_post("/api/settings", api_settings_post)
    r.add_post("/api/services/{name}/{action}", api_service)
    r.add_get("/api/telegram", api_tg_status)
    r.add_post("/api/telegram/code", api_tg_code)
    r.add_post("/api/telegram/verify", api_tg_verify)
    r.add_post("/api/telegram/logout", api_tg_logout)
    r.add_post("/api/request", api_request)
    r.add_post("/api/explo-ticket", api_explo_ticket)
    r.add_get("/background", background)
    r.add_post("/api/background", api_background_set)
    r.add_post("/api/background/remove", api_background_remove)
    r.add_static("/static/", STATIC)
    app.on_response_prepare.append(drop_server_header)
    return app


def build_proxy():
    app = web.Application(middlewares=[check_host], client_max_size=50 * 1024 ** 2)
    app.cleanup_ctx.append(proxy_client)
    app.router.add_route("*", "/{tail:.*}", explo_proxy)
    app.on_response_prepare.append(drop_server_header)
    return app


async def main():
    runners = []
    for app, port in ((build_app(), UI_PORT), (build_proxy(), EXPLO_PROXY_PORT)):
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        runners.append(runner)
    log.info("control panel on :%s, Explo on :%s%s", UI_PORT, EXPLO_PROXY_PORT,
             " (default password: you'll be asked to change it)" if must_change() else "")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
