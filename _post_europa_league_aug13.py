"""
One-off: post today's UEFA Europa League picks, Europa League fixtures ONLY.

Context (13 Aug 2026): Europa League tracking shipped after the 12:00 picks job
had already run, so today's card went out with 5 Conference League picks and no
Europa ones — see PROJECT_SUMMARY.md "Europa League added". Twelve Europa League
Q3 fixtures kick off 17:00-19:00Z tonight, so this posts a supplementary,
Europa-League-only run for them. Same shape as _post_conference_league_jul30.py.

Deliberately narrow, unlike the full daily job:
  - Europa League fixtures only. analyse_with_claude() is handed ONLY that
    bucket, so it cannot return a pick for any other competition — today's 5
    already-delivered Conference League picks are outside its input entirely
    and cannot be touched, re-analysed or re-logged.
  - Discord only: per-pick embeds to 'europa-league' (BOTH tiers), plus one
    picks card to 'picks-cards' behind a short explanatory header.
    NO Telegram (today's daily card already went out at 12:00 — a second one
    would be a new outward post) and NO other league channel.
  - No Odds API call: there is no key for Europa League qualifying
    ('soccer_uefa_europa_league_qualification' is 404 UNKNOWN_SPORT) and the
    league-phase key returns an empty event list until September, so the call
    could only ever return nothing. Picks are Claude-odds-only, exactly as
    Conference League qualifying already is.
  - Picks ARE logged to the Picks tab with their Pick Tier, so auto_results
    settles them tonight on the normal path.

TIERING — every pick from this run is logged as EXTENDED, deliberately.

The standard 13 Aug 2026 split would make ranks 1-5 Core. That is overridden
here because today's 12:00 job already logged 5 Conference League picks as the
day's Core book: adding 5 more Core picks would make 13 Aug a 10-Core day,
double the baseline's usual daily rate, in the very series the tier split
exists to protect — and on the same date that already carries the prompt-regime
boundary. Logging the whole supplementary run as Extended keeps the Core series
at a clean 5 picks for the day while these six still settle tonight, still post
to Discord, and still earn their own P&L in the tier breakdown.

Consequence, stated plainly: these six do NOT feed the running total, the
bankroll column, the Summary headline figures, or calibration / edge / CLV.
They are measured only in the Pick Tier Breakdown. That is the intended trade.

Because no pick is Core, the card is rendered from the run's TOP 5 BY RANK
rather than from the (empty) Core set — otherwise a supplementary run of this
shape could never produce a card at all.

Run:  python _post_europa_league_aug13.py [--dry-run]
      --dry-run analyses, renders the card and prints everything, but sends
      nothing and logs nothing.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from env_loader import load_env

load_env()

from card_generator import generate_picks_card                    # noqa: E402
from discord_bot import send_to_discord                           # noqa: E402
from excel_tracker import (                                       # noqa: E402
    PICK_TIER_CORE,
    PICK_TIER_EXTENDED,
    calculate_kelly_stake,
)
from main import (                                                # noqa: E402
    CORE_PICKS_PER_RUN,
    # MAX_PICKS_PER_RUN was imported here and never used; it ceased to exist on
    # 15 Aug 2026 when the cap became per-league (main.MAX_PICKS_PER_LEAGUE).
    _discord_pick_embed,
    _kickoff_lookup,
    analyse_with_claude,
    enrich_with_context,
    fetch_upcoming_matches,
    partition_fixtures,
)
from tracker import log_pick                                      # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

LEAGUE = "Europa League"

# Every pick from this run is logged at this tier — see the module docstring.
FORCE_TIER = PICK_TIER_EXTENDED

# How many picks the card shows. Mirrors generate_picks_card's own internal
# slice; named here because with no Core picks the card is built from the run's
# top N by rank instead of from the Core set.
CARD_PICKS = 5

# Where --dry-run parks the picks it analysed, so --use-saved can send exactly
# what was reviewed. analyse_with_claude() calls Claude fresh on every run and
# its output is not deterministic — a second invocation returns a different set
# (measured on 13 Aug 2026: one dry run produced 6 picks, the next 8, with
# different fixtures). Without this, "review the dry run, then send" would ship
# picks nobody had actually looked at, which defeats the point of the gate.
SAVED_PICKS = Path(__file__).parent / "cards" / "_europa_aug13_picks.json"

HEADER = (
    "⚽ **UEFA EUROPA LEAGUE — TONIGHT'S PICKS**\n"
    "Europa League is now tracked, so its games join the daily card from here on. "
    "These are tonight's Q3 fixtures, posted separately because the competition "
    "was added after this morning's run — they are extra picks alongside today's "
    "main card, not a replacement for it."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="analyse and render the card but send nothing and log nothing; "
                         "saves the analysed picks for a later --use-saved send")
    ap.add_argument("--use-saved", action="store_true",
                    help="send the picks saved by the last --dry-run instead of "
                         "re-analysing, so what was reviewed is what ships")
    args = ap.parse_args()

    if args.use_saved:
        if not SAVED_PICKS.exists():
            log.error("No saved picks at %s — run --dry-run first.", SAVED_PICKS)
            sys.exit(1)
        saved = json.loads(SAVED_PICKS.read_text(encoding="utf-8"))
        picks, kickoff_lookup = saved["picks"], saved["kickoff_lookup"]
        log.info("Loaded %d saved pick(s) from %s — no re-analysis.",
                 len(picks), SAVED_PICKS.name)
        _deliver(picks, kickoff_lookup, dry_run=False)
        return

    all_matches = fetch_upcoming_matches()
    # partition_fixtures drops anything already kicked off (_is_upcoming), so a
    # late run simply analyses fewer fixtures rather than picking a started game.
    fixtures = partition_fixtures(all_matches).get(LEAGUE, [])
    if not fixtures:
        log.error("No upcoming %s fixtures left in the window — nothing to post.", LEAGUE)
        sys.exit(1)
    log.info("%d upcoming %s fixtures still pre-kickoff", len(fixtures), LEAGUE)

    only_europa = {LEAGUE: fixtures}

    try:
        enrich_with_context(only_europa)
    except Exception as exc:
        log.warning("Context enrichment failed — proceeding without form/H2H: %s", exc)

    picks = analyse_with_claude(only_europa)
    if not picks:
        log.error("Claude returned no picks — aborting.")
        sys.exit(1)

    # Guard the one thing that would corrupt today's book: a pick for any
    # competition other than Europa League cannot legitimately appear here.
    stray = [p for p in picks if p.get("league") != LEAGUE]
    if stray:
        log.error("Refusing to continue — analysis returned %d non-%s pick(s): %s",
                  len(stray), LEAGUE, "; ".join(str(p.get("match")) for p in stray))
        sys.exit(1)

    try:
        kickoff_lookup = _kickoff_lookup(only_europa)
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

    # Override the rank-derived tier: this whole run is Extended.
    for pick in picks:
        pick["pick_tier"] = FORCE_TIER
    log.info("Tier override: all %d pick(s) logged as %s", len(picks), FORCE_TIER)

    if args.dry_run:
        SAVED_PICKS.parent.mkdir(exist_ok=True)
        SAVED_PICKS.write_text(
            json.dumps({"picks": picks, "kickoff_lookup": kickoff_lookup}, indent=2),
            encoding="utf-8",
        )
        log.info("Saved %d pick(s) to %s — send them with --use-saved.",
                 len(picks), SAVED_PICKS.name)

    _deliver(picks, kickoff_lookup, dry_run=args.dry_run)


def _deliver(picks: list[dict], kickoff_lookup: dict, *, dry_run: bool) -> None:
    """Print the run, render the card, then (unless dry_run) log and send."""
    card_picks = picks[:CARD_PICKS]   # top N by rank; no Core set to draw from

    print("\n" + "=" * 78)
    print(f"{LEAGUE.upper()} SUPPLEMENTARY RUN — {len(picks)} pick(s), "
          f"ALL logged as {FORCE_TIER}")
    print("=" * 78)
    for p in picks:
        kelly = p.get("kelly") or {}
        on_card = "card" if p in card_picks else "    "
        print(f'  #{p.get("rank"):<2} [{p["pick_tier"]:8}] {on_card}  {p["match"]}')
        print(f'      {p["bet_type"]}: {p["pick"]} @ {p["odds"]} '
              f'({p.get("confidence")}, p={p.get("probability")}, '
              f'stake={kelly.get("stake", "n/a")})')
        print(f'      kickoff {kickoff_lookup.get(p["match"], "?")}')
    print("=" * 78)
    print(f"ALL {len(picks)} picks are Extended tier: logged and settled tonight,")
    print("posted to Discord, and measured in the Summary tab's Pick Tier Breakdown —")
    print("but excluded from the running total, bankroll, Summary headline figures")
    print("and calibration / edge / CLV. Today's Core book stays the 5 Conference")
    print("League picks logged at 12:00.")
    print("=" * 78 + "\n")

    card = generate_picks_card(card_picks, session="morning") if card_picks else None
    if card is not None:
        log.info("Card rendered (top %d by rank): %s", len(card_picks), card)
    else:
        log.warning("No picks to render — no card.")

    if dry_run:
        print("--dry-run: nothing sent, nothing logged.")
        if card is not None:
            print(f"Card written to: {card}")
        print(f"Picks saved to : {SAVED_PICKS}")
        print("Send exactly these with: python _post_europa_league_aug13.py --use-saved")
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
                pick_tier=pick.get("pick_tier", FORCE_TIER),
            )
        except Exception as exc:
            log.warning("Failed to log pick %s: %s", pick.get("match"), exc)

    # Per-pick embeds — BOTH tiers, europa-league only. _discord_pick_embed
    # labels Extended picks '· EXTENDED #n' in the author line.
    sent = 0
    for pick in picks:
        if send_to_discord("europa-league", embed=_discord_pick_embed(pick)):
            sent += 1
        else:
            log.error("Embed send FAILED for %s", pick.get("match"))
    log.info("Posted %d/%d embed(s) to 'europa-league'", sent, len(picks))

    if not send_to_discord("picks-cards", message=HEADER):
        log.error("Header send to 'picks-cards' failed — check the Discord token/mapping.")
    if card is not None:
        if send_to_discord("picks-cards", image_path=card):
            log.info("Europa League picks card posted to 'picks-cards': %s", card.name)
        else:
            log.error("Card send to 'picks-cards' FAILED.")


if __name__ == "__main__":
    main()
