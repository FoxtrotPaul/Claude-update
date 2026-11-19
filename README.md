# Claude Referral Monitor

Watches public sources for fresh `https://claude.ai/referral/<code>` (Guest Pass) links, filters out fakes/spam/stale posts, and sends alerts to your Telegram. Runs free on GitHub Actions.

## What each source costs (checked Nov 2026)

| Source | Cost | Notes |
|---|---|---|
| Hacker News (Algolia) | Free, no key | On by default |
| Mastodon hashtags | Free, no key | On by default |
| Reddit | Free, no key | On by default. Reddit often blocks datacenter IPs (GitHub Actions). Run `--check` to see. OAuth needs Reddit approval now |
| Bluesky | Free account | Search needs login; use an **app password** |
| X official API | **Pay-per-use, no free tier** (since Feb 2026), ~$0.005 per post read | We use `since_id`, so you pay only for *new* matches, with a daily cap (default 100 reads = $0.50/day max) |
| X via twitterapi.io | ~$0.15 per 1K tweets, small free trial credit | Experimental |
| Threads official search | Free | Only searches *your own* posts until Meta approves `threads_keyword_search` |
| Exa web search | $10 free credit/month (~2,500 Lite searches) | Optional; default polling ~58 searches/day |
| Any RSS (e.g. Google Alerts) | Free | Slow (hours), but catches the open web |

## Spam / fake-link protection

1. Host must be exactly `claude.ai` (rejects `claude.ai.evil.com`, `claude-ai.com`, `claude.ai@evil.com`, ...).
2. The alert link is **rebuilt** as `https://claude.ai/referral/<code>`; the raw URL from the post is never forwarded.
3. Placeholder / word-like codes rejected (`YOUR-CODE`, `xxxxxxxx`, `examplecode`).
4. Posts older than 48h ignored (configurable); future-dated posts ignored.
5. "Selling / price / $ / WhatsApp / crypto" posts dropped.
6. One author posting more than 3 distinct codes in 24h = list/aggregator/farm, all suppressed (a real referrer has at most 3 passes).
7. Every code alerts once, ever (14-day memory).
8. Optional link check (`VALIDATE=label|drop|off`). Best-effort only: claude.ai may bot-block it, and a page loading does not prove the pass is unused.

## Setup

### 1. Telegram bot
1. In Telegram, open **@BotFather** → `/newbot` → pick a name and a username ending in `bot` → copy the **token**.
2. Open your new bot and press **Start** (a bot cannot message you until you do).
3. In a browser open `https://api.telegram.org/bot<TOKEN>/getUpdates`, find `"chat":{"id":123456789` → that number is your **chat id**.
4. In Telegram, set the bot chat to *not muted* and a loud notification sound.

### 2. Bluesky (free, recommended)
Create an account at bsky.app → Settings → Privacy and security → **App passwords** → create one. Use the handle + app password (never your main password).

### 3. GitHub (hosting)
1. Create a **public** repo (public = unlimited free Actions minutes; private repos get 2,000 min/month, this job needs far more).
2. Upload everything in this folder, including the hidden `.github/` folder.
3. Settings → Secrets and variables → Actions → **New repository secret**:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `BSKY_HANDLE`, `BSKY_APP_PASSWORD` (+ any optional ones).
4. Actions tab → enable workflows → **referral-monitor** → **Run workflow**. Open the run and read the log.
5. Send yourself a test: run locally `python monitor.py --test-telegram`, or temporarily add that command to a workflow_dispatch run.

Public repo means public logs and a public `state/seen.json` (codes only, which are public anyway). Secrets stay hidden.

### 4. Verify before trusting it (do this)
Locally: `pip install -r requirements.txt`, set the env vars from `.env.example`, then
`python monitor.py --check` prints, per source, `OK: N posts fetched` or `FAILED: reason`. Anything FAILED from GitHub's IPs (usually Reddit) can be run from a home PC/phone (Termux) instead: `python monitor.py --daemon`.

## Notes and limits
- GitHub cron is delayed 5–30 min at busy times and pauses after 60 days of repo inactivity; the weekly heartbeat commit prevents that and pings you "alive".
- You get a Telegram warning if a source fails 6 cycles in a row.
- Guest Pass terms: recipient must be new to paid Claude, card required, converts to $20/month unless cancelled before day 7. Each link is single-use and expires 7 days after the referrer created it.
- Run tests: `python -m unittest discover -s tests -v`
