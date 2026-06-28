"""Manual one-shot trigger — fetch fixtures, run Claude, send to Telegram, log picks."""
import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from main import (
    _kickoff_hour_utc,
    fetch_upcoming_matches,
    partition_fixtures,
    analyse_with_claude,
    format_telegram_message,
    send_to_telegram,
)
from tracker import log_pick, picks_exist_for_session


async def run():
    force = "--force" in sys.argv
    evening = "--evening" in sys.argv

    if evening:
        log.info("Manual evening run triggered")
        session = "evening"
        header = "Evening Picks"
    else:
        log.info("Manual morning run triggered")
        session = "morning"
        header = "Football Picks"

    if not force and picks_exist_for_session(session):
        log.info(
            "%s picks already logged for today — skipping (use --force to override)",
            session.capitalize(),
        )
        return

    all_matches = fetch_upcoming_matches()
    log.info("Fetched %d total matches (next 48 hours)", len(all_matches))

    if evening:
        all_matches = [m for m in all_matches if (_kickoff_hour_utc(m) or 0) >= 18]
        log.info("Evening filter: %d matches with kickoff >= 18:00 UTC", len(all_matches))

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
                session=session,
            )
        except Exception as exc:
            log.warning("Failed to log pick: %s", exc)

    await send_to_telegram(format_telegram_message(picks, header=header))
    log.info("Sent %d pick(s) to Telegram", len(picks))


asyncio.run(run())
