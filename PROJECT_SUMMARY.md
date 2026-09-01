# Football Picks Bot — Project Summary

## 1. Project Overview

An automated football betting analysis bot that:
- Fetches upcoming fixtures from a live football API (RapidAPI)
- Enriches each fixture with last-5 team form and head-to-head history from the same API
- Sends the enriched fixture list to Claude AI (claude-sonnet-4-6) for betting analysis
- Posts the day's picks to Discord at 12:00 Brussels time: a branded PNG card to `picks-cards` and each pick as an embed in its own per-league channel
- Discord is the ONLY delivery surface — Telegram was removed from the whole repo on 18 Aug 2026 (see section 5)
- Automatically checks match results every 30 minutes and updates Google Sheets
- Polls closing odds every 15 minutes as kickoff approaches, for closing line value (CLV) tracking
- Posts a weekly performance summary every Monday at 09:05 Brussels time with a PNG card
- Tracks all picks and P&L in a Google Sheet with conditional formatting, a Picks tab and a Summary tab

Covered competitions: Premier League, Belgian Jupiler Pro League, Bundesliga, La Liga, Serie A, Ligue 1 (the last four added 19 Jul 2026; their 2026-27 seasons open 15-28 Aug 2026, so they produce no fixtures before then), UEFA Champions League (added 4 Aug 2026 — qualifying rounds, league phase and knockouts; **already producing fixtures**, Q3 is under way), UEFA Conference League (added 30 Jul 2026, same three stages), and FIFA World Cup 2026 (ended 19 Jul 2026). Both UEFA competitions and the Jupiler Pro League are matched by stable parent id rather than a pinned feed id — see "Parent-id leagueId resolution" below.

⚠️ **The Jupiler Pro League produced zero picks from the initial commit until 8 Aug 2026.** It was pinned in `LEAGUES` as leagueId `900433`, which the live feed never returns — the Belgian fixtures carry a season-scoped id (`937988` in 2026-27, parent `40`). `partition_fixtures()` therefore matched nothing for it on every run, so its fixtures were never fetched, never analysed and never rejected: the pipeline simply never saw them. Confirmed 8 Aug 2026 against the Google Sheet's Picks tab — 187 logged picks, **0 with League = Jupiler Pro League** (164 FIFA World Cup 2026, 21 Conference League, 2 Friendlies). Fixed 8 Aug 2026 by moving Belgium onto the parent-resolution path. Consequence for calibration: there is no Belgian sample at all, and the League Breakdown's Jupiler row reading zero is a real absence of data, not variance.

Since 9 Jul 2026 the repo also hosts a **fully separate tennis picks system** (ATP/WTA) — see the "Tennis System — SEPARATE from football" section below. The two systems share the Railway process and API keys but no data paths, tabs, or calibration samples. Since 6 Aug 2026 tennis no longer uses The Odds API at all (football-only) — tennis CLV is disabled by choice; see section 7.

---

## 2. File Structure

```
football-bot/
│
├── run_all.py            Entry point for Railway — combines all 4 schedulers into one process
├── main.py               Daily picks: fetches fixtures, enriches with form/H2H, runs Claude analysis, posts to Discord
├── auto_results.py       Automatic result checker — polls API every 30 min, updates Sheets, posts result cards
├── closing_odds.py       Closing line value (CLV) tracker — polls odds every 15 min near kickoff, writes 'Closing Odds'
├── weekly_summary.py     Posts Monday performance summary + monthly calibration report to Discord with PNG card
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
├── tennis_closing_odds.py    TENNIS closing line value (CLV) tracker — **DISABLED 6 Aug 2026** (no-op; tennis makes no Odds API calls)
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
| `GOOGLE_SHEETS_ID` | ID from the Google Sheet URL (between /d/ and /edit) |
| `GOOGLE_CREDENTIALS_JSON` | Full service account JSON (minified, single line) |
| `DISCORD_BOT_TOKEN` | **Required.** Discord bot token (Developer Portal → Bot → Reset Token). If unset, all delivery is skipped silently and NOTHING is posted anywhere — since 18 Aug 2026 Discord is the only surface, so this is no longer an optional extra. |
| `DISCORD_CHANNELS_JSON` | *Optional per key.* Single-line JSON dict mapping channel keys to Discord channel IDs, e.g. `{"picks-cards":"111...","results-cards":"222...","weekly-cards":"333...","premier-league":"444...","jupiler-pro-league":"555...","world-cup":"666...","bundesliga":"999...","la-liga":"aaa...","serie-a":"bbb...","ligue-1":"ccc...","tennis-picks":"777...","tennis-results":"888..."}`. Any missing key is skipped silently; several keys may point at the same channel ID. The `tennis-picks` / `tennis-picks-lower` / `tennis-results` keys carry ALL tennis delivery. |
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
- **Process:** Single process running six APScheduler jobs — four football, two tennis (the tennis jobs share the process but no data paths):
  - Daily picks (football) — cron, 12:00 Europe/Brussels
  - Weekly summary (football) — cron, Monday 09:05 Europe/Brussels
  - Live result checks (football) — interval, every 30 minutes
  - Closing odds check (football CLV) — interval, every 15 minutes
  - Daily tennis picks — cron, 12:30 Europe/Brussels (30 min after football's daily picks)
  - Tennis live result checks — interval, every 30 minutes
  - _(A tennis closing-odds job ran every 15 min until 6 Aug 2026; removed when The Odds API was switched off for tennis — see section 7.)_

**To deploy a change:**
1. Edit code locally
2. `git add . && git commit -m "message" && git push origin main`
3. Railway auto-redeploys within ~2 minutes

---

## 5. Telegram — REMOVED (18 Aug 2026)

Telegram was the original delivery channel and is now entirely gone: no
`python-telegram-bot` dependency, no `TELEGRAM_*` environment variable, no
`telegram` import, no send anywhere in the repo. Discord is the only surface for
football and tennis alike (section 5b). Do not reintroduce it.

**Railway variables to delete:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`,
`TELEGRAM_IG_CHANNEL_ID`. Nothing reads them; leaving them set is harmless but
`TELEGRAM_BOT_TOKEN` is a live credential and should be revoked in @BotFather as
well, because it appeared in plaintext in the Railway deploy logs (see below).

**What moved, so nothing was lost.** Three things were Telegram-only and were
ported rather than dropped:

| Was Telegram-only | Now |
|---|---|
| Weekly summary TEXT (Monday 09:05) | `weekly-cards`, via `send_long_to_discord` |
| Monthly calibration report (first Monday) | `weekly-cards`, via `send_long_to_discord` |
| Kelly suggested stake (in the picks digest) | `Stake` field on each **Core** pick embed |
| `auto_results.py --results` per-pick + P&L lines (CLI only) | `results-cards` |

The MarkdownV2 builders in `weekly_summary.py` are unchanged; a
`_to_discord_markdown()` converter at the send boundary turns `*bold*` into
`**bold**` and strips MarkdownV2's backslash escapes, so the escaping rules stay
in one place.

**What was dropped deliberately:** the Telegram picks digest itself (the numbered
text list of the day's Core picks). The per-pick embeds already carry the same
picks in richer form, and its one unique element — the Kelly stake — moved into
the Core embed. The dedicated Instagram Telegram channel is also gone; the IG
card is still generated, saved to `/cards`, and posted to `picks-cards`.

**Credential leak, now closed.** `httpx` logs the full request URL at INFO, and
`python-telegram-bot` put its bot token in the URL PATH — so
`api.telegram.org/bot<TOKEN>/sendMessage` was written to the Railway logs in
plaintext (5 occurrences in the 15-18 Aug window). Removing Telegram removes the
leak; `httpx`/`httpcore` are additionally pinned to WARNING in `main.py` and
`tennis_main.py`. Audited at the same time: the Anthropic SDK is httpx-backed but
sends its key as a header, and RapidAPI / The Odds API use `requests`, which
never logs URLs — no other key leaks.

---

## 5b. Discord Delivery (added 9 Jul 2026)

Delivery via `discord_bot.py` — no changes to pick generation or calibration. Since 18 Aug 2026 this is **the** delivery layer for football and tennis alike, not a mirror: a failed send here is the delivery failing, not a duplicate going missing. Send-only: uses Discord's REST API directly through `requests` (no discord.py dependency, no gateway/event client).

`send_to_discord()` truncates message content at 2000 characters. Long reports (weekly summary, monthly calibration) must therefore go through **`send_long_to_discord()`**, which splits on line boundaries and paces the chunks.

**Channel mapping** (`DISCORD_CHANNELS_JSON` keys → what gets posted there):

| Key | Content | Sent from |
|---|---|---|
| `picks-cards` | Daily picks PNG card, **plus** the Instagram-variant card (`generate_picks_card_ig`) — both land in this same channel every run (since 11 Jul 2026, intentional) | `main.py` (after the Telegram card send; IG card sent right after its optional `TELEGRAM_IG_CHANNEL_ID` send) |
| `results-cards` | Football live result notifications (text) from the 30-min automatic trigger; plus the results PNG card when the manual football `--results` path runs | `run_all.py` `live_results_check` / `auto_results.py --live` / `auto_results.py --results` |
| `weekly-cards` | Weekly summary PNG card | `weekly_summary.py` |
| `premier-league` | Each Premier League pick as an embed | `main.py` |
| `jupiler-pro-league` | Each Jupiler Pro League pick as an embed | `main.py` |
| `world-cup` | Each World Cup 2026 pick as an embed | `main.py` |
| `bundesliga` | Each Bundesliga pick as an embed (league tracked since 19 Jul 2026; first fixtures ~28 Aug 2026) | `main.py` |
| `la-liga` | Each La Liga pick as an embed (league tracked since 19 Jul 2026; first fixtures ~15 Aug 2026) | `main.py` |
| `serie-a` | Each Serie A pick as an embed (league tracked since 19 Jul 2026; first fixtures ~22 Aug 2026) | `main.py` |
| `ligue-1` | Each Ligue 1 pick as an embed (league tracked since 19 Jul 2026; first fixtures ~21 Aug 2026) | `main.py` |
| `champions-league` | Each Champions League pick as an embed (tracked since 4 Aug 2026; live immediately — Q3 fixtures were already in the 48h window that day) | `main.py` |
| `conference-league` | Each Conference League pick as an embed (key added 12 Aug 2026; channel id `1537177968999268444` mapped and verified reachable 13 Aug 2026) | `main.py` |
| `europa-league` | Each Europa League pick as an embed (tracked since 13 Aug 2026; live immediately — 12 Q3 fixtures were already in the 48h window that day). Channel id `1537453739252908123`, verified reachable 13 Aug 2026 | `main.py` |
| `tennis-picks` | **TENNIS (Discord-only)** — dated header (text) + each TOP-TIER tennis pick as an embed (both players inside `TENNIS_RANK_THRESHOLD`) at 12:30 Brussels, plus the picks-failed alert, plus the branded daily tennis picks PNG card (`generate_tennis_picks_card`, all of the day's picks across both tiers — added 11 Jul 2026) | `tennis_main.py` |
| `tennis-picks-lower` | **TENNIS (Discord-only)** — dated header (text) + each LOWER-TIER tennis pick as an embed (either player outside the threshold, or unranked). *New key 10 Jul 2026 — awaiting a Discord channel ID; until it is added to `DISCORD_CHANNELS_JSON`, lower-tier picks are skipped silently (still logged to Sheets).* | `tennis_main.py` |
| `tennis-results` | **TENNIS (Discord-only)** — each settled tennis pick's result text from the 30-min automatic checker | `run_all.py` `tennis_live_results_check` |
| `opus-shadow` | **Opus 5 shadow experiment only** (13 Aug 2026) — every Opus pick as a text embed, all leagues, both tiers, author line `OPUS 5 SHADOW · <league> · <tier> #<rank>` and a `SIM` stake tag. No cards. Results are **never** posted here — settlement writes to the sheet only. Channel id `1537471803440762992`, verified reachable 13 Aug 2026. Also the experiment's master gate: while this key is absent from `DISCORD_CHANNELS_JSON` the whole shadow is inert (no model call, no cost, no sheet or Odds API usage). | `opus_shadow.py` |
| `usage` | Daily API usage + cost report at 23:50 Brussels — Anthropic tokens/cost per job, The Odds API units against the 20,000/month tier (football-only spend since 6 Aug 2026), RapidAPI football + tennis quotas (added 4 Aug 2026) | `run_all.py` `usage_summary_job` → `usage_tracker.py` |

The league-name → key routing lives in `main.py`'s `DISCORD_LEAGUE_CHANNEL_KEYS`.

**Conference League got its own channel on 12 Aug 2026**, reversing the 30 Jul 2026
decision to leave it off `DISCORD_LEAGUE_CHANNEL_KEYS`. Reason: card-only routing
made it the one tracked competition whose picks had a *single* Discord surface, so
anything dropped from the card vanished from Discord entirely — while a Premier
League pick in the same situation still reached its league channel. That mattered
because Conference League qualifying has been carrying most of the book since
early Aug. Both UEFA competitions now route identically.

**Europa League joined them on 13 Aug 2026** — the last of the three UEFA club
competitions to be tracked. All three now route identically: parent-id resolution,
own Discord channel, own Odds API key entry. See "Europa League added" below for
why it was excluded until then.

Both channel ids are mapped as of 13 Aug 2026 (`conference-league`
`1537177968999268444`, `europa-league` `1537453739252908123`), each confirmed with
a read-only `GET /v10/channels/{id}` — same guild `1524850408852291754` and same
category as `champions-league`. Remember `DISCORD_CHANNELS_JSON` must be set on
**Railway** as well as locally; a key missing there makes `send_to_discord()` log
`Discord channel key '<key>' not mapped — skipping` and those picks reach Discord
via the card only. Nothing errors; the rest of the run is untouched. (Same pattern
as `tennis-picks-lower` above.)

**Pick embed format** (built by `discord_bot.py`'s `build_pick_embed()`, since 10 Jul 2026 — previously plain text): title = match name; stripe colour by confidence (High = green `#00c853`, Medium = blue `#2196f3`, Low = orange `#ff6f00`); a full-width **Pick** field with the selection; inline **Bet Type** / **Odds** / **Confidence** fields side by side (Odds shows a SINGLE figure — the real market price when one was matched, otherwise the estimate; see "Model-name-free output" below); the full reasoning as the description; a `🔥 VALUE` footer only when the pick beat the market by ≥5pp. Context renders as the small author line — the league for football, `Tour | Tournament | Surface` for tennis. Card/result sends (`picks-cards`, `results-cards`, `weekly-cards`, `tennis-results`) are unchanged plain text/images.

**Model-name-free output (4 Aug 2026).** No user-facing string names the model. Two rules:

1. **One odds figure, never two.** Every surface shows the real market price when
   one was matched, and falls back to the estimate when it wasn't — labelled just
   `Odds`. The old `Claude 1.85 · Mkt 1.93` / `Claude X | Market Y` comparison is
   gone from Discord embeds, both picks cards (Telegram + IG) and the Telegram
   message. The `🔥 VALUE` / `[VALUE]` flag survives unchanged — it is the useful
   half of the comparison and names nothing.
2. **This is display-only.** `Claude Prob %` and `Market Prob %` are still written
   to the sheet exactly as before, because `calibration.py` (Brier/edge) and the
   CLV job read them. Do not "tidy" those columns away.

Internal logs, docstrings, exception text and column headers may still say Claude.
The one trap: `_notify_picks_failed(reason)` / `_notify_tennis_picks_failed(reason)`
relay `reason` verbatim into a Discord alert, so reason strings passed to
them are user-facing — keep model names in the adjacent `log.error` instead.

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
| Picks | Date, Match, Bet Type, Pick, Odds, Confidence, Result, Profit/Loss, Running Total P&L, Bankroll (€), Claude Prob %, Market Prob %, League, Kickoff UTC, Closing Odds, Market Odds, **Pick Tier** (added 13 Aug 2026). **'Odds' is Claude's estimate; 'Market Odds' is the matched market price and is what settlement pays out at** (see 'Settlement pays the market price', 9 Aug 2026). **'Pick Tier' is `Core` (the 5 highest-conviction picks across all competitions that day) or `Extended` (every other pick; up to 10 per competition per day — it was `Core` = ranks 1-5 / `Extended` = ranks 6-10 of one 10-pick list until 15 Aug 2026); BLANK MEANS CORE and the 217 rows logged before 13 Aug 2026 are deliberately left blank — never backfill them** (see 'Ranked picks and the Core/Extended split'). Running Total P&L and Bankroll are Core-only and stay blank on Extended rows. |
| Summary | **Core-tier only** (13 Aug 2026) apart from the final tier block — every figure below is computed from Core rows, so Extended picks never move the headline win rate, P&L or bankroll. A **Pick Tier Breakdown** table is appended at the bottom (wins / losses / win rate / total P&L / picks for Core vs Extended) — the one section that deliberately sees both tiers. Auto-calculated stats: win rate, total P&L, bankroll, ROI, best bet type, best confidence level, Bet Type Breakdown table, and (4 Aug 2026) a **League Breakdown** table — wins / losses / win rate / total P&L / picks per competition, sorted by P&L descending. Built from the Picks tab's League column; deliberately one section in this tab, never per-league tabs, so calibration data stays unified. Every tracked competition gets a row even at zero picks (most open their 2026-27 season mid-to-late August — zeros are expected, not a bug), leagues found in the sheet but not in `TRACKED_LEAGUES` are appended rather than dropped, and the 119 picks logged before the League column existed group under `(no league recorded)` so the section's P&L reconciles exactly with the headline total. Note `Picks` here counts every logged pick incl. pending/void, unlike Bet Type Breakdown's `Total Picks` (settled wins + losses only). Below it sits a **Bet Type × League Breakdown** (4 Aug 2026) — win rate, P&L and pick count for every league/bet-type cell, leagues ordered by P&L to match the section above. A cell shows a win rate only at `_MIN_CELL_SAMPLE` (10) or more **decided** picks and otherwise reads `insufficient data`; the gate is on the rate's own denominator (wins + losses), not on settled count, because 10 settled picks that are 8 VOIDs and 2 decided would otherwise print exactly the 2-sample rate the rule exists to hide. P&L and pick count always show — only the rate is unsafe at low n. Only leagues with ≥1 settled pick appear, so not-yet-started competitions add no rows here (they stay visible at zero in the League Breakdown). On 4 Aug 2026, 11 of 18 cells read `insufficient data` — slicing two ways splits an already-small sample hard, and that is the honest state, not a gap to fill. |
| Tennis Picks | **Tennis system only** — Date, Match, Bet Type, Pick, Odds, Confidence, Result, P&L, Claude Prob %, Market Prob %, Kickoff/Start Time, Closing Odds, Rank Tier ('Top 150' / 'Lower Ranked', for future per-tier calibration), Stake € (SIM), Running P&L (u), Bankroll € (SIM), Player IDs. Written exclusively by `tennis_excel_tracker.py`; no football code ever touches this tab and no tennis code ever touches Picks/Summary. |
| Tennis Summary | **Tennis system only** — mirror of football's Summary tab, rebuilt by `_refresh_tennis_summary()` whenever a tennis result settles (`finalize_tennis_workbook()`, called from auto-results and the manual override): overall record, win rate, units P&L, simulated bankroll + ROI, best bet type / confidence level, Bet Type Breakdown (win-rate-desc), plus a tennis-only Rank Tier Breakdown (Top 150 vs Lower Ranked; pre-10-Jul rows show as '(untracked)'). Header labels the staking as SIMULATED. |
| Opus Shadow Picks | **Opus 5 shadow experiment only** (13 Aug 2026) — mirrors the football Picks columns including `Pick Tier`, plus two SIMULATED staking columns appended at the end: `Stake EUR (SIM)` and `Bankroll EUR (SIM)` (€100 start, half-Kelly capped at 5%, same logic as the tennis tab). Written exclusively by `opus_tracker.py`; **no football code reads it and it reads no football tab**, which is what keeps Opus out of calibration/edge/CLV and the football Summary. Running Total P&L here is tier-BLIND (both tiers move it) — the shadow has no baseline to protect. `Closing Odds` stays empty: the shadow does no closing-odds polling, so it cannot eat football's CLV budget. |
| Fable Picks | **HISTORICAL — discontinued experiment data, kept for reference.** Rows logged by the Fable 5 shadow experiment (12-18 Jul 2026) with the football Picks structure minus staking/bankroll columns (units P&L only). Nothing reads or writes this tab anymore — the Fable pipeline was removed on 19 Jul 2026 (see the discontinued-experiment note in section on the Fable 5 shadow pipeline). |

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
- Up to **10** ranked value picks **per competition** per day (so 30+ on a busy slate across up to
  10 competitions), of which the **best 5 across the whole slate are Core** and every other pick is
  **Extended** — see "Per-league picks and global Core selection" below. Was up to 10 globally
  13-15 Aug 2026, and a flat top 5 before that.
- Picks use actual team names (never generic "Home Win" / "Away Win")
- Supported bet types: Match Winner, Both Teams to Score, Over/Under Goals, Asian Handicap, Double Chance
- Knockout Match Winner picks carry an explicit time scope (since 12 Jul 2026): "(90 min)" =
  regulation only (3-way market, a 90-minute draw loses it), "(Full-Time incl. ET/Pens)" = team to
  advance (2-way market). WC fixtures on non-group-stage leagueIds are sent to Claude with
  `knockout: true`; `auto_results.py` settles each scope from the API's status.reason (FT/AET/Pen) —
  a match reaching extra time was by definition level at 90, and a shootout winner can't be read
  from the score, so full-time picks decided on penalties stay PENDING for manual settlement via
  `update_result.py`. Unscoped picks follow the bookmaker default: 90 minutes only — for EVERY bet
  type, not just Match Winner (since 12 Jul 2026). The API only returns the final (incl.-ET) score,
  so when a match goes past 90 each bet type derives what the guaranteed 90-minute draw implies:
  Asian Handicap settles on a goal difference of 0, Double Chance 'or draw' wins / '12' loses,
  Over/Under settles when the max possible regulation total (2 × the lower final score) is below
  the line, BTTS 'No' wins when either side finished scoreless; anything still ambiguous (e.g.
  Over 2.5 with a 3-2 AET score) stays PENDING for manual settlement via `update_result.py`.
- Estimated decimal odds from Claude's market knowledge
- Confidence rating per pick (High / Medium / Low)
- 2–3 sentence reasoning per pick citing form, head-to-head, and value
- Duplicate pick prevention (won't re-post same pick same day)
- Single daily job at 12:00 Brussels — evening picks job removed

### Parent-id leagueId resolution (Conference League 30 Jul 2026; Champions League 4 Aug 2026; Jupiler Pro League 8 Aug 2026; Europa League 13 Aug 2026)
The by-date fixtures feed returns no competition name — only a `leagueId`, and for
some competitions that id is **season-specific** (and for UEFA, stage-specific too).
It rotates when qualifying ends and the league phase begins, and again every season,
so pinning it makes the competition silently vanish from the picks. Champions League
is the clearest proof: its feed id was `904988` in the 2025-26 league phase and is
`937348` in 2026-27 qualifying. **This is why those competitions are not in
`LEAGUES`** — that dict is only for leagues the feed tags with a stable id.

Instead `partition_fixtures()` matches against the *stable* fotmob parent ids in
`PARENT_RESOLVED_IDS`:

| Competition | Qualification parent | Main parent |
|---|---|---|
| Jupiler Pro League | — | `40` |
| Champions League | `10611` | `42` |
| Europa League | `10613` | `73` |
| Conference League | `10615` | `10216` |

- Fast path: any match whose `leagueId` is in `FEED_LEAGUE_IDS[competition]` (seeded
  with `937988` / `937348` / `937349` / `937351`) is bucketed straight away — **zero extra API calls**.
- Self-heal: only when some tracked competition yields nothing does
  `_discover_feed_ids()` run. It resolves unfamiliar `leagueId`s to their
  `parentLeagueId` via `football-get-match-detail` (one lookup per distinct id,
  capped at `MAX_PARENT_LOOKUPS_PER_RUN = 12`) and files each hit under the
  competition that owns that parent.
- **Candidate ranking is what makes that cap survivable, and fixture count alone is
  not enough.** A UEFA round is one of the bigger blocks on its matchday, so
  largest-first finds it; a domestic matchday is small. Measured on the live
  8 Aug 2026 slate: 138 unfamiliar ids, and the 3-match Belgian block ranked **#62**
  by size — far outside the cap. Ranking by size alone would have left the Jupiler
  fix cosmetic, self-healing in theory and never in practice.
  So blocks are ranked by **roster overlap first** (what fraction of the block's own
  team ids appear in a missing competition's known roster, from `ROSTER_PARENTS`
  via `football-get-all-matches-by-league` — one cached call per parent), then by
  fixture count. That moved the Belgian block from #62 to **#1**.
- Roster overlap only *ranks*; the `parentLeagueId` lookup still decides. That split
  matters: the roster is the parent's last completed season, so it misses promoted
  clubs — on 8 Aug 2026 parent `40` covered 13 of the 16 clubs in the 2026-27 Belgian
  slate (Kortrijk, Lommel, Beveren had just come up). Ranking absorbs a 13/16 roster
  without trouble; deciding membership on it would have dropped every fixture
  involving a promoted club. **Never promote roster overlap to a membership test.**
- **One shared sweep serves every competition, and it must.** `_parent_league_cache`
  records each id exactly once, so a second per-competition sweep would skip every
  id the first had already resolved — including its own, filed as cached misses.
  Anything added here goes in `PARENT_RESOLVED_IDS`, never in a parallel sweep.
- `_parent_league_cache` caches hits *and* misses for the process lifetime, so a
  rotated id costs one discovery sweep and never again. `main.py` runs as a
  long-lived scheduler, so the cache resets only on redeploy.

Verified 30 Jul 2026: 19 Conference League Q2 fixtures bucketed, Europa League
Qualification (`937349`, parent `10613`) correctly excluded *at that time*, wiped
seed rediscovered. (That exclusion was deliberate and is now reversed — Europa
League has been tracked since 13 Aug 2026; see "Europa League added" below. The
30 Jul run is what first identified `937349` → parent `10613`, and that mapping was
re-confirmed live on 13 Aug before seeding it.)
Re-verified 4 Aug 2026 for Champions League: `937348` resolved live to parent `10611`
("Champions League Qualification"), `904988` (17 Sep 2025) to parent `42`
("Champions League"); 2 Q3 fixtures bucketed on the fast path with no extra calls; a
wiped CL seed rediscovered `937348` and produced an identical fixture set with
Conference League unaffected.
Verified 8 Aug 2026 for Belgium: 3 Jupiler fixtures bucketed on the fast path
(Standard–Cercle, St.Truiden–Lommel, Westerlo–Union SG); with the seed and both
caches wiped, discovery rediscovered `937988` via parent `40` on the **first** lookup
and returned an identical fixture set, UEFA competitions unaffected.

### Europa League added (13 Aug 2026)
The last of the three UEFA club competitions to be tracked, completing the set.
Until this date Europa League fixtures were **invisible to the pipeline** — not
dropped late, never fetched: `_discover_feed_ids()` resolved the block to parent
`10613`, matched it against no entry in `PARENT_RESOLVED_IDS`, and rejected it
(`main.py` `_discover_feed_ids`, the `break` on a parent hit). Because rejection
happens before analysis, **no Europa League pick has ever existed** — the Sheet has
zero Europa rows all-time, so there was no backlog to recover when the competition
was switched on.

Five edits, mirroring exactly what Conference League needed:

| Where | Added |
|---|---|
| `PARENT_RESOLVED_IDS` | `"Europa League": {73, 10613}` |
| `FEED_LEAGUE_IDS` | `{937349}` (Europa League Qualification 2026-27) |
| `ODDS_API_SPORT_KEYS` | `soccer_uefa_europa_league` (qualifying is Claude-odds-only — see the odds caveat above) |
| `DISCORD_LEAGUE_CHANNEL_KEYS` | `"Europa League": "europa-league"` |
| `SYSTEM_PROMPT` | named in the competition list **and** in the two-legged-tie paragraph, which previously said "Champions League and Conference League" — that paragraph carries the this-leg-only betting rule, so leaving it out would have let Europa ties be priced as elimination games |

Verified 13 Aug 2026 immediately after the change: `partition_fixtures()` bucketed
**12 Europa League fixtures** (Rangers–Jagiellonia, Hearts–Benfica, Anderlecht–PAOK,
Beşiktaş–Hradec Kralove et al.) on the fast path with zero extra API calls, alongside
25 Conference League and 1 Jupiler — Conference League's count unchanged, confirming
the new bucket takes nothing from it. `937349` re-resolved live to parent `10613`
before seeding.

**Live on the day it shipped, mid-window.** Unlike Bundesliga/La Liga/Serie A/Ligue 1
(tracked 19 Jul, first fixtures weeks later), Europa League Q3 was already playing —
12 fixtures kicked off 17:00–19:00Z on 13 Aug, hours after the change. The 12:00 run
that day predated the wiring, so it produced 5 Conference League picks and no Europa
ones. Whether to backfill that gap with a supplementary run is the same call as
30 Jul (see `_post_conference_league_jul30.py`); the mechanics that make it safe are
in section 9.

### Which pinned ids can go stale (audit, 8 Aug 2026)
Run this check before adding any league, and re-check if a league ever goes quiet:
resolve one of its live fixtures through `football-get-match-detail` and compare
`parentLeagueId` to the id you intend to pin. **Self-resolving → safe in `LEAGUES`;
anything else is season-scoped and belongs in `PARENT_RESOLVED_IDS`.**

| League | Pinned id | Resolves to | Verdict |
|---|---|---|---|
| Premier League | `47` | `47` | Stable parent — safe |
| Bundesliga | `54` | `54` | Stable parent — safe |
| La Liga | `87` | `87` | Stable parent — safe |
| Serie A | `55` | `55` | Stable parent — safe |
| Ligue 1 | `53` | `53` | Stable parent — safe |
| Jupiler Pro League | ~~`900433`~~ | `40` (feed `937988`) | **Season-scoped — moved out of `LEAGUES`** |

So this does **not** recur next August: all five remaining `LEAGUES` entries are
stable parent ids that the feed uses directly, and Belgium — the only season-scoped
one — is now on the self-healing path. The shape is a useful tell: the stable ids are
small (2 digits), while season-scoped ids are 6-digit (`900433`, `937988`, `937348`).

**Odds caveat (Conference League and Europa League):** The Odds API has no key for
either competition's *qualifying* rounds — only `soccer_uefa_europa_conference_league`
and `soccer_uefa_europa_league` for the main competitions, both inactive until the
league phase. Qualifying picks in both are therefore Claude-odds-only (no market
odds, no value flag).

Re-checked for Europa League on 13 Aug 2026: `soccer_uefa_europa_league` answers
`200` with an **empty** event list, and `soccer_uefa_europa_league_qualification`
does not exist at all (`404 UNKNOWN_SPORT`). Same provider gap as Conference
League, same non-fix: the plan tier is irrelevant.

**Measured 6 Aug 2026 — this is a provider gap, NOT a quota or mapping problem,
and upgrading the plan does not fix it.** Verified by sweeping the `/events`
listing of **all 67 soccer keys** in the catalogue (that endpoint is quota-free —
confirmed, it does not move `x-requests-used`) against the day's six
Europa/Conference qualifying fixtures:

- **Zero** of them appear under *any* sport key. The only hits for those clubs
  were their own domestic fixtures days later (Ajax → Eredivisie, Braga →
  Primeira Liga, Twente → Eredivisie, Lugano → Swiss Super League, Shelbourne
  and Bohemians → League of Ireland, Rapid Wien → Austrian Bundesliga).
- `soccer_uefa_europa_league` → **0 events**. `soccer_uefa_europa_conference_league`
  → **0 events**. Neither has a qualifying key anywhere in the catalogue.
- `soccer_uefa_champs_league_qualification` → **10 events** listed for 11 Aug.

So UEFA qualifying is not categorically excluded by the provider: **Champions
League qualifying is covered, Europa and Conference qualifying are not.** Any
card built solely from Europa/Conference qualifiers will show estimated odds on
every pick regardless of tier or quota. Do not re-diagnose this as a budget
issue. Re-test with the free `/events` sweep before assuming the gap has closed —
the league phase from September is the point at which the main-competition keys
go active.
**Champions League does not have this problem:** it maps to a *tuple* of keys,
`("soccer_uefa_champs_league", "soccer_uefa_champs_league_qualification")`, tried in
order with the first non-empty result winning. Measured 4 Aug 2026: an out-of-season
key answers HTTP 200 with `[]` and is **not billed** against the quota (only a key
returning fixtures costs the 6 units of 2 regions × 3 markets), so the fallback is
free and CL qualifying picks get real market odds today. `_fetch_odds_events()`
accepts a plain string or a tuple, so every other competition is unchanged.

### API usage & cost tracking (`usage_tracker.py`, added 4 Aug 2026)

All four paid sources were verified **programmatically readable** on 4 Aug 2026 — nothing here is
dashboard-only, estimated, or recalled:

| Source | Mechanism | Verified reading |
|---|---|---|
| Anthropic | `message.usage` on every response | `input_tokens`, `output_tokens`, cache read/write, `service_tier` |
| The Odds API | `x-requests-used` / `x-requests-remaining` headers | 3/20,000 used (6 Aug 2026 — **paid tier live** on a regenerated key; the original key never picked the upgrade up, see below) |
| RapidAPI football | `X-RateLimit-Requests-*` headers | 407/20,000, resets ~11.7d |
| RapidAPI tennis | same headers, **separate subscription** on the same key | 6,003/150,000, resets ~5.8d |

**Anthropic is the only one that must be recorded rather than read.** Its usage is per-response and
ephemeral, and there is no org-level usage/cost API — so `record_anthropic_usage()` appends a row to
the **`API Usage` Sheets tab** on every Claude call, in both `main.py` and `tennis_main.py`, *before*
the JSON parse can raise (the call is billed whether or not the parse succeeds). It never raises: a
dropped row costs a line of reporting accuracy, an exception would cost the day's picks.

**Storage is Sheets, not `picks.db`** — Railway's filesystem is ephemeral and SQLite would not
survive a redeploy.

**The monthly figure is labelled "since tracking started", not month-to-date.** Calls made before
4 Aug 2026 cannot be recovered from any API, so presenting a true MTD number would be fiction. The
report says so in the message itself.

**Odds API units ≠ requests.** One call bills `regions × markets` — measured, not assumed. The bot
sends **one** region since 6 Aug 2026: `eu` × `h2h,totals,spreads` = **3 units** (verified: one call
moved `x-requests-used` 432 → 435, exactly half the old `eu,uk` cost of 6). Dropping the `uk` region
loses 3 of 12 bookmakers per event — `betfair_ex_uk`, `betfair_sb_uk`, `boylesports`, `paddypower` —
and **keeps `betfair_ex_eu`**, so the sharpest price source survives and the averaged consensus in
`_parse_odds_event()` barely moves.

**Paid tier since 6 Aug 2026: 20,000 units/month ($30/mo)**, up from the 500 free tier. The original
key never picked the upgrade up (it kept reporting `used + remaining = 500`); the fix that worked was
**regenerating the key** in The Odds API dashboard, not waiting for propagation. The replacement key
(set in Railway and in local `.env` on 6 Aug 2026) reports the full 20,000 and bills normally.

**Football-only since 6 Aug 2026.** The tennis pipeline no longer calls The Odds API at all — no
picks-run enrichment, no closing-odds polling — so the entire allowance is football's. See section 7
for the tennis consequences. Single switch: `tennis_main.TENNIS_ODDS_API_ENABLED = False`.

A **hard stop** (`odds_budget_exhausted()`) halts *all* closing-odds polling below the reserve, so
the quota can never hit zero mid-month and take the picks-run odds enrichment down with it —
enrichment produces live value flags, so polling is what yields. The reserve is **derived from the
limit the API reports**, not hardcoded: `odds_api_reserve()` returns
`max(50, 0.10 × reported_limit)` → 2,000 units on the paid tier, 50 on the free one. That matters
operationally: a key that has not yet picked up a plan change still gets a sane stop instead of
either halting polling outright (hardcoded 2,000 against a 500 key) or failing to protect anything.
The daily summary prints an explicit ❗ when the reported tier is smaller than `ODDS_API_MONTHLY_LIMIT`.

The check uses `GET /v4/sports`, which is **free** (verified: it does not move `x-requests-used`),
and it **fails open** — an unreadable quota must not silently disable polling for a month. The daily
summary warns below 25% remaining.

**Budget at the current caps** (3 units/call), tennis contributing zero. Re-costed 13 Aug 2026 —
the previous figure understated enrichment because it predated two changes on that date:

| Source | Requests/day | Units/day |
|---|---|---|
| Football closing-odds polling (self-imposed cap) | 60 | 180 |
| Football picks enrichment | 0-10 | **0-30** |
| Opus 5 shadow enrichment (separate pick set) | 0-10 | **0-30** |
| **Worst case** | 80 | **240 ≈ 7,300/month, ~36% of the tier** |

**Enrichment is one billed `/odds` call per COMPETITION, not per pick** (15 Aug 2026). `/odds`
returns every event in a competition in one 3-unit request, so `enrich_picks_with_real_odds` fetches
once per competition and matches each pick client-side — the same shape `closing_odds.py` has always
had. Its cache used to be keyed `(home, away, league)`, i.e. per fixture, which bought the identical
league-wide response once per pick.

That re-key is why the table above still holds after the per-league cap took a busy run from 10 picks
to 30+: the picks-run cost now scales with the number of **competitions** with fixtures (10 at the
absolute most, and 4.3 on the measured average day), not with the pick count. Under the old
per-fixture key the same change would have taken enrichment to ~100 requests / ~300 units a day and
the worst case to ~510 units/day ≈ 15,500/month ≈ 78% of the tier. The Opus shadow row is unaffected —
it makes its own enrichment pass over its own (still ≤10) picks.

Live meter on 13 Aug 2026: **150 units used of 20,000** since the 6 Aug reset — actual burn sits far
under the worst case, because the 60-request polling cap is rarely reached. (Before tennis was
switched off on 6 Aug 2026 the same caps projected 351 units/day ≈ 10,900/month, ~54% — tennis
polling 40/day + tennis enrichment 12/day were the difference.)

> ⚠️ The 25% warning is a *fraction of the tier*, so it rescales automatically — but at 20,000 units
> that is 5,000 remaining, which a healthy month reaches around day 17. Expect it to fire routinely;
> it is now a calendar artefact rather than a signal. Replacing it with a burn-rate projection
> (warn when projected month-end usage exceeds the limit) is the real fix and is **not yet done**.

### Form & H2H enrichment (added 29 Jun 2026 — **dead on arrival, repaired 14 Aug 2026**)

Before calling Claude, `enrich_with_context()` adds per-fixture `home_form` /
`away_form` (W/D/L, oldest → newest), `home_recent` / `away_recent` (scorelines
with H/A venue), `h2h` (recent meetings with dates and scores) and `h2h_record`
(the all-time tally, labelled by team name). Data is injected into the JSON
payload so Claude can weigh recent form.

**It produced nothing at all for its first 46 days.** Both endpoints it called
answered `404 Endpoint does not exist` — `football-get-team-matches` has no
equivalent on this host under any name, and the H2H route is really
`football-get-head-to-head`. The failures were logged at `log.debug` while
`basicConfig` sets `INFO`, so nothing was ever emitted, and the summary line
printed a healthy-looking `home=N/A away=N/A h2h=0` at INFO. Confirmed by three
independent lines: the strings were introduced in `4077b69` and never edited;
every recorded run 5-14 Aug billed 37-100 input tokens per fixture where
enrichment costs ~505; and of 64 archived Discord pick embeds none cite form or
H2H while three (14, 15, 19 Jul) explicitly say the data was unavailable. So
**every football pick from 29 Jun to 14 Aug 2026 was made on team names alone** —
which is what the calibration/edge/CLV series over that period actually measures.

How the two feeds work now:

| | Source | Cost |
|---|---|---|
| Form | `football-get-matches-by-date` walked backwards from yesterday, capped at `FORM_LOOKBACK_DAYS = 35`, stopping early once every team has `FORM_MATCHES = 5` | 1 call per **date**, shared by every team in the pool — 35 on a cold start, **1/day** thereafter |
| H2H | `football-get-head-to-head?eventid=` per fixture | 1 call per fixture, cached by event id; the 48-hour window offers each fixture twice, so only its first appearance pays |

Projected RapidAPI football cost: **40-73 calls on a cold-start day** (35 + pool
size) and **3-20/day in steady state**. A cold start is a *deploy*, not a
calendar day — the caches live for the process lifetime and Railway redeploys on
every push to `main`. Worst case (a redeploy every day plus 38-fixture pools) is
~2,200/month, **11% of the 20,000 tier**; realistic steady state is under 3%.
Measured baseline before this change was ~47 calls/day.

- **The H2H payload sits under `response.lineup`, not `response.matches`** like
  every other endpoint here, and its rows carry their own shape: names under
  `home.name` (not `longName`) and the score as one `status.scoreStr` string.
  `_summarize_h2h_match` exists for exactly that reason — do not point
  `_summarize_match` at these rows. The list also contains future fixtures, so
  it is filtered on `status.finished` first.
- `lineup.summary` is `[home wins, draws, away wins]` **oriented on the queried
  fixture** — verified against Cercle Brugge vs St.Truiden, where `[14, 6, 7]`
  reproduced the home side's W14/D6/L7 across all 27 played meetings. It is
  emitted with team names spelled out so the orientation cannot be misread.
- `_day_feed_cache` is permanent (past results never change) and is deliberately
  **not** `auto_results._matches_cache`, which wraps the same endpoint behind a
  30-minute TTL because it settles in-progress fixtures. Same endpoint, opposite
  lifetime.
- A form string may hold fewer than 5 results, or none — near a season start a
  club genuinely has not played five matches. Measured on the 14 Aug pool: 35
  days gave 5 matches for 6 of 16 teams and 3+ for 11 of 16, and widening to 42
  recovered one more, so the limit is the calendar rather than the window.
  SYSTEM_PROMPT tells Claude to read the field as "up to 5" and to lower
  conviction rather than invent form it was not given.
- **Failures are logged at WARNING, never below**, a fixture that retrieved
  nothing logs `Context EMPTY` instead of a success line, and a run where
  nothing at all was retrieved logs at ERROR. This is the guard against a repeat:
  the original bug was invisible precisely because its only log statement was
  the one that made it look fine.
- The three `time.sleep(1)` calls per fixture were removed with the rewrite —
  they paced requests that were failing anyway, costing ~23s per run for no
  data. Verified safe: 10 back-to-back calls return 200 with no 429.

### Asian Handicap half results (added)
- Quarter-line handicaps (±0.25, ±0.75, ±1.25, ±1.75 …) are detected automatically
- Each quarter line is split into its two component half-lines and evaluated separately
- Combined result: WIN+VOID → HALF WIN, VOID+LOSS → HALF LOSS
- P&L: HALF WIN = `+0.50 × (odds − 1)` units; HALF LOSS = `−0.50` units
- HALF WIN / HALF LOSS flow through the entire stack: Sheets, formatting, Summary, notifications

### Extra-time settlement: single match vs two-legged tie (fixed 12 Aug 2026)

Unscoped picks settle on **90 minutes** (the bookmaker default; knockout picks may
override with a `(90 min)` / `(Full-Time incl. ET/Pens)` suffix since 12 Jul 2026).
The API returns only the final score, so when a match runs past 90 minutes the
regulation score is unknown and `evaluate_pick()` derives what it can. What is
derivable depends on **why** it went past 90:

| | Single match | Two-legged tie |
|---|---|---|
| Why ET happened | scores level at 90' | **aggregate** level at 90' |
| 90' goal difference | guaranteed `0` | **derived from the aggregate** (below) |
| Match Winner / AH / Double Chance | settled off the derived margin | settled off the derived margin |
| BTTS | `LOSS`/`WIN` if a side was scoreless over 120' (goals are monotonic), else `PENDING` | same |
| Over/Under | `PENDING` unless the largest reachable 90' total is under the line | same |

Detected via `status.aggregatedStr`, which is present only on two-legged ties and
is passed to `evaluate_pick(..., two_legged=True, aggregate=...)`.

#### The 90-minute margin is derivable — the 90-minute SCORE is not (1 Sep 2026)

**The API publishes no period scores anywhere.** Checked exhaustively 1 Sep 2026,
so nobody has to check again: `football-get-match-detail` returns 792 bytes of
metadata with **no score of any kind** (matchId, round, team colours,
`parentLeagueId`, kickoff, started/finished); `football-get-match-score` returns
only the **final** score; and `football-get-match-events`, `-statistics`,
`-stats`, `-timeline`, `-lineups`, `-shotmap`, `-momentum`, `-goals`, `-period`,
`-halftime`, `-summary`, `-info`, `-h2h`, `-player-stats`, `-odds`,
`-live-matches` and `-list-events` **all 404 — they do not exist on this host**.
`status.halfs` carries period START TIMESTAMPS, never period scores.

The 90-minute **margin**, however, follows from arithmetic. Extra time in a
two-legged tie is triggered by the aggregate being level at the end of 90 minutes
of the second leg, and each side's first-leg goals are `aggregate − this leg's
goals`, so:

```
h90 − a90 = (agg_away − final_away) − (agg_home − final_home)
```

That settles every market paying on the margin — Match Winner, Double Chance and
Asian Handicap — even though the score itself stays unknown. LASK 5-1 Celtic
(agg 5-4) yields **+3**: LASK led by exactly three at 90', whether the night was
3-0 or 4-1. Validated against **all 15** finished two-legged AET/shootout ties in
the 19-27 Aug feeds, UEFA and CONMEBOL alike.

Over/Under and BTTS stay `PENDING` when ambiguous: the margin does not pin the
TOTAL. The Over/Under bound is now computed from the margin
(`2 × min(final_away, final_home − reg_gd) + reg_gd`), which reduces to the old
`2 × min(home, away)` for a single match and is strictly tighter than the final
total for a two-legged one — so it settles `Under` in more cases without ever
guessing.

`_regulation_goal_difference()` returns `None` — leaving the pick `PENDING` —
when the aggregate is missing, unparseable, **below this leg's score for either
side** (bad data, or an aggregate published the other way round), or implies a
margin unreachable inside the final score. A margin that cannot be trusted is
never settled.

#### A shootout with no extra time settles exactly (1 Sep 2026)

`extra_time` now means **extra time was actually played**, read from
`status.halfs.firstExtraHalfStarted`, not "the tie needed separating". A shootout
straight after 90 minutes — the CONMEBOL format, and many domestic cups — leaves
the published score EQUAL to the 90-minute score, so *every* market settles
exactly. Treating "went to penalties" as "went past 90" sent all of those to
manual settlement for nothing. When the `halfs` block is missing entirely on a
shootout the code assumes extra time was played, which costs a manual settlement
rather than risking a bet settled off the wrong score.

Before this fix a two-legged tie was settled as though regulation had ended level.
A team can lead 1-0 on the night and still play extra time because the aggregate is
level, so a correct Match Winner pick could be booked as LOSS. The Over/Under case
was worse: the `2 × min` bound is *tighter* than the final total and is only valid
under the draw inference, so a 3-0 AET tie would have settled `Under 2.5` as WIN
even though all three goals may have come inside 90 minutes.

**Audit of the full history (12 Aug 2026):** all 207 settled picks were matched to
their API fixture (no gaps); 27 ran past 90 minutes. 24 were single matches (inference valid, unaffected). **3 were two-legged
— r170, r202, r203 — and none was mis-settled**: r170 (Gent vs LNZ Cherkasy, Over
2.5, pens 0-0) still settles LOSS under the corrected bound; r202 was settled by
hand on 12 Aug after external verification; r203 (CSKA 1948 vs Panathinaikos, MW
Panathinaikos Win, AET 1-2, agg 2-3) is correct as LOSS because leg 1 finished
**1-1** — a level first leg forces the second leg to be level at 90' too, which
collapses the two-legged case back to the single-match one.

*Update 1 Sep 2026:* the margin derivation reproduces that hand-analysis
automatically — `(3−2) − (2−1) = 0`, level at 90', so `Panathinaikos Win` settles
LOSS — and r203 was settled by the normal path on 1 Sep after sitting PENDING
since 10 Aug. The 12 Aug trade ("return PENDING rather than guess") is no longer
a trade for these markets: the margin is not a guess.

⚠️ The same audit found three **single-match** rows that *are* mis-settled, from
before the 12 Jul 2026 ET rules existed — see Known Limitations.

#### Two-legged audit of every settled pick (13 Aug 2026)

Run to confirm the 12 Aug fix landed and to find rows settled *before* it by the invalid inference.
Method: for every settled pick in a UEFA competition (the only ones that can be two-legged), re-fetch
the fixture from the API, read `status.aggregatedStr` and `status.reason`, and re-run the **current**
`evaluate_pick` against the stored Result.

- 207 settled picks total; **31** in UEFA competitions.
- **15** carried an aggregate score — but 12 of those finished in normal time (`FT`), so the extra-time
  inference never applied and they settled on the plain final score. Correct either way.
- **3** actually went past 90 minutes in a two-legged tie. Those are the only rows the bug could reach:

| Row | Date | Fixture | Pick | Leg / agg | Stored | Current logic | Verdict |
|---|---|---|---|---|---|---|---|
| 170 | 30 Jul | Gent vs LNZ Cherkasy | Over 2.5 Goals | 0-0 / 0-0, pens | LOSS | LOSS | **OK** — the monotonic bound settles it: the 120' total was 0, so the 90' total cannot have reached 2.5 |
| 202 | 10 Aug | Bodø/Glimt vs Union St.Gilloise | BTTS Yes | 3-2 / 6-5, AET | WIN (+0.75u) | PENDING | **Review** — see below |
| 203 | 10 Aug | CSKA 1948 vs Panathinaikos | Match Winner: Panathinaikos Win | 1-2 / 2-3, AET | LOSS (−1.00u) | PENDING | **Mis-settled** |

**Row 203 is a genuine mis-settlement.** It was auto-settled before the 12 Aug fix, when
`evaluate_pick` still inferred a regulation draw from extra time. That inference made the away pick a
LOSS mechanically. In reality the aggregate was level at 90' of the second leg (that is *why* extra
time happened) and the 1-2 leg score includes ET goals, so the 90-minute result — which is what an
unscoped pick settles on — is genuinely underivable. It should read PENDING and be settled by hand.

**Row 202 needs your confirmation rather than a verdict from me.** The current evaluator returns
PENDING (both sides scored across 120', but whether both scored inside 90' cannot be derived). It
currently reads WIN. This is the *same pick* the PENDING-alerting change of 12 Aug was written for —
`auto_results.py`'s comment names "the 10 Aug Bodø/Glimt BTTS pick" as the row that sat unsettled and
stranded. That strongly suggests the WIN was entered **manually** by someone who checked the real
90-minute score, in which case it is correct and should stay. I cannot distinguish a manual settlement
from an automatic one by reading the sheet, so it is flagged, not changed.

**Row 203 was reverted to PENDING on 13 Aug 2026; row 202 was deliberately left alone.** Reverting a
settlement that is probably correct would itself be a regression, and the manual-settlement inference
for row 202 is strong enough to leave standing.

Only the Result and Profit/Loss cells were cleared — odds, probability, league, kickoff and tier all
remain, so it is an ordinary unsettled pick again. Effect on the Core book:

| | Before | After |
|---|---|---|
| Core settled | 178 | **177** |
| Core W / L | 117 / 61 | **117 / 60** |
| Core win rate | 65.7% | **66.1%** |
| **Core settled P&L** | **+26.15u** | **+27.15u** |
| calibration sample | 81 | **80** |

⚠️ **Row 203 needs settling by hand and nothing will chase it.** The live 30-minute job calls
`run_auto_results(2)` — a **2-day** lookback — and the row is dated 10 Aug, so it already sits outside
that window and will not be re-checked. Even inside it, `evaluate_pick` correctly returns PENDING for
this fixture, so it would raise an alert rather than settle. Settle it with the real 90-minute result:

```
python update_result.py "CSKA 1948 vs Panathinaikos" "Panathinaikos Win" WIN|LOSS
```

Everything settled from **12 Aug onward** goes through the fixed evaluator, so the 20 Aug Europa and
Conference return legs — the first large block of two-legged second legs since the fix — settle
correctly: an aggregate-triggered extra time returns PENDING and raises a PENDING alert rather than
booking a false LOSS.

### Fixture name matching: transliteration and reversed sides (fixed 1 Sep 2026)

Three rows never settled and stranded past the 7-day lookback because
`_find_api_match` could not resolve them. The pick's match string is echoed back
through Claude, which silently rewrites names:

| Row | Sheet says | API says | Cause |
|---|---|---|---|
| 246 | `Egnatia vs Lillestrom` | `Egnatia vs Lillestrøm` | `ø` transliterated |
| 251 | `Nordsjaelland vs St. Gallen` | `Nordsjælland vs St. Gallen` | `æ` transliterated |
| 243 | `Bodø/Glimt vs NEC Nijmegen` | `NEC Nijmegen vs Bodø/Glimt` | home/away reversed |

`ø` and `æ` do **not** decompose under NFKD — Unicode treats them as distinct
letters, not accented forms — so stripping combining marks is not enough.
`_normalise_team()` transliterates them explicitly (plus `å ð þ đ ł ß œ ı`) before
the NFKD pass. Deliberately limited to case, diacritics and whitespace:
punctuation is left alone, because looser matching starts hitting genuinely
different clubs and a wrong fixture settles a real bet off someone else's result.

Reversed sides are tried **only after** the correct orientation has been
exhausted across every candidate date. In a two-legged tie both orientations
exist as real fixtures on different dates, so a greedy reversed match could
settle a pick against the wrong leg. Settlement then uses the API's own
home/away — scores, names and handicap sides all come from the matched fixture —
so a reversed match still settles correctly.

All three rows settled on 1 Sep 2026: r243 WIN (+0.70), r246 LOSS, r251 LOSS.

### PENDING alerting (added 12 Aug 2026)

A `PENDING` verdict means a human must settle the row via `update_result.py`. It
used to increment `stats['errors']` and nothing more, so a row could sit unsettled
until it aged out of the `LOOKBACK_DAYS` window and was stranded permanently — which
is what happened to the 10 Aug Bodø/Glimt BTTS pick (caught by hand at ~28h, one day
from being lost). Now `run_auto_results()` collects `stats['pending_alerts']` and
`run_all.py` posts each to **`results-cards`** (Discord-only):

- **first sighting** → `⏳ NEEDS MANUAL SETTLEMENT`, with the score, the match status
  (ET / pens / two-legged + aggregate), the reason it could not be settled, the sheet
  row, and the exact `update_result.py` command
- **still unsettled 24h after kickoff** (`PENDING_FOLLOWUP_HOURS`) → `🔁 STILL
  UNSETTLED after Nh`, plus a warning that it is about to leave the lookback window

De-duplicated per `(match, bet_type, pick)` so the 30-minute poll does not repeat
itself. If a pick is first seen when already past 24h — a Railway restart clears the
in-process state, or the API resolved the fixture late — it emits the follow-up
variant once rather than both alerts 30 minutes apart. State is in-process only, so a
restart can re-send one alert; that is the safe direction to fail.

### Real odds & value flagging (added)
- `fetch_real_odds()` pulls live h2h/totals/spreads (Asian handicap) odds from The Odds API per fixture
- Each Claude pick is matched to its real market outcome; a pick is flagged as "value" only when Claude's implied probability exceeds the market's by ≥5 percentage points
- Both Claude's estimated odds and the real market odds are shown side by side on the picks card and the pick embed
- If `ODDS_API_KEY` is missing, the fixture/market can't be matched, or the API call fails, the pick silently falls back to Claude-only odds (no crash, no message)

### Cross-day duplicate logging (fixed 13 Aug 2026)

**A fixture inside the 48-hour window was picked and logged on two consecutive days, and one match
then settled both rows — booking its P&L twice and counting it twice in calibration.**

Cause, confirmed in code:

| Layer | Dedupe key | Why it missed this |
|---|---|---|
| `main.analyse_with_claude` | `(match, bet_type)` | **within a single run only** — it never sees yesterday's picks. (Since 15 Aug 2026 it is applied per competition response *and* once across the merged list, so the key's reach within a run is unchanged.) |
| `excel_tracker.log_to_excel` | `(date, match, bet_type, pick)` | **date-scoped** — a repeat on the next day is a different key |
| `tracker.log_pick` (picks.db) | `(date, match, bet_type, pick, session)` | same, date-scoped |

`fetch_upcoming_matches` pulls today **and** tomorrow, so a fixture kicking off tomorrow is offered to
Claude on both days. Nothing compared the new pick against already-logged, still-unsettled rows.

**Fix:** `log_to_excel` and `opus_tracker.log_opus_pick` now skip when **any unsettled row exists for
that fixture**, regardless of bet type. **One fixture carries at most one open bet.**

The key was widened twice, because each narrower version leaked:

| Key | What it still let through |
|---|---|
| `(date, match, bet_type, pick)` — original | everything below; date-scoped, so any next-day repeat |
| `(match, bet_type, pick)` | Canada vs Morocco BTTS logged `Yes` on 3 Jul and `No` on 4 Jul — opposite sides of one market |
| `(match, bet_type)` | two different bet types on one fixture — two stakes riding on a single result |
| **`(match)` — current** | — |

The last case matters beyond bookkeeping: nothing downstream treats two bets on one fixture as
correlated, so the sheet's P&L and calibration both read them as independent samples when they are
not. Only unsettled rows block; once a row has a Result the fixture is over, so a later row on it is a
data problem worth seeing rather than hiding.

**This is a logging-level fix only.** Picks cards and the Discord embeds all render
the unfiltered `picks` list, so a repeated fixture still appears on the card exactly as before — only
the sheet write is suppressed.

**Measured impact at the time of the fix** (223 rows, 30 Jun - 13 Aug 2026):

| Key | Duplicated groups | Redundant rows | Double-counted P&L |
|---|---|---|---|
| `(match, bet_type, pick)` — exact | 15 | 15 | +3.35u |
| **`(match, bet_type)` — the fix's key** | **16** | **16** | **+4.20u** |
| `(match)` — any bet on the same fixture | 25 | 32 | +5.42u |

At the fix's key: **+4.20u double-counted against a reported Core settled P&L of +31.57u — a 13.3%
overstatement**, from 16 of 223 rows (7.2%). Corrected figure would be **+27.37u**.

**Not covered by this fix, by design:** two *different* bet types on one fixture (e.g. Midtjylland vs
Bohemian logged `Match Winner` on 12 Aug and `Asian Handicap` on 13 Aug). Those are genuinely distinct
bets rather than a duplicate, but they do concentrate two stakes on one result — the `(match)` row
above quantifies that at a further 9 groups / 16 rows / +1.22u beyond the fix's key.

#### Retroactive correction (13 Aug 2026) — `Pick Tier = Duplicate`

History was then corrected to match the go-forward rule: for every fixture logged on more than one
date, the **first** pick keeps its tier and every later one is tagged `Pick Tier = Duplicate`.

**Nothing was deleted.** A `Duplicate` row is neither Core nor Extended, so `_core_rows` and
`_extended_rows` both drop it and it falls out of every metric — running total, bankroll, Summary,
calibration, edge, CLV, weekly card — with no new filter anywhere. The bet, its odds and its settled
result stay on the sheet, and reversing an exclusion is editing one cell back to `Core`.

| | Before | After |
|---|---|---|
| Rows on the Picks tab | 223 | **223** (none deleted) |
| Core picks / settled | 217 / 207 | **185 / 178** |
| Core win rate | 67.0% | **65.7%** |
| **Core settled P&L** | **+31.57u** | **+26.15u** |
| calibration sample | 98 | **81** |
| edge sample | 34 | **26** |
| CLV sample | 27 | **20** |

The 32 excluded rows went **21W / 7L, 75.0%, +5.42u** — a materially better record than the Core book
they were inflating (65.7%). That is the shape you would expect from double-counting: a fixture the
model liked enough to pick twice is one it was more often right about, so the duplicates skewed the
headline upward rather than averaging out. The Summary tab now carries a `Duplicate` row in its Pick
Tier Breakdown so the exclusion stays visible rather than silently missing.

Three of the 32 were still PENDING when tagged (Shelbourne vs Ajax, Dinamo Minsk vs Braga, Midtjylland
vs Bohemian, all 13 Aug). `get_pending_picks_rows` has no tier filter, so they will still **settle**
normally tonight and carry a real Result and P&L — they simply will not enter Core. That is deliberate:
settle for the record, exclude from the metrics.

> Separately noted on 13 Aug 2026: the Picks tab's `A1` header cell had been blanked to `' '` instead
> of `Date`. Harmless — no code indexes that column by name (every reader uses positional index 0) and
> the row data was intact — but restored to `Date` so the header matches `PICKS_HEADERS` again. Cause
> not established; `init_excel()` only appends missing *trailing* headers, so it would not have
> repaired this on its own.

### Canada vs Morocco: the bot bet both sides of one market (3-4 Jul 2026)

Surfaced by the duplicate audit and worth its own entry, because the duplication is the *symptom* and
the calibration signal underneath it is the finding.

**What was logged — three rows on one fixture, not two:**

| Date | Bet type | Pick | Odds | Claude Prob | Result | P&L |
|---|---|---|---|---|---|---|
| 03 Jul | Both Teams to Score | **Yes** | 2.10 | **55%** | LOSS | −1.00u |
| 04 Jul | Match Winner | Morocco Win | 2.30 | 52% | WIN | +1.30u |
| 04 Jul | Both Teams to Score | **No** | 1.85 | **55%** | WIN | +0.85u |

**The mechanism.** Every daily run is independent. `analyse_with_claude` receives only the fixture
payload — never the picks already logged — and nothing in `SYSTEM_PROMPT` or the enriched context
mentions existing positions. The 48-hour window put this fixture in front of the model on both days,
and the sheet guard was date-scoped, so the second day's contradictory pick was written as if it were
a fresh, independent bet. No component was in a position to notice.

**The part that isn't just a plumbing bug.** The model assigned **55% to `Yes` on one day and 55% to
`No` on the next**, at the same `Medium` confidence. Those are mutually exclusive outcomes of one
binary market: the two stated probabilities sum to 110%. This was not a considered update on new
information — the form and H2H context were unchanged between runs, and the stated probability did not
move at all. It is the same fixture being judged twice with no memory of the first judgement, and the
number attached to whichever side came out.

**What it cost.** Holding both sides at 2.10 and 1.85 is a 101.7% book: the position could only return
+0.10u (if BTTS hit) or −0.15u (if it didn't). It didn't, so the pair returned **−0.15u** — a
directional bet accidentally converted into a flat position that pays the vig either way. Small in
isolation; the point is that no part of the system could distinguish it from two genuine edges.

**Is it prevented now?** At the logging layer, yes — the match-level guard means the 4 Jul run would
find an unsettled Canada vs Morocco row and skip *both* of that day's picks before they reached the
sheet. Two residual gaps, stated plainly:

1. **The guard is at logging level by design, so the model still makes the contradictory pick**, and it
   still reaches the picks card, Telegram and Discord. A reader of the card on 4 Jul would have seen
   `BTTS No` on a fixture the previous day's card called `BTTS Yes`, with nothing marking the reversal.
2. **Nothing feeds prior picks back into the prompt**, so the model has no way to be consistent across
   runs even in principle. Fixing *that* means putting open positions into the payload — a real change
   to the analysis contract, not a logging guard, and it is not done.

A sweep for the same pattern across all 223 rows found only **two** fixtures with multiple picks on one
market: this one, and Argentina vs Algeria (`Argentina -1.5` and `Argentina -2.5` — the same side at
two lines, correlated rather than contradictory). So the contradiction is rare, not systemic — but it
was undetectable before this audit, which is the reason to keep the `Duplicate` tag visible.

### Ranked picks and the Core/Extended split (13 Aug 2026 — cap superseded 15 Aug, see below)

**Pick volume doubles from this date: 5 per run → up to 10.** Claude now returns picks *ranked*
best-to-worst — rank 1 the highest-conviction bet, rank 10 the weakest it would still genuinely
place — and `MAX_PICKS_PER_RUN` rose 5 → 10 (`CORE_PICKS_PER_RUN = 5` marks the tier boundary).

> The rank ranges in the table below describe **13-15 Aug 2026 only**. From 15 Aug the cap is per
> competition and tier is decided by an explicit global selection step, not by a rank number — see
> "Per-league picks and global Core selection". Every other column in the table still holds exactly.

| Tier | Ranks (13-15 Aug 2026) | Sheet / settlement | Card | Discord | Calibration / edge / CLV | Running total, Bankroll, Summary totals |
|---|---|---|---|---|---|---|
| **Core** | 1-5 | logged + settled | **yes** | league channel | **yes** | **yes** |
| **Extended** | 6-10 | logged + settled | no | league channel, labelled `· EXTENDED #n` | **no** | **no** |

**The Core baseline is unaffected.** This is the whole point of the design, and it holds in the
strongest sense available: not one historical row was rewritten. Core keeps its own card, Telegram
post, running total, bankroll, Summary figures and every calibration/edge/CLV report, so the series
running unbroken since **30 Jun 2026** stays directly comparable through the October read and beyond.
Verified on the day: with 217 rows on the sheet, the three reports returned identical sample sizes
(calibration 98, edge 34, CLV 27) with the tier filter active and bypassed — it is provably a no-op
on history.

**Why a column, not a separate tab.** The alternative was an "Extended Picks" tab. That would have
meant rebuilding the settlement path the way the Tennis tab did — `tennis_excel_tracker.py` duplicates
~15 functions of `excel_tracker.py` (reader, writer, finalizer, running total, its own Summary tab).
`get_pending_picks_rows()` selects purely on "Result empty + inside the lookback window" with no
league, tier or source filter, so Extended rows in the Picks tab settle through the **existing,
already-hardened path with zero new settlement code**, and inherit the PENDING alerting added on
12 Aug for free. A separate tab strands picks silently if any one of the new reader, finalizer or
scheduler wiring is wrong; a missed report filter is merely visible and reversible. The column trades
an invisible failure for a visible one.

**Blank means Core, permanently.** `_row_tier()` reads a missing column, a short row or an empty cell
as Core, which is why the 217 pre-existing rows needed no backfill. **Never "migrate" old rows by
filling this column in** — leaving them blank is what guarantees the baseline cannot drift.

**One filter, seven sites.** `excel_tracker._core_rows()` is the single Core filter; every Core
aggregation routes through it rather than repeating an inline tier check:

| Site | Why it must be Core-only |
|---|---|
| `_recalculate_running_total` | cols I/J — the headline P&L and bankroll series |
| `_refresh_summary` | Summary tab, which also feeds the picks-card footer win rate |
| `calibration._settled_prob_rows` | feeds `calibration_report()` **and** `edge_report()` |
| `calibration.clv_report` | closing odds ARE collected for Extended, but never scored here |
| `get_weekly_data` | weekly card + `update_result.py` recap |
| `get_bet_type_breakdown` | weekly card breakdown (and `get_overall_win_rate` via it) |
| `get_picks_for_date` | results card + per-pick result notifications |

Deliberately **not** filtered: `get_pending_picks_rows` (both tiers must settle) and
`get_unsettled_picks_with_kickoff` (both tiers collect closing odds).

**Tier comparison.** `get_tier_breakdown()` returns picks/settled/W/L/win rate/P&L per tier, and a
`PICK TIER BREAKDOWN` block is appended to the Summary tab. This is the one reporting path allowed to
see both tiers — comparing them is its purpose.

**Never pad.** The prompt states that returning fewer than the cap is correct when fewer genuine value
bets exist, that there is no penalty for a short list, and that a padded pick is worse than a missing
one because every returned pick is staked for real. A short list logs at INFO, never as a warning —
treating it as a fault is what would pressure the list back toward filler. *(From 15 Aug 2026 this is
per competition, and most competitions returning 0-3 picks against an allowance of 10 is the expected
shape, not a fault. The rule binds the Core selection step too: Core is capped at 5, never padded up
to 5.)*

**Rank is assigned from array position, not from Claude's `rank` field.** Dedup and the cap already
operate on position, so a returned number could tier a pick differently from where it actually sits
(or collide after a duplicate is dropped). Claude's *ordering* is respected; its *numbering* is not
load-bearing. Verified with a duplicate injected at rank 1: it was removed and ranks stayed dense 1-10.
*(From 15 Aug 2026 that position is `league_rank` — dense 1-10 **within one competition's response**,
with no global sequence at all. Tier no longer follows from it: it is set by the global selection
step, and Core carries a separate `rank` 1-5 while Extended carries none.)*

#### ⚠️ Prompt regime change — forward series only (13 Aug 2026)

Ranks 1-5 are now selected by a **different prompt** than before this date. `SYSTEM_PROMPT` asks for
a ranked list of up to 10 rather than "the top 5", and Claude reasoning about a 10-deep ranking may
order its best five differently than when asked for five outright.

- **Historical data is untouched**, so the 30 Jun → 13 Aug 2026 baseline is unaffected as *stored data*.
- **Going forward, "Core" is not produced by a byte-identical process.** When reading the October
  calibration report, treat 13 Aug 2026 as a soft regime boundary: a shift in Core's measured
  behaviour after this date could be the prompt change rather than model drift. Note it before
  concluding anything about calibration.
- The alternative — two separate Claude calls to keep the Core prompt byte-identical — was rejected:
  it doubles token cost and can return the same fixture in both tiers. *(Superseded 15 Aug 2026: the
  run now makes one call per competition plus a selection call. The duplicate-fixture objection does
  not apply to that shape — a fixture belongs to exactly one competition — and the cost was measured
  rather than assumed. See below.)*

### Per-league picks and global Core selection (15 Aug 2026)

**The cap moved from 10 per run to 10 per competition per run.** `MAX_PICKS_PER_LEAGUE = 10`
replaces `MAX_PICKS_PER_RUN`; `CORE_PICKS_PER_RUN = 5` is unchanged and still **global**. A busy slate
therefore produces 30+ picks where it produced at most 10 — measured on the real fixture feed, the
next 21 run windows average 4.3 competitions and 21 fixtures, peaking at 8 competitions / 47 fixtures
on 27 Aug.

**Why the shape changed.** One call over the whole slate makes competitions compete for ten slots, so
a 24-fixture Conference League qualifying round and a 4-fixture La Liga matchday were ranked against
each other for space. Each competition now gets its own call and is judged on its own merits.

**How a run works now** (`main.analyse_with_claude`):

1. **One call per competition** — `_analyse_one_league()` sends `LEAGUE_SYSTEM_PROMPT` plus that
   competition's fixtures only, and returns up to 10 conviction-ranked picks. `league_rank` is
   assigned from array position *within that competition*, and the pick's `league` is **overwritten**
   with the competition actually sent (the call was handed one league, so the name is known for
   certain — and it drives Discord routing, the Odds API sport key and the sheet's League column).
2. **Merge + dedupe** on `(match, bet_type)`, restoring the cross-competition dedupe the single call
   used to give for free. It should never fire — a fixture belongs to one competition — but the
   leagueId sets `partition_fixtures` matches on are discovered at runtime.
3. **Global Core selection** — `_select_core_picks()` marks the day's best 5 across every
   competition. Above 5 candidates it asks Claude once more (`CORE_SELECTION_PROMPT`), passing the
   candidate picks only — no fixtures, no form, no H2H — and taking back **ids**, so that step can
   re-rank but can never reword a pick, move a price or invent a bet. At 5 or fewer candidates it
   skips the call entirely and orders deterministically.
4. **Tier assignment** — the 5 selected are Core with `rank` 1-5; everything else is Extended with
   `rank = None`. **Tier is never inferred from a rank number any more:** a league's rank-1 pick is
   Extended whenever it loses the global cut.

| Tier | How it is chosen | Volume | Sheet / settlement | Card | Discord | Calibration / edge / CLV | Running total, Bankroll, Summary totals |
|---|---|---|---|---|---|---|---|
| **Core** | best 5 on the whole slate, selected globally each run | exactly 5/day (fewer if the slate offers fewer) | logged + settled | **yes** | league channel | **yes** | **yes** |
| **Extended** | every other returned pick | up to 10 per competition per day | logged + settled | no | league channel, labelled `· EXTENDED · league rank n` | **no** | **no** |

**Core is unchanged in every respect that matters.** Still 5, still global, still the only tier
reaching the card, the running total, the bankroll, the Summary figures and every
calibration/edge/CLV report — `excel_tracker._core_rows()` remains the single filter. Core picks are
also logged **first**, so where a fixture somehow carries two picks the sheet's one-open-bet-per-
fixture guard resolves it in Core's favour.

**Never pad — more important here, not less.** Ten leagues each allowed 10 picks is an allowance, not
a target. `LEAGUE_SYSTEM_PROMPT` states that an empty list for a competition is a normal and expected
answer, that a short list costs nothing, and that the model is judged on the strike rate of what it
returns rather than on how much of the allowance it used; it also tells the model explicitly that it
is seeing one competition in isolation and must not measure it against how many picks a competition
"ought" to produce. Confirmed on the first live run: a 12-fixture, 2-competition slate returned 8
picks against an allowance of 20 (La Liga 3, Jupiler Pro League 5).

**Cost — measured, not estimated.** The system prompt is 1,765 tokens and the enriched payload runs
~655 tokens per fixture (`count_tokens`, 15 Aug 2026). Per-league fan-out repeats the system prompt
per call, so it is **cached**: identical across every call in a run and only seconds apart, the first
call writes it at 1.25× and the rest read it at 0.1×, and `usage_tracker` already prices both. On
single-competition days the cache is skipped, since the write premium would be pure loss.

| Slate | Before (1 call) | After (N+1 calls) | Picks |
|---|---|---|---|
| 15 Aug live run — 2 competitions, 12 fixtures | $0.047 | **$0.060** (measured: 3 calls, 1,877 tokens cached and re-read) | 6-8 |
| Typical — 4-5 competitions, ~21 fixtures | ~$0.08 | ~$0.11 | 10-12 |
| Peak — 8 competitions, 47 fixtures | ~$0.12 | ~$0.20 | 20-25 |
| Cap-bound worst case — 8 competitions × 10 picks | — | $0.36 in one run | 80 |

**≈$1.90 → ≈$2.90 a month** at the measured slate mix (+~50%, ~+$1). Output tokens, not input, are
what scale: each pick costs ~175 output tokens, so the bill tracks how many bets the model actually
finds. `max_tokens` per league call rose 2,048 → 4,096 — a 10-pick response measured 1,999 tokens on
13 Aug 2026, i.e. one verbose run away from truncating into an unparseable response.

**Odds API cost went DOWN, despite 3× the picks.** `enrich_picks_with_real_odds` now caches by
**competition** instead of by fixture: `/odds` returns every event in a competition in one billed
3-unit request, so one fetch serves all of that competition's picks and the rest is matched
client-side (exactly what `closing_odds.py` already did). Keyed per fixture it bought the same
league-wide response once per pick — harmless at 5 picks/day, but 10 picks in one competition would
have meant 10 identical requests and 30 units for data already in hand. Picks-run enrichment is now
**3 units per competition** (≤30/day at 10 competitions) rather than 3 units per pick (up to 240/day).

**Failure isolation.** A competition whose call fails costs that competition only: it is logged at
ERROR, skipped, and the rest of the slate is still analysed and delivered. Only a run where *every*
competition failed raises and fires the picks-failed Discord alert — one alert, never one per
league. If the Core selection call fails the run continues on a deterministic fallback order (each
competition's rank-1 pick first, best edge first), because that step decides ordering only, never
which bets exist.

**Two volume fixes that the pick count forced:**

- **Sheet writes are batched** (`excel_tracker.log_picks_batch`, `tracker.log_picks_batch`). Writing
  picks one at a time costs ~4 Sheets API calls each *and* repaints every row of the sheet per pick,
  so a 30-pick run would have made ~120 calls and 30 full repaints inside a couple of minutes — into
  Sheets' per-minute quota, where the failure mode is a pick delivered to Discord but missing from
  the sheet. The batch reads once, appends once and repaints once, with the identical duplicate
  guards (staged rows are checked against each other exactly as if already written). `log_to_excel`
  is unchanged for single-pick callers and now shares the guard and row builder with the batch path.
- **Discord sends are paced** at `DISCORD_PICK_SEND_DELAY = 1.0s`. A single competition can now post
  10 embeds back to back, and `send_to_discord` retries a 429 exactly once — an unpaced burst would
  spend that retry immediately and start dropping picks.

**The Opus 5 shadow deliberately did NOT follow.** It keeps one global call capped at 10, on its own
frozen constants (`OPUS_MAX_PICKS_PER_RUN` / `OPUS_CORE_PICKS_PER_RUN`) and the unchanged
`main.SYSTEM_PROMPT`, which is byte-identical to the pre-change prompt (verified by hash). Fanning it
out would take a side experiment from ~$4.29/month to roughly $17-25 at Opus pricing. The consequence
is recorded honestly below: the model is **no longer the only variable**.

#### ⚠️ Second prompt regime boundary — forward series only (15 Aug 2026)

Core is now produced by a **different mechanism** again: per-competition prompts plus a selection
call, instead of one cross-competition ranking. Historical rows are untouched, so the stored baseline
is unaffected — but when the October calibration report is read, treat **both 13 Aug and 15 Aug 2026**
as soft regime boundaries before concluding anything about model drift. The 14 Aug form/H2H repair is
a third. Extended also stops being comparable to Core as a sample: it accrues roughly 5× faster from
this date and spans every competition, so the Summary's tier breakdown compares "the globally
selected 5" against "everything else", not two adjacent rank bands.

### Sheets quota loss: six slates written to Discord, never to the sheet (fixed 1 Sep 2026)

**128 picks across 20, 22, 26, 27, 28 and 29 Aug 2026 were delivered to Discord and never
reached the Picks tab.** They will never settle, never book P&L, and never enter the
Summary, calibration, edge or CLV series. Found by a manual Discord-vs-sheet audit on
1 Sep 2026, not by any alert — nothing anywhere reported it.

**Cause.** `calculate_kelly_stake()` called `get_bet_type_breakdown()`, which reads the
ENTIRE Picks tab — one full-sheet read. `daily_picks_job` sized every pick in a loop, so
the run made **one full-sheet read per pick**. Google Sheets allows **60 reads per minute
per service account across the whole spreadsheet**, shared with the tennis tabs, the Opus
tab and the usage ledger. While a slate was ~10 picks this fit. The per-league cap
(15 Aug 2026) took slates to 20-30, the loop exhausted the minute's budget, and the
`log_picks_batch` read that ran immediately afterwards took the 429.

The failure was silent because every read in `excel_tracker` is wrapped in
`except Exception: log.error(...); return <neutral>`, and the caller logged
`log.info("Logged %d of %d pick(s)")`. A day that lost all 29 picks printed one INFO line
in the Railway logs and nothing else. The verbatim error, from the logs:

```
ERROR Sheets read failed: APIError: [429]: Quota exceeded for quota metric 'Read requests'
and limit 'Read requests per minute per user' of service 'sheets.googleapis.com'
INFO  Logged 0 of 29 pick(s) to the sheet
```

The read counts confirm it exactly: on 29 Aug (29 picks) nine reads 429'd — the last eight
Kelly reads plus the batch read — meaning the first 21 succeeded. Every day with ≥21 picks
failed; every day with ≤19 succeeded. No exceptions in 14 days.

**Fixes, in order of what actually matters:**

1. **The per-pick read is gone.** `calculate_kelly_stake(..., breakdown=None)` now takes a
   precomputed breakdown; `daily_picks_job` and `_run_now.py` read it **once per run** and
   pass it in. A 29-pick slate went from **29 full-sheet reads to 1**. This is the fix —
   the rest is defence in depth. Never move that read back inside the loop.
2. **Silence is no longer possible.** `log_picks_batch` returns `BatchLogResult(written,
   skipped, failed)` instead of a bare int. `skipped` is the duplicate guard doing its job;
   `failed` is a lost pick. Any non-zero `failed` — **partial or total** — fires
   `usage_tracker.alert_sheet_write_failure()` into the ops `usage` channel with the
   counts. A run where 25 of 29 landed used to look identical to a clean one.
3. **Retry with backoff.** `with_sheets_retry()` retries 429/5xx with exponential backoff
   and jitter (4 attempts, 20s base — the quota window is per minute). It **re-raises**
   when exhausted rather than converting failure to a silent success.
4. **Interval jobs phase-shifted** in `run_all.py` (`start_date` offsets of 1/4/7/10 min).
   The three 30-minute jobs used to fire in the same scheduler pass and pile their reads
   into one minute. This reduces collisions but cannot prevent them — interval phase drifts
   on every Railway restart — so it is never the thing to rely on.

**Not fixed here (deliberate):** the 128 lost picks are NOT backfilled. Their odds were
live at pick time and their results are now known, so writing them in retrospectively
would inject 128 rows of hindsight into the calibration and P&L baseline. They stay lost
and documented.

**Related hazard found the same day:** `_run_now.py` ran `asyncio.run(run())` at module
level with no `__main__` guard, so merely importing it executed a full live picks run and
posted to subscriber channels. It now has the guard, as does `_opus_restake_aug13.py`,
whose module-level body rewrites the Opus stake column.

### Weekly summary reports Extended beside Core (1 Sep 2026)

The weekly summary text posted to `weekly-cards` every Monday 09:05 now carries a
second block: Extended picks, wins, losses, win rate and P&L for the same week,
under its own heading.

Why it was worth adding: since the per-league cap (15 Aug 2026) Extended is
several times Core's volume — 90 Extended picks against 30 Core in the last seven
days — and none of it appeared anywhere in the weekly report. The tier was being
tracked and settled but never read.

- **Core figures are unchanged.** `get_weekly_data()` returns the same top-level
  keys computed the same way; Extended lives under a new `"extended"` key.
  Verified by running the pre-change implementation and the new one against the
  same sheet snapshot and comparing every Core key.
- **The weekly CARD stays Core-only.** `generate_weekly_card` reads named Core
  keys through `.get()`, so the extra key is inert for it. Same for
  `update_result.py`'s recap.
- **One sheet read serves both tiers.** `_core_rows` and `_extended_rows` filter
  the same `get_all_values()` result. Adding a second read for Extended would
  reintroduce exactly the pressure that caused the 20-29 Aug quota loss.
- **Same arithmetic for both.** `_weekly_tier_stats()` was extracted from
  `get_weekly_data` and is used for Core and Extended alike, so the two win rates
  are comparable. It is deliberately not `_tier_stats()`, which uses a
  wins+losses denominator; the Core weekly figure has always divided by every
  settled row and honoured `_WIN_RATE_EXCLUDE`, and that had to stay.
- **The section states the caveat every send:** Extended has no stake, no
  bankroll, no running total, and is excluded from calibration/edge/CLV — so the
  two P/L figures must not be added together.
- A week with no Core picks still renders the Extended block; the tiers are
  independent, so an empty Core week is not necessarily an empty week.
- The all-time bet-type table underneath is Core-only (`get_bet_type_breakdown`)
  and is now labelled as such, which it needed once a second tier appeared above it.

### Probability calibration engine (added — `calibration.py`)
- Claude must now output a `probability` field per pick (0-100, its estimated true win probability), logged to the 'Claude Prob %' column; the market implied probability (100 / market odds) is logged to 'Market Prob %' when real odds were found
- `calibration_report()` — buckets settled WIN/LOSS picks by stated probability (<50%, 50-60% … 90-100%) and compares Claude's average stated probability to the actual win rate per bucket, plus a Brier score (well-calibrated = actual ≈ stated)
- `edge_report()` — average Claude-vs-market edge for winners vs losers, and ROI of picks where Claude's probability exceeded the market's vs where it didn't
- Monthly calibration summary posted to Discord's `weekly-cards` on the first Monday of each month (piggybacks the weekly summary job), with sample size and a warning below 300 settled picks
- No backfill: picks logged before the columns existed have no probability data and are skipped
- Run manually: `python calibration.py`

#### Early calibration observation — favourite *under*confidence (6 Aug 2026)

**Logged before the formal report has a usable sample. This is an observation, not a finding:
n=3, one competition, one day. Do not act on it or change the prompt because of it.**

The 6 Aug card ran entirely on Conference League qualifying, which has no Odds API key (see the
odds caveat above), so all five picks displayed Claude's estimated odds. Three of those estimates,
compared against the real market prices available at the time:

| Pick | Claude odds (implied) | Market odds (implied) | Gap | 'Claude Prob %' logged |
|---|---|---|---|---|
| FC Midtjylland Win | 1.60 (62.5%) | 1.36 (73.5%) | **11.0pp** | 67 (6.5pp low) |
| Braga −1.5 AH | 1.80 (55.6%) | 1.43 (69.9%) | **14.4pp** | 65 (4.9pp low) |
| FC Twente Win | 1.55 (64.5%) | 1.20 (83.3%) | **18.8pp** | 68 (15.3pp low) |

The direction is consistent across all three: Claude's estimated odds are **longer** than the
market's, i.e. it is **under**confident on short-priced favourites. That is the *opposite* of the
overconfidence the "LLM overconfidence risk" limitation below anticipates. Note also that the
`probability` field sits closer to the market than the quoted `odds` do — the two outputs of the
same pick disagree with each other, by 4-9pp here.

**Caveat that keeps this honest:** the market figures are raw single-price implied probabilities
and still carry the bookmaker margin, so they overstate true probability by roughly 2-4pp on a
3-way market. De-vigged, the gap narrows but does not reverse.

**Two consequences worth watching — neither acted on:**
1. *Value flags would suppress on favourites.* `enrich_picks_with_real_odds()` flags value only
   when Claude's implied probability beats the market's by ≥5pp. Systematic underconfidence pushes
   Claude below the market, so favourites would rarely flag even where coverage exists.
2. *P&L is overstated on no-coverage picks.* `auto_results.py` computes `pnl = odds − 1` from the
   stored 'Odds' column, which is the **estimate** whenever no market odds matched. A winning
   Midtjylland pick logs +0.60u where the real price paid +0.36u. Every Conference League
   qualifying pick since 30 Jul 2026 carries this inflation in the tracked P&L.
   **Partly fixed 9 Aug 2026** — settlement now pays the market price wherever one was matched
   (see below). This entry still stands for genuinely uncovered fixtures like Conference League
   qualifying: with no market price at all, the estimate is the only number available, so those
   picks keep whatever inflation the estimate carries.

Re-check against `calibration_report()` once the sample reaches ~300 settled picks (~Oct 2026).
If the direction holds there, it is a prompt/scoring issue rather than noise.

### Settlement pays the market price (fixed 9 Aug 2026)

**The bug.** The picks card has shown the real market price since 4 Aug 2026 ("one odds figure,
never two"), but settlement kept computing `pnl = odds − 1` off the 'Odds' column — Claude's
*estimate*. Any pick whose card showed a market price different from the estimate booked a payout
nobody received. Live example: Westerlo vs Union St.Gilloise (8 Aug) settled +1.00u off a 2.00
estimate where the card showed 1.69 — the real payout was +0.69u.

**The fix.** `excel_tracker.settlement_odds_from_row()` is now the single source of truth for the
settlement price: matched market price when there is one, Claude's estimate when there isn't.
`pnl_for_result()` is likewise the single P&L definition, shared by the automatic checker
(`auto_results.py`) and the manual override (`update_result`) so the two cannot drift.
`get_picks_for_date()` resolves the same way, so result notifications quote the price actually paid.

**Storage.** A 'Market Odds' column (appended to `PICKS_HEADERS`, self-migrating via `init_excel`)
now stores the matched price verbatim, written by `main.py` → `tracker.log_pick` →
`log_to_excel(market_odds=…)`. Rows logged before 9 Aug 2026 have only 'Market Prob %', which is
`100 / market_odds` rounded to 1 dp — invertible, and exact after rounding to 2 dp at the short
prices this bot picks, but lossy past roughly 6.00 (an 8.50 price round-trips to 8.47). The
explicit column is preferred; the derivation is the fallback for historical rows only.

**Historical rows.** 15 settled picks had a market price but were booked at the estimate:
9 overstated (+1.74u), 6 understated (−1.75u), **net −0.01u** on a 3.49u gross absolute error.
The net is a rounding artefact, not a systematic bias — the estimate runs long on some picks and
short on others, so the headline P&L was very nearly right by cancellation while most individual
rows were wrong.

Row 191 (Westerlo vs Union St.Gilloise, 8 Aug) was corrected by hand on 9 Aug 2026: +1.00u → +0.69u,
dropping total P&L from 30.67u to 30.36u and the bankroll to €403.60. **The other 14 are still
booked at the estimate** — they are listed below, and the decision to leave them is deliberate, not
an oversight. Anything reading per-pick P&L (`edge_report`, `clv_report`) sees those 14 as they were
settled.

| Row | Date | Pick | Est | Market | Booked | Correct | Δ |
|---|---|---|---|---|---|---|---|
| 116 | 04 Jul | Morocco Win | 2.30 | 1.80 | +1.30 | +0.80 | +0.50 |
| 118 | 04 Jul | France Win | 1.40 | 1.18 | +0.40 | +0.18 | +0.22 |
| 122 | 05 Jul | Over 2.5 Goals | 1.85 | 1.70 | +0.85 | +0.70 | +0.15 |
| 123 | 05 Jul | England Win | 1.80 | 2.40 | +0.80 | +1.40 | −0.60 |
| 125 | 05 Jul | England −0.5 AH | 1.80 | 2.35 | +0.80 | +1.35 | −0.55 |
| 130 | 06 Jul | Over 2.5 Goals | 1.72 | 2.00 | +0.72 | +1.00 | −0.28 |
| 131 | 07 Jul | Argentina Win | 1.30 | 1.34 | +0.30 | +0.34 | −0.04 |
| 132 | 07 Jul | Over 2.5 Goals | 1.75 | 1.95 | +0.75 | +0.95 | −0.20 |
| 136 | 09 Jul | France Win | 1.65 | 1.58 | +0.65 | +0.58 | +0.07 |
| 139 | 09 Jul | Under 2.5 Goals | 1.90 | 1.84 | +0.90 | +0.84 | +0.06 |
| 141 | 10 Jul | Spain Win | 1.75 | 1.63 | +0.75 | +0.63 | +0.12 |
| 143 | 10 Jul | Over 2.5 Goals | 2.00 | 1.79 | +1.00 | +0.79 | +0.21 |
| 145 | 10 Jul | Spain −0.5 AH | 1.75 | 1.65 | +0.75 | +0.65 | +0.10 |
| 146 | 11 Jul | England Win | 1.85 | 1.93 | +0.85 | +0.93 | −0.08 |

Net across the remaining 14: **−0.32u, i.e. currently *under*stated** (8 overstated by +1.43u,
6 understated by −1.75u) — correcting them would raise total P&L from 30.36u to 30.68u. Row 191
happened to be the single largest overstatement, so removing it flipped the sign of the remainder;
the gross error is what matters per-row, not this near-zero net. To correct them, rewrite the 'Profit/Loss'
cell with `pnl_for_result(result, market_odds_from_row(row, header))` and call
`finalize_workbook()` once at the end.

### Closing Line Value (CLV) tracking (added — `closing_odds.py`)
- Each pick's kickoff time is captured from the RapidAPI fixture data at pick-log time and stored in the 'Kickoff UTC' column (plus 'League', for odds-batching)
- `closing_odds_job` polls every 15 minutes; for any unsettled pick whose kickoff is 5-65 minutes away, it fetches current market odds from The Odds API and overwrites the 'Closing Odds' column — the last write before kickoff becomes the closing price
- Odds API calls are batched per competition (one request covers every due match in that league that cycle), not one request per match
- Self-imposed cap of **60** Odds API requests/day (raised from 12 on 6 Aug 2026 with the paid tier); polling is skipped with a warning if exceeded. Sizing: a pick's closing window is 5-65 min and the poller runs every 15 min, so covering one competition's kickoff wave costs 4 requests — 60/day buys ~15 competition-waves, i.e. real coverage across staggered kickoff blocks rather than a token single poll. **Re-check this against the per-league cap (15 Aug 2026):** the poller is tier-BLIND by design (both tiers collect closing odds) and now chases 30+ unsettled picks spread over up to 10 competitions, each with several kickoff waves, so 15 waves/day is no longer comfortable headroom. When the cap trips, polling stops for the rest of the day — and since the job takes rows in sheet order rather than Core-first, the skipped waves can be the ones carrying Core picks, which is the only tier CLV is scored on. Not yet changed: the two candidate fixes are raising the cap (the picks-run enrichment re-key freed ~210 units/day of worst-case budget, so there is room) or ordering `get_unsettled_picks_with_kickoff` Core-first
- `calibration.py`'s `clv_report()` computes CLV = (original odds / closing odds − 1) × 100 for every settled pick with both values — average CLV, % of picks with positive CLV, and ROI split between positive- and negative-CLV picks
- Appended to the existing monthly calibration report, with the same below-300-picks sample size warning
- Purely additive measurement: never touches pick generation, Kelly staking, or the calibration engine's existing reports; every step fails silently on error
- Run manually: `python closing_odds.py`

### Kelly Criterion staking (added)
- Each pick gets a suggested stake calculated as half-Kelly, capped at 5% of real bankroll
- Based on historical win rate for that specific bet type from settled Sheets data
- Falls back to flat 1-unit (€10) stake when fewer than 10 settled picks exist for the bet type
- Key constants in `excel_tracker.py`: `UNIT_STAKE = 10.0`, `REAL_BANKROLL = 1500.0`
- **Pass `breakdown=` when sizing more than one pick.** Without it the function reads the whole Picks tab on every call; a per-pick loop then blows Google's 60-reads-per-minute quota and silently kills the sheet write that follows (see "Sheets quota loss" above)
- Stake suggestion is shown as the `Stake` field on each **Core** pick embed (Extended picks carry no stake — they sit outside the tracked book)

### PNG pick and result cards (added — `card_generator.py`)
- Dark neon aesthetic: black background, neon green accents, styled text
- **Picks card** (1080×1080): generated after daily picks are posted; posted to Discord's `picks-cards`
- **Results card** (1080×1080): generated after results are finalized; posted to Discord's `results-cards`
- **Weekly summary card** (1080×1080): generated and sent with the Monday weekly summary
- Cards saved to `cards/` folder; win rate in the footer is pulled live from the Summary sheet
- Font: DejaVu Sans Mono, bundled in `fonts/` (Consolas et al. remain later fallbacks on Windows)

### Discord delivery (added — `discord_bot.py`)
- Every daily picks card and weekly card is posted to its Discord card channel
- Live result notifications (the automatic 30-minute checker) post to Discord; the results PNG card additionally mirrors when the manual `--results` path generates it
- Each individual pick is routed as a Discord embed to a league-specific channel (`premier-league` / `jupiler-pro-league` / `world-cup` / `bundesliga` / `la-liga` / `serie-a` / `ligue-1` / `champions-league` / `europa-league` / `conference-league`) — see section 5b for the embed format
- Entirely fail-silent — see section 5b for the mapping structure and guarantees

### Tracking and reporting
- Auto result detection with score-based evaluation for all supported bet types
- Live result notifications sent to Discord as each match finishes
- Running P&L tracked per pick and cumulatively; bankroll column updates after every result
- Bet type breakdown in Summary sheet: wins, losses, win rate %, total P&L per bet type
- Bet type breakdown also included in weekly Monday summary
- Weekly summary date range shows the completed previous week (fixed from current week)
- Win rate in `get_summary_win_rate()` scans by label (not hardcoded cell address) — robust to row additions
- World Cup 2026 support: membership decided by `_is_wc_match()` — BOTH teams must be confirmed WC participants, checked on every fixture; the `leagueId` only disqualifies (domestic club competitions) and separates group stage from knockout
- Youth team filtering (U19, U21, U23 matches excluded)

**World Cup validation-ordering bug, fixed 4 Aug 2026.** The selection used to read
`leagueId in WC_2026_IDS or _is_wc_knockout(...)`, and `_is_wc_knockout()` returned
`False` as soon as it saw a known WC id — so any fixture on an id in `WC_2026_IDS`
was accepted with **no participant validation at all**. One wrong id in that set
therefore silently overrode a correct check. One was wrong: `914609`, seeded as the
"opening batch (Jun 11)", is the international **`Friendlies`** id (parent 114), and
it logged `Vietnam vs Myanmar` as a World Cup pick on 18 Jul 2026 even though Myanmar
is not a participant. `914609` is removed and the participant check is now
unconditional. Replayed over 40 cached days / ~3,100 fixtures the change flips
exactly one selection (that match), and **zero** fixtures on a known WC group id are
rejected by the stricter check — so it costs no genuine coverage. Residual risk worth
knowing: a friendly between two *participant* nations would still pass the participant
fallback; only an explicit non-WC id list would close that, and the WC block is
date-gated shut (`WC_2026_END`, 19 Jul 2026) so it cannot fire again as written.

---

### Claude Opus 5 shadow experiment (added 13 Aug 2026 — `opus_shadow.py`, `opus_tracker.py`)

Runs `claude-opus-5` over the **exact same enriched fixture pool** `daily_picks_job` just used for
its production Sonnet picks — same fixtures, same form/H2H context, same `SYSTEM_PROMPT`, same
10-pick ranked format, same Core/Extended tiers, same never-pad rule, and **no fixture data
re-fetched** (zero extra RapidAPI cost).

> **The model stopped being the only variable on 15 Aug 2026.** Production moved to one call per
> competition capped at 10 each; the shadow deliberately stayed a single whole-slate call capped at
> 10, on its own frozen `OPUS_MAX_PICKS_PER_RUN` / `OPUS_CORE_PICKS_PER_RUN` and the unchanged
> `main.SYSTEM_PROMPT` (byte-identical to the pre-change prompt, verified by hash). Fanning the
> shadow out per league would take it from ~$4.29/month to roughly $17-25 at Opus pricing — a
> decision worth taking deliberately rather than inheriting from a production constant. What stays
> comparable is the question both models answer from the same pool: **which are the best 5 bets
> here**. What is no longer comparable is the harness — Sonnet's Core is chosen by a per-league
> prompt plus a selection call, Opus's by one cross-competition ranking. Treat that as a known
> confound in any Core-vs-Core read, and re-cost before changing it. Same design as the Fable 5
shadow (12-18 Jul 2026, removed in `35846c9`) — which is why the generic hooks that experiment left
behind are reused rather than a second engine being written.

**Master gate.** The whole thing is inert unless `opus-shadow` is present in `DISCORD_CHANNELS_JSON`
— no model call (so no cost), no sheet writes, no Odds API usage. Turning it off is deleting one key
on Railway.

**Where it hooks in.** Last statement group in `daily_picks_job`, *after* every production surface has
already logged and delivered, wrapped so a shadow failure can only ever cost the shadow. Settlement is
a 30-minute `opus_shadow_results_check` job in `run_all.py` that drops its `resolved` list on the
floor — results go to the sheet and nowhere else.

**Settlement reuses the production engine**, via `run_auto_results`'s three hooks:

```python
run_auto_results(lookback_days,
                 pending_source=get_pending_opus_rows,
                 row_writer=update_opus_row_result,
                 finalizer=finalize_opus_sheet,
                 alert_scope="opus-shadow")     # NOT optional — see below
```

No second evaluation engine — deliberately. The 9-11 Jul 2026 tennis outage came from a bespoke
settlement path pointed at a results endpoint that could not return finished matches; a copy-pasted
engine here would be the same class of risk. `run_auto_results` reads exactly six keys off a pending
row (`sheet_row`, `date`, `match`, `bet_type`, `pick`, `odds`), verified against its source before
`opus_tracker` was written.

#### `alert_scope` — the sharp edge of reusing one settlement engine for two tabs

An adversarial review of this change caught a real isolation breach in the first draft, and the fix
is now a **standing rule: any caller settling a tab other than football MUST pass its own
`alert_scope`.**

`auto_results._pending_alerted` / `_pending_followed_up` are *module-level* sets, shared by every
caller of `run_auto_results`. They were originally keyed `(match, bet_type, pick)` only. Because both
pipelines analyse the identical fixture pool with the identical prompt — and PENDING is a property of
the *fixture* (a two-legged tie, an unhandled bet type), not of the model — an Opus PENDING row would
routinely produce the same key as a football one. The shadow's caller discards its alerts, so the
football row's alert would then never be raised **anywhere**, and that row could age past
`LOOKBACK_DAYS` and strand silently: the exact 10 Aug Bodø/Glimt failure the 12 Aug alerting change
exists to prevent, reintroduced through a shared global. The key is now
`(alert_scope, match, bet_type, pick)`.

Two related hardenings landed with it:

- **`row_writer` may return `False` to mean "the write failed"**, and `run_auto_results` now honours
  it — the row is left PENDING, counted under `errors`, and not reported as resolved. A writer
  returning `None` still means success, so football is unaffected. This is what
  `update_opus_row_result` returns a bool *for*; in the first draft it returned one and the caller
  ignored it, so the documented protection did not exist.
- **The shadow's PENDING rows are logged loudly** in `run_all.opus_shadow_results_check` rather than
  dropped with the rest of its alerts. They stay out of Discord (the experiment has no alert channel)
  but are visible in the Railway logs, so shadow data cannot rot unnoticed.

> The same bug existed in tennis and was **fixed on 13 Aug 2026** — see "Tennis settlement: a failed
> write no longer reports success" in the tennis section below.

**Isolation — verified, not asserted.** Opus rows live only in `Opus Shadow Picks`. `calibration.py`'s
three reports default to `excel_tracker._picks_ws`, and nothing points them here, so Opus cannot reach
calibration, edge or CLV *by construction* — there is no code path, not merely a filter. Measured on
13 Aug 2026 with 9 Opus rows live on the spreadsheet: calibration 98, edge 34, CLV 27, football Picks
223 rows / 217 Core / 6 Extended — every figure identical to before the shadow existed.

**The tab is painted like the Picks tab** (13 Aug 2026): frozen bold header on dark green `#1a5c38`,
alternating white / `#e8f5e9` data rows banded **by sheet row** so both tabs stripe in step when read
side by side, Result cell coloured by outcome (`#00c853` WIN, `#ffab00` HALF WIN, `#ff6d00` HALF LOSS,
`#d50000` LOSS, banding for VOID/blank), thick border, auto-sized columns. `apply_opus_formatting()`
runs once per run after the batch is appended (appended rows carry no formatting) and again inside
`finalize_opus_sheet` after settlement — recalculate first, repaint second, the same order
`excel_tracker` uses. It is a **separate function** from `excel_tracker._apply_formatting`, which is
hard-wired to `ss.worksheet("Picks")`; only the colour *constants* are imported, so the tabs cannot
drift apart visually while no football row is ever read or written from here.

**SIM staking is flat: €1000 start, €100 on every pick** (`OPUS_STARTING_BANKROLL` / `OPUS_FLAT_STAKE`
in `opus_tracker.py`, set 13 Aug 2026 — previously €100 start with half-Kelly sizing off a €2 unit).
`calculate_opus_stake()` therefore takes **no arguments**: a stake that varied with bet type, odds or
the settled record would not be flat, so there is nothing to pass. Flat sizing keeps the SIM bankroll a
direct readout of *pick quality* — with Kelly it also measures the staking model, which is not the
question the shadow exists to answer. Still SIM: no Opus pick is staked for real, and every stake and
bankroll figure on the tab and in the Discord embed carries the SIM tag.

`recalculate_opus_running_totals` rebuilds the bankroll from **each row's own Stake cell**, not from the
constant, so changing the sizing needs the existing rows backfilled too or the curve mixes two scales —
done for the 9 live rows by `_opus_restake_aug13.py` (kept as the audit trail; it touches no other tab).
After the backfill: WIN +0.40u → €1040.00, LOSS −1.00u → €940.00.

**Settlement verified live, not assumed** (13 Aug 2026). Five probe rows for already-finished 11 Aug
fixtures were inserted into the Opus tab and settled through the hooks above: `checked=14, updated=5,
not_finished=9, errors=0`; WIN/LOSS matched the football tab's outcomes, Running Total P&L accumulated
(+0.30 → −0.70 → −0.20 → +0.60 → −0.40) and the SIM bankroll tracked it (€100.60 → €98.60 → €99.60 →
€101.20 → €99.20 — the then-current €100/€2 sizing, since replaced by €1000/€100 flat). The nine same-day picks correctly stayed PENDING. Probes were then deleted and the
totals recalculated. *(Probe P&L differs slightly from the football tab's on the same fixtures — the
probes carried no Market Odds, so they settled at the estimate. Correct behaviour, different input.)*

**Cost — measured on the live 38-fixture slate, not estimated.** Opus 5 is **$5 / input MTok and $25 /
output MTok** (confirmed against the live models reference on the day, not recalled). It runs
**adaptive thinking by default and thinking bills as output**, which is ~79% of the cost:

| Effort | Input | Output | Cost/run | Monthly | Latency |
|---|---|---|---|---|---|
| `low` | 6,010 | 1,413 | $0.065 | $1.99 | 18s |
| `medium` | 6,010 | 2,440 | $0.091 | $2.77 | 31s |
| **`high`** (API default, what it runs at) | 6,010 | 4,441 | **$0.141** | **$4.29** | 56s |

`high` deliberately: throttling the shadow would understate Opus and make the comparison misleading.
For reference the production Sonnet football run is ~$0.06 on a light slate and ~$0.20 at peak since
it went per-league (it was ~$0.026/run when this table was written). `OPUS_MAX_TOKENS = 16000`
because `max_tokens` caps thinking **plus** response text on Opus 5 — the 2,048 production used then
would truncate (production itself now sends 4,096 per league call).

**Two Opus 5 API specifics this code depends on.** Thinking blocks precede the text block, so the
parser takes the first `type == "text"` block rather than `content[0]` (the exact bug that broke the
Fable shadow on its first live run, `03b3ff0`). And `usage_tracker.ANTHROPIC_PRICING` gained a
`claude-opus-5` row — without it `anthropic_cost()` logs the tokens at **$0.00 with a warning** (it
does not fall back to another model's rate), so Opus spend would have read as free.

**Odds:** Opus picks different fixtures, so it runs its own `enrich_picks_with_real_odds` pass — up to
30 units/day, ~912/month, 4.6% of the 20,000 tier. No closing-odds polling for the shadow.

### Fable 5 shadow pipeline — DISCONTINUED 19 Jul 2026 (ran 12-18 Jul 2026)

**The experiment is over and the code is fully removed.** It was a side-by-side model comparison: each day after the production Sonnet 4.6 picks, the same enriched fixture pool was sent to claude-fable-5, with picks logged to the 'Fable Picks' sheet tab and posted to a dedicated `fable-picks` Discord channel, plus separate settlement, closing-odds polling (own 6/day budget) and calibration.

**Why discontinued:** user decision on 18 Jul 2026 (preference — one model's picks, no parallel experiment feed), reinforced by the perception that Fable picks were getting mixed into the regular football Discord feed. **Shutdown investigation (19 Jul 2026):** a full scan of the delivered Discord messages found *no* Fable content in any production channel — all Fable messages ever delivered sit in `#fable-picks`, correctly tagged 'Fable 5 experiment'. `send_to_discord()` has no fallback routing, so the 18 Jul 403 permission outage could not have redirected sends. The mixed-feed impression most likely came from (a) the 12 Jul test batch, where Fable posted picks for the untracked Swedish Allsvenskan because its fixture pool wasn't yet filtered to tracked leagues, and (b) 15 Jul, when Fable's World Cup picks landed in `#fable-picks` one minute after the production World Cup picks — near-identical embeds side by side in the server.

**What was removed (19 Jul 2026):** `fable_shadow.py`, `fable_tracker.py`, `fable_calibration.py`; the generation call in `main.py`'s `daily_picks_job`; the settlement and closing-odds jobs in `run_all.py`; the `fable-picks` key from the local `DISCORD_CHANNELS_JSON` (remove it from Railway's copy too — harmless but dead). The generic source/writer/budget hooks on `run_auto_results` and `run_closing_odds_check` remain (unused). **Kept for reference:** the 'Fable Picks' sheet tab with its 12-18 Jul data, and the `#fable-picks` Discord channel history. Nothing generates, settles, or posts Fable picks anymore; do not rebuild this without an explicit user request.

## Known Limitations & Future Issues (not yet addressed)

- **Odds timing bias** — *In progress, CLV tracking live from 4 Jul 2026.* Market probabilities in column L are still captured at 9AM pick time, and `edge_report` is still flattering by an unknown amount for picks logged before the fix. `closing_odds_job` now polls The Odds API 5-65 minutes before each kickoff and logs the true closing price to a separate 'Closing Odds' column; `calibration.py`'s `clv_report()` measures closing line value on top of it. This resolves the bias for every pick logged from 4 Jul 2026 onward — historical picks before that date have no closing odds and are excluded from `clv_report()`. Sample size is still tiny; see the calibration sample size limitation below.
- **Calibration sample size** — `calibration_report` and `edge_report` are statistically meaningless below ~300 settled picks with probability data. Data collection started 30 Jun 2026. Do not draw conclusions from early monthly reports.
- **Win rate is the wrong success metric** — a high win rate at low average odds can still be break-even or negative ROI. The metric that matters is ROI vs market implied probability, which the `edge_report` now tracks.
- **LLM overconfidence risk** — Claude's stated probabilities are uncalibrated and likely systematically overconfident on favorites. The calibration engine exists specifically to measure this gap. *Note: the first spot check (6 Aug 2026, n=3 — see "Early calibration observation" above) pointed the **other** way, showing 11-19pp **under**confidence on short-priced favourites. Far too small a sample to overturn this expectation; recorded so the formal report is read against both hypotheses, not just this one.*
- **No market data at all for Europa/Conference League qualifying** — *provider gap, not a budget or mapping problem; the 20,000-unit paid tier does not fix it.* Measured 6 Aug 2026 across all 67 soccer keys (see "Odds caveat" above): those fixtures exist under no sport key. Consequences while the bot picks these competitions: no value flags, no `Market Prob %` (so the picks contribute to `calibration_report` but never to `edge_report`/`clv_report`), and **P&L computed off Claude's estimated odds**, which the 6 Aug spot check showed run 11-19pp short of real market prices — i.e. tracked returns on these picks are inflated. Champions League qualifying is unaffected (covered, 10 events listed for 11 Aug).
- ⚠️ **Three pre-12-Jul-2026 rows are mis-settled under the current 90-minute policy — not yet corrected.** Found by the 12 Aug 2026 two-legged audit (below). Before 12 Jul 2026 settlement used the raw final score with no extra-time awareness; the 90-minute default arrived that day and nothing backfilled the rows settled under the old rule. Three are provably wrong because their match went to ET/pens as a **single** match, which guarantees regulation ended level: **r103** (1 Jul, Belgium vs Senegal, AH Belgium −0.5 @1.85, stored WIN, regulation goal difference was 0 → LOSS), **r111** (3 Jul, Argentina vs Cape Verde, MW Argentina Win @1.12, stored WIN → LOSS) and **r146** (11 Jul, Norway vs England, MW England Win @1.93, stored WIN → LOSS). Net effect: running P&L is overstated by **4.82 units** (31.57 → 26.75 if corrected). A further four BTTS rows (**r87, r90, r105, r147**) are genuinely underivable — the 90' score cannot be recovered from an AET/pens final score — so their stored values are unverifiable rather than known-wrong. Correcting any of these rewrites historical P&L and the calibration sample, so it is left as a deliberate decision.
- **Two-legged Over/Under and BTTS remain manual when ambiguous** — the 90-minute margin is derivable but the TOTAL is not, so a pick like r282 (Sabah FK vs Hapoel Beer Sheva, Over 2.5, AET 5-2 agg 6-4) stays `PENDING`: the 90-minute total could be 1, 3 or 5. A *minimum*-total bound (`|reg_gd|`) would settle `Over` in some of these — LASK vs Celtic had a margin of 3, so its 90-minute total was at least 3 — but that was deliberately not added on 1 Sep 2026, keeping the change to markets that pay on the margin. Available as a follow-up.
- **No injury/lineup data** — the bot has form and H2H context but no player availability, injury status, or individual player form. Napoleon Games odds are also not in The Odds API, so market comparison uses consensus European bookmaker odds instead.
- ⚠️ **128 picks from 20-29 Aug 2026 are permanently absent from the Picks tab** — delivered to Discord, lost to a silent Sheets 429 (see "Sheets quota loss" above). Affected dates: 20, 22, 26, 27, 28, 29 Aug. They are deliberately **not** backfilled: their results are now known, so adding them retrospectively would inject hindsight into the P&L and calibration baseline. Consequences — the Aug sample is ~128 picks short of what was actually published, the weekly summary for 24-30 Aug reported 11 Core picks against 35 posted, and any Discord-vs-sheet reconciliation over that window will not balance. The cause is fixed; the hole is not fillable.
- **Kelly stakes based on thin data** — bet-type win rates driving Kelly calculations are based on small samples (10-30 picks per type) and may regress significantly.

---

## Roadmap

Completion estimates per area — update these percentages whenever a related change ships.

| Area | Done | Status |
|---|---|---|
| Bot core | 99% | Picks analysed **one competition per Claude call** since 15 Aug 2026, with a global selection step naming the day's Core 5 — the sheet write path batched and Discord sends paced to carry the resulting 30+ picks a day. Extra-time settlement made two-legged-aware and every `PENDING` now alerts to `results-cards` on sight and again 24h after kickoff (12 Aug 2026), so a pick can no longer strand unsettled until it ages out of the lookback window. Live — picks, results, sheets, cards, Telegram all automated on Railway; Summary tab gained a per-league breakdown and all user-facing output is model-name-free (4 Aug 2026). Settlement now pays the market price shown on the card rather than Claude's estimate, via a new 'Market Odds' column (9 Aug 2026). Total-failure alerting closed its last blind spot on 18 Aug 2026: the football picks-failed alert now fires on Telegram AND Discord independently and states the upstream reason, the tennis job alerts on API failure at all, and `_run_now.py` delivers to Discord like the job it stands in for — an exhausted API credit balance had silently killed three consecutive slates. Telegram removed entirely 18 Aug 2026 — Discord is the sole delivery surface, the weekly summary text / monthly calibration report / Kelly stake were ported rather than dropped, and the bot-token-in-URL log leak went with it. API health is now visible in `usage`: a credit-balance 400 alerts immediately (deduped per day) and the daily summary opens with the last call's outcome plus the age of the last success, so a zero-cost day can no longer be mistaken for a quiet one. Credit BALANCE stays absent by design — no Anthropic endpoint exposes it (Console only), and a guessed figure would be worse than none. Two-legged extra-time settlement stopped needing a human on 1 Sep 2026: the 90-minute goal DIFFERENCE is derived from the aggregate (`h90-a90 = (agg_away-final_away) - (agg_home-final_home)`), which settles Match Winner, Double Chance and Asian Handicap automatically — validated against all 15 real two-legged AET/shootout ties — while Over/Under and BTTS correctly stay PENDING because the margin does not pin the total; a shootout with no extra time now settles exactly on the final score, and fixture matching folds diacritics and tries reversed sides, clearing four rows stranded since 10-19 Aug. 1 Sep 2026 also closed the matching blind spot on the SHEET side: a pick batch that does not fully land now alerts to `usage` with written/skipped/failed counts, so a partial write is as visible as a total one — previously both printed one INFO line and nothing else |
| Data quality | 94% | Picks-per-run hard-capped in `analyse_with_claude()` (12 Aug 2026), closing a gap where the card rendered `picks[:5]` while the sheet logged every pick the model returned — so a 6th+ pick was settled into P&L without ever being shown (last bit 29-30 Jun 2026, 7 picks). That cap became **per competition** (`MAX_PICKS_PER_LEAGUE = 10`) on 15 Aug 2026, so card and sheet now diverge *by design*: the sheet carries 30+ picks and the card the 5 Core ones, and it is the tier split — not the cap — that keeps them consistent. The card's backstop was re-cut as a tier filter rather than a positional `[:5]` in the same change, and picks-run Odds API enrichment was re-keyed per competition instead of per fixture, which cut its worst case from ~300 to ~30 units/day. Jupiler Pro League fixed 8 Aug 2026 — a stale pinned leagueId (`900433`) had kept it at **zero picks for the bot's entire history**; moved onto the self-healing parent-id path (parent `40`) with roster-ranked discovery, and all five remaining pinned domestic ids audited as stable parents so this cannot recur at the next season rollover. The Odds API on the 20,000-unit paid tier since 6 Aug 2026 — polling caps raised 12→60 (football) and 12→40 (tennis), single-region `eu` calls at 3 units, tier-proportional hard stop; Europa/Conference qualifying confirmed to have **no market data at any tier** (provider gap). Odds API + closing odds (CLV) live since 4 Jul 2026. **Form/H2H enrichment was NOT live despite this line previously claiming it was** — both its endpoints 404'd from 29 Jun to 14 Aug 2026 and the failures were logged at DEBUG under an INFO root logger, so every football pick in that window was made on team names alone; repaired 14 Aug 2026 onto `football-get-matches-by-date` (form) + `football-get-head-to-head` (H2H) with failures now at WARNING/ERROR. Knockout picks time-scoped (90 min vs incl. ET/Pens) with ET/pens-aware settlement for ALL bet types — Match Winner, O/U, AH, BTTS, Double Chance — since 12 Jul 2026; UEFA Conference League added 30 Jul 2026 with self-healing leagueId resolution (its qualifying rounds have no Odds API key, so those picks are Claude-odds-only); UEFA Champions League added 4 Aug 2026 on that same resolution path, with a qualifying→main Odds API key fallback so its qualifying picks DO get market odds; no injuries/lineups. **The sheet write path stopped losing data silently on 1 Sep 2026**: sizing every pick in a loop made one full-sheet read PER PICK (`calculate_kelly_stake` → `get_bet_type_breakdown`), which exceeded Google's 60-reads-per-minute quota once the per-league cap took slates past ~20 picks and silently 429'd the batch write — 128 picks over six days reached Discord and never the sheet. The read is now once per run (29→1), `log_picks_batch` returns written/skipped/**failed** and any non-zero `failed` alerts to `usage` whether the loss is partial or total, the batch read/append retry 429s with backoff, and the interval jobs are phase-shifted so their reads no longer land in one minute. Settlement coverage improved the same day: the 90-minute goal difference on two-legged extra-time ties is now derived from the aggregate rather than sent to manual settlement, shootouts with no extra time settle exactly on the final score, and fixture matching folds diacritics and tries reversed home/away — four rows stranded since 10-19 Aug 2026 (r203, r243, r246, r251) settled automatically, leaving only genuinely ambiguous totals pending |
| Calibration engine | 15% | Infrastructure done, collecting since 30 Jun 2026 (+ CLV since 4 Jul); verdict ~Oct at 300 picks. First spot check logged 6 Aug 2026 (n=3, favourite underconfidence) — an observation on the record, no engine change. **Regime break at 14 Aug 2026:** every pick logged before that date was made with no form and no H2H (see "Form & H2H enrichment"), so the pre-14-Aug rows measure the model reasoning from team names alone. Treat the series as two samples rather than one when the verdict is read, and do not attribute a change in calibration after this date to model drift. **Second break at 15 Aug 2026:** Core is selected by a different mechanism from that date — one call per competition plus a global selection call, instead of one cross-competition ranking (see "Per-league picks and global Core selection"), so it is the third boundary in the series alongside 13 and 14 Aug |
| Content pipeline | 96% | Cards automatic; auto-posted to Discord (Telegram removed 18 Aug 2026), only IG posting still manual. The weekly summary text gained an Extended-tier section on 1 Sep 2026 — picks/wins/losses/win rate/P&L reported beside Core and never merged into it, with the card and the Core figures deliberately untouched |
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
| CLV tracker | `tennis_closing_odds.py` — **DISABLED 6 Aug 2026**, no-op, not scheduled | `closing_odds.py` (unchanged, still polling) |
| Calibration | `tennis_calibration.py` (own Brier, edge, CLV reports, own 300-pick threshold) | `calibration.py` |
| Auto results | `tennis_auto_results.py` + `run_all.py` `tennis_live_results_check` (every 30 min) | `auto_results.py` + `live_results_check` |
| Manual settle/override | `tennis_update_result.py` | `update_result.py` |
| Duplicate-run guard | reads the Tennis Picks tab | SQLite `picks.db` (tennis never touches it) |

### Pipeline (mirrors the football flow)

- **Fixtures:** "Tennis API - ATP WTA ITF" (MatchStat) on RapidAPI — ATP + WTA singles for the next 48 hours, capped at 25 fixtures/tour on busy days. Doubles are filtered out. Uses the same `RAPIDAPI_KEY`; the RapidAPI account must be **subscribed to this API** (separate from the football one). Host overridable via `TENNIS_RAPIDAPI_HOST`.
- **Fixture pool — paginated + tier-first (11 Jul 2026):** the daily job now reads the FULL fixtures-by-date slate (paginated; ~200-300+ fixtures/tour/day incl. qualifying, juniors and worldwide ITF events) instead of the first 10 only. Because a plain soonest-first cap then filled all 25 slots with early-starting ITF Futures matches (observed: 20/25 went to M15/W15 Rancho Santa Fe and Wimbledon dropped out entirely), fixtures are sorted tournament-tier-first (`_tier_priority`: Grand Slam → 1000 → 500 → 250 → 125/Challenger → unknown → ITF/Future) before the `MAX_FIXTURES_PER_TOUR` = 25 cap. Observed effect on 11 Jul: ATP pool went from 12 arbitrary fixtures to 240 candidates capped to 20× Wimbledon + 5× ATP 250 Bastad.
- **Rankings & rank tier split (10 Jul 2026):** fixture data carries no rankings, so each player's `currentRank` is fetched from the `player/profile/{id}` endpoint (cached per run; ~2 extra API calls per fixture, ≤100/day worst case). No fixtures are excluded by rank. Instead, picks are split into two Discord channels by tier: both players inside the top `TENNIS_RANK_THRESHOLD` (default 150) → `tennis-picks`; either player outside or unranked → `tennis-picks-lower`. Every pick goes to exactly one channel; per-tier counts are logged each run. The tier ('Top 150' / 'Lower Ranked') is logged to the Sheet's 'Rank Tier' column so `tennis_calibration.py` can eventually report calibration/CLV per tier. Ranks are sent to Claude (`player1_rank`/`player2_rank`) and shown in the pick embed's author line as `#54 vs #88` (`NR` = unranked).
- **Enrichment:** per fixture — tournament name/surface/tier (`tournament/info`), last-5 form per player (`player/past-matches`), and head-to-head (`fixtures/h2h`); capped at 20 enriched fixtures per run. In this API's archive data the first-listed player is always the winner.
- **Claude analysis:** separate `TENNIS_SYSTEM_PROMPT` (claude-sonnet-4-6) — weights player form, H2H, surface type (Hard/Clay/Grass), and tournament tier. Bet types: **Match Winner, Total Games Over/Under, Set Betting, Handicap (games)**. Outputs the same JSON shape as football, incl. the calibration `probability` field.
- **Real odds — REMOVED 6 Aug 2026:** the tennis pipeline no longer calls The Odds API. Picks are posted and logged on Claude's own odds, with no `market_odds`, no 'Market Prob %', and no 🔥 VALUE flag. _(Previously: tournaments are dynamic per-event sport keys `tennis_atp_*` / `tennis_wta_*`, discovered at runtime via the quota-free `/v4/sports` call, max 12 odds requests per picks run, same ≥5pp value-flag rule as football; Set Betting never had an Odds API market anyway.)_
- **Posting:** Discord-ONLY — a dated header (text) plus each pick as an embed (section 5b format, `Tour | Tournament | Surface` as the author line) to the `tennis-picks` Discord channel at **12:30 Europe/Brussels**, its own schedule slot 30 min after the football picks. After the embeds, a branded PNG picks card (`generate_tennis_picks_card` in `card_generator.py` — same THEPICKSAI style as football, with tour · tournament · surface · ranks context lines) is sent to `tennis-picks` with all of the day's picks regardless of tier (added 11 Jul 2026). No Telegram send exists in the tennis pipeline.
- **Tracking:** 'Tennis Picks' tab — Date, Match, Bet Type, Pick, Odds, Confidence, Result, P&L, Claude Prob %, Market Prob %, Kickoff/Start Time, Closing Odds, Rank Tier, Stake € (SIM), Running P&L (u), Bankroll € (SIM). Results are WIN/LOSS/VOID (units P&L: WIN = odds−1, LOSS = −1). No half results — tennis game handicaps and totals use half lines. The 'Rank Tier' column enables future per-tier calibration/CLV reports in `tennis_calibration.py` once each tier has enough settled picks.
- **Tennis Summary tab (12 Jul 2026):** `_refresh_tennis_summary()` rebuilds a dedicated 'Tennis Summary' tab after every settle via `finalize_tennis_workbook()` (the tennis mirror of football's `finalize_workbook`) — record/win rate/units P&L, simulated bankroll + ROI, best bet type & confidence level, Bet Type Breakdown sorted by win rate, and a tennis-only Rank Tier Breakdown per tier.
- **Staking — STAGED, currently SIMULATED (11 Jul 2026):** tennis stakes use the same half-Kelly / 5%-cap logic as football (`calculate_tennis_kelly_stake` in `tennis_excel_tracker.py`, per-bet-type historical win rate, flat `TENNIS_UNIT_STAKE` €2 below 10 settled picks, €0 on negative edge), sized against `TENNIS_REAL_BANKROLL` = **€100 — a fresh bankroll fully independent of football's €1500**. **No real money is on tennis yet**: every stake is tagged `SIM` in the sheet's 'Stake € (SIM)' column and in the Discord embed's Stake field, and the running 'Bankroll € (SIM)' column (start €100, ±units × €2, recalculated on every settle like football's col J) is paper-only. The plan is to switch to real money once the user decides the pipeline is trustworthy (results flowing correctly + enough settled picks to judge calibration); at that point the SIM tags come off and the constants get re-based to the real deposit. **Since 6 Aug 2026 that decision has to be made on calibration alone** — with the Odds API off for tennis there is no forward CLV evidence, so the usual "positive CLV + good calibration" test is only half available (see the CLV bullet above).
- **CLV — DISABLED BY CHOICE, 6 Aug 2026:** the tennis pipeline makes **no Odds API calls of any kind**. `tennis_closing_odds.py` returns immediately (before any Sheets read or network call) and `run_all.py` no longer schedules it; the picks-run enrichment is off by the same switch, `tennis_main.TENNIS_ODDS_API_ENABLED = False`. The whole Odds API allowance now goes to football, which keeps full usage. **What this costs:** the tennis 'Closing Odds' column stops accruing, so `tennis_calibration.py`'s `tennis_clv_report()` is frozen at the sample collected 9 Jul – 6 Aug 2026 and no new pick can ever have CLV. **Consequence for the real-money decision — the tennis criterion (positive CLV + good calibration) can now only be PARTIALLY evaluated: calibration yes, CLV no.** Calibration is unaffected because it reads Claude's `probability` and the settled Result, neither of which involves the Odds API — Brier score, per-confidence buckets and the 300-pick threshold all keep working normally. The edge report loses its market input in the same way CLV does. **No picks are excluded from any metric or report** — every pick still counts in calibration, results, P&L, the Tennis Summary tab and the bet-type/rank-tier breakdowns; only the market-derived columns are absent. To restore CLV, flip the flag, re-add the scheduler job, and the caps below apply again. _(Previous sizing, kept for re-enabling: own cap of 40 tennis odds requests/day budgeted separately from football's 60 — set lower despite tennis batching worse, since each tournament is its own sport key, because tennis produces fewer picks (~3-5/day); picks-run enrichment `MAX_TENNIS_ODDS_KEYS_PER_RUN` = 12.)_
- **Auto results:** `tennis_auto_results.py` (scheduled every 30 min via `run_all.py`'s `tennis_live_results_check`) scans unsettled Tennis Picks rows, finds each match in the Tennis API's fixtures-by-date (both tours, start date + next day, 30-min cache), and settles all four bet types from the set-score string: Match Winner by sets won, Total Games by summed games vs the line, Set Betting by exact set score from the picked player's perspective, Handicap by game margin + line. Retirements/walkovers settle **VOID** for every bet type (conservative — bookmaker rules differ; override with `tennis_update_result.py` if your book settled differently). Each newly settled pick posts its result text to the `tennis-results` Discord channel — Discord-only, never Telegram and never the football `results-cards` channel.

### Tennis settlement: a failed write no longer reports success (fixed 13 Aug 2026)

`update_tennis_row_result` used to swallow every Sheets exception into a `log.error` and return
`None` on both the success and failure paths. Its callers ignored that return, so a **failed write
was indistinguishable from a successful one**:

- `tennis_auto_results.py` counted the row in `stats["updated"]` and appended it to `resolved`, which
  `run_all.tennis_live_results_check` posts to the `tennis-results` Discord channel. So a Sheets
  outage produced a "✅ WIN … settled" message for a row that was **still PENDING on the sheet** —
  and would stay PENDING until it aged past the lookback window and stranded, exactly like the
  9-11 Jul 2026 rows, except now with a Discord message actively asserting the opposite.
- `update_tennis_result` (the manual `tennis_update_result.py` override) printed `Updated : <match>`
  and returned `True` regardless — so an operator hand-settling a stranded pick could be told it
  worked when nothing had been written.
- `tennis_closing_odds.py` logged `wrote <price>` unconditionally (less damaging: a missing closing
  price costs CLV coverage, not a false settlement claim).

Both writers now return `bool` and all three callers gate on it. A failed result write leaves the row
PENDING — so the next 30-minute cycle simply retries it — counts an `errors` entry, and announces
nothing. Found by an adversarial review of the Opus 5 shadow work, which had deliberately not
reproduced the pattern; the same gate now exists in football (`run_auto_results` honours a `False`
from `row_writer`), the Opus shadow, and tennis.

### Tennis limitations (own list, separate from football's)

- **Retirements/walkovers settle VOID for all bet types** — bookmaker rules differ (many settle Match Winner if a set was completed). Override a specific pick with `python tennis_update_result.py "Sinner vs Alcaraz" "Match Winner" WIN` when your book settled differently.
- **Auto-results FIXED 11 Jul 2026** (was broken since launch — the fixtures-by-date endpoint is paginated, schedule-only AND ephemeral: started/finished fixtures are *removed* from the slate, and it never carries a `result` field). Settling now works in two tiers: **primary** — the daily job logs `tour:p1Id|p2Id` to the Sheet's 'Player IDs' column at pick time, and the checker settles each pick with a single `/player/past-matches/{playerId}` call (`result` "6-1 6-0", `match_winner`, `result_type`; non-'completed' types settle VOID), matched by both player ids + UTC day; **fallback** — rows without stored ids scan the day's paginated slate (up to 30 pages) to recover ids, which only works while the fixture is still listed. Verified 11 Jul with 5 real settles (incl. Maria def. Stefanini 6-1 6-0 → LOSS, Mejia WIN +0.65u). Three pre-fix rows (Bernet/Bondioli, Johnson/Hohmann, Brancaccio/Gomez) left the slate before ids could be captured and must be settled manually via `tennis_update_result.py`.
- **No market data at all since 6 Aug 2026** — The Odds API is switched off for tennis by choice, so no pick made from that date carries market odds, 'Market Prob %', a value flag, or a closing price. The edge and CLV reports are frozen at their 9 Jul – 6 Aug sample; calibration and every other report continue with the full pick set. This is a deliberate budget decision, not a bug — do not "fix" it by re-adding Odds API calls to the tennis pipeline without the user asking.
- **Set Betting picks have no market/closing odds**, so they contribute to the calibration report but never to the edge/CLV reports. (Moot while the switch above is off — that now applies to every bet type.)
- **Calibration sample size** — all tennis reports are statistically meaningless below ~300 settled picks. Same rule as football, independent counter.

### Tennis roadmap (independent — do NOT merge into the football percentages)

| Area | Done | Status |
|---|---|---|
| Tennis bot core | 95% | Picks, Sheets tab, Discord-only delivery (`tennis-picks`/`tennis-results`, 10 Jul 2026 — no Telegram), daily picks PNG card + simulated Kelly staking/bankroll + working auto-results via Player IDs → past-matches (11 Jul 2026) + Tennis Summary tab with bet-type & rank-tier breakdowns (12 Jul 2026) all live. CLV polling ran 9 Jul – 6 Aug 2026, then switched off by choice (Odds API is football-only) |
| Tennis data quality | 55% | Form/H2H/surface enrichment + live rankings & two-tier channel split with Rank Tier tracking (10 Jul 2026); no injury/retirement data. **Down from 65% on 6 Aug 2026** — market odds removed with the Odds API switch-off, so picks carry no market comparison |
| Tennis calibration engine | 10% | Infrastructure done, collecting from 9 Jul 2026; verdict ~Oct-Nov 2026 at 300 picks. Unaffected by the Odds API switch-off (reads Claude probability + Result only) |
| Tennis auto-results | 90% | Live from 10 Jul 2026, all 4 bet types; retirements settle VOID (manual override available) |
| Tennis proven edge | 0% | Blocked on tennis calibration data — and now **only partially provable**: no forward CLV since 6 Aug 2026, so the verdict rests on calibration alone |

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
python tennis_closing_odds.py                                          # NO-OP since 6 Aug 2026 (Odds API off for tennis) — logs and exits
python tennis_update_result.py "Sinner vs Alcaraz" "Match Winner" WIN  # manually settle/override a tennis pick (WIN/LOSS/VOID)
python tennis_calibration.py                                           # print tennis calibration / edge / CLV reports (CLV + edge frozen at the 9 Jul – 6 Aug sample)
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
