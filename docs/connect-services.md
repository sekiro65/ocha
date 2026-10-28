# Connecting other services

Where to find every key, URL and account OCHA asks for, and how to put OCHA behind a domain. Each section says which panel field the value goes in.

- [Jellyfin](#jellyfin)
- [Navidrome / Subsonic](#navidrome--subsonic)
- [ListenBrainz](#listenbrainz)
- [Spotify](#spotify)
- [Soulseek (slskd)](#soulseek-slskd)
- [YouTube cookies](#youtube-cookies-only-if-youtube-blocks-you)
- [Telegram music bot](#telegram-music-bot)
- [Running behind a domain](#running-behind-a-domain)

---

## Jellyfin

Panel: **Settings → Library**, with *Media server* = Jellyfin.

1. Install Jellyfin if you haven't: [jellyfin.org/docs/general/installation](https://jellyfin.org/docs/general/installation/).
2. Make sure your music library points at the same folder as OCHA's `MUSIC_PATH`. In Jellyfin: **Dashboard → Libraries → Music → Folders**.
3. Turn on **Dashboard → Libraries → Music → "Prefer non-standard artists tag"**, so collaborations link to every artist (see the README).
4. Create an API key: **Dashboard → API Keys → +**, and name it `OCHA`. In newer versions this is under *Administration → Dashboard → Advanced → API Keys*.
5. In OCHA, set:
   - **Server URL**: `http://<jellyfin-lan-ip>:8096`. Use the LAN IP, not `localhost`: inside the OCHA container, `localhost` means the container itself.
   - **Music library**: the library name exactly as Jellyfin shows it (often `Music`).
   - **API key**: the key from step 4.

Jellyfin docs: [API keys](https://jellyfin.org/docs/general/server/settings/#api-keys) · [music libraries](https://jellyfin.org/docs/general/server/media/music/)

## Navidrome / Subsonic

Panel: **Settings → Library**, with *Media server* = Navidrome / Subsonic.

1. Install Navidrome: [navidrome.org/docs/installation](https://www.navidrome.org/docs/installation/). Point its `MusicFolder` at OCHA's `MUSIC_PATH`.
2. Create a user OCHA can use (an admin account works). Playlists will belong to this user.
3. In OCHA, set **Server URL** (`http://<navidrome-lan-ip>:4533`), **Username** and **Password**.

Any server that speaks the Subsonic API should work the same way.

## ListenBrainz

Panel: **Settings → Discovery**.

ListenBrainz builds personal playlists ("Weekly Exploration", "Weekly Jams", "Daily Jams") from what you listen to.

1. Create a free account at [listenbrainz.org](https://listenbrainz.org).
2. Send it your listening history so it has something to recommend from. In Jellyfin, install the [ListenBrainz plugin](https://github.com/lyarenei/jellyfin-plugin-listenbrainz). Navidrome [has it built in](https://www.navidrome.org/docs/usage/scrobbling/#listenbrainz). You can also [import from Spotify or Last.fm](https://listenbrainz.org/settings/import/).
3. Copy your user token from [listenbrainz.org/settings](https://listenbrainz.org/settings/) (*User token*).
4. In OCHA, set **ListenBrainz username** and **ListenBrainz token**.

New accounts get their first exploration playlist after about a week of listens.

## Spotify

Panel: **Settings → Discovery**. You need this for Spotify playlists in Explo, Spotify links in requests, and the Telegram downloader (which looks tracks up on Spotify).

1. Sign in at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) with any Spotify account. Free accounts work.
2. **Create app**:
   - *App name*: `OCHA`; *description*: anything.
   - *Redirect URI*: `http://127.0.0.1:8888/callback`. OCHA doesn't use it, but Spotify requires one.
   - *Which API*: tick **Web API**, accept the terms and save.
3. Open the app → **Settings** and copy the **Client ID**. Click *View client secret* and copy the **Client secret**.
4. In OCHA, set **Spotify client ID** and **Spotify client secret**.

OCHA only reads public catalogue data (track names, ISRCs, public playlists). It never signs in as you.

## Soulseek (slskd)

Panel: **Settings → Downloads**, with *Source* = Soulseek (slskd).

[slskd](https://github.com/slskd/slskd) is a Soulseek client with an API. OCHA's compose file includes it as an optional service.

1. Create a Soulseek account. slskd registers one the first time it signs in with a new username.
2. Add the account to `.env`:
   ```env
   SLSK_USERNAME=your-soulseek-name
   SLSK_PASSWORD=your-soulseek-password
   ```
3. Start slskd with OCHA:
   ```bash
   docker compose --profile slskd up -d
   ```
4. Give OCHA an API key. Edit `slskd/slskd.yml` (created on first start) and add:
   ```yaml
   web:
     authentication:
       username: pick-a-username      # slskd's own web login
       password: pick-a-long-password
       api_keys:
         ocha:
           key: paste-a-random-string-of-32-characters
           role: readwrite
   ```
   Then run `docker compose restart slskd`. Generate a random key with `openssl rand -hex 16`.
5. In OCHA, set **slskd URL** = `http://slskd:5030` and **slskd API key** = the key from step 4.

slskd's web UI only listens on the host itself (`http://127.0.0.1:5030`). To use it from another computer, use an SSH tunnel: `ssh -L 5030:127.0.0.1:5030 you@server`.

Also share some files back. Soulseek is a sharing network, and many users won't upload to people who share nothing. See the [slskd configuration docs](https://github.com/slskd/slskd/blob/master/docs/config.md).

## YouTube cookies (only if YouTube blocks you)

At home, YouTube downloads just work. From data-centre IPs (most VPS providers), YouTube often answers *"Sign in to confirm you're not a bot"*, and the request fails with that message. The fix is to hand yt-dlp the cookies of a signed-in browser:

1. Use a **spare Google account**, not your main one. YouTube may flag accounts used this way.
2. In a private browser window, sign in to YouTube with it.
3. Export cookies in Netscape format with an extension such as *Get cookies.txt LOCALLY* ([Chrome](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) / [Firefox](https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/)), for `youtube.com`.
4. Save the file as `config/cookies.txt` (inside `CONFIG_PATH`) and close the private window without signing out.

OCHA uses the file automatically and keeps it readable by its own user only. More background: [yt-dlp FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp).

## Telegram music bot

This one has its own guide: **[Telegram downloader setup](telegram-userbot.md)**. It covers getting an API ID and hash from my.telegram.org, picking a bot, signing in from the panel, and the built-in rate limits.

---

## Running behind a domain

Use this to open OCHA as `https://ocha.example.com` instead of `http://192.168.1.20:8090`, for example to use it on your phone away from home.

OCHA needs **two hostnames**, one for the panel (port 8090) and one for the Explo dashboard (port 8091). Explo's app uses absolute paths, so it can't live under a sub-path. For example:

- `ocha.example.com` → `http://<ocha-host>:8090`
- `ocha-explo.example.com` → `http://<ocha-host>:8091`

Then tell OCHA about them in `.env` and run `docker compose up -d`:

```env
UI_PUBLIC_URL=https://ocha.example.com
EXPLO_PUBLIC_URL=https://ocha-explo.example.com
```

OCHA answers only on hostnames it knows: these two, any IP address, `localhost`, and `.local`/`.lan`/`.home.arpa` names. Add others with `ALLOWED_HOSTS=nas.example.com`. This blocks DNS-rebinding attacks from web pages.

### Caddy

```caddy
ocha.example.com {
	reverse_proxy 192.168.1.20:8090
}

ocha-explo.example.com {
	reverse_proxy 192.168.1.20:8091 {
		flush_interval -1   # Explo streams its live run log
	}
}
```

Caddy gets HTTPS certificates automatically ([docs](https://caddyserver.com/docs/quick-starts/reverse-proxy)).

### nginx

```nginx
server {
    server_name ocha.example.com;
    location / {
        proxy_pass http://192.168.1.20:8090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;   # replace, don't append
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
server {
    server_name ocha-explo.example.com;
    location / {
        proxy_pass http://192.168.1.20:8091;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;                              # live run log
    }
}
```

Add TLS with [Certbot](https://certbot.eff.org/) or your usual setup.

### Cloudflare Tunnel

With [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/), you can publish OCHA without opening any ports:

1. Create a tunnel and add two *public hostnames*: `ocha.example.com` → `http://<ocha-host>:8090`, and `ocha-explo.example.com` → `http://<ocha-host>:8091`.
2. In `.env`, also set `TRUSTED_IP_HEADER=CF-Connecting-IP`, so the login rate limit sees each visitor's real IP instead of the tunnel's.
3. Keep the ports private. Don't also publish 8090/8091 to the internet, or visitors could bypass Cloudflare.

### Proxy notes

- **Visitor IPs.** OCHA rate-limits sign-in attempts per IP. It trusts forwarding headers only when the request comes from a private address (your proxy). By default it reads the last `X-Forwarded-For` hop, which is the one your proxy wrote. Set `TRUSTED_IP_HEADER` if your proxy uses a different header.
- **HTTPS cookies.** With `UI_PUBLIC_URL` set to an `https://` address, OCHA marks its cookies *Secure*.
- **Caching.** OCHA versions its CSS and JS, so a CDN never serves an old copy after an update.
