# Social Watcher

Checks YouTube channels and X profiles every ~5 minutes from the cloud (GitHub
Actions, free) and sends each NEW post to your private Telegram bot chat.
Your phone can be off or offline: Telegram holds the messages and delivers them
when the phone reconnects.

## Setup (about 15 minutes)

### 1. Telegram bot
1. In Telegram, open **@BotFather** -> send `/newbot` -> pick a name and username.
2. Copy the **bot token** it gives you.
3. Open your new bot and press **Start** (send any message to it).
4. In a browser open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   and find `"chat":{"id": 123456789`. That number is your **chat id**.
5. In Telegram, keep notifications ON for this bot chat; mute your other chats.

### 2. GitHub
1. Create a GitHub account and a **new repository** (see "Public vs private" below).
2. Upload all files from this folder (keep the `.github/workflows/watch.yml` path).
3. Repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID`   = your chat id
4. Repo -> **Settings -> Actions -> General -> Workflow permissions** ->
   choose **Read and write permissions** -> Save.
5. Repo -> **Actions** tab -> enable workflows -> **watch** -> **Run workflow**
   with "Send a test message" ticked. You should get a Telegram message.

### 3. Add your channels and profiles
Edit `config.yaml` (you can edit it on github.com). The first run of a new source
only records what already exists, so you won't get a flood of old posts.

- YouTube: `handle: "@name"` or `channel_id: UC...` (channel_id is most reliable).
- X: `username: name`.

## X (Twitter): read this
X has no free API. This project supports two free, best-effort methods:

- **Nitter**: put an instance in `settings.nitter_instances` in config.yaml.
  Public instances are mostly blocked/rate-limited; a self-hosted one needs a
  throwaway X account.
- **Twikit**: add secrets `X_AUTH_TOKEN` and `X_CT0` (cookies of a THROWAWAY X
  account, from your browser's dev tools -> Application -> Cookies on x.com).
  Unofficial; may break; may get that account restricted; against X's terms.
  GitHub's servers use datacenter IPs, which X may block.

If X stops working you get ONE Telegram warning (after 12 failed runs in a row);
YouTube keeps working. Backup that never breaks: on X, follow the profile and
tap the bell -> "All posts".

## Public vs private repo
- **Public**: unlimited free Actions minutes, 5-minute checks. Anyone can see
  `config.yaml` and `state.json` (your watch list). To hide the list, create a
  secret `WATCH_CONFIG` containing the whole YAML; it overrides config.yaml.
- **Private**: free plans have a monthly minutes limit (about 2,000 at the time
  of writing; verify current limits). 5-minute checks would exceed it, so
  change the cron in `watch.yml` to `*/30 * * * *`.

## Good to know
- GitHub may delay scheduled runs by several minutes. This is "near real time",
  not instant.
- GitHub can pause scheduled workflows in repos with no activity for 60 days.
  The watcher commits a daily heartbeat to avoid this, but check the Actions tab
  now and then.
- A phone call is not supported (it would not reach an offline phone and real
  calls cost money). Telegram's message sound is the alert.
- Local test: `pip install requests pyyaml`, then `python watcher.py --dry-run`.

## Adding a platform (Instagram, Telegram channels, Reddit...)
Create `plugins/<name>.py` with:

```python
NAME = "reddit"; EMOJI = "👽"
def key(src): return src["subreddit"].lower()          # unique id for state
def fetch(src, entry, settings):                       # newest first
    return [{"id": "...", "title": "...", "url": "https://..."}, ...]
```
Then add `- platform: reddit` entries to config.yaml. Nothing else changes.
(Reddit and Mastodon have free RSS; Instagram is blocked like X, so expect the
same fragility.)
