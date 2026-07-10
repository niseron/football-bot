"""
tennis_auto_results.py — automatic result checker for the TENNIS system.

Mirrors the football auto_results.py structure: scans unsettled Tennis Picks
rows, fetches completed matches from the Tennis API (fixtures by date, both
tours), evaluates each bet type, writes Result/P&L to the Tennis Picks tab,
and returns the newly settled picks so run_all.py's tennis_live_results_check
can send Discord notifications from the identical trigger.

Fully independent of the football data path: reads/writes only via
tennis_excel_tracker. Notifications are Discord-ONLY — each settled pick's
result text goes to the 'tennis-results' Discord channel key (tennis never
touches Telegram, unlike football's Telegram + Discord delivery).

Bet type settlement (units: WIN = odds−1, LOSS = −1, VOID = 0):
- Match Winner     — picked player won the match
- Total Games      — sum of games across all sets vs the line (exact integer
                     line hit = VOID; normal .5 lines never push)
- Set Betting      — exact set score from the picked player's perspective
- Handicap (games) — picked player's games + handicap vs opponent's games
                     (exact zero margin on an integer line = VOID)
- Retirement / walkover / abandoned — VOID for every bet type (conservative;
  bookmaker rules differ — override manually with tennis_update_result.py
  if your book settled differently)

Usage:
    python tennis_auto_results.py       # run once immediately and exit
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from tennis_excel_tracker import get_pending_tennis_picks, update_tennis_row_result
from tennis_main import TOURS, _data_list, _tennis_get, player_match

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 4  # picks cover fixtures up to 48h out, plus finish-day slack

_fixtures_cache: dict[tuple[str, date], tuple[datetime, list[dict]]] = {}
_CACHE_TTL = timedelta(minutes=30)


# ── Result notification text (sent to Discord 'tennis-results' by run_all.py) ─

def _format_tennis_result_notification(r: dict) -> str:
    _EMOJI  = {"WIN": "✅", "LOSS": "❌", "VOID": "⬜"}
    emoji   = _EMOJI.get(r["result"], "⬜")
    pnl_str = f"+{r['pnl']:.2f}" if r["pnl"] >= 0 else f"{r['pnl']:.2f}"
    return (
        f"🎾 {emoji} {r['result']} — {r['match']}\n"
        f"Bet: {r['bet_type']} | Odds: {r['odds']:.2f}\n"
        f"Pick: {r['pick']}\n"
        f"Result: {r['score_desc']}\n"
        f"P&L: {pnl_str} units"
    )


# ── Tennis API fetch ─────────────────────────────────────────────────────────

def _fetch_day_fixtures(tour: str, dt: date) -> list[dict]:
    """Cached fixtures-by-date for one tour/day (includes finished matches with a result)."""
    key = (tour, dt)
    now = datetime.now()
    if key in _fixtures_cache:
        fetched_at, fixtures = _fixtures_cache[key]
        if now - fetched_at < _CACHE_TTL:
            return fixtures
    time.sleep(1)
    fixtures = _data_list(_tennis_get(f"/tennis/v2/{tour}/fixtures/{dt.strftime('%Y-%m-%d')}"))
    _fixtures_cache[key] = (now, fixtures)
    log.info("  Tennis API: fetched %d %s fixtures for %s", len(fixtures), tour.upper(), dt)
    return fixtures


def _candidate_dates(p: dict) -> list[date]:
    """Days the pick's match could appear on: start date (+1 for overnight finishes)."""
    start_utc = p.get("start_utc", "")
    if start_utc:
        try:
            start = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
            return [start.date(), start.date() + timedelta(days=1)]
        except (ValueError, TypeError):
            pass
    return [p["date"], p["date"] + timedelta(days=1), p["date"] + timedelta(days=2)]


def _find_fixture(fixtures: list[dict], p1_q: str, p2_q: str) -> dict | None:
    """Find a fixture whose two players match the pick's, in either order."""
    for f in fixtures:
        n1 = (f.get("player1") or {}).get("name", "")
        n2 = (f.get("player2") or {}).get("name", "")
        if not n1 or not n2:
            continue
        if (player_match(n1, p1_q) and player_match(n2, p2_q)) or \
           (player_match(n1, p2_q) and player_match(n2, p1_q)):
            return f
    return None


# ── Score parsing & bet evaluation ───────────────────────────────────────────

_PAREN_RE = re.compile(r"\([^)]*\)")           # tiebreak details: 7-6(4)
_SET_RE   = re.compile(r"(\d+)-(\d+)")
_RET_RE   = re.compile(r"\b(ret\.?|retired|w/?o\.?|walkover|def\.|abd\.?|abandoned)", re.IGNORECASE)
_OU_RE    = re.compile(r"(over|under)\s*([\d.]+)", re.IGNORECASE)
_HC_RE    = re.compile(r"^(.*?)\s*([+-]\d+(?:\.\d+)?)$")
_SETS_RE  = re.compile(r"^(.*?)\s*(\d+)\s*-\s*(\d+)$")
_WIN_SUFFIX_RE = re.compile(r"\s+(to\s+win|win)$", re.IGNORECASE)


def _parse_sets(result_str: str) -> list[tuple[int, int]]:
    """Set scores as (side1_games, side2_games) tuples, tiebreak details stripped."""
    return [(int(a), int(b)) for a, b in _SET_RE.findall(_PAREN_RE.sub("", result_str or ""))]


def _picked_side(pick_name: str, p1_name: str, p2_name: str) -> int | None:
    """1 or 2 for which side the picked player is; None if ambiguous/unmatched."""
    is1 = player_match(p1_name, pick_name)
    is2 = player_match(p2_name, pick_name)
    if is1 == is2:
        return None
    return 1 if is1 else 2


def evaluate_tennis_pick(
    bet_type: str,
    pick: str,
    p1_name: str,
    p2_name: str,
    result_str: str,
) -> str:
    """
    Return WIN, LOSS, VOID, or PENDING (unrecognised bet type / data missing).
    p1_name/p2_name and result_str are from the fixture (result is from
    side 1's perspective). Retirements/walkovers settle everything as VOID.
    """
    bt = bet_type.lower()
    pk = pick.strip()

    sets = _parse_sets(result_str)
    if not sets:
        return "PENDING"

    if _RET_RE.search(result_str):
        return "VOID"

    sets1 = sum(1 for a, b in sets if a > b)
    sets2 = sum(1 for a, b in sets if b > a)
    if sets1 == sets2:
        return "PENDING"  # can't determine a winner — leave for manual review
    games1 = sum(a for a, _ in sets)
    games2 = sum(b for _, b in sets)

    # ── Match Winner ─────────────────────────────────────────────────────────
    if "winner" in bt or "moneyline" in bt:
        side = _picked_side(_WIN_SUFFIX_RE.sub("", pk), p1_name, p2_name)
        if side is None:
            return "PENDING"
        won = (side == 1 and sets1 > sets2) or (side == 2 and sets2 > sets1)
        return "WIN" if won else "LOSS"

    # ── Handicap (games) — before totals: 'Handicap (games)' contains 'games' ─
    if "handicap" in bt or "spread" in bt:
        m = _HC_RE.match(pk)
        if not m:
            return "PENDING"
        side = _picked_side(m.group(1).strip(), p1_name, p2_name)
        if side is None:
            return "PENDING"
        hc = float(m.group(2))
        margin = (games1 - games2 if side == 1 else games2 - games1) + hc
        if margin > 0:
            return "WIN"
        if margin < 0:
            return "LOSS"
        return "VOID"

    # ── Total Games Over/Under ───────────────────────────────────────────────
    if "total" in bt or "over" in bt or "under" in bt or "games" in bt:
        m = _OU_RE.search(pk) or _OU_RE.search(bt)
        if not m:
            return "PENDING"
        side_word, line = m.group(1).lower(), float(m.group(2))
        total = games1 + games2
        if total == line:
            return "VOID"
        if side_word == "over":
            return "WIN" if total > line else "LOSS"
        return "WIN" if total < line else "LOSS"

    # ── Set Betting (exact set score, picked player's perspective) ───────────
    if "set" in bt:
        m = _SETS_RE.match(pk)
        if not m:
            return "PENDING"
        side = _picked_side(m.group(1).strip(), p1_name, p2_name)
        if side is None:
            return "PENDING"
        want_won, want_lost = int(m.group(2)), int(m.group(3))
        got_won, got_lost = (sets1, sets2) if side == 1 else (sets2, sets1)
        return "WIN" if (got_won, got_lost) == (want_won, want_lost) else "LOSS"

    log.warning("evaluate_tennis_pick: unhandled bet_type='%s' pick='%s'", bet_type, pick)
    return "PENDING"


def _score_description(p1_name: str, p2_name: str, result_str: str) -> str:
    sets = _parse_sets(result_str)
    if not sets:
        return result_str or "?"
    if _RET_RE.search(result_str):
        return f"{result_str} (retirement/walkover — settled VOID)"
    sets1 = sum(1 for a, b in sets if a > b)
    sets2 = sum(1 for a, b in sets if b > a)
    winner = p1_name if sets1 > sets2 else p2_name
    games = f"{sum(a for a, _ in sets)}-{sum(b for _, b in sets)} games"
    return f"{winner} won {result_str} ({games})"


# ── Core checker ─────────────────────────────────────────────────────────────

def run_tennis_auto_results(lookback_days: int = LOOKBACK_DAYS) -> tuple[dict, list[dict]]:
    """
    Scan pending Tennis Picks rows, fetch completed matches from the Tennis
    API, update the sheet. Returns (stats_dict, list_of_newly_resolved_picks)
    — the same contract as football's run_auto_results.
    """
    stats = {"checked": 0, "updated": 0, "not_finished": 0, "no_match": 0, "errors": 0}
    resolved: list[dict] = []

    pending = get_pending_tennis_picks(lookback_days)
    if not pending:
        log.info("No pending tennis picks in the lookback window.")
        return stats, resolved

    log.info("Found %d pending tennis pick(s) to check.", len(pending))
    now = datetime.now(timezone.utc)

    for p in pending:
        stats["checked"] += 1
        match = p["match"]

        if " vs " not in match:
            log.warning("Cannot parse tennis match name '%s'", match)
            stats["errors"] += 1
            continue
        p1_q, p2_q = [s.strip() for s in match.split(" vs ", 1)]

        # Skip fixtures that can't have finished yet (start time still ahead)
        start_utc = p.get("start_utc", "")
        if start_utc:
            try:
                start = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                if start > now:
                    stats["not_finished"] += 1
                    continue
            except (ValueError, TypeError):
                pass

        fixture = None
        for dt in _candidate_dates(p):
            if dt > date.today() + timedelta(days=1):
                continue
            for tour in TOURS:
                try:
                    fixture = _find_fixture(_fetch_day_fixtures(tour, dt), p1_q, p2_q)
                except Exception as exc:
                    log.error("  Tennis API fetch failed for %s/%s: %s", tour, dt, exc)
                    continue
                if fixture:
                    break
            if fixture:
                break

        if fixture is None:
            log.info("'%s' — not found in Tennis API yet", match)
            stats["no_match"] += 1
            continue

        result_str = (fixture.get("result") or "").strip()
        if not result_str:
            log.info("'%s' — match not finished yet", match)
            stats["not_finished"] += 1
            continue

        p1_name = (fixture.get("player1") or {}).get("name", p1_q)
        p2_name = (fixture.get("player2") or {}).get("name", p2_q)

        result = evaluate_tennis_pick(p["bet_type"], p["pick"], p1_name, p2_name, result_str)

        if result == "PENDING":
            log.warning("Could not evaluate tennis bet_type='%s' pick='%s' result='%s'",
                        p["bet_type"], p["pick"], result_str)
            stats["errors"] += 1
            continue

        odds = p["odds"]
        if result == "WIN":
            pnl = round(odds - 1, 2)
        elif result == "LOSS":
            pnl = -1.0
        else:
            pnl = 0.0

        update_tennis_row_result(p["sheet_row"], result, pnl)
        stats["updated"] += 1

        score_desc = _score_description(p1_name, p2_name, result_str)
        log.info("%s [%s→%s]  %s  P&L %+.2f", match, p["pick"], result, result_str, pnl)

        resolved.append({
            "match":      match,
            "bet_type":   p["bet_type"],
            "pick":       p["pick"],
            "odds":       odds,
            "result":     result,
            "pnl":        pnl,
            "score_desc": score_desc,
        })

    if stats["updated"]:
        log.info("Tennis Picks sheet updated — %d row(s) written.", stats["updated"])
    else:
        log.info("No tennis changes.")

    return stats, resolved


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stats, resolved = run_tennis_auto_results()
    print(f"\n  Checked     : {stats['checked']}")
    print(f"  Updated     : {stats['updated']}")
    print(f"  Not finished: {stats['not_finished']}")
    print(f"  No API match: {stats['no_match']}")
    print(f"  Errors      : {stats['errors']}")
    for r in resolved:
        print(f"\n{_format_tennis_result_notification(r)}")
