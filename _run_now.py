"""Manual one-shot trigger — fetch fixtures, run Claude, log picks, post to Discord."""
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
    _kickoff_lookup,
    _pick_log_entry,
    _discord_pick_embed,
    DISCORD_LEAGUE_CHANNEL_KEYS,
    DISCORD_PICK_SEND_DELAY,
)
from discord_bot import send_to_discord
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

    log.info("%d Core pick(s), %d Extended — Core goes on the card",
             len(core_picks), extended)

    card = None
    try:
        from card_generator import generate_picks_card
        card = generate_picks_card(core_picks, session=session)
        log.info("Picks card generated: %s", card.name)
    except Exception as exc:
        log.warning("Picks card failed (non-fatal): %s", exc)

    # Discord delivery — mirrors daily_picks_job exactly, and is now the whole
    # of delivery. Omitted until 18 Aug 2026, which meant a manual recovery run
    # published a DIFFERENT book to a different audience than the job it stands
    # in for: the Sheet got the picks, the league channels got nothing. Both
    # tiers post to their league channel; the embed marks which is which, and
    # the send is paced for the same reason it is in the job — one competition
    # can send 10 embeds back to back and send_to_discord retries a 429 exactly
    # once. send_to_discord never raises.
    try:
        if card is not None:
            send_to_discord("picks-cards", image_path=card)
        sent = 0
        for pick in picks:
            channel_key = DISCORD_LEAGUE_CHANNEL_KEYS.get(pick.get("league", ""))
            if not channel_key:
                continue
            if sent:
                await asyncio.sleep(DISCORD_PICK_SEND_DELAY)
            send_to_discord(channel_key, embed=_discord_pick_embed(pick))
            sent += 1
        log.info("Discord: sent %d of %d pick(s) to their league channels", sent, len(picks))
    except Exception as exc:
        log.warning("Discord picks delivery failed (non-fatal): %s", exc)


asyncio.run(run())
