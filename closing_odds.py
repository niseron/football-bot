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
#
# Sized for the 20,000-unit paid tier (6 Aug 2026) at the current 3 units/call.
# A pick's closing window is 5-65 minutes before kickoff and the poller runs
# every 15 min, so fully covering ONE competition's kickoff wave costs 4
# requests. 60/day therefore buys ~15 competition-waves — every pick's closing
# price captured across several staggered kickoff blocks, not a token single
# poll. Budget: 60 x 3 = 180 units/day = ~5,600/month, ~28% of the tier.
# Tennis budgets separately (see tennis_closing_odds.py) and the two together
# are still under half the allowance, leaving room for Historical Odds work.
MAX_DAILY_REQUESTS = 60

# Only poll for matches whose kickoff falls in this window from "now".
_WINDOW_MIN_MINUTES = 5
_WINDOW_MAX_MINUTES = 65

# Per-pipeline request counters (keyed by budget_key, e.g. "football")
# sharing one daily reset. Separate budgets on purpose: an alternative
# caller can pass its own small cap so it can never crowd out the production
# football CLV coverage that the calibration verdict depends on.
_request_counts: dict[str, int] = {}
_request_count_date: date | None = None


def _reset_counter_if_new_day() -> None:
    global _request_count_date
    today = date.today()
    if _request_count_date != today:
        _request_count_date = today
        _request_counts.clear()


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


def run_closing_odds_check(
    *,
    picks_source=None,
    odds_writer=None,
    budget_key: str = "football",
    max_daily: int = MAX_DAILY_REQUESTS,
) -> None:
    """
    One poll cycle: find due picks, fetch odds once per competition
    represented among them (batched, not one request per match), write
    Closing Odds for every due pick that finds a market match.

    The hooks default to the production football tab. A caller may pass its
    own reader/writer AND its own budget_key/max_daily, so alternative
    polling can never consume the production football request budget —
    football's CLV coverage (the calibration verdict input) stays protected.
    """
    _reset_counter_if_new_day()

    picks_source = picks_source or get_unsettled_picks_with_kickoff
    odds_writer  = odds_writer or update_closing_odds

    try:
        picks = picks_source()
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

    # Monthly reserve hard stop, checked before the daily cap. The daily cap
    # bounds a single day; this bounds the month. Without it a busy stretch
    # can drain the quota to zero and take the picks-run odds enrichment down
    # with it — enrichment produces live value flags, so polling yields first.
    # The check itself is free (see usage_tracker.fetch_odds_quota).
    from usage_tracker import odds_budget_exhausted
    if odds_budget_exhausted():
        log.warning(
            "closing_odds: monthly Odds API reserve reached — polling halted for '%s'",
            budget_key,
        )
        return

    if _request_counts.get(budget_key, 0) >= max_daily:
        log.warning(
            "closing_odds: daily Odds API request cap (%d) for '%s' already reached — skipping this poll",
            max_daily, budget_key,
        )
        return

    log.info("closing_odds: %d pick(s) due for a closing-odds check (%s)", len(due), budget_key)

    # Batch: one Odds API call per competition represented among the due
    # picks this cycle, instead of one call per match.
    by_league: dict[str, list[dict]] = {}
    for p in due:
        by_league.setdefault(p.get("league", ""), []).append(p)

    for league, league_picks in by_league.items():
        sport_key = ODDS_API_SPORT_KEYS.get(league)
        if not sport_key:
            continue  # competition not mapped to an Odds API sport — skip silently

        if _request_counts.get(budget_key, 0) >= max_daily:
            log.warning(
                "closing_odds: daily Odds API request cap (%d) for '%s' reached mid-poll — stopping",
                max_daily, budget_key,
            )
            break

        try:
            events = _fetch_odds_events(sport_key)
        except Exception as exc:
            log.warning("closing_odds: odds fetch failed for '%s' (non-fatal): %s", league, exc)
            continue
        _request_counts[budget_key] = _request_counts.get(budget_key, 0) + 1

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

                odds_writer(p["sheet_row"], closing_odds)
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
