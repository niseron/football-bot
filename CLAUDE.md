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

## Pick Tiers — Football Only (13 Aug 2026, cap made per-league 15 Aug 2026)

Football makes **one Claude call per competition**, each returning up to
`MAX_PICKS_PER_LEAGUE = 10` conviction-ranked picks for that competition alone — so a
busy day yields 30+ picks across up to 10 competitions. A global selection step
(`_select_core_picks`) then marks the best `CORE_PICKS_PER_RUN = 5` of the whole slate
**Core**; every other pick is **Extended** (so at most 10 per competition per day).
Both tiers are logged to the Picks tab, settled by the same `auto_results.py` path, and
posted to their league's Discord channel (Extended embeds are labelled
`· EXTENDED · league rank n`). Only Core reaches the picks card, the Telegram post, the
running total/bankroll columns, the Summary totals, and the calibration/edge/CLV reports.

**Tier does not follow from a rank number.** It did until 15 Aug 2026 (ranks 1-5 Core,
6-10 Extended of one global list); now `league_rank` is the pick's position *inside its
own competition* and a league's rank-1 pick is Extended whenever it loses the global cut.
Core picks carry `rank` 1-5; Extended picks carry `rank = None`. Never re-derive the tier
from a position, and never present a bare `#n` to a reader — the number is league-scoped.

**Never pad, per league.** Ten per competition is an allowance, not a target: an empty
list for a competition is a correct answer, and most competitions return 0-3. The same
holds for the selection step — Core is capped at 5, never padded up to 5.

**Failure is per competition.** One league's call failing is logged and skipped; the rest
of the slate still delivers. Only a run where every competition failed raises and sends
the picks-failed alert — never one alert per league. A failed Core selection call
falls back to a deterministic order; it decides ordering only, never which bets exist.

**The picks-failed alert goes to BOTH Telegram and Discord `picks-cards`, independently**
(18 Aug 2026). It was Telegram-only, behind a missing-token guard that returned early, so
nothing reached the surface the outage is actually noticed on: an exhausted API credit
balance killed three consecutive whole slates (16-18 Aug 2026) in silence. Neither surface
may gate the other. The alert also carries the first upstream error as a `Reason:` line —
every competition failing almost always has ONE shared cause, and naming it is what turns
a three-day outage into a same-morning fix. Both channels are subscriber-facing, so that
text is scrubbed of model AND vendor names (`_scrub_model_names`) and truncated to
`ALERT_DETAIL_MAX_CHARS`. `tennis_main._notify_tennis_picks_failed` took the same detail
argument and was finally wired to the API-failure path, which had been silent on every
surface since it was written.

**Volume plumbing that must stay batched/paced.** Sheet writes go through
`log_picks_batch` (one read, one append, one repaint — `log_to_excel` costs ~4 API calls
and a full-sheet repaint *per pick*), and Discord pick embeds are paced by
`DISCORD_PICK_SEND_DELAY`. Picks-run Odds API enrichment caches per COMPETITION, not per
fixture — one 3-unit `/odds` call serves all of that competition's picks.

`excel_tracker._core_rows()` is the SINGLE filter enforcing the Core/Extended reporting
split — route any new Core aggregation through it rather than writing an inline tier
check. A **blank** `Pick Tier` cell means Core, which is what keeps every row logged
before 13 Aug 2026 valid without a backfill: never fill that column in on historical rows.

Tennis is untouched by all of this — no tiers, no ranking, `tennis_*` modules
unchanged. Details in PROJECT_SUMMARY.md, "Ranked picks and the Core/Extended split".

## Opus 5 Shadow Experiment — Football Only (13 Aug 2026)

`opus_shadow.py` + `opus_tracker.py` run `claude-opus-5` over the same enriched
fixture pool `daily_picks_job` just used (no second RapidAPI fetch). **The model
stopped being the only variable on 15 Aug 2026:** production went per-competition
while the shadow deliberately stayed ONE whole-slate call capped at 10, on its own
frozen `OPUS_MAX_PICKS_PER_RUN` / `OPUS_CORE_PICKS_PER_RUN` and the unchanged
`main.SYSTEM_PROMPT` (byte-identical to the pre-change prompt). Both models still
answer "the best 5 bets in this pool", so Core-vs-Core still means something — but
the harness differs, and fanning the shadow out would take it from ~$4.29 to
~$17-25/month. Do not change that by inheriting a production constant; re-cost first. Picks go to the `Opus Shadow Picks` tab and the
`opus-shadow` Discord channel as text embeds (both tiers, SIM stakes, no cards);
**results go to the sheet only, never to Discord**.

- **Master gate:** the whole experiment is inert unless `opus-shadow` is in
  `DISCORD_CHANNELS_JSON`. Removing that key on Railway turns it off — no model
  call, no cost, no sheet or Odds API usage.
- **SIM staking is flat: €1000 start, €100 per bet** (`OPUS_STARTING_BANKROLL` /
  `OPUS_FLAT_STAKE`, 13 Aug 2026 — no Kelly, so the bankroll column reads as pick
  quality rather than staking-model performance). `recalculate_opus_running_totals`
  rebuilds the bankroll from each row's own Stake cell, so changing the sizing means
  backfilling the existing rows too.
- **The tab is styled like Picks** via `apply_opus_formatting()` (own function —
  `excel_tracker._apply_formatting` is hard-wired to the Picks worksheet; only the
  colour constants are shared). Runs after the batch is logged and again in
  `finalize_opus_sheet`, always *after* the recalculation.
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

## Duplicate Logging Guard (13 Aug 2026)

`fetch_upcoming_matches` covers a 48-hour window, so a fixture kicking off
tomorrow is offered to Claude today **and** tomorrow. `analyse_with_claude`
dedupes `(match, bet_type)` only *within one run* (since 15 Aug 2026: inside each
competition's response, then once more across the merged list), and the sheet
writers' original guard was date-scoped — so the same bet was logged twice and one match
settled both rows, booking P&L twice.

`excel_tracker.log_to_excel` (and its batch sibling `log_picks_batch`, which shares
the same `_duplicate_skip_reason` guard and also checks rows staged earlier in the
same batch) and `opus_tracker.log_opus_pick` skip when **any unsettled row exists for
that fixture**, regardless of bet type — one fixture carries at most one open bet.
Picks are logged Core-first, so a collision resolves in Core's favour. Do not narrow this key: `(match, bet_type, pick)`
let opposite sides of one market through (Canada vs Morocco BTTS Yes then No,
3-4 Jul 2026) and `(match, bet_type)` still allowed two bet types on one
fixture, which nothing downstream treats as correlated.

A third tier value, `PICK_TIER_DUPLICATE = "Duplicate"`, marks rows excluded by
the retroactive correction. Duplicate rows are neither Core nor Extended, so
they drop out of every metric automatically — do not add them to `_core_rows`
or `_extended_rows`, and do not "clean up" the tag: it is the audit trail.

This is a **logging-level** guard on purpose: cards, Telegram and Discord all
render the unfiltered `picks` list, so a repeated fixture still shows on the
card. Do not push this filter up into `analyse_with_claude` or the delivery
path.

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
