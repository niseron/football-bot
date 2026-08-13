# Football Picks Bot

Read `PROJECT_SUMMARY.md` for the full project overview: architecture, file
structure, environment variables, deployment (Railway), Google Sheets layout,
current features, and known limitations.

## Discord Delivery

`discord_bot.py` handles all Discord delivery (send-only, REST via
`requests` — no discord.py). Env vars: `DISCORD_BOT_TOKEN` plus
`DISCORD_CHANNELS_JSON`, a single-line JSON dict mapping the keys
`picks-cards`, `results-cards`, `weekly-cards`, `premier-league`,
`jupiler-pro-league`, `world-cup`, `bundesliga`, `la-liga`, `serie-a`,
`ligue-1`, `champions-league`, `europa-league`, `conference-league`, `tennis-picks`, `tennis-picks-lower`,
`tennis-results`, `usage` to Discord channel IDs. Fail-silent: `send_to_discord()` never raises, and any
missing token/key skips that piece without touching the rest of the flow.
For football, Discord is purely additive (mirrors Telegram). Individual pick
messages (league channels + `tennis-picks`) are Discord EMBEDS built by
`discord_bot.py`'s `build_pick_embed()` — never plain text; card and result
sends stay plain text/images. Test all configured channels with
`python discord_bot.py --test`. Details in PROJECT_SUMMARY.md section 5b.

## Tennis Delivery — Discord-ONLY

The tennis system never posts to Telegram, unlike football's Telegram +
Discord pattern. Reason: user preference — Discord is easier to view. Tennis
picks are split by rank tier: both players inside `TENNIS_RANK_THRESHOLD`
(default 150) → `tennis-picks`; either player outside or unranked →
`tennis-picks-lower` (the tier is also logged to the Sheet's 'Rank Tier'
column). The picks-failed alert goes to `tennis-picks`. Settled results go
to `tennis-results` (`run_all.py` `tennis_live_results_check`) — never the
football `results-cards` channel.
`TELEGRAM_TENNIS_CHANNEL_ID` was removed on 10 Jul 2026; do not reintroduce
it or add any Telegram send to the tennis pipeline.

Sheet writers in this pipeline return `bool` and their callers gate on it
(fixed 13 Aug 2026): `update_tennis_row_result` and `update_tennis_closing_odds`
used to swallow exceptions and return `None`, so a failed write still counted as
settled and still posted "✅ settled" to `tennis-results` while the row stayed
PENDING. A failed result write must leave the row PENDING for the next cycle to
retry and announce nothing — never report a settlement you did not write.

## Pick Tiers — Football Only (13 Aug 2026)

Football returns up to 10 ranked picks per run (`MAX_PICKS_PER_RUN = 10`), split by
`CORE_PICKS_PER_RUN = 5`: ranks 1-5 are **Core**, ranks 6-10 **Extended**. Both tiers
are logged to the Picks tab, settled by the same `auto_results.py` path, and posted
to their league's Discord channel (Extended embeds are labelled `· EXTENDED #n`).
Only Core reaches the picks card, the Telegram post, the running total/bankroll
columns, the Summary totals, and the calibration/edge/CLV reports.

`excel_tracker._core_rows()` is the SINGLE filter enforcing that — route any new
Core aggregation through it rather than writing an inline tier check. A **blank**
`Pick Tier` cell means Core, which is what keeps every row logged before this date
valid without a backfill: never fill that column in on historical rows.

Tennis is untouched by all of this — no tiers, no ranking, `tennis_*` modules
unchanged. Details in PROJECT_SUMMARY.md, "Ranked picks and the Core/Extended split".

## Opus 5 Shadow Experiment — Football Only (13 Aug 2026)

`opus_shadow.py` + `opus_tracker.py` run `claude-opus-5` over the same enriched
fixture pool `daily_picks_job` just used, so the model is the only variable (no
second RapidAPI fetch). Picks go to the `Opus Shadow Picks` tab and the
`opus-shadow` Discord channel as text embeds (both tiers, SIM stakes, no cards);
**results go to the sheet only, never to Discord**.

- **Master gate:** the whole experiment is inert unless `opus-shadow` is in
  `DISCORD_CHANNELS_JSON`. Removing that key on Railway turns it off — no model
  call, no cost, no sheet or Odds API usage.
- **Never point calibration/CLV/edge or the football Summary at the Opus tab.**
  Isolation is structural (separate tab, no shared code path), and that is the
  experiment's whole value — an Opus row in the football baseline invalidates it.
- **Settlement reuses `run_auto_results`' hooks** (`pending_source` / `row_writer`
  / `finalizer`). Do not write a second evaluation engine — that is what broke
  tennis settlement 9-11 Jul 2026.
- **Any caller settling a non-football tab MUST pass its own `alert_scope`.**
  `auto_results._pending_alerted` / `_pending_followed_up` are module-level sets
  shared by every caller; without a distinct scope a shadow PENDING consumes
  football's alert slot and a real football row can strand silently. A
  `row_writer` may return `False` to mean the write failed — the row is then
  left PENDING rather than reported settled.
- Opus 5 returns thinking blocks **before** the text block: parse the first
  `type == "text"` block, never `content[0]`. `max_tokens` caps thinking plus
  response together, hence `OPUS_MAX_TOKENS = 16000`.
- Adding a model to `usage_tracker.ANTHROPIC_PRICING` is required for cost
  tracking — an unlisted model logs tokens at **$0.00 with a warning** rather
  than guessing a rate.

Details in PROJECT_SUMMARY.md, "Claude Opus 5 shadow experiment".

## Working Rules

- Load `.env` via `from env_loader import load_env; load_env()` — never call
  `dotenv.load_dotenv()` directly. `load_env()` guards against the UTF-8 BOM
  issue that silently broke the first .env variable on 10 Jul 2026, and since
  19 Jul 2026 it also silently rewrites `.env` without the BOM when one is
  found (VS Code kept re-adding it on save). Claude Code hooks in the
  workspace `.claude/settings.json` do the same fix at session start and
  after every Edit/Write, so a BOM in `.env` never needs manual attention.

- Always commit and push after completing any code change — never leave changes uncommitted at the end of a task.
- When a shipped change affects a Roadmap area in `PROJECT_SUMMARY.md`, update that area's completion percentage in the same commit.
