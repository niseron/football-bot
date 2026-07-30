"""
One-off: post today's UEFA Conference League picks to Discord 'picks-cards'.

Context (30 Jul 2026): Conference League tracking shipped after the 12:00
picks job had already run, so today's card went out without it. This posts a
supplementary Conference-League-only card for tonight's Q2 fixtures.

Deliberately narrow, unlike the full daily job:
  - Conference League fixtures only (other competitions already posted at 12:00)
  - Discord 'picks-cards' only — no Telegram, no per-league channels, so
    today's already-delivered picks are not duplicated
  - No Odds API call: there is no key for Conference League *qualifying*, and
    the monthly free-tier quota was down to 2 requests on 30 Jul 2026
  - Picks ARE logged to the tracker, so auto_results can settle them normally

Run:  python _post_conference_league_jul30.py [--dry-run]
"""
import argparse
import logging
import sys

from env_loader import load_env

load_env()

from card_generator import generate_picks_card          # noqa: E402
from discord_bot import send_to_discord                 # noqa: E402
from excel_tracker import calculate_kelly_stake         # noqa: E402
from main import (                                      # noqa: E402
    analyse_with_claude,
    enrich_with_context,
    fetch_upcoming_matches,
    partition_fixtures,
    _kickoff_lookup,
)
from tracker import log_pick                            # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

LEAGUE = "Conference League"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="analyse and render the card but send nothing")
    args = ap.parse_args()

    all_matches = fetch_upcoming_matches()
    fixtures = partition_fixtures(all_matches).get(LEAGUE, [])
    if not fixtures:
        log.error("No upcoming %s fixtures left in the window — nothing to post.", LEAGUE)
        sys.exit(1)
    log.info("%d upcoming %s fixtures", len(fixtures), LEAGUE)

    only_conference = {LEAGUE: fixtures}

    try:
        enrich_with_context(only_conference)
    except Exception as exc:
        log.warning("Context enrichment failed — proceeding without form/H2H: %s", exc)

    picks = analyse_with_claude(only_conference)
    if not picks:
        log.error("Claude returned no picks — aborting.")
        sys.exit(1)

    try:
        kickoff_lookup = _kickoff_lookup(only_conference)
    except Exception as exc:
        log.warning("Kickoff lookup failed (non-fatal): %s", exc)
        kickoff_lookup = {}

    for pick in picks:
        try:
            pick["kelly"] = calculate_kelly_stake(
                pick["bet_type"], float(pick["odds"]), pick.get("confidence", "")
            )
        except Exception as exc:
            log.warning("Kelly stake failed for %s: %s", pick.get("match"), exc)

    print("\n" + "=" * 68)
    for i, p in enumerate(picks, 1):
        print(f'{i}. {p["match"]}')
        print(f'   {p["bet_type"]}: {p["pick"]} @ {p["odds"]} '
              f'({p.get("confidence")}, p={p.get("probability")})')
    print("=" * 68 + "\n")

    card = generate_picks_card(picks, session="morning")
    log.info("Card rendered: %s", card)

    if args.dry_run:
        log.info("--dry-run: nothing sent, nothing logged.")
        return

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
                session="morning",
                claude_prob=float(claude_prob) if claude_prob is not None else None,
                market_prob=None,   # no Odds API call — see module docstring
                kickoff_utc=kickoff_lookup.get(pick["match"], ""),
            )
        except Exception as exc:
            log.warning("Failed to log pick %s: %s", pick.get("match"), exc)

    header = (
        "⚽ **UEFA CONFERENCE LEAGUE — TONIGHT'S PICKS**\n"
        "Conference League is now tracked, so its games join the daily card "
        "from here on. These are today's fixtures, posted separately because "
        "the competition was added after this morning's run."
    )
    if not send_to_discord("picks-cards", message=header):
        log.error("Header send to 'picks-cards' failed — check the Discord token/mapping.")
    if send_to_discord("picks-cards", image_path=card):
        log.info("Conference League picks card posted to 'picks-cards': %s", card.name)
    else:
        log.error("Card send to 'picks-cards' FAILED.")


if __name__ == "__main__":
    main()
