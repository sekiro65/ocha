# Telegram downloader (userbot) setup

OCHA can get tracks from a Telegram "music bot": a bot that answers a Spotify link with the audio file. Bots can't talk to other bots, so OCHA signs in as a normal Telegram **user account** and messages the music bot for you. This is called a **userbot**.

You only need this if you pick **Telegram music bot** as the download source. YouTube and Soulseek don't use Telegram at all.

## Is OCHA a bot?

No. OCHA is a server app with a web panel. Only its Telegram part is automated, and it behaves like a quiet person:

- It signs in with your phone number like the Telegram app does, not with a BotFather token.
- It sends Spotify links to the music bot one at a time, at most one every 20 seconds.
- It never replies or chats. It downloads the file and leaves.
- It ignores files from anyone except the music bot, so strangers can't make it download things.
- It won't ask for the same track twice within 30 days.

## Before you start

- **Use a spare account.** Telegram can limit accounts that automate a lot. A second number (a cheap SIM or a virtual number that receives Telegram codes) keeps your main account safe.
- **Pick a music bot you trust.** It's a third party and OCHA doesn't host or recommend any. Only download music you have the right to.
- **Open the bot once from your phone** on the account you'll use and press **Start**. Many bots ignore accounts that never started them.

## 1. Get an API ID and hash

Every app that signs in to a Telegram account has to identify itself. These aren't a password and don't give access on their own.

1. Go to [my.telegram.org](https://my.telegram.org) and sign in with the account OCHA will use.
2. Open **API development tools**.
3. Fill in an app title and a short name, both at least 5 characters and without the word "Telegram"
   (for example `OCHA music` / `ochamusic`). Leave the URL empty, pick platform **Desktop**, and create it.
4. Copy **App api_id** (a number) and **App api_hash** (a long string).

## 2. Note the music bot's username

Open the music bot in Telegram and copy its **@username** from its profile, e.g. `@examplemusicbot`.
OCHA looks it up when it starts. (Its numeric ID works too, if you have it.)

## 3. Save the settings

In OCHA, open **Settings → Downloads**:

| Field | Value |
|---|---|
| Source | Telegram music bot |
| Telegram API ID | the api_id from step 1 |
| Telegram API hash | the api_hash from step 1 |
| Music bot | the @username from step 2 |

Select **Save**.

## 4. Sign in

The **Telegram sign-in** card appears next to the settings.

1. Enter the phone number with its country code, for example `+91 98765 43210`, and select **Send code**.
2. Telegram sends a code **inside the Telegram app** (not by SMS). Enter it and select **Sign in**.
3. If the account has two-step verification, enter that password when asked.

The card then shows **Signed in as …** and the Telegram service starts. You can check it under **Activity → Services**.

## 5. Test it

Open **Request**, paste a Spotify track link, and select **Request**. The track appears under **Activity → Downloads**, then gets sorted into your library. **Logs → Telegram** shows each step.

## Good to know

- The sign-in is saved in `config/telegram/bot_session.session`. Treat that file like a password: anyone with it can use the account.
- To switch accounts, select **Sign out of Telegram**, then sign in again.
- Optional: set `ADMIN_ID` (your own Telegram user ID) in `.env` to get a message when a file needs manual sorting or the server comes back online, and to send files to the account yourself.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Save the Telegram API ID and hash first" | Fill both in Settings and select Save before signing in. |
| The code never arrives | Look in the Telegram app on another device signed in to that account, under the "Telegram" chat. |
| "PHONE_CODE_EXPIRED" or "PHONE_CODE_INVALID" | Select Send code again and use the newest code. |
| "A wait of N seconds is required" | Telegram is rate-limiting sign-ins. Wait that long and try again. |
| Requests stay on "Waiting" | Open the music bot from your phone and press Start, then check Logs → Telegram. |
| "Telegram bot answered 401" | Restart the container so the bot and Explo share the same internal key. |
