"""One-off recovery — repost 18 Jul 2026 content that Discord rejected with 403.

The 12:00 job ran fine (Sheet + Telegram delivered) but every football-channel
send failed: the server's permission overwrites dropped Send Messages for the
bot role everywhere except #jupiler-pro-league. Run this AFTER restoring the
bot's Send/Embed/Attach permissions. It only re-sends the missed Discord
pieces — it does not touch the Sheet, Telegram, or picks.db.

Sources: pick fields from the Picks sheet rows for 18-Jul-2026; reasoning
recovered from the Railway deploy log of the 12:00 run. The Fable shadow
picks are deliberately NOT reposted — the Fable experiment is being shut
down (user decision, 18 Jul 2026).

Usage:  python _repost_jul18_discord.py
"""
import logging
import sys

from env_loader import load_env

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from auto_results import _format_result_notification
from card_generator import generate_picks_card
from discord_bot import build_pick_embed, send_to_discord

PICKS = [
    {
        "match": "France vs England", "league": "FIFA World Cup 2026",
        "bet_type": "Match Winner", "pick": "Draw (90 min)",
        "odds": 3.20, "probability": 32, "confidence": "Medium",
        "reasoning": (
            "France vs England knockout fixtures are historically tight, low-scoring "
            "affairs with both sides possessing elite defensive organization and "
            "tournament pedigree. Neither side tends to blow the other away in major "
            "tournament settings, making a draw after 90 minutes a genuinely plausible "
            "outcome at value odds. At ~3.20, the implied probability is around 31%, "
            "which aligns closely with our estimate of 32%, but the historical pattern "
            "of cagey encounters between these nations gives slight confidence this is "
            "not underpriced."
        ),
    },
    {
        "match": "France vs England", "league": "FIFA World Cup 2026",
        "bet_type": "Both Teams to Score", "pick": "Yes",
        "odds": 2.10, "probability": 52, "confidence": "Medium",
        "reasoning": (
            "Both France and England carry significant attacking depth and have "
            "historically found the net against each other in major tournaments. With "
            "the pressure of a knockout stage and both teams needing to push for a "
            "result if level late on, goals at both ends are a realistic outcome. At "
            "2.10 (implied ~48%), our estimate of 52% suggests marginal but real value."
        ),
    },
    {
        "match": "France vs England", "league": "FIFA World Cup 2026",
        "bet_type": "Asian Handicap", "pick": "England +0.5",
        "odds": 2.30, "probability": 50, "confidence": "Low",
        "reasoning": (
            "France carry home-continent advantage and are typically slight favourites "
            "against England, but England's tournament resilience and squad depth make "
            "them competitive. An Asian handicap of +0.5 means England win or draw "
            "covers the bet, and with a 50% probability estimate against implied odds "
            "of ~43%, there is value here — though the lack of recent form data limits "
            "conviction."
        ),
    },
    {
        "match": "Vietnam vs Myanmar", "league": "FIFA World Cup 2026",
        "bet_type": "Match Winner", "pick": "Vietnam Win",
        "odds": 1.75, "probability": 58, "confidence": "Medium",
        "reasoning": (
            "Vietnam, playing at home, hold a structural advantage over Myanmar, who "
            "are typically ranked lower in Southeast Asian football and have less "
            "World Cup tournament experience at this stage. Home support and "
            "familiarity with conditions should give Vietnam the edge. At 1.75 "
            "(implied ~57%), our 58% estimate represents slim but present value."
        ),
    },
    {
        "match": "Vietnam vs Myanmar", "league": "FIFA World Cup 2026",
        "bet_type": "Over 2.5 Goals", "pick": "Over 2.5 Goals",
        "odds": 2.05, "probability": 50, "confidence": "Low",
        "reasoning": (
            "Matches between Southeast Asian nations at World Cup level can be open "
            "and high-scoring, particularly when one side (Myanmar) may be forced to "
            "chase the game. Vietnam's home attacking intent combined with potential "
            "Myanmar defensive vulnerabilities makes over 2.5 goals plausible at "
            "roughly even money. With no recent form data available, confidence is "
            "limited, but the odds offer fair value at our 50% estimate versus the "
            "implied 49%."
        ),
    },
]

# The two Vietnam picks settled 4-0 during the outage; results-cards never got them
RESULTS = [
    {
        "match": "Vietnam vs Myanmar", "bet_type": "Match Winner",
        "pick": "Vietnam Win", "odds": 1.75, "result": "WIN", "pnl": 0.75,
        "home_name": "Vietnam", "away_name": "Myanmar",
        "home_score": 4, "away_score": 0,
    },
    {
        "match": "Vietnam vs Myanmar", "bet_type": "Over 2.5 Goals",
        "pick": "Over 2.5 Goals", "odds": 2.05, "result": "WIN", "pnl": 1.05,
        "home_name": "Vietnam", "away_name": "Myanmar",
        "home_score": 4, "away_score": 0,
    },
]


def main() -> None:
    # Probe with the first embed — if permissions are still broken, stop here
    first, *rest = PICKS
    if not send_to_discord("world-cup", embed=build_pick_embed(first, context=first["league"])):
        log.error("Send to 'world-cup' still failing — Discord permissions not restored yet. Aborting.")
        sys.exit(1)

    for pick in rest:
        send_to_discord("world-cup", embed=build_pick_embed(pick, context=pick["league"]))
    log.info("Posted %d pick embeds to 'world-cup'", len(PICKS))

    try:
        card = generate_picks_card(PICKS, session="morning")
        send_to_discord("picks-cards", image_path=card)
        log.info("Picks card posted to 'picks-cards' (local render): %s", card.name)
    except Exception as exc:
        log.warning("Picks card repost failed (non-fatal): %s", exc)

    for r in RESULTS:
        send_to_discord("results-cards", message=_format_result_notification(r))
    log.info("Posted %d result notifications to 'results-cards'", len(RESULTS))

    log.info("Repost complete.")


if __name__ == "__main__":
    main()
