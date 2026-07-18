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
`ligue-1`, `tennis-picks`, `tennis-picks-lower`,
`tennis-results` to Discord channel IDs. Fail-silent: `send_to_discord()` never raises, and any
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

## Working Rules

- Load `.env` via `from env_loader import load_env; load_env()` — never call
  `dotenv.load_dotenv()` directly. `load_env()` guards against the UTF-8 BOM
  issue that silently broke the first .env variable on 10 Jul 2026.

- Always commit and push after completing any code change — never leave changes uncommitted at the end of a task.
- When a shipped change affects a Roadmap area in `PROJECT_SUMMARY.md`, update that area's completion percentage in the same commit.
