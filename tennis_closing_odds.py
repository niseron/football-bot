"""
tennis_closing_odds.py — closing line value (CLV) tracker for the TENNIS system.

Runs every 15 minutes (scheduled from run_all.py). Scans unsettled tennis
picks for any whose match start is 5-65 minutes away, fetches current market
odds from The Odds API across the active tennis tournament sport keys, and
overwrites the 'Closing Odds' column in the 'Tennis Picks' tab — the last
write before the start becomes the closing price. tennis_calibration.py's
clv_report() consumes these values.

Fully separate from the football closing_odds.py: its own request counter,
its own daily cap, and reads/writes only via tennis_excel_tracker. Never
touches the football Picks tab or football odds budget accounting.

Run manually:
    python tennis_closing_odds.py
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from tennis_excel_tracker import get_unsettled_tennis_picks_with_start, update_tennis_closing_odds
from tennis_main import (
    fetch_active_tennis_sport_keys,
    fetch_tennis_odds_events,
    match_tennis_market_odds,
    parse_tennis_odds_event,
    player_match,
)

log = logging.getLogger(__name__)

# Self-imposed daily cap on tennis Odds API odds requests (the /sports key
# discovery call is quota-free and not counted). Independent of the football
# closing-odds cap — the two systems budget separately.
MAX_DAILY_TENNIS_REQUESTS = 12

_WINDOW_MIN_MINUTES = 5
_WINDOW_MAX_MINUTES = 65

_request_count = 0
_request_count_date: date | None = None


def _reset_counter_if_new_day() -> None:
    global _request_count, _request_count_date
    today = date.today()
    if _request_count_date != today:
        _request_count_date = today
        _request_count = 0


def _in_start_window(start_utc: str, now: datetime) -> bool:
    """True if start_utc is between _WINDOW_MIN_MINUTES and _WINDOW_MAX_MINUTES from now."""
    if not start_utc:
        return False
    try:
        start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    minutes_away = (start - now).total_seconds() / 60
    return _WINDOW_MIN_MINUTES <= minutes_away <= _WINDOW_MAX_MINUTES


def run_tennis_closing_odds_check() -> None:
    """
    One poll cycle: find due tennis picks, fetch odds once per active tennis
    sport key (batched, not per match), write Closing Odds for every due pick
    that finds a market match.
    """
    global _request_count
    _reset_counter_if_new_day()

    try:
        picks = get_unsettled_tennis_picks_with_start()
    except Exception as exc:
        log.warning("tennis_closing_odds: could not read unsettled picks (non-fatal): %s", exc)
        return

    if not picks:
        return

    now = datetime.now(timezone.utc)
    try:
        due = [p for p in picks if _in_start_window(p["start_utc"], now)]
    except Exception as exc:
        log.warning("tennis_closing_odds: start window filtering failed (non-fatal): %s", exc)
        return

    if not due:
        return

    if _request_count >= MAX_DAILY_TENNIS_REQUESTS:
        log.warning(
            "tennis_closing_odds: daily Odds API request cap (%d) already reached — skipping",
            MAX_DAILY_TENNIS_REQUESTS,
        )
        return

    log.info("tennis_closing_odds: %d pick(s) due for a closing-odds check", len(due))

    sport_keys = fetch_active_tennis_sport_keys()  # quota-free discovery call
    if not sport_keys:
        log.info("tennis_closing_odds: no active tennis sport keys — nothing to fetch")
        return

    # Batch: one odds request per active tennis tournament key, stopping as
    # soon as every due pick has been matched or the daily cap is reached.
    unmatched = list(due)
    for key in sport_keys:
        if not unmatched:
            break
        if _request_count >= MAX_DAILY_TENNIS_REQUESTS:
            log.warning(
                "tennis_closing_odds: daily Odds API request cap (%d) reached mid-poll — stopping",
                MAX_DAILY_TENNIS_REQUESTS,
            )
            break

        try:
            events = fetch_tennis_odds_events(key)
        except Exception as exc:
            log.warning("tennis_closing_odds: odds fetch failed for '%s' (non-fatal): %s", key, exc)
            continue
        _request_count += 1

        if not events:
            continue

        still_unmatched = []
        for p in unmatched:
            try:
                match = p.get("match", "")
                if " vs " not in match:
                    continue
                p1, p2 = [s.strip() for s in match.split(" vs ", 1)]
                event = next(
                    (
                        e for e in events
                        if (player_match(e.get("home_team", ""), p1) and player_match(e.get("away_team", ""), p2))
                        or (player_match(e.get("home_team", ""), p2) and player_match(e.get("away_team", ""), p1))
                    ),
                    None,
                )
                if event is None:
                    still_unmatched.append(p)
                    continue

                closing_odds = match_tennis_market_odds(
                    {"bet_type": p.get("bet_type", ""), "pick": p.get("pick", "")},
                    parse_tennis_odds_event(event),
                )
                if closing_odds is None:
                    continue  # event found but no market for this bet type (e.g. Set Betting)

                update_tennis_closing_odds(p["sheet_row"], closing_odds)
                log.info(
                    "tennis_closing_odds: wrote %.2f for '%s' | %s (start %s)",
                    closing_odds, match, p.get("pick", ""), p.get("start_utc", ""),
                )
            except Exception as exc:
                log.debug("tennis_closing_odds: skipped a pick (%s): %s", p.get("match"), exc)
                continue
        unmatched = still_unmatched


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_tennis_closing_odds_check()
    print("Done.")
