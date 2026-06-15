"""Manual one-shot trigger — fetch fixtures, run Claude, send to Telegram, log picks."""
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from main import (
    fetch_upcoming_matches,
    partition_fixtures,
    analyse_with_claude,
    format_telegram_message,
    send_to_telegram,
)
from tracker import log_pick


async def run():
    log.info("Manual run triggered")

    all_matches = fetch_upcoming_matches()
    log.info("Fetched %d total matches (next 48 hours)", len(all_matches))

    fixtures_by_league = partition_fixtures(all_matches)
    if not fixtures_by_league:
        log.info("No upcoming fixtures found — nothing to send")
        return

    for league, fx in fixtures_by_league.items():
        log.info("  %s: %d fixtures", league, len(fx))

    picks = analyse_with_claude(fixtures_by_league)
    log.info("Claude returned %d pick(s)", len(picks))

    for pick in picks:
        try:
            log_pick(
                match=pick["match"],
                league=pick["league"],
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
            )
        except Exception as exc:
            log.warning("Failed to log pick: %s", exc)

    await send_to_telegram(format_telegram_message(picks))
    log.info("Sent %d pick(s) to Telegram", len(picks))


asyncio.run(run())
