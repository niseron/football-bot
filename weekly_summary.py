"""
Standalone weekly summary scheduler.
Posts a performance summary to Telegram every Monday at 09:05 Europe/Brussels.

Run alongside main.py:
    python weekly_summary.py
"""
import asyncio
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot

from excel_tracker import get_weekly_data

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")


def _esc(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def build_weekly_message(data: dict) -> str:
    if not data:
        return (
            "*ℹ️ Weekly Summary*\n\n"
            "_No picks logged yet\\. The bot will start posting picks once fixtures are available\\._"
        )

    total   = data["total_picks"]
    wins    = data["wins"]
    losses  = data["losses"]
    pending = data["pending"]
    rate    = data["win_rate"]
    pnl_w   = data["pnl_week"]
    rt      = data["running_total"]
    best    = data["best_pick"]
    w_start = data["week_start"]
    w_end   = data["week_end"]

    pnl_sign = "+" if pnl_w >= 0 else ""
    rt_sign  = "+" if rt    >= 0 else ""

    lines = [
        f"*\U0001f4ca Weekly Performance Summary*",
        f"_{_esc(w_start)} \\- {_esc(w_end)}_\n",
    ]

    if total == 0:
        # Nothing sent this week yet
        lines.append("_No picks sent this week yet\\._")
        pending_all = data.get("pending_picks", [])
        if pending_all:
            lines.append(f"\n*Pending picks from earlier:*")
            for p in pending_all[:5]:
                lines.append(
                    f"  • {_esc(p['match'])} \\| "
                    f"{_esc(p['bet_type'])} → {_esc(str(p['pick']))} "
                    f"@ {_esc(str(p['odds']))}"
                )
        return "\n".join(lines)

    lines += [
        f"\U0001f3af *Picks this week:* {total}",
        f"✅ Wins: *{wins}*  ❌ Losses: *{losses}*  ⏳ Pending: *{pending}*",
        f"\U0001f4c8 Win rate: *{_esc(str(rate))}%*",
        f"\U0001f4b0 P/L this week: *{_esc(pnl_sign + f'{pnl_w:.2f}')} units*",
    ]

    if best:
        best_pnl  = best.get("pnl", 0) or 0
        best_odds = best.get("odds", 0) or 0
        lines.append(
            f"\U0001f3c6 Best pick: _{_esc(str(best['match']))} "
            f"— {_esc(str(best['pick']))} @ {_esc(str(best_odds))} "
            f"\\({_esc('+' + f'{best_pnl:.2f}')} units\\)_"
        )
    elif pending > 0:
        lines.append(f"⏳ *{pending} pick\\(s\\) still pending result*")

    lines.append(f"\U0001f4c9 Running total P/L: *{_esc(rt_sign + f'{rt:.2f}')} units*")
    lines.append("\n_Bet responsibly\\. Past performance does not guarantee future results\\._")

    return "\n".join(lines)


async def post_weekly_summary():
    log.info("Building weekly summary...")
    data    = get_weekly_data()
    message = build_weekly_message(data)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=message,
        parse_mode="MarkdownV2",
    )
    log.info("Weekly summary posted to Telegram")


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        post_weekly_summary,
        "cron",
        day_of_week="mon",
        hour=9,
        minute=5,
        timezone="Europe/Brussels",
    )
    scheduler.start()
    log.info("Weekly summary scheduler started — posts every Monday at 09:05 Europe/Brussels")

    # Uncomment to test immediately on startup:
    # await post_weekly_summary()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
