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
    _kickoff_lookup,
    _pick_log_entry,
    _send_photo,
)
from excel_tracker import PICK_TIER_CORE, calculate_kelly_stake
from tracker import log_picks_batch, picks_exist_for_session


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

    try:
        kickoff_lookup = _kickoff_lookup(fixtures_by_league)
    except Exception as exc:
        log.warning("Kickoff lookup build failed (non-fatal): %s", exc)
        kickoff_lookup = {}

    # pick_tier, kickoff_utc and market_odds must all be passed through here —
    # _pick_log_entry is the same builder daily_picks_job uses, so this script
    # cannot drift from it. Before 13 Aug 2026 this script omitted those fields,
    # which was harmless while a run produced 5 picks and every pick was Core.
    # Once a run could return more, omitting pick_tier meant the "Core" default
    # labelled Extended picks as Core too — so a single `python _run_now.py`
    # would have fed them straight into the running total, the Summary tab and
    # the calibration / edge / CLV baseline that the tier split exists to
    # protect. Written as one batch for the same reason daily_picks_job is: a
    # per-league run can produce 30+ picks.
    try:
        written = log_picks_batch(
            [_pick_log_entry(p, kickoff_lookup) for p in picks], session=session,
        )
        log.info("Logged %d of %d pick(s) to the sheet", written, len(picks))
    except Exception as exc:
        log.warning("Failed to log picks: %s", exc)

    # Telegram and the card carry CORE only, matching daily_picks_job — this
    # script is a stand-in for that job, so it must not publish a different book.
    core_picks = sorted(
        [p for p in picks if p.get("pick_tier", PICK_TIER_CORE) == PICK_TIER_CORE],
        key=lambda p: p.get("rank") or 99,
    )
    extended = len(picks) - len(core_picks)

    await send_to_telegram(format_telegram_message(core_picks, header="Football Picks"))
    log.info("Sent %d Core pick(s) to Telegram (%d Extended logged, not sent)",
             len(core_picks), extended)

    try:
        from card_generator import generate_picks_card
        card = generate_picks_card(core_picks, session=session)
        await _send_photo(card)
        log.info("Picks card sent: %s", card.name)
    except Exception as exc:
        log.warning("Picks card failed (non-fatal): %s", exc)


asyncio.run(run())
