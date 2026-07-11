# Football Picks Bot — Project Summary

## 1. Project Overview

An automated football betting analysis bot that:
- Fetches upcoming fixtures from a live football API (RapidAPI)
- Enriches each fixture with last-5 team form and head-to-head history from the same API
- Sends the enriched fixture list to Claude AI (claude-sonnet-4-6) for betting analysis
- Posts the top 5 value picks daily to a Telegram channel at 12:00 Brussels time as a text message and a branded PNG card
- Mirrors delivery to Discord (purely additive): picks/results/weekly PNG cards to card channels, plus each pick's text routed to a per-league Discord channel
- Automatically checks match results every 30 minutes and updates Google Sheets
- Polls closing odds every 15 minutes as kickoff approaches, for closing line value (CLV) tracking
- Posts a weekly performance summary every Monday at 09:05 Brussels time with a PNG card
- Tracks all picks and P&L in a Google Sheet with conditional formatting, a Picks tab and a Summary tab

Covered competitions: Premier League, Belgian Jupiler Pro League, FIFA World Cup 2026.

Since 9 Jul 2026 the repo also hosts a **fully separate tennis picks system** (ATP/WTA) — see the "Tennis System — SEPARATE from football" section below. The two systems share the Railway process and API keys but no data paths, tabs, or calibration samples.

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
├── discord_bot.py        Discord delivery layer — send_to_discord() via Discord REST API (send-only, fail-silent)
├── env_loader.py         .env loading with a UTF-8 BOM guard — all entry points use load_env(), never load_dotenv() directly
│
├── calibration.py        Probability calibration engine — calibration_report() + edge_report() + clv_report()
├── update_result.py      CLI script to manually mark a pick WIN/LOSS/VOID/HALF WIN/HALF LOSS
├── backtest.py           Backtesting script against 2023-24 historical data (CSV output)
├── _run_now.py           Manual one-shot trigger — fetch + analyse + post immediately
│
├── tennis_main.py            TENNIS system (separate) — daily ATP/WTA picks pipeline
├── tennis_excel_tracker.py   TENNIS Sheets layer — reads/writes ONLY the 'Tennis Picks' tab
├── tennis_auto_results.py    TENNIS automatic result checker — polls every 30 min via run_all.py
├── tennis_closing_odds.py    TENNIS closing line value (CLV) tracker
├── tennis_calibration.py     TENNIS calibration engine — independent reports & 300-pick threshold
├── tennis_update_result.py   CLI to manually settle/override a tennis pick WIN/LOSS/VOID
│
├── cards/                Output folder for generated PNG cards (gitignored)
├── START_BOT.bat         Windows launcher — opens 4 cmd windows for local development
├── Procfile              Railway process definition: worker: python run_all.py
├── runtime.txt           Python version for Railway: python-3.12
├── fonts/                Bundled DejaVu Sans Mono TTFs (+ license) — card text on any OS
├── requirements.txt      Python dependencies
│
├── .env                  Local secrets (not committed — in .gitignore)
├── .gitignore            Excludes .env, picks.db, picks_tracker.xlsx, __pycache__, cards/
└── PROJECT_SUMMARY.md    This file
```

---

## 3. Environment Variables

All of these must be set in Railway's Variables tab (and in `.env` for local use):

> **.env encoding:** save the file as plain UTF-8 **without BOM**. A BOM once made python-dotenv silently fail to load the first line's variable (10 Jul 2026). All entry points now load `.env` via `env_loader.load_env()`, which tolerates a BOM (`utf-8-sig`) and logs a warning when one is present — never call `dotenv.load_dotenv()` directly in new code.

| Variable | Purpose |
|---|---|
| `RAPIDAPI_KEY` | RapidAPI key for the live football data API |
| `ODDS_API_KEY` | The Odds API key for real market odds (h2h/totals/spreads) used to flag value picks |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude AI analysis |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID where picks are posted |
| `GOOGLE_SHEETS_ID` | ID from the Google Sheet URL (between /d/ and /edit) |
| `GOOGLE_CREDENTIALS_JSON` | Full service account JSON (minified, single line) |
| `TELEGRAM_IG_CHANNEL_ID` | *Optional.* Telegram channel/chat ID that receives the Instagram-formatted picks card (`generate_picks_card_ig`) for manual download and posting. If unset, that card is still generated, saved to `/cards`, and sent to Discord's `picks-cards` channel — only the Telegram send is skipped. |
| `DISCORD_BOT_TOKEN` | *Optional for football, required for tennis delivery.* Discord bot token (Developer Portal → Bot → Reset Token). If unset, all Discord delivery is skipped silently — football's Telegram is unaffected, but tennis (Discord-only) posts nowhere. |
| `DISCORD_CHANNELS_JSON` | *Optional per key.* Single-line JSON dict mapping channel keys to Discord channel IDs, e.g. `{"picks-cards":"111...","results-cards":"222...","weekly-cards":"333...","premier-league":"444...","jupiler-pro-league":"555...","world-cup":"666...","tennis-picks":"777...","tennis-results":"888..."}`. Any missing key is skipped silently; several keys may point at the same channel ID. The `tennis-picks` / `tennis-picks-lower` / `tennis-results` keys carry ALL tennis delivery (tennis is Discord-only — no Telegram). |
| `TENNIS_RAPIDAPI_HOST` | *Optional (tennis system).* Overrides the tennis data API host. Defaults to `tennis-api-atp-wta-itf.p.rapidapi.com` ("Tennis API - ATP WTA ITF" by MatchStat). The RapidAPI account behind `RAPIDAPI_KEY` must be subscribed to this API. |
| `TENNIS_RANK_THRESHOLD` | *Optional (tennis system).* Rank tier cutoff, default `150`. No fixtures are excluded by rank — picks where BOTH players rank inside the top N go to the `tennis-picks` Discord channel; all others (either player outside, or unranked) go to `tennis-picks-lower`. The tier ('Top 150' / 'Lower Ranked') is also logged to the Sheet's 'Rank Tier' column. Per-tier pick counts are logged every run. |

---

## 4. Railway Deployment

- **Platform:** Railway (railway.app)
- **GitHub repo:** https://github.com/niseron/football-bot
- **Auto-deploy:** Yes — every push to `main` triggers a redeploy
- **Process type:** `worker` (defined in Procfile — no HTTP port needed)
- **Entry point:** `python run_all.py`
- **Python version:** 3.12 (runtime.txt)
- **Font support:** DejaVu Sans Mono TTFs are bundled in `fonts/` and tried first by `card_generator._font()` — no system font package needed. (A `nixpacks.toml` installing `fonts-dejavu` was documented here before, but that file was never committed, and `_font()` only searched `C:\Windows\Fonts` paths — so every Railway render fell back to Pillow's ~11px bitmap font and cards collapsed, e.g. 1080×460 on 11 Jul 2026. Fixed 11 Jul 2026.)
- **Process:** Single process running seven APScheduler jobs — four football, three tennis (the tennis jobs share the process but no data paths):
  - Daily picks (football) — cron, 12:00 Europe/Brussels
  - Weekly summary (football) — cron, Monday 09:05 Europe/Brussels
  - Live result checks (football) — interval, every 30 minutes
  - Closing odds check (football CLV) — interval, every 15 minutes
  - Daily tennis picks — cron, 12:30 Europe/Brussels (30 min after football's daily picks)
  - Tennis closing odds check (tennis CLV) — interval, every 15 minutes
  - Tennis live result checks — interval, every 30 minutes

**To deploy a change:**
1. Edit code locally
2. `git add . && git commit -m "message" && git push origin main`
3. Railway auto-redeploys within ~2 minutes

---

## 5. Telegram Channel

- **Channel ID:** `-1003617316561`
- **Message format:** MarkdownV2
- **What gets posted:**
  - Daily picks at 12:00 — MarkdownV2 text message + 1080×1080 PNG picks card
  - Result notifications when a pick settles (WIN / LOSS / HALF WIN / HALF LOSS with score and P&L)
  - Results card (PNG) posted after all picks for a day are settled
  - Weekly summary every Monday at 09:05 — text + PNG weekly summary card

---

## 5b. Discord Delivery (added 9 Jul 2026)

Delivery channel via `discord_bot.py` — no changes to pick generation or calibration. For **football** it is purely additive (mirrors what already goes to Telegram). For **tennis** it is the ONLY delivery channel — see the tennis section; tennis never posts to Telegram (user preference: Discord is easier to view). Send-only: uses Discord's REST API directly through `requests` (no discord.py dependency, no gateway/event client).

**Channel mapping** (`DISCORD_CHANNELS_JSON` keys → what gets posted there):

| Key | Content | Sent from |
|---|---|---|
| `picks-cards` | Daily picks PNG card, **plus** the Instagram-variant card (`generate_picks_card_ig`) — both land in this same channel every run (since 11 Jul 2026, intentional) | `main.py` (after the Telegram card send; IG card sent right after its optional `TELEGRAM_IG_CHANNEL_ID` send) |
| `results-cards` | Football live result notifications (text) — mirrored from the same 30-min automatic trigger that sends them to Telegram; plus the results PNG card when the manual football `--results` path runs | `run_all.py` `live_results_check` / `auto_results.py --live` / `auto_results.py --results` |
| `weekly-cards` | Weekly summary PNG card | `weekly_summary.py` |
| `premier-league` | Each Premier League pick as an embed | `main.py` |
| `jupiler-pro-league` | Each Jupiler Pro League pick as an embed | `main.py` |
| `world-cup` | Each World Cup 2026 pick as an embed | `main.py` |
| `tennis-picks` | **TENNIS (Discord-only)** — dated header (text) + each TOP-TIER tennis pick as an embed (both players inside `TENNIS_RANK_THRESHOLD`) at 12:30 Brussels, plus the picks-failed alert | `tennis_main.py` |
| `tennis-picks-lower` | **TENNIS (Discord-only)** — dated header (text) + each LOWER-TIER tennis pick as an embed (either player outside the threshold, or unranked). *New key 10 Jul 2026 — awaiting a Discord channel ID; until it is added to `DISCORD_CHANNELS_JSON`, lower-tier picks are skipped silently (still logged to Sheets).* | `tennis_main.py` |
| `tennis-results` | **TENNIS (Discord-only)** — each settled tennis pick's result text from the 30-min automatic checker | `run_all.py` `tennis_live_results_check` |

The league-name → key routing lives in `main.py`'s `DISCORD_LEAGUE_CHANNEL_KEYS`.

**Pick embed format** (built by `discord_bot.py`'s `build_pick_embed()`, since 10 Jul 2026 — previously plain text): title = match name; stripe colour by confidence (High = green `#00c853`, Medium = blue `#2196f3`, Low = orange `#ff6f00`); a full-width **Pick** field with the selection; inline **Bet Type** / **Odds** / **Confidence** fields side by side (Odds shows `Claude X | Market Y` when real odds were matched); the full reasoning as the description; a `🔥 VALUE` footer only when the pick beat the market by ≥5pp. Context renders as the small author line — the league for football, `Tour | Tournament | Surface` for tennis. Card/result sends (`picks-cards`, `results-cards`, `weekly-cards`, `tennis-results`) are unchanged plain text/images.

**Fail-silent guarantee:** `send_to_discord(channel_key, message=None, image_path=None)` never raises. Missing `DISCORD_BOT_TOKEN`, missing/malformed `DISCORD_CHANNELS_JSON`, an unmapped key, a bad image path, or a Discord API error each log one line and return `False` — the Telegram flow can never be affected. Rate limits (HTTP 429) get one retry after Discord's `retry_after`.

**Bot setup (already done):** application + bot in the Discord Developer Portal, no privileged intents, invited with the `bot` OAuth2 scope and View Channels / Send Messages / Attach Files / Embed Links permissions.

**To test all configured channels** (sends a text + image to each):
```
python discord_bot.py --test
```
Verified 9 Jul 2026: all 6 channels received the test message and image.

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
| Tennis Picks | **Tennis system only** — Date, Match, Bet Type, Pick, Odds, Confidence, Result, P&L, Claude Prob %, Market Prob %, Kickoff/Start Time, Closing Odds, Rank Tier ('Top 150' / 'Lower Ranked', for future per-tier calibration). Written exclusively by `tennis_excel_tracker.py`; no football code ever touches this tab and no tennis code ever touches Picks/Summary. |

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
- Single daily job at 12:00 Brussels — evening picks job removed

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
- Font: DejaVu Sans Mono, bundled in `fonts/` (Consolas et al. remain later fallbacks on Windows)

### Discord delivery (added — `discord_bot.py`)
- Every daily picks card and weekly card is mirrored to Discord right after its Telegram send
- Live result notifications (the automatic 30-minute checker) mirror to Discord from the identical trigger as the Telegram notification; the results PNG card additionally mirrors when the manual `--results` path generates it
- Each individual pick is routed as a Discord embed to a league-specific channel (`premier-league` / `jupiler-pro-league` / `world-cup`) — see section 5b for the embed format
- Entirely fail-silent — see section 5b for the mapping structure and guarantees

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
| Content pipeline | 95% | Cards automatic; auto-posted to Telegram + Discord (9 Jul 2026), only IG posting still manual |
| Socials | 40% | Accounts + branding + IG-formatted card (`generate_picks_card_ig`, 1080×1350, top 3 picks) done; auto-delivered to Discord's `picks-cards` channel every run (11 Jul 2026) and optionally to a Telegram chat via `TELEGRAM_IG_CHANNEL_ID` for manual download — actual Instagram posting is still manual, zero posts so far |
| Proven edge | 5% | Blocked on calibration data |
| Site/app/monetization | 0% | Deliberately parked until edge is proven |

The roadmap percentages above are **football only** — the tennis system below tracks its own roadmap and is never merged into these numbers.

---

## Tennis System — SEPARATE from football

A second, fully independent picks pipeline for ATP/WTA tennis, added 9 Jul 2026. It shares the Railway process, the Discord bot token, and the API keys — and **nothing else**. No shared calibration data, no shared Sheet columns/tabs, no shared SQLite, no shared functions in the data path. A bug or bad streak in one system cannot contaminate the other's data or reports.

**Delivery is Discord-ONLY (since 10 Jul 2026)** — unlike football, which posts to Telegram and mirrors to Discord, tennis never touches Telegram at all. Reason: user preference — Discord is easier to view. Picks go to the `tennis-picks` channel key and settled results to `tennis-results` (both in `DISCORD_CHANNELS_JSON`, never the football channels). The former `TELEGRAM_TENNIS_CHANNEL_ID` variable is removed and must not be reintroduced.

### Data collection start date: **9 Jul 2026**
### Independent verdict timeline: ~300 settled tennis picks with probability data — at ~3-5 picks/day, expect a first meaningful calibration read around **Oct-Nov 2026**. This clock is completely separate from the football calibration timeline; do not merge the two samples or compare their early reports.

### Architecture

| Piece | Tennis | Football equivalent (NOT shared) |
|---|---|---|
| Picks pipeline | `tennis_main.py` | `main.py` |
| Sheets layer | `tennis_excel_tracker.py` → 'Tennis Picks' tab only | `excel_tracker.py` → Picks/Summary tabs |
| CLV tracker | `tennis_closing_odds.py` (own daily request cap) | `closing_odds.py` |
| Calibration | `tennis_calibration.py` (own Brier, edge, CLV reports, own 300-pick threshold) | `calibration.py` |
| Auto results | `tennis_auto_results.py` + `run_all.py` `tennis_live_results_check` (every 30 min) | `auto_results.py` + `live_results_check` |
| Manual settle/override | `tennis_update_result.py` | `update_result.py` |
| Duplicate-run guard | reads the Tennis Picks tab | SQLite `picks.db` (tennis never touches it) |

### Pipeline (mirrors the football flow)

- **Fixtures:** "Tennis API - ATP WTA ITF" (MatchStat) on RapidAPI — ATP + WTA singles for the next 48 hours, capped at 25 fixtures/tour on busy days. Doubles are filtered out. Uses the same `RAPIDAPI_KEY`; the RapidAPI account must be **subscribed to this API** (separate from the football one). Host overridable via `TENNIS_RAPIDAPI_HOST`.
- **Rankings & rank tier split (10 Jul 2026):** fixture data carries no rankings, so each player's `currentRank` is fetched from the `player/profile/{id}` endpoint (cached per run; ~2 extra API calls per fixture, ≤100/day worst case). No fixtures are excluded by rank. Instead, picks are split into two Discord channels by tier: both players inside the top `TENNIS_RANK_THRESHOLD` (default 150) → `tennis-picks`; either player outside or unranked → `tennis-picks-lower`. Every pick goes to exactly one channel; per-tier counts are logged each run. The tier ('Top 150' / 'Lower Ranked') is logged to the Sheet's 'Rank Tier' column so `tennis_calibration.py` can eventually report calibration/CLV per tier. Ranks are sent to Claude (`player1_rank`/`player2_rank`) and shown in the pick embed's author line as `#54 vs #88` (`NR` = unranked).
- **Enrichment:** per fixture — tournament name/surface/tier (`tournament/info`), last-5 form per player (`player/past-matches`), and head-to-head (`fixtures/h2h`); capped at 20 enriched fixtures per run. In this API's archive data the first-listed player is always the winner.
- **Claude analysis:** separate `TENNIS_SYSTEM_PROMPT` (claude-sonnet-4-6) — weights player form, H2H, surface type (Hard/Clay/Grass), and tournament tier. Bet types: **Match Winner, Total Games Over/Under, Set Betting, Handicap (games)**. Outputs the same JSON shape as football, incl. the calibration `probability` field.
- **Real odds:** The Odds API lists tennis tournaments as dynamic per-event sport keys (`tennis_atp_*` / `tennis_wta_*`), so active keys are discovered at runtime via the quota-free `/v4/sports` call (max 6 odds requests per picks run). Same ≥5pp value-flag rule as football. Set Betting has no Odds API market → those picks stay Claude-odds-only.
- **Posting:** Discord-ONLY — a dated header (text) plus each pick as an embed (section 5b format, `Tour | Tournament | Surface` as the author line) to the `tennis-picks` Discord channel at **12:30 Europe/Brussels**, its own schedule slot 30 min after the football picks. No Telegram send exists in the tennis pipeline.
- **Tracking:** 'Tennis Picks' tab — Date, Match, Bet Type, Pick, Odds, Confidence, Result, P&L, Claude Prob %, Market Prob %, Kickoff/Start Time, Closing Odds, Rank Tier. Results are WIN/LOSS/VOID (units P&L: WIN = odds−1, LOSS = −1). No half results — tennis game handicaps and totals use half lines. The 'Rank Tier' column enables future per-tier calibration/CLV reports in `tennis_calibration.py` once each tier has enough settled picks.
- **CLV:** `tennis_closing_odds.py` polls every 15 min for picks starting in 5-65 min and overwrites the tennis 'Closing Odds' column; `tennis_calibration.py`'s `tennis_clv_report()` consumes it. Own self-imposed cap of 12 tennis odds requests/day, budgeted separately from the football cap.
- **Auto results:** `tennis_auto_results.py` (scheduled every 30 min via `run_all.py`'s `tennis_live_results_check`) scans unsettled Tennis Picks rows, finds each match in the Tennis API's fixtures-by-date (both tours, start date + next day, 30-min cache), and settles all four bet types from the set-score string: Match Winner by sets won, Total Games by summed games vs the line, Set Betting by exact set score from the picked player's perspective, Handicap by game margin + line. Retirements/walkovers settle **VOID** for every bet type (conservative — bookmaker rules differ; override with `tennis_update_result.py` if your book settled differently). Each newly settled pick posts its result text to the `tennis-results` Discord channel — Discord-only, never Telegram and never the football `results-cards` channel.

### Tennis limitations (own list, separate from football's)

- **Retirements/walkovers settle VOID for all bet types** — bookmaker rules differ (many settle Match Winner if a set was completed). Override a specific pick with `python tennis_update_result.py "Sinner vs Alcaraz" "Match Winner" WIN` when your book settled differently.
- **Result detection depends on the fixtures-by-date endpoint filling the `result` field** for finished matches. If a finished match never shows a result there, the pick stays pending and must be settled manually.
- **Set Betting picks have no market/closing odds**, so they contribute to the calibration report but never to the edge/CLV reports.
- **Calibration sample size** — all tennis reports are statistically meaningless below ~300 settled picks. Same rule as football, independent counter.

### Tennis roadmap (independent — do NOT merge into the football percentages)

| Area | Done | Status |
|---|---|---|
| Tennis bot core | 85% | Picks, Sheets tab, Discord-only delivery (`tennis-picks`/`tennis-results`, 10 Jul 2026 — no Telegram), CLV polling, auto-results all live |
| Tennis data quality | 65% | Form/H2H/surface enrichment + live rankings & two-tier channel split with Rank Tier tracking (10 Jul 2026) + dynamic Odds API keys; no injury/retirement data |
| Tennis calibration engine | 10% | Infrastructure done, collecting from 9 Jul 2026; verdict ~Oct-Nov 2026 at 300 picks |
| Tennis auto-results | 90% | Live from 10 Jul 2026, all 4 bet types; retirements settle VOID (manual override available) |
| Tennis proven edge | 0% | Blocked on tennis calibration data |

---

## 8. Still To Do

All previously listed items are complete. The bot is fully operational on Railway.

---

## 9. Running the Bot Locally

Double-click `START_BOT.bat` in the `football-bot` folder. It opens 4 separate command windows:

| Window | Command | Purpose |
|---|---|---|
| Picks Bot | `python main.py` | Scheduled daily picks at 12:00 |
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

**To test Discord delivery** (posts a test text + image to every channel in `DISCORD_CHANNELS_JSON`):
```
python discord_bot.py --test
```

**To manually update a pick result:**
```
python update_result.py "Brazil vs Morocco" "BTTS" WIN
```
Supports: `WIN`, `LOSS`, `VOID`, `HALF WIN`, `HALF LOSS`

**Tennis system (all commands hit only the Tennis Picks tab):**
```
python tennis_main.py --now                                            # one-shot: fetch + analyse + post tennis picks now
python tennis_main.py                                                  # start the tennis scheduler (12:30 Brussels)
python tennis_auto_results.py                                          # one-shot: check + settle tennis results now
python tennis_closing_odds.py                                          # one closing-odds poll for tennis picks
python tennis_update_result.py "Sinner vs Alcaraz" "Match Winner" WIN  # manually settle/override a tennis pick (WIN/LOSS/VOID)
python tennis_calibration.py                                           # print tennis calibration / edge / CLV reports
```

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
