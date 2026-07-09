# Football Picks Bot

Read `PROJECT_SUMMARY.md` for the full project overview: architecture, file
structure, environment variables, deployment (Railway), Google Sheets layout,
current features, and known limitations.

## Discord Delivery

`discord_bot.py` mirrors Telegram content to Discord (send-only, REST via
`requests` — no discord.py). Env vars: `DISCORD_BOT_TOKEN` plus
`DISCORD_CHANNELS_JSON`, a single-line JSON dict mapping the keys
`picks-cards`, `results-cards`, `weekly-cards`, `premier-league`,
`jupiler-pro-league`, `world-cup` to Discord channel IDs. Delivery is purely
additive and fail-silent: `send_to_discord()` never raises, and any missing
token/key skips that piece without touching the Telegram flow. Test all
configured channels with `python discord_bot.py --test`. Details in
PROJECT_SUMMARY.md section 5b.

## Working Rules

- Always commit and push after completing any code change — never leave changes uncommitted at the end of a task.
- When a shipped change affects a Roadmap area in `PROJECT_SUMMARY.md`, update that area's completion percentage in the same commit.
