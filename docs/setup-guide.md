# Setup walkthrough

This takes you from nothing to a library that fills itself. Plan on about 15 minutes, plus however long your first playlist takes to download.

**On this page**

1. [Before you start](#1-before-you-start)
2. [Install](#2-install)
3. [First sign-in](#3-first-sign-in)
4. [Settings, tab by tab](#4-settings-tab-by-tab)
5. [Request your first song](#5-request-your-first-song)
6. [Playlists and schedules (Explo)](#6-playlists-and-schedules-explo)
7. [Watching it work](#7-watching-it-work)
8. [Put it on your phone](#8-put-it-on-your-phone)
9. [Updating OCHA](#9-updating-ocha)

---

## 1. Before you start

You need:

- **A machine that runs Docker** with Docker Compose, such as a home server, NAS, mini PC or VPS. Install Docker from [docs.docker.com/get-docker](https://docs.docker.com/get-docker/).
- **A media server**: [Jellyfin](https://jellyfin.org/docs/general/installation/) or [Navidrome](https://www.navidrome.org/docs/installation/) (or another Subsonic-compatible server).
- **Somewhere playlists come from**: a free [ListenBrainz](https://listenbrainz.org) account, which builds a personal "Weekly Exploration" playlist from what you listen to, and/or Spotify or Apple Music playlists you want mirrored.

Optional, depending on your choices later:

- A free **Spotify developer app**, for Spotify playlists and links ([how](connect-services.md#spotify)).
- **slskd**, if you want to download from Soulseek ([how](connect-services.md#soulseek-slskd)).
- A spare **Telegram account**, if you want to use a Telegram music bot ([how](telegram-userbot.md)).

## 2. Install

```bash
git clone https://github.com/sekiro65/ocha.git
cd ocha
cp .env.example .env
```

Open `.env` in any editor. For most people only two things matter:

- **Folders.** `MUSIC_PATH` is where your sorted library lives. If Jellyfin or Navidrome already scans a music folder, put that absolute path here, for example `MUSIC_PATH=/srv/media/music`. `DOWNLOADS_PATH` is a staging folder and can stay as it is.
- **`PUID` / `PGID`.** These are the user and group that own your music folder. Run `id` on the host to find yours. OCHA runs as this user and never as root.

Leave `UI_PASSWORD=Melovepassword` as it is. You'll replace it from the panel in a minute.

Start it:

```bash
docker compose up -d
docker compose logs -f ocha   # optional: watch it start, Ctrl+C to stop watching
```

The first start builds the image, which takes a few minutes. When the log shows `Starting OCHA as 1000:1000`, it's ready.

## 3. First sign-in

Open **`http://<your-server-ip>:8090`** (for example `http://192.168.1.20:8090`).

![Sign in](screenshots/sign-in.webp)

Sign in with **`Melovepassword`**. Because this default is public, OCHA won't do anything else until you choose your own password:

![Choose your password](screenshots/choose-password.webp)

Pick something at least 8 characters long. From now on the panel uses it, and the `.env` value no longer matters.

> Forgot it later? Delete `config/ui/auth.json` and restart the container. The `.env` password works again.

## 4. Settings, tab by tab

Open **Settings**. Each tab has its own **Save** button, which lights up once you change something. Fields only appear when they apply to your choices.

### Library: your media server

| Field | What to enter |
|---|---|
| **Media server** | *Jellyfin* or *Navidrome / Subsonic* |
| **Server URL** | How OCHA reaches it, for example `http://192.168.1.10:8096` (Jellyfin) or `http://192.168.1.10:4533` (Navidrome). The server must be reachable from the OCHA container, so use the LAN IP rather than `localhost`. |
| **Music library** | The library's name exactly as it appears in Jellyfin, usually `Music` |
| **API key** (Jellyfin) | Jellyfin → Dashboard → API Keys → **+** ([details](connect-services.md#jellyfin)) |
| **Username / Password** (Navidrome) | A Navidrome user OCHA can create playlists as ([details](connect-services.md#navidrome--subsonic)) |

The **Folders** section underneath shows where downloads land and where sorted music goes. These come from `.env`. To change them, edit `.env` and run `docker compose up -d` again.

### Discovery: where playlists come from

| Field | What to enter |
|---|---|
| **ListenBrainz username** | Your username on listenbrainz.org |
| **ListenBrainz token** | listenbrainz.org → your profile → **Settings** → *User token* ([details](connect-services.md#listenbrainz)) |
| **Spotify client ID / secret** | From your Spotify developer app ([details](connect-services.md#spotify)). Needed for Spotify playlists, Spotify links in requests, and the Telegram downloader. |

### Downloads: where the music comes from

Pick a **Source**:

| Source | Good for | Extra setup |
|---|---|---|
| **YouTube** (default) | Works out of the box at home | None. On a VPS you may need [cookies](connect-services.md#youtube-cookies-only-if-youtube-blocks-you). |
| **Soulseek (slskd)** | Lossless (FLAC) files | Run slskd and paste its URL and API key ([details](connect-services.md#soulseek-slskd)) |
| **Telegram music bot** | High-quality files from a bot you trust | API ID and hash, the bot's @username, and a sign-in ([full guide](telegram-userbot.md)) |

![Download settings](screenshots/settings-downloads.webp)

If you choose Telegram, a **Telegram sign-in** card appears under the form. Enter the phone number of the account OCHA should use, then the code Telegram sends you (and your two-step password, if you set one).

### Panel: password and looks

- **Password**: change your panel password at any time. All other signed-in browsers are signed out.
- **Reduce transparency**: solid cards instead of glass, for readability or older devices.
- **Background**: upload your own wallpaper (JPEG, PNG or WebP), or reset to the default.
- **Sign out** (on phones; on a computer it's at the bottom of the sidebar).

## 5. Request your first song

Open **Request**, type `Artist – Song` (or paste a Spotify or YouTube link) and press **Request**.

![Request](screenshots/request.webp)

The request moves from *Working* to *Done*. A few seconds later the music sorter files it into `MUSIC_PATH/Artist/Album/`. Your media server picks it up on its next scan, and Explo triggers one after playlist runs.

If a request fails, the line under it tells you why ("Nothing found on YouTube", "Set the slskd URL in Settings", and so on).

## 6. Playlists and schedules (Explo)

Open the **Explo** tab. This is the [Explo](https://github.com/LumePart/Explo) dashboard, signed in for you.

- **Built-in playlists**: *Weekly Exploration*, *Daily Jams* and the other ListenBrainz playlists for your account.
- **Custom playlists**: paste a public Spotify, Apple Music or ListenBrainz playlist link to mirror it.
- **Run now**: fetch a playlist immediately.
- **Schedule**: choose how often each playlist refreshes. OCHA's scheduler runs scheduled playlists one at a time, so they never fight over downloads.

Each run works out which tracks you're missing, downloads only those, waits for the sorter to file them, rescans your media server, and builds the playlist there.

## 7. Watching it work

- **Activity** shows the library size, what's downloading right now, free disk space, and each service (music sorter, Explo, playlist schedule, Telegram) with a **Restart** button.
- **Logs** has live logs for each service. *Sorter* shows every file it imported or set aside, and *Runs* shows playlist runs.

![Logs](screenshots/logs.webp)

Files the sorter couldn't match are listed in `downloads/watcher-needs-review.txt`. Duplicates go to `downloads/.duplicates` instead of being downloaded again and again.

## 8. Put it on your phone

The panel is built for phones as well as desktops. Open it in your phone's browser, then:

- **iPhone (Safari)**: Share → *Add to Home Screen*
- **Android (Chrome)**: ⋮ → *Install app* / *Add to Home screen*

It opens full-screen like an app, with the tabs at the bottom.

To use it away from home, put it behind a domain with HTTPS. See [running behind a domain](connect-services.md#running-behind-a-domain).

## 9. Updating OCHA

```bash
cd ocha
git pull
docker compose up -d --build
```

Your settings, password, Telegram session and databases live in `CONFIG_PATH` and survive updates.
