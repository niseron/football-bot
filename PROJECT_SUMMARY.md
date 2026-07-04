# Football Picks Bot — Project Summary

## 1. Project Overview

An automated football betting analysis bot that:
- Fetches upcoming fixtures from a live football API (RapidAPI)
- Enriches each fixture with last-5 team form and head-to-head history from the same API
- Sends the enriched fixture list to Claude AI (claude-sonnet-4-6) for betting analysis
- Posts the top 5 value picks daily to a Telegram channel at 09:00 Brussels time as a text message and a branded PNG card
- Automatically checks match results every 30 minutes and updates Google Sheets
- Polls closing odds every 15 minutes as kickoff approaches, for closing line value (CLV) tracking
- Posts a weekly performance summary every Monday at 09:05 Brussels time with a PNG card
- Tracks all picks and P&L in a Google Sheet with conditional formatting, a Picks tab and a Summary tab

Covered competitions: Premier League, Belgian Jupiler Pro League, FIFA World Cup 2026.

---

## 2. File Structure

```
football-bot/
│
├── run_all.py            Entry point for Railway — combines all 4 schedulers into one process
├── main.py               Daily picks: fetches fixtures, enriches with form/H2H, runs Claude analysis, posts to Telegram
├── auto_results.py       Automatic result checker — polls API every 30 min, updates Sheets, posts result cards
├── closing_odds.py       Closing line value (CLV) tracker — polls odds every 15 min near kickoff, writes 'Closing Odds'
├── weekly_summary.py     Posts Monday performance summary to Telegram with PNG card
├── excel_tracker.py      Google Sheets data layer — all read/write to the spreadsheet
├── tracker.py            SQLite layer — local backup of every pick in picks.db
├── card_generator.py     Generates branded 1080×1080 PNG cards (picks, results, weekly summary)
│
├── calibration.py        Probability calibration engine — calibration_report() + edge_report() + clv_report()
├── update_result.py      CLI script to manually mark a pick WIN/LOSS/VOID/HALF WIN/HALF LOSS
├── backtest.py           Backtesting script against 2023-24 historical data (CSV output)
├── _run_now.py           Manual one-shot trigger — fetch + analyse + post immediately
│
├── cards/                Output folder for generated PNG cards (gitignored)
├── START_BOT.bat         Windows launcher — opens 4 cmd windows for local development
├── Procfile              Railway process definition: worker: python run_all.py
├── runtime.txt           Python version for Railway: python-3.12
├── nixpacks.toml         Railway build config — installs fonts-dejavu for card text rendering
├── requirements.txt      Python dependencies
│
├── .env                  Local secrets (not committed — in .gitignore)
├── .gitignore            Excludes .env, picks.db, picks_tracker.xlsx, __pycache__, cards/
└── PROJECT_SUMMARY.md    This file
```

---

## 3. Environment Variables

All of these must be set in Railway's Variables tab (and in `.env` for local use):

| Variable | Purpose |
|---|---|
| `RAPIDAPI_KEY` | RapidAPI key for the live football data API |
| `ODDS_API_KEY` | The Odds API key for real market odds (h2h/totals/spreads) used to flag value picks |
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
- **Font support:** `nixpacks.toml` installs `fonts-dejavu` so Pillow can render card text on Railway
- **Process:** Single process running four APScheduler jobs:
  - Daily picks — cron, 09:00 Europe/Brussels
  - Weekly summary — cron, Monday 09:05 Europe/Brussels
  - Live result checks — interval, every 30 minutes
  - Closing odds check (CLV tracking) — interval, every 15 minutes

**To deploy a change:**
1. Edit code locally
2. `git add . && git commit -m "message" && git push origin main`
3. Railway auto-redeploys within ~2 minutes

---

## 5. Telegram Channel

- **Channel ID:** `-1003617316561`
- **Message format:** MarkdownV2
- **What gets posted:**
  - Daily picks at 09:00 — MarkdownV2 text message + 1080×1080 PNG picks card
  - Result notifications when a pick settles (WIN / LOSS / HALF WIN / HALF LOSS with score and P&L)
  - Results card (PNG) posted after all picks for a day are settled
  - Weekly summary every Monday at 09:05 — text + PNG weekly summary card

---

## 6. Google Sheets Setup

- **Spreadsheet name:** Football Picks Tracker
- **Spreadsheet ID:** `1wY7_Y1QB2Cl-X3s5QqC3LGaCEjjwcGmhY-VPTxLa46U`
- **Service account:** `football-bot@football-bot-499516.iam.gserviceaccount.com`
- **GCP project:** `football-bot-499516`
- **APIs enabled:** Google Sheets API, Google Drive API

**Sheet tabs:**

| Tab | Columns |
|---|---|
| Picks | Date, Match, Bet Type, Pick, Odds, Confidence, Result, Profit/Loss, Running Total P&L, Bankroll (€), Claude Prob %, Market Prob %, League, Kickoff UTC, Closing Odds |
| Summary | Auto-calculated stats: win rate, total P&L, bankroll, ROI, best bet type, best confidence level, bet type breakdown table |

**Conditional formatting (applied via batchUpdate on every write):**

| Result | Cell colour |
|---|---|
| WIN | Green (`#00c853`) |
| HALF WIN | Amber (`#ffab00`) |
| HALF LOSS | Deep orange (`#ff6d00`) |
| LOSS | Red (`#d50000`) |
| Bankroll ≥ €100 | Light green row |
| Bankroll < €100 | Light red row |

---

## 7. Current Bot Features

### Core picks pipeline
- Top 5 value picks per day across all tracked competitions
- Picks use actual team names (never generic "Home Win" / "Away Win")
- Supported bet types: Match Winner, Both Teams to Score, Over/Under Goals, Asian Handicap, Double Chance
- Estimated decimal odds from Claude's market knowledge
- Confidence rating per pick (High / Medium / Low)
- 2–3 sentence reasoning per pick citing form, head-to-head, and value
- Duplicate pick prevention (won't re-post same pick same day)
- Single daily job at 09:00 Brussels — evening picks job removed

### Form & H2H enrichment (added)
- Before calling Claude, `enrich_with_context()` fetches from RapidAPI:
  - Last 5 matches for the home team (W/D/L form string + score details + home/away venue)
  - Last 5 matches for the away team
  - Last 5 head-to-head meetings between the two teams
- Data is injected into the JSON payload sent to Claude so it can factor in recent form
- Team results are cached within a run so the same team across multiple fixtures only hits the API once
- All enrichment calls are individually try/except'd — any failure is logged and skipped without affecting pick generation

### Asian Handicap half results (added)
- Quarter-line handicaps (±0.25, ±0.75, ±1.25, ±1.75 …) are detected automatically
- Each quarter line is split into its two component half-lines and evaluated separately
- Combined result: WIN+VOID → HALF WIN, VOID+LOSS → HALF LOSS
- P&L: HALF WIN = `+0.50 × (odds − 1)` units; HALF LOSS = `−0.50` units
- HALF WIN / HALF LOSS flow through the entire stack: Sheets, formatting, Summary, notifications

### Real odds & value flagging (added)
- `fetch_real_odds()` pulls live h2h/totals/spreads (Asian handicap) odds from The Odds API per fixture
- Each Claude pick is matched to its real market outcome; a pick is flagged as "value" only when Claude's implied probability exceeds the market's by ≥5 percentage points
- Both Claude's estimated odds and the real market odds are shown side by side in the Telegram message and the picks card
- If `ODDS_API_KEY` is missing, the fixture/market can't be matched, or the API call fails, the pick silently falls back to Claude-only odds (no crash, no message)

### Probability calibration engine (added — `calibration.py`)
- Claude must now output a `probability` field per pick (0-100, its estimated true win probability), logged to the 'Claude Prob %' column; the market implied probability (100 / market odds) is logged to 'Market Prob %' when real odds were found
- `calibration_report()` — buckets settled WIN/LOSS picks by stated probability (<50%, 50-60% … 90-100%) and compares Claude's average stated probability to the actual win rate per bucket, plus a Brier score (well-calibrated = actual ≈ stated)
- `edge_report()` — average Claude-vs-market edge for winners vs losers, and ROI of picks where Claude's probability exceeded the market's vs where it didn't
- Monthly calibration summary posted to Telegram on the first Monday of each month (piggybacks the weekly summary job), with sample size and a warning below 300 settled picks
- No backfill: picks logged before the columns existed have no probability data and are skipped
- Run manually: `python calibration.py`

### Closing Line Value (CLV) tracking (added — `closing_odds.py`)
- Each pick's kickoff time is captured from the RapidAPI fixture data at pick-log time and stored in the 'Kickoff UTC' column (plus 'League', for odds-batching)
- `closing_odds_job` polls every 15 minutes; for any unsettled pick whose kickoff is 5-65 minutes away, it fetches current market odds from The Odds API and overwrites the 'Closing Odds' column — the last write before kickoff becomes the closing price
- Odds API calls are batched per competition (one request covers every due match in that league that cycle), not one request per match
- Self-imposed cap of 12 Odds API requests/day (keeps this job + main.py's morning odds enrichment comfortably under the 500/month free-tier limit); polling is skipped with a warning if exceeded
- `calibration.py`'s `clv_report()` computes CLV = (original odds / closing odds − 1) × 100 for every settled pick with both values — average CLV, % of picks with positive CLV, and ROI split between positive- and negative-CLV picks
- Appended to the existing monthly calibration Telegram message, with the same below-300-picks sample size warning
- Purely additive measurement: never touches pick generation, Kelly staking, or the calibration engine's existing reports; every step fails silently on error
- Run manually: `python closing_odds.py`

### Kelly Criterion staking (added)
- Each pick gets a suggested stake calculated as half-Kelly, capped at 5% of real bankroll
- Based on historical win rate for that specific bet type from settled Sheets data
- Falls back to flat 1-unit (€10) stake when fewer than 10 settled picks exist for the bet type
- Key constants in `excel_tracker.py`: `UNIT_STAKE = 10.0`, `REAL_BANKROLL = 1500.0`
- Stake suggestion is included in the Telegram pick message

### PNG pick and result cards (added — `card_generator.py`)
- Dark neon aesthetic: black background, neon green accents, styled text
- **Picks card** (1080×1080): generated after daily picks are posted; sent as a photo to Telegram
- **Results card** (1080×1080): generated after results are finalized; sent as a photo to Telegram
- **Weekly summary card** (1080×1080): generated and sent with the Monday weekly summary
- Cards saved to `cards/` folder; win rate in the footer is pulled live from the Summary sheet
- Font: DejaVu (installed on Railway via `nixpacks.toml`)

### Tracking and reporting
- Auto result detection with score-based evaluation for all supported bet types
- Live result notifications sent to Telegram as each match finishes
- Running P&L tracked per pick and cumulatively; bankroll column updates after every result
- Bet type breakdown in Summary sheet: wins, losses, win rate %, total P&L per bet type
- Bet type breakdown also included in weekly Monday summary
- Weekly summary date range shows the completed previous week (fixed from current week)
- Win rate in `get_summary_win_rate()` scans by label (not hardcoded cell address) — robust to row additions
- World Cup 2026 support: group-stage and knockout match detection via team name fallback
- Youth team filtering (U19, U21, U23 matches excluded)

---

## Known Limitations & Future Issues (not yet addressed)

- **Odds timing bias** — *In progress, CLV tracking live from 4 Jul 2026.* Market probabilities in column L are still captured at 9AM pick time, and `edge_report` is still flattering by an unknown amount for picks logged before the fix. `closing_odds_job` now polls The Odds API 5-65 minutes before each kickoff and logs the true closing price to a separate 'Closing Odds' column; `calibration.py`'s `clv_report()` measures closing line value on top of it. This resolves the bias for every pick logged from 4 Jul 2026 onward — historical picks before that date have no closing odds and are excluded from `clv_report()`. Sample size is still tiny; see the calibration sample size limitation below.
- **Calibration sample size** — `calibration_report` and `edge_report` are statistically meaningless below ~300 settled picks with probability data. Data collection started 30 Jun 2026. Do not draw conclusions from early monthly reports.
- **Win rate is the wrong success metric** — a high win rate at low average odds can still be break-even or negative ROI. The metric that matters is ROI vs market implied probability, which the `edge_report` now tracks.
- **LLM overconfidence risk** — Claude's stated probabilities are uncalibrated and likely systematically overconfident on favorites. The calibration engine exists specifically to measure this gap.
- **No injury/lineup data** — the bot has form and H2H context but no player availability, injury status, or individual player form. Napoleon Games odds are also not in The Odds API, so market comparison uses consensus European bookmaker odds instead.
- **Kelly stakes based on thin data** — bet-type win rates driving Kelly calculations are based on small samples (10-30 picks per type) and may regress significantly.

---

## Roadmap

Completion estimates per area — update these percentages whenever a related change ships.

| Area | Done | Status |
|---|---|---|
| Bot core | 95% | Live — picks, results, sheets, cards, Telegram all automated on Railway |
| Data quality | 75% | Odds API + form/H2H + closing odds (CLV) live since 4 Jul 2026; no injuries/lineups |
| Calibration engine | 15% | Infrastructure done, collecting since 30 Jun 2026 (+ CLV since 4 Jul); verdict ~Oct at 300 picks |
| Content pipeline | 90% | Cards automatic, posting manual |
| Socials | 35% | Accounts + branding + IG-formatted card generator (`generate_picks_card_ig`, 1080×1350, top 3 picks) done; zero posts, no auto-posting pipeline yet |
| Proven edge | 5% | Blocked on calibration data |
| Site/app/monetization | 0% | Deliberately parked until edge is proven |

---

## 8. Still To Do

All previously listed items are complete. The bot is fully operational on Railway.

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

**To check and settle results now:**
```
python auto_results.py --results
```

**To run a closing-odds poll now** (writes 'Closing Odds' for any pick 5-65 min from kickoff):
```
python closing_odds.py
```

**To manually update a pick result:**
```
python update_result.py "Brazil vs Morocco" "BTTS" WIN
```
Supports: `WIN`, `LOSS`, `VOID`, `HALF WIN`, `HALF LOSS`

**To apply a manual fix with custom P&L:**
```
python auto_results.py --fix-brazil-japan
```

Requires a `.env` file in the `football-bot` folder with all 6 environment variables set.

---

## 10. GitHub Repository

**URL:** https://github.com/niseron/football-bot

**Branch:** `main`

**Key commits:**
1. `Initial commit` — full bot with Railway deployment files
2. `fix: read RAPIDAPI_KEY from os.environ at call time` — fixed 401 API error
3. `feat: migrate data storage from Excel to Google Sheets` — replaced openpyxl with gspread
4. `Add Asian Handicap half results, form/H2H enrichment, and Brazil vs Japan fix` — quarter-line AH detection, form/H2H context injected into Claude prompt, HALF WIN/HALF LOSS throughout the stack
