"""
closing_odds.py — closing line value (CLV) tracker.

Runs every 15 minutes (scheduled from run_all.py). Scans today's unsettled
picks for any whose kickoff is 5-65 minutes away, fetches current market odds
from The Odds API for that match/market, and overwrites the 'Closing Odds'
column — the last write before kickoff becomes the closing price. Closing
odds are the true baseline for measuring edge; see calibration.py's
clv_report() for how they're used.

Purely additive: reads the Picks sheet and writes only to the Closing Odds
column. Never touches pick generation, Kelly staking, or the calibration
engine, and never writes Result/Profit/Loss.

All errors are caught and logged — a failure here never affects any other
job, and this module makes no changes to existing behaviour on its own.

Run manually:
    python closing_odds.py
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from excel_tracker import get_unsettled_picks_with_kickoff, update_closing_odds
from main import ODDS_API_SPORT_KEYS, _fetch_odds_events, _match_market_odds, _parse_odds_event, _team_match

log = logging.getLogger(__name__)

# Self-imposed daily cap so a scheduling bug (or an unexpectedly busy fixture
# day) can't burn through The Odds API quota. Reset at midnight local process
# time — "local counter" per the design brief, not persisted across restarts.
# Kept low enough that this job's usage plus main.py's morning odds-enrichment
# calls stay comfortably under the 500/month free-tier limit (12/day here is
# ~360/month; enrichment adds a handful more per day on top of that).
MAX_DAILY_REQUESTS = 12

# Only poll for matches whose kickoff falls in this window from "now".
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


def _in_kickoff_window(kickoff_utc: str, now: datetime) -> bool:
    """True if kickoff_utc is between _WINDOW_MIN_MINUTES and _WINDOW_MAX_MINUTES from now."""
    if not kickoff_utc:
        return False
    try:
        kickoff = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    minutes_away = (kickoff - now).total_seconds() / 60
    return _WINDOW_MIN_MINUTES <= minutes_away <= _WINDOW_MAX_MINUTES


def run_closing_odds_check() -> None:
    """
    One poll cycle: find due picks, fetch odds once per competition
    represented among them (batched, not one request per match), write
    Closing Odds for every due pick that finds a market match.
    """
    global _request_count
    _reset_counter_if_new_day()

    try:
        picks = get_unsettled_picks_with_kickoff()
    except Exception as exc:
        log.warning("closing_odds: could not read unsettled picks (non-fatal): %s", exc)
        return

    if not picks:
        return

    now = datetime.now(timezone.utc)
    try:
        due = [p for p in picks if _in_kickoff_window(p["kickoff_utc"], now)]
    except Exception as exc:
        log.warning("closing_odds: kickoff window filtering failed (non-fatal): %s", exc)
        return

    if not due:
        return

    if _request_count >= MAX_DAILY_REQUESTS:
        log.warning(
            "closing_odds: daily Odds API request cap (%d) already reached — skipping this poll",
            MAX_DAILY_REQUESTS,
        )
        return

    log.info("closing_odds: %d pick(s) due for a closing-odds check", len(due))

    # Batch: one Odds API call per competition represented among the due
    # picks this cycle, instead of one call per match.
    by_league: dict[str, list[dict]] = {}
    for p in due:
        by_league.setdefault(p.get("league", ""), []).append(p)

    for league, league_picks in by_league.items():
        sport_key = ODDS_API_SPORT_KEYS.get(league)
        if not sport_key:
            continue  # competition not mapped to an Odds API sport — skip silently

        if _request_count >= MAX_DAILY_REQUESTS:
            log.warning(
                "closing_odds: daily Odds API request cap (%d) reached mid-poll — stopping",
                MAX_DAILY_REQUESTS,
            )
            break

        try:
            events = _fetch_odds_events(sport_key)
        except Exception as exc:
            log.warning("closing_odds: odds fetch failed for '%s' (non-fatal): %s", league, exc)
            continue
        _request_count += 1

        if not events:
            continue

        for p in league_picks:
            try:
                match = p.get("match", "")
                if " vs " not in match:
                    continue
                home, away = [s.strip() for s in match.split(" vs ", 1)]
                event = next(
                    (
                        e for e in events
                        if _team_match(e.get("home_team", ""), home)
                        and _team_match(e.get("away_team", ""), away)
                    ),
                    None,
                )
                if event is None:
                    continue

                real_odds = _parse_odds_event(event)
                closing_odds = _match_market_odds(
                    {"bet_type": p.get("bet_type", ""), "pick": p.get("pick", "")}, real_odds
                )
                if closing_odds is None:
                    continue

                update_closing_odds(p["sheet_row"], closing_odds)
                log.info(
                    "closing_odds: wrote %.2f for '%s' | %s (kickoff %s)",
                    closing_odds, match, p.get("pick", ""), p.get("kickoff_utc", ""),
                )
            except Exception as exc:
                log.debug("closing_odds: skipped a pick (%s): %s", p.get("match"), exc)
                continue


if __name__ == "__main__":
    from env_loader import load_env

    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_closing_odds_check()
    print("Done.")
