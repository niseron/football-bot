"""Manual one-shot trigger — fetch fixtures, run Claude, send to Telegram, log picks."""
import asyncio
import logging
import sys

from env_loader import load_env

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from main import (
    fetch_upcoming_matches,
    partition_fixtures,
    enrich_with_context,
    enrich_picks_with_real_odds,
    analyse_with_claude,
    format_telegram_message,
    send_to_telegram,
    _send_photo,
)
from excel_tracker import calculate_kelly_stake
from tracker import log_pick, picks_exist_for_session


async def run():
    force = "--force" in sys.argv
    session = "morning"

    log.info("Manual run triggered")

    if not force and picks_exist_for_session(session):
        log.info("Picks already logged for today — skipping (use --force to override)")
        return

    all_matches = fetch_upcoming_matches()
    log.info("Fetched %d total matches (next 48 hours)", len(all_matches))

    fixtures_by_league = partition_fixtures(all_matches)
    if not fixtures_by_league:
        log.info("No upcoming fixtures found — nothing to send")
        return

    for league, fx in fixtures_by_league.items():
        log.info("  %s: %d fixtures", league, len(fx))

    try:
        enrich_with_context(fixtures_by_league)
    except Exception as exc:
        log.warning("Context enrichment failed — proceeding without form/H2H data: %s", exc)

    picks = analyse_with_claude(fixtures_by_league)
    log.info("Claude returned %d pick(s)", len(picks))

    try:
        enrich_picks_with_real_odds(picks)
    except Exception as exc:
        log.warning("Real odds enrichment failed — proceeding with Claude odds only: %s", exc)

    try:
        for pick in picks:
            pick["kelly"] = calculate_kelly_stake(
                pick["bet_type"], float(pick["odds"]), pick.get("confidence", "")
            )
    except Exception as exc:
        log.warning("Kelly stake calculation failed (picks will send without it): %s", exc)

    for pick in picks:
        try:
            claude_prob = pick.get("probability")
            log_pick(
                match=pick["match"],
                league=pick["league"],
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
                session=session,
                claude_prob=float(claude_prob) if claude_prob is not None else None,
                market_prob=pick.get("market_prob"),
            )
        except Exception as exc:
            log.warning("Failed to log pick: %s", exc)

    await send_to_telegram(format_telegram_message(picks, header="Football Picks"))
    log.info("Sent %d pick(s) to Telegram", len(picks))

    try:
        from card_generator import generate_picks_card
        card = generate_picks_card(picks, session=session)
        await _send_photo(card)
        log.info("Picks card sent: %s", card.name)
    except Exception as exc:
        log.warning("Picks card failed (non-fatal): %s", exc)


asyncio.run(run())
