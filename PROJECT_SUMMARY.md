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

Covered competitions: Premier League, Belgian Jupiler Pro League, Bundesliga, La Liga, Serie A, Ligue 1 (the last four added 19 Jul 2026; their 2026-27 seasons open 15-28 Aug 2026, so they produce no fixtures before then), UEFA Champions League (added 4 Aug 2026 — qualifying rounds, league phase and knockouts; **already producing fixtures**, Q3 is under way), UEFA Conference League (added 30 Jul 2026, same three stages), and FIFA World Cup 2026 (ended 19 Jul 2026). Both UEFA competitions and the Jupiler Pro League are matched by stable parent id rather than a pinned feed id — see "Parent-id leagueId resolution" below.

⚠️ **The Jupiler Pro League produced zero picks from the initial commit until 8 Aug 2026.** It was pinned in `LEAGUES` as leagueId `900433`, which the live feed never returns — the Belgian fixtures carry a season-scoped id (`937988` in 2026-27, parent `40`). `partition_fixtures()` therefore matched nothing for it on every run, so its fixtures were never fetched, never analysed and never rejected: the pipeline simply never saw them. Confirmed 8 Aug 2026 against the Google Sheet's Picks tab — 187 logged picks, **0 with League = Jupiler Pro League** (164 FIFA World Cup 2026, 21 Conference League, 2 Friendlies). Fixed 8 Aug 2026 by moving Belgium onto the parent-resolution path. Consequence for calibration: there is no Belgian sample at all, and the League Breakdown's Jupiler row reading zero is a real absence of data, not variance.

Since 9 Jul 2026 the repo also hosts a **fully separate tennis picks system** (ATP/WTA) — see the "Tennis System — SEPARATE from football" section below. The two systems share the Railway process and API keys but no data paths, tabs, or calibration samples. Since 6 Aug 2026 tennis no longer uses The Odds API at all (football-only) — tennis CLV is disabled by choice; see section 7.

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
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID where picks are posted |
| `GOOGLE_SHEETS_ID` | ID from the Google Sheet URL (between /d/ and /edit) |
| `GOOGLE_CREDENTIALS_JSON` | Full service account JSON (minified, single line) |
| `TELEGRAM_IG_CHANNEL_ID` | *Optional.* Telegram channel/chat ID that receives the Instagram-formatted picks card (`generate_picks_card_ig`) for manual download and posting. If unset, that card is still generated, saved to `/cards`, and sent to Discord's `picks-cards` channel — only the Telegram send is skipped. |
| `DISCORD_BOT_TOKEN` | *Optional for football, required for tennis delivery.* Discord bot token (Developer Portal → Bot → Reset Token). If unset, all Discord delivery is skipped silently — football's Telegram is unaffected, but tennis (Discord-only) posts nowhere. |
| `DISCORD_CHANNELS_JSON` | *Optional per key.* Single-line JSON dict mapping channel keys to Discord channel IDs, e.g. `{"picks-cards":"111...","results-cards":"222...","weekly-cards":"333...","premier-league":"444...","jupiler-pro-league":"555...","world-cup":"666...","bundesliga":"999...","la-liga":"aaa...","serie-a":"bbb...","ligue-1":"ccc...","tennis-picks":"777...","tennis-results":"888..."}`. Any missing key is skipped silently; several keys may point at the same channel ID. The `tennis-picks` / `tennis-picks-lower` / `tennis-results` keys carry ALL tennis delivery (tennis is Discord-only — no Telegram). |
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
| `bundesliga` | Each Bundesliga pick as an embed (league tracked since 19 Jul 2026; first fixtures ~28 Aug 2026) | `main.py` |
| `la-liga` | Each La Liga pick as an embed (league tracked since 19 Jul 2026; first fixtures ~15 Aug 2026) | `main.py` |
| `serie-a` | Each Serie A pick as an embed (league tracked since 19 Jul 2026; first fixtures ~22 Aug 2026) | `main.py` |
| `ligue-1` | Each Ligue 1 pick as an embed (league tracked since 19 Jul 2026; first fixtures ~21 Aug 2026) | `main.py` |
| `champions-league` | Each Champions League pick as an embed (tracked since 4 Aug 2026; live immediately — Q3 fixtures were already in the 48h window that day) | `main.py` |
| `conference-league` | Each Conference League pick as an embed. *New key 12 Aug 2026 — awaiting a Discord channel ID; until it is added to `DISCORD_CHANNELS_JSON`, these picks are skipped silently and reach Discord via the card only (still logged to Sheets).* | `main.py` |
| `tennis-picks` | **TENNIS (Discord-only)** — dated header (text) + each TOP-TIER tennis pick as an embed (both players inside `TENNIS_RANK_THRESHOLD`) at 12:30 Brussels, plus the picks-failed alert, plus the branded daily tennis picks PNG card (`generate_tennis_picks_card`, all of the day's picks across both tiers — added 11 Jul 2026) | `tennis_main.py` |
| `tennis-picks-lower` | **TENNIS (Discord-only)** — dated header (text) + each LOWER-TIER tennis pick as an embed (either player outside the threshold, or unranked). *New key 10 Jul 2026 — awaiting a Discord channel ID; until it is added to `DISCORD_CHANNELS_JSON`, lower-tier picks are skipped silently (still logged to Sheets).* | `tennis_main.py` |
| `tennis-results` | **TENNIS (Discord-only)** — each settled tennis pick's result text from the 30-min automatic checker | `run_all.py` `tennis_live_results_check` |
| `usage` | Daily API usage + cost report at 23:50 Brussels — Anthropic tokens/cost per job, The Odds API units against the 20,000/month tier (football-only spend since 6 Aug 2026), RapidAPI football + tennis quotas (added 4 Aug 2026) | `run_all.py` `usage_summary_job` → `usage_tracker.py` |

The league-name → key routing lives in `main.py`'s `DISCORD_LEAGUE_CHANNEL_KEYS`.

**Conference League got its own channel on 12 Aug 2026**, reversing the 30 Jul 2026
decision to leave it off `DISCORD_LEAGUE_CHANNEL_KEYS`. Reason: card-only routing
made it the one tracked competition whose picks had a *single* Discord surface, so
anything dropped from the card vanished from Discord entirely — while a Premier
League pick in the same situation still reached its league channel. That mattered
because Conference League qualifying has been carrying most of the book since
early Aug. Both UEFA competitions now route identically.

⚠️ **Needs a channel id.** `"Conference League": "conference-league"` is in
`DISCORD_LEAGUE_CHANNEL_KEYS`, but until a `conference-league` id is added to
`DISCORD_CHANNELS_JSON` on Railway, `send_to_discord()` logs
`Discord channel key 'conference-league' not mapped — skipping` and those picks
keep reaching Discord via the card only. Nothing errors; the rest of the run is
untouched. (Same pattern as `tennis-picks-lower` above.)

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
relay `reason` verbatim into a Telegram/Discord alert, so reason strings passed to
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
| Picks | Date, Match, Bet Type, Pick, Odds, Confidence, Result, Profit/Loss, Running Total P&L, Bankroll (€), Claude Prob %, Market Prob %, League, Kickoff UTC, Closing Odds, Market Odds. **'Odds' is Claude's estimate; 'Market Odds' is the matched market price and is what settlement pays out at** (see 'Settlement pays the market price', 9 Aug 2026). |
| Summary | Auto-calculated stats: win rate, total P&L, bankroll, ROI, best bet type, best confidence level, Bet Type Breakdown table, and (4 Aug 2026) a **League Breakdown** table — wins / losses / win rate / total P&L / picks per competition, sorted by P&L descending. Built from the Picks tab's League column; deliberately one section in this tab, never per-league tabs, so calibration data stays unified. Every tracked competition gets a row even at zero picks (most open their 2026-27 season mid-to-late August — zeros are expected, not a bug), leagues found in the sheet but not in `TRACKED_LEAGUES` are appended rather than dropped, and the 119 picks logged before the League column existed group under `(no league recorded)` so the section's P&L reconciles exactly with the headline total. Note `Picks` here counts every logged pick incl. pending/void, unlike Bet Type Breakdown's `Total Picks` (settled wins + losses only). Below it sits a **Bet Type × League Breakdown** (4 Aug 2026) — win rate, P&L and pick count for every league/bet-type cell, leagues ordered by P&L to match the section above. A cell shows a win rate only at `_MIN_CELL_SAMPLE` (10) or more **decided** picks and otherwise reads `insufficient data`; the gate is on the rate's own denominator (wins + losses), not on settled count, because 10 settled picks that are 8 VOIDs and 2 decided would otherwise print exactly the 2-sample rate the rule exists to hide. P&L and pick count always show — only the rate is unsafe at low n. Only leagues with ≥1 settled pick appear, so not-yet-started competitions add no rows here (they stay visible at zero in the League Breakdown). On 4 Aug 2026, 11 of 18 cells read `insufficient data` — slicing two ways splits an already-small sample hard, and that is the honest state, not a gap to fill. |
| Tennis Picks | **Tennis system only** — Date, Match, Bet Type, Pick, Odds, Confidence, Result, P&L, Claude Prob %, Market Prob %, Kickoff/Start Time, Closing Odds, Rank Tier ('Top 150' / 'Lower Ranked', for future per-tier calibration), Stake € (SIM), Running P&L (u), Bankroll € (SIM), Player IDs. Written exclusively by `tennis_excel_tracker.py`; no football code ever touches this tab and no tennis code ever touches Picks/Summary. |
| Tennis Summary | **Tennis system only** — mirror of football's Summary tab, rebuilt by `_refresh_tennis_summary()` whenever a tennis result settles (`finalize_tennis_workbook()`, called from auto-results and the manual override): overall record, win rate, units P&L, simulated bankroll + ROI, best bet type / confidence level, Bet Type Breakdown (win-rate-desc), plus a tennis-only Rank Tier Breakdown (Top 150 vs Lower Ranked; pre-10-Jul rows show as '(untracked)'). Header labels the staking as SIMULATED. |
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
- Top 5 value picks per day across all tracked competitions
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

### Parent-id leagueId resolution (Conference League 30 Jul 2026; Champions League 4 Aug 2026; Jupiler Pro League 8 Aug 2026)
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
| Conference League | `10615` | `10216` |

- Fast path: any match whose `leagueId` is in `FEED_LEAGUE_IDS[competition]` (seeded
  with `937988` / `937348` / `937351`) is bucketed straight away — **zero extra API calls**.
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
Qualification (`937349`, parent `10613`) correctly excluded, wiped seed rediscovered.
Re-verified 4 Aug 2026 for Champions League: `937348` resolved live to parent `10611`
("Champions League Qualification"), `904988` (17 Sep 2025) to parent `42`
("Champions League"); 2 Q3 fixtures bucketed on the fast path with no extra calls; a
wiped CL seed rediscovered `937348` and produced an identical fixture set with
Conference League unaffected.
Verified 8 Aug 2026 for Belgium: 3 Jupiler fixtures bucketed on the fast path
(Standard–Cercle, St.Truiden–Lommel, Westerlo–Union SG); with the seed and both
caches wiped, discovery rediscovered `937988` via parent `40` on the **first** lookup
and returned an identical fixture set, UEFA competitions unaffected.

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

**Odds caveat (Conference League only):** The Odds API has no key for Conference
League *qualifying* — only `soccer_uefa_europa_conference_league` for the main
competition, which is inactive until the league phase. Qualifying picks are
therefore Claude-odds-only (no market odds, no value flag).

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

**Budget at the current caps** (3 units/call), tennis now contributing zero: football polling 60/day
+ football enrichment ~5/day = **195 units/day ≈ 6,000/month, ~30% of the tier**, leaving ~14,000
units for Historical Odds work and spikes. (Before tennis was switched off on 6 Aug 2026 the same
caps projected 351 units/day ≈ 10,900/month, ~54% — tennis polling 40/day + tennis enrichment 12/day
were the difference.)

> ⚠️ The 25% warning is a *fraction of the tier*, so it rescales automatically — but at 20,000 units
> that is 5,000 remaining, which a healthy month reaches around day 17. Expect it to fire routinely;
> it is now a calendar artefact rather than a signal. Replacing it with a burn-rate projection
> (warn when projected month-end usage exceeds the limit) is the real fix and is **not yet done**.

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

### Extra-time settlement: single match vs two-legged tie (fixed 12 Aug 2026)

Unscoped picks settle on **90 minutes** (the bookmaker default; knockout picks may
override with a `(90 min)` / `(Full-Time incl. ET/Pens)` suffix since 12 Jul 2026).
The API returns only the final score, so when a match runs past 90 minutes the
regulation score is unknown and `evaluate_pick()` derives what it can. What is
derivable depends on **why** it went past 90:

| | Single match | Two-legged tie |
|---|---|---|
| Why ET happened | scores level at 90' | **aggregate** level at 90' |
| Regulation result | guaranteed a draw | **could be anything** |
| Match Winner / AH / Double Chance | settled off the guaranteed draw | `PENDING` |
| BTTS | `LOSS`/`WIN` if a side was scoreless over 120' (goals are monotonic), else `PENDING` | same |
| Over/Under | bound is `2 × min(home, away)` | bound is the **final total** |

Detected via `status.aggregatedStr`, which is present only on two-legged ties and
is passed to `evaluate_pick(..., two_legged=True)`.

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
collapses the two-legged case back to the single-match one. The new code returns
`PENDING` for r202/r203 rather than guessing; that is the intended trade.

⚠️ The same audit found three **single-match** rows that *are* mis-settled, from
before the 12 Jul 2026 ET rules existed — see Known Limitations.

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
- Both Claude's estimated odds and the real market odds are shown side by side in the Telegram message and the picks card
- If `ODDS_API_KEY` is missing, the fixture/market can't be matched, or the API call fails, the pick silently falls back to Claude-only odds (no crash, no message)

### Probability calibration engine (added — `calibration.py`)
- Claude must now output a `probability` field per pick (0-100, its estimated true win probability), logged to the 'Claude Prob %' column; the market implied probability (100 / market odds) is logged to 'Market Prob %' when real odds were found
- `calibration_report()` — buckets settled WIN/LOSS picks by stated probability (<50%, 50-60% … 90-100%) and compares Claude's average stated probability to the actual win rate per bucket, plus a Brier score (well-calibrated = actual ≈ stated)
- `edge_report()` — average Claude-vs-market edge for winners vs losers, and ROI of picks where Claude's probability exceeded the market's vs where it didn't
- Monthly calibration summary posted to Telegram on the first Monday of each month (piggybacks the weekly summary job), with sample size and a warning below 300 settled picks
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
- Self-imposed cap of **60** Odds API requests/day (raised from 12 on 6 Aug 2026 with the paid tier); polling is skipped with a warning if exceeded. Sizing: a pick's closing window is 5-65 min and the poller runs every 15 min, so covering one competition's kickoff wave costs 4 requests — 60/day buys ~15 competition-waves, i.e. real coverage across staggered kickoff blocks rather than a token single poll
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
- Each individual pick is routed as a Discord embed to a league-specific channel (`premier-league` / `jupiler-pro-league` / `world-cup` / `bundesliga` / `la-liga` / `serie-a` / `ligue-1` / `champions-league` / `conference-league`) — see section 5b for the embed format
- Entirely fail-silent — see section 5b for the mapping structure and guarantees

### Tracking and reporting
- Auto result detection with score-based evaluation for all supported bet types
- Live result notifications sent to Telegram as each match finishes
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
- **No injury/lineup data** — the bot has form and H2H context but no player availability, injury status, or individual player form. Napoleon Games odds are also not in The Odds API, so market comparison uses consensus European bookmaker odds instead.
- **Kelly stakes based on thin data** — bet-type win rates driving Kelly calculations are based on small samples (10-30 picks per type) and may regress significantly.

---

## Roadmap

Completion estimates per area — update these percentages whenever a related change ships.

| Area | Done | Status |
|---|---|---|
| Bot core | 98% | Extra-time settlement made two-legged-aware and every `PENDING` now alerts to `results-cards` on sight and again 24h after kickoff (12 Aug 2026), so a pick can no longer strand unsettled until it ages out of the lookback window. Live — picks, results, sheets, cards, Telegram all automated on Railway; Summary tab gained a per-league breakdown and all user-facing output is model-name-free (4 Aug 2026). Settlement now pays the market price shown on the card rather than Claude's estimate, via a new 'Market Odds' column (9 Aug 2026) |
| Data quality | 90% | Picks-per-run hard-capped at `MAX_PICKS_PER_RUN` in `analyse_with_claude()` (12 Aug 2026), closing a gap where the card rendered `picks[:5]` while the sheet logged every pick the model returned — so a 6th+ pick was settled into P&L without ever being shown (last bit 29-30 Jun 2026, 7 picks). Jupiler Pro League fixed 8 Aug 2026 — a stale pinned leagueId (`900433`) had kept it at **zero picks for the bot's entire history**; moved onto the self-healing parent-id path (parent `40`) with roster-ranked discovery, and all five remaining pinned domestic ids audited as stable parents so this cannot recur at the next season rollover. The Odds API on the 20,000-unit paid tier since 6 Aug 2026 — polling caps raised 12→60 (football) and 12→40 (tennis), single-region `eu` calls at 3 units, tier-proportional hard stop; Europa/Conference qualifying confirmed to have **no market data at any tier** (provider gap). Odds API + form/H2H + closing odds (CLV) live since 4 Jul 2026; knockout picks time-scoped (90 min vs incl. ET/Pens) with ET/pens-aware settlement for ALL bet types — Match Winner, O/U, AH, BTTS, Double Chance — since 12 Jul 2026; UEFA Conference League added 30 Jul 2026 with self-healing leagueId resolution (its qualifying rounds have no Odds API key, so those picks are Claude-odds-only); UEFA Champions League added 4 Aug 2026 on that same resolution path, with a qualifying→main Odds API key fallback so its qualifying picks DO get market odds; no injuries/lineups |
| Calibration engine | 15% | Infrastructure done, collecting since 30 Jun 2026 (+ CLV since 4 Jul); verdict ~Oct at 300 picks. First spot check logged 6 Aug 2026 (n=3, favourite underconfidence) — an observation on the record, no engine change |
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
