# Football Picks Bot

Read `PROJECT_SUMMARY.md` for the full project overview: architecture, file
structure, environment variables, deployment (Railway), Google Sheets layout,
current features, and known limitations.

## Delivery — Discord ONLY (18 Aug 2026)

**Telegram was removed from the entire repo on 18 Aug 2026.** There is no
`python-telegram-bot` dependency, no `TELEGRAM_*` env var, and no `telegram`
import anywhere — do not reintroduce one. Discord is the only delivery surface
for football and tennis alike, which changes how a failed send must be read: it
is no longer "the mirror missed one", it is the delivery failing. Two pieces of
content were TELEGRAM-ONLY and were ported rather than dropped — the weekly
summary text and the monthly calibration report (both now `weekly-cards`) — and
one, the Kelly stake, moved from the picks digest into the Core pick embed. The
Telegram picks digest itself is gone with no replacement: the per-pick embeds
already carry the same picks in richer form.

## Discord Delivery

`discord_bot.py` handles all Discord delivery (send-only, REST via
`requests` — no discord.py). Env vars: `DISCORD_BOT_TOKEN` plus
`DISCORD_CHANNELS_JSON`, a single-line JSON dict mapping the keys
`picks-cards`, `results-cards`, `weekly-cards`, `premier-league`,
`jupiler-pro-league`, `world-cup`, `bundesliga`, `la-liga`, `serie-a`,
`ligue-1`, `champions-league`, `europa-league`, `conference-league`, `tennis-picks`, `tennis-picks-lower`,
`tennis-results`, `usage` to Discord channel IDs. Fail-silent: `send_to_discord()` never raises, and any
missing token/key skips that piece without touching the rest of the flow.
Individual pick messages (league channels + `tennis-picks`) are Discord EMBEDS
built by `discord_bot.py`'s `build_pick_embed()` — never plain text; card and
result sends stay plain text/images. A **Core** football embed also carries a
`Stake` field (the Kelly figure, which lived only in the removed Telegram
digest); Extended embeds deliberately do not, because a stake on a pick outside
the tracked book would read as a claim on a bankroll it is excluded from.

Long text — the weekly summary, the monthly calibration report — must go through
`send_long_to_discord()`, which splits on line boundaries. Plain
`send_to_discord()` **truncates** at 2000 characters, which would silently eat
the tail of a report. Test all configured channels with
`python discord_bot.py --test`. Details in PROJECT_SUMMARY.md section 5b.

## Tennis Delivery — Discord-ONLY

Tennis has always been Discord-only; since 18 Aug 2026 so is football, so this
is no longer a difference between the two pipelines — what remains specific to
tennis is WHICH channels it uses. Tennis picks are split by rank tier: both players inside `TENNIS_RANK_THRESHOLD`
(default 150) → `tennis-picks`; either player outside or unranked →
`tennis-picks-lower` (the tier is also logged to the Sheet's 'Rank Tier'
column). The picks-failed alert goes to `tennis-picks`. Settled results go
to `tennis-results` (`run_all.py` `tennis_live_results_check`) — never the
football `results-cards` channel.
`TELEGRAM_TENNIS_CHANNEL_ID` was removed on 10 Jul 2026 and every remaining
`TELEGRAM_*` variable on 18 Aug 2026; do not reintroduce any of them.

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
`· EXTENDED · league rank n`). Only Core reaches the picks card, the `Stake` embed
field, the running total/bankroll columns, the Summary totals, and the
calibration/edge/CLV reports.

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

**The picks-failed alert goes to Discord `picks-cards` and must never fail quietly**
(18 Aug 2026). It was Telegram-only behind a missing-token guard that returned early, so
an exhausted API credit balance killed three consecutive whole slates (16-18 Aug 2026)
in silence and went unnoticed for three days. `_notify_picks_failed` therefore CHECKS the
return value of `send_to_discord` and logs an error when delivery did not land — keep that
check. The alert carries the first upstream error as a `Reason:` line: every competition
failing almost always has ONE shared cause, and naming it is what turns a three-day outage
into a same-morning fix. The channel is subscriber-facing, so that text is scrubbed of
model AND vendor names (`_scrub_model_names`) and truncated to `ALERT_DETAIL_MAX_CHARS`.
`tennis_main._notify_tennis_picks_failed` takes the same `detail` argument and is wired to
the API-failure path, which had been silent on every surface since it was written.

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

This is a **logging-level** guard on purpose: the card and the Discord embeds
both render the unfiltered `picks` list, so a repeated fixture still shows on the
card. Do not push this filter up into `analyse_with_claude` or the delivery
path.

## Google Sheets Quota — the read budget is shared and finite (1 Sep 2026)

Google allows **60 read requests per minute per service account, across the whole
spreadsheet** — football tabs, tennis tabs, the Opus tab and the usage ledger all draw on
one budget. Every read in `excel_tracker` is wrapped in `except: log.error(); return
<neutral>`, so exceeding it does not raise: it returns empty and the run continues.

That cost 128 picks over six days (20/22/26/27/28/29 Aug 2026). `calculate_kelly_stake`
read the entire Picks tab, `daily_picks_job` called it once per pick, and once the
per-league cap took slates past ~20 picks the loop drained the minute's budget — so the
`log_picks_batch` read immediately afterwards took the 429 and the whole slate went to
Discord and nowhere else. Full postmortem in PROJECT_SUMMARY.md, "Sheets quota loss".

- **Never put a full-sheet read inside a per-pick loop.** `calculate_kelly_stake` takes
  `breakdown=` for exactly this reason; read it once per run and pass it down. Any new
  per-pick enrichment that touches the sheet must be hoisted the same way. The rule is
  one read per RUN, not per pick.
- **`log_picks_batch` returns `BatchLogResult(written, skipped, failed)`, not an int.**
  `skipped` is the duplicate guard working as designed; `failed` is a **lost pick** — it
  was eligible, nothing wrote it, and nothing downstream will ever see it. Callers must
  route any non-zero `failed` through `main._notify_sheet_write_gap()`, which alerts the
  ops `usage` channel. A PARTIAL write alerts exactly as loudly as a total one: 25 of 29
  landing is not a good day, it is four picks lost. Do not collapse these three counts
  back into one number.
- **`with_sheets_retry()` re-raises when its attempts are exhausted.** It must never
  convert a permanent failure into a silent success — the caller decides what a failure
  means. Retries cover 429/5xx only; a malformed request fails fast.
- **Interval jobs in `run_all.py` are phase-shifted via `start_date` offsets.** Keep them
  apart, but never rely on it: interval phase drifts on every Railway restart, so it
  reduces collisions and cannot prevent them. Correctness has to hold without it.
- **One-off scripts must not run on import.** `_run_now.py` had no `__main__` guard, so
  `import _run_now` fired a full live picks run and posted to subscriber channels
  (1 Sep 2026). It and `_opus_restake_aug13.py` are guarded now — keep any new `_*.py`
  script the same way; importing a file must never deliver or write anything.

## Extra-Time Settlement — derive the margin, never the score (1 Sep 2026)

**The feed publishes no period scores. Do not go looking again.** Verified
exhaustively 1 Sep 2026: `football-get-match-detail` is 792 bytes of metadata
with no score at all, `football-get-match-score` gives only the final score, and
`-match-events / -statistics / -stats / -timeline / -lineups / -shotmap /
-momentum / -goals / -period / -halftime / -summary / -info / -h2h /
-player-stats / -odds / -live-matches / -list-events` **all 404**.
`status.halfs` holds period START TIMESTAMPS, never period scores.

- **The 90-minute MARGIN is derivable; the 90-minute SCORE is not.** Extra time
  in a two-legged tie means the aggregate was level at 90' of the second leg, so
  `h90 - a90 = (agg_away - final_away) - (agg_home - final_home)`. That settles
  **Match Winner, Double Chance and Asian Handicap** — all pay on the margin.
  **Over/Under and BTTS stay PENDING** unless a bound closes them: the margin
  does not pin the total. Do not "finish the job" by inventing a total.
- **`_regulation_goal_difference()` returning None means PENDING, always.** It
  returns None when the aggregate is missing, unparseable, below this leg's
  score for either side, or implies a margin unreachable inside the final score.
  Never settle a margin that failed those gates — a wrong margin books a real
  bet off arithmetic that did not hold.
- **`extra_time` means EXTRA TIME WAS ACTUALLY PLAYED**, read from
  `status.halfs.firstExtraHalfStarted` — not "the tie needed separating". A
  shootout straight after 90 minutes (CONMEBOL, many domestic cups) leaves the
  published score EQUAL to the 90-minute score and settles exactly for every
  market. When `halfs` is missing on a shootout, assume extra time: that costs a
  manual settlement, the other direction settles off the wrong score.
- **Reversed home/away is a LAST RESORT.** Exhaust the correct orientation
  across every candidate date first. Both orientations are real fixtures in a
  two-legged tie, so a greedy reversed match settles against the wrong leg.
- **`_normalise_team()` folds case, diacritics and whitespace — nothing else.**
  `ø æ å ð þ đ ł ß œ ı` need explicit transliteration because NFKD does not
  decompose them. Do not extend it to punctuation: looser matching starts
  hitting genuinely different clubs, and a wrong fixture settles a real bet off
  someone else's result.

Details, the validation set and the audit history in PROJECT_SUMMARY.md,
"Extra-time settlement" and "Fixture name matching".

## API Failure Visibility — the `usage` channel (18 Aug 2026)

`usage_tracker` owns the ops view of API health, separately from the
subscriber-facing picks-failed alert:

- **Immediate alert.** `alert_anthropic_failure(job, exc, model)` posts to the
  `usage` Discord channel when a call fails with an exhausted credit balance,
  carrying the verbatim error and the job that failed. Wired into every
  Anthropic failure path (`football-picks`, `football-core-select`,
  `tennis-picks`, `opus-shadow`). **Deduped per day**: a slate where all ten
  competitions fail posts ONE alert, not ten — do not remove that guard, an
  alert channel that cries wolf gets muted and then the next outage is silent
  again. Only credit-balance failures alert; a 429/529/network blip is recorded
  but not announced, because those self-heal and the picks-failed alert already
  covers a whole-slate loss.
- **`usage` is an OPS channel, so the alert is NOT scrubbed.** The raw error
  including vendor and model names goes out verbatim — that is the opposite of
  the picks-failed alert's rule (`main._scrub_model_names`), and deliberately so:
  the reader here is the operator, who needs the exact message.
- **Status line in the daily summary.** `build_daily_summary()` opens with the
  last Anthropic call's outcome and, when it failed, the age of the last
  *successful* call. A dead account otherwise produces no usage rows at all,
  which reads exactly like a quiet day with no fixtures — this line is what
  separates "nothing to do" from "nothing works", and is the backstop for the
  alert being missed.
- **Failures live in their own `API Failures` tab**, never the `API Usage` cost
  ledger. Every reader of that ledger counts rows as calls and sums the cost
  column; a zero-cost failure row would inflate the call count and dilute the
  per-job figures.

**Credit balance is not retrievable programmatically — do not add it.** Checked
against the live docs 18 Aug 2026: the Admin API has no balance endpoint, and
the Usage & Cost API (`/v1/organizations/cost_report`) reports *spend incurred*,
not *credit remaining*. It also needs a separate Admin API key
(`sk-ant-admin01-...`) and is unavailable to individual accounts entirely.
Remaining balance is Console-only (platform.claude.com/settings/billing). Never
put an estimated or inferred balance figure in the summary — a wrong number
there is worse than no number, because it would be trusted.

## Working Rules

- Load `.env` via `from env_loader import load_env; load_env()` — never call
  `dotenv.load_dotenv()` directly. `load_env()` guards against the UTF-8 BOM
  issue that silently broke the first .env variable on 10 Jul 2026, and since
  19 Jul 2026 it also silently rewrites `.env` without the BOM when one is
  found (VS Code kept re-adding it on save). Claude Code hooks in the
  workspace `.claude/settings.json` do the same fix at session start and
  after every Edit/Write, so a BOM in `.env` never needs manual attention.

- **Never let a credential reach the logs.** `httpx` logs the full request URL at
  INFO, so any SDK that puts a secret in the URL leaks it into Railway's log
  stream — `python-telegram-bot` did exactly that with its bot token (5 plaintext
  copies before 18 Aug 2026). `httpx`/`httpcore` are pinned to WARNING in both
  `main.py` and `tennis_main.py` (the standalone entry point that never imports
  `main`). The Anthropic SDK is httpx-backed but sends its key as a header, and
  RapidAPI/Odds API go through `requests`, which never logs URLs — so no key
  leaks today. Re-check this whenever an httpx-backed SDK is added.

- Always commit and push after completing any code change — never leave changes uncommitted at the end of a task.
- When a shipped change affects a Roadmap area in `PROJECT_SUMMARY.md`, update that area's completion percentage in the same commit.
