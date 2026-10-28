# Security

OCHA is a single-user, self-hosted app. One password protects the control panel and the Explo dashboard behind it. This page covers what that password protects, how, and what to keep in mind when you expose OCHA to the internet.

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's **[private vulnerability reporting](../../security/advisories/new)** on this repository instead. Describe what you found, how to reproduce it, and what an attacker could do with it.

## What's protected, and how

| Area | Protection |
|---|---|
| **First sign-in** | Every install starts with the password `Melovepassword`. Until you replace it, the panel accepts no other action and won't open Explo. The default can't be reused. |
| **Passwords** | Stored as bcrypt (cost 12) in `config/ui/auth.json`, readable by OCHA only. Changing it signs out every other browser. |
| **Sign-in** | 5 failed attempts per IP per 10 minutes, counted before the password is checked, so parallel guesses can't slip through. Forwarded IP headers are trusted only from a private-address proxy. |
| **Sessions** | Random 256-bit tokens in `HttpOnly`, `SameSite=Lax` cookies (`Secure` behind HTTPS). They expire after 7 days, or 24 hours idle. The Explo dashboard gets its own cookie, which never outlives the panel session that created it. Signing out ends both. |
| **Cross-site requests** | Every change must come from the panel's own pages: same-origin `Origin` and a JSON body. Pages can't be framed by other sites (CSP `frame-ancestors`), and scripts load only from OCHA itself. |
| **Hostnames** | Unknown `Host` names are refused, which blocks DNS-rebinding pages from reaching a LAN panel. |
| **Explo** | Listens on `127.0.0.1` inside the container. Only the panel's authenticated proxy reaches it, and OCHA's cookies are stripped before requests are forwarded. |
| **Downloads** | Remote metadata and file names (playlist titles, Soulseek peer paths, Telegram file names) can't write outside the download and playlist folders: `.` and `..` are neutralised and paths are checked before use. Soulseek user names are URL-encoded. |
| **Telegram** | Only files from the configured music bot (or `ADMIN_ID`) are accepted. The bot's local API is bound to `127.0.0.1` and needs a random key generated on first start. |
| **Container** | All services run as `PUID:PGID` with no Linux capabilities, `no-new-privileges`, and a read-only image. The entrypoint uses root only to fix folder ownership, and doesn't follow symlinks while doing so. Secrets files are `chmod 600`. |
| **Optional slskd** | Its web UI is published on `127.0.0.1` only. OCHA talks to it over the Docker network. |

## Audits

The code was reviewed in two source-level security audits (October 2026). Everything they found was fixed:

- Forwarded-IP headers could be spoofed to dodge the login rate limit, and the rate limit could be raced with parallel requests.
- The panel and Explo shared one cookie name on the same host, so signing out could leave the original session alive. Explo sessions could also outlive their parent session.
- No hostname check (DNS rebinding), and a password-less first-run setup screen that could be claimed. Both are gone: OCHA now ships with a default password that must be changed.
- `..` in remote track metadata, MPD playlist names or Soulseek peer paths could reach files outside the download folders.
- Peer-controlled Soulseek user names went into API URLs unencoded.
- The optional slskd port was published on all interfaces.
- Plus hardening: settings values with Unicode line separators, Beets query escaping, constant-time key comparison, playlist-ID validation, and stale CSS behind CDNs.

## Your part

- **Change the default password** on first sign-in. OCHA makes you.
- **Use HTTPS** if OCHA is reachable from the internet ([how](docs/connect-services.md#running-behind-a-domain)). Plain HTTP sends your session cookie in the clear.
- **Don't publish ports you don't need.** Behind a reverse proxy or tunnel, OCHA's own ports can stay private.
- **Treat `config/` as secret.** It holds your settings (API keys), the password hash, the Telegram session (equivalent to being signed in to that account) and YouTube cookies. Keep backups of it encrypted.
- **Keep OCHA updated**: `git pull && docker compose up -d --build`.
