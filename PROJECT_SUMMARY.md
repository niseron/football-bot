# Football Picks Bot — Project Summary

## 1. Project Overview

An automated football betting analysis bot that:
- Fetches upcoming fixtures from a live football API (RapidAPI)
- Sends the fixture list to Claude AI (claude-sonnet-4-6) for betting analysis
- Posts the top 5 value picks daily to a Telegram channel at 09:00 Brussels time
- Automatically checks match results every 30 minutes and updates Google Sheets
- Posts a weekly performance summary every Monday at 09:05 Brussels time
- Tracks all picks and P&L in a Google Sheet with a Picks tab and a Summary tab

Covered competitions: Premier League, Belgian Jupiler Pro League, FIFA World Cup 2026.

---

## 2. File Structure

```
football-bot/
│
├── run_all.py            Entry point for Railway — combines all 3 schedulers into one process
├── main.py               Daily picks: fetches fixtures, runs Claude analysis, posts to Telegram
├── auto_results.py       Automatic result checker — polls API every 30 min, updates Sheets
├── weekly_summary.py     Posts Monday performance summary to Telegram
├── excel_tracker.py      Google Sheets data layer — all read/write to the spreadsheet
├── tracker.py            SQLite layer — local backup of every pick in picks.db
│
├── update_result.py      CLI script to manually mark a pick WIN/LOSS/VOID
├── backtest.py           Backtesting script against 2023-24 historical data (CSV output)
├── _run_now.py           Manual one-shot trigger — fetch + analyse + post immediately
│
├── START_BOT.bat         Windows launcher — opens 4 cmd windows for local development
├── Procfile              Railway process definition: worker: python run_all.py
├── runtime.txt           Python version for Railway: python-3.12
├── requirements.txt      Python dependencies
│
├── .env                  Local secrets (not committed — in .gitignore)
├── .gitignore            Excludes .env, picks.db, picks_tracker.xlsx, __pycache__
└── PROJECT_SUMMARY.md    This file
```

---

## 3. Environment Variables

All of these must be set in Railway's Variables tab (and in `.env` for local use):

| Variable | Purpose |
|---|---|
| `RAPIDAPI_KEY` | RapidAPI key for the live football data API |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude AI analysis |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID where picks are posted |
| `GOOGLE_SHEETS_ID` | ID from the Google Sheet URL (between /d/ and /edit) |
| `GOOGLE_CREDENTIALS_JSON` | Full service account JSON (minified, single line) |

---

## 4. Railway Deployment

- **Platform:** Railway (railway.app)
- **GitHub repo:** https://github.com/niseron/football-bot
- **Auto-deploy:** Yes — every push to `main` triggers a redeploy
- **Process type:** `worker` (defined in Procfile — no HTTP port needed)
- **Entry point:** `python run_all.py`
- **Python version:** 3.12 (runtime.txt)
- **Process:** Single process running three APScheduler jobs:
  - Daily picks — cron, 09:00 Europe/Brussels
  - Weekly summary — cron, Monday 09:05 Europe/Brussels
  - Live result checks — interval, every 30 minutes

**To deploy a change:**
1. Edit code locally
2. `git add . && git commit -m "message" && git push origin main`
3. Railway auto-redeploys within ~2 minutes

---

## 5. Telegram Channel

- **Channel ID:** `-1003617316561`
- **Message format:** MarkdownV2
- **What gets posted:**
  - Daily picks at 09:00 (5 picks with match, bet type, odds, confidence, reasoning)
  - Result notifications when a pick settles (WIN/LOSS with score and P&L)
  - Weekly summary every Monday at 09:05 (win rate, P&L, best pick of the week)

---

## 6. Google Sheets Setup

- **Spreadsheet name:** Football Picks Tracker
- **Spreadsheet ID:** `1wY7_Y1QB2Cl-X3s5QqC3LGaCEjjwcGmhY-VPTxLa46U`
- **Service account:** `football-bot@football-bot-499516.iam.gserviceaccount.com`
- **GCP project:** `football-bot-499516`
- **APIs enabled:** Google Sheets API, Google Drive API
- **Status:** Fully working locally — connection tested and verified

**Sheet tabs:**

| Tab | Columns |
|---|---|
| Picks | Date, Match, Bet Type, Pick, Odds, Confidence, Result, Profit/Loss, Running Total P&L |
| Summary | Auto-calculated stats: win rate, total P&L, best bet type, best confidence level |

**Pending:** Add `GOOGLE_SHEETS_ID` and `GOOGLE_CREDENTIALS_JSON` to Railway Variables tab so the deployed bot writes to Sheets.

---

## 7. Current Bot Features

- Top 5 value picks per day across all tracked competitions
- Picks use actual team names (never generic "Home Win" / "Away Win")
- Supports bet types: Match Winner, Both Teams to Score, Over/Under Goals, Asian Handicap, Double Chance
- Estimated decimal odds from Claude's market knowledge
- Confidence rating per pick (High / Medium / Low)
- 2–3 sentence reasoning per pick (form, head-to-head, value)
- Duplicate pick prevention (won't re-post same pick same day)
- Auto result detection with score-based evaluation for all supported bet types
- Live result notifications sent to Telegram as soon as a match finishes
- Running P&L tracked per pick and cumulatively
- Weekly summary with win rate, weekly P&L, best pick of the week
- World Cup 2026 support with group-stage and knockout match detection
- Youth team filtering (U19, U21, U23 matches are excluded)

---

## 8. Still To Do

- [ ] Add `GOOGLE_SHEETS_ID` and `GOOGLE_CREDENTIALS_JSON` to Railway Variables tab
- [ ] Verify Railway worker process is enabled in Railway dashboard settings
- [ ] Confirm first live pick is logged to Google Sheets after Railway redeploy

---

## 9. Running the Bot Locally

Double-click `START_BOT.bat` in the `football-bot` folder. It opens 4 separate command windows:

| Window | Command | Purpose |
|---|---|---|
| Picks Bot | `python main.py` | Scheduled daily picks at 09:00 |
| Weekly Summary | `python weekly_summary.py` | Scheduled Monday summary at 09:05 |
| Results Schedule | `python auto_results.py --schedule` | Nightly result check at 00:15 |
| Results Live | `python auto_results.py --live` | Live check every 30 minutes |

**For a one-shot manual run** (fetch + post picks immediately):
```
python _run_now.py
```

**To manually update a pick result:**
```
python update_result.py "Brazil vs Morocco" "BTTS" WIN
```

Requires a `.env` file in the `football-bot` folder with all 6 environment variables set.

---

## 10. GitHub Repository

**URL:** https://github.com/niseron/football-bot

**Branch:** `main`

**Commits so far:**
1. `Initial commit` — full bot with Railway deployment files
2. `fix: read RAPIDAPI_KEY from os.environ at call time` — fixed 401 API error
3. `feat: migrate data storage from Excel to Google Sheets` — replaced openpyxl with gspread
