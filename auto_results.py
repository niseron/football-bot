"""
Automatic result checker for the football betting bot.

Usage:
    python auto_results.py              # run once immediately and exit
    python auto_results.py --schedule   # nightly daemon at 00:15 Brussels
    python auto_results.py --live       # check every 30 min + Telegram alerts
"""
from __future__ import annotations

import logging
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

from discord_bot import send_to_discord
from env_loader import load_env
from excel_tracker import (
    EXCEL_PATH,
    finalize_workbook,
    get_pending_picks_rows,
    get_picks_for_date,
    init_excel,
    update_row_result,
)

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HOST          = "free-api-live-football-data.p.rapidapi.com"
LOOKBACK_DAYS = 7

_matches_cache: dict[date, tuple[datetime, list[dict]]] = {}
_CACHE_TTL = timedelta(minutes=30)
_last_api_call: float = 0.0


# ── Telegram ──────────────────────────────────────────────────────────────────

def _telegram_send(text: str) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHANNEL_ID")
    if not token or not chat_id:
        log.warning("Telegram not configured — skipping notification")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        r.raise_for_status()
        log.info("Telegram notification sent")
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)


def _telegram_send_photo(path) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHANNEL_ID")
    if not token or not chat_id:
        return
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={"chat_id": chat_id},
                files={"photo": f},
                timeout=30,
            )
        r.raise_for_status()
        log.info("Telegram photo sent: %s", path)
    except Exception as exc:
        log.error("Telegram photo send failed: %s", exc)


def _score_description(
    bet_type: str,
    pick: str,
    home_name: str,
    away_name: str,
    home_score: int,
    away_score: int,
) -> str:
    """Human-readable result line for a Telegram notification."""
    bt    = bet_type.lower()
    total = home_score + away_score
    score = f"{home_score}-{away_score}"

    if any(x in bt for x in ("over", "under", "total goals", "o/u")):
        return f"{score} ({total} goals total)"

    if any(x in bt for x in ("both teams to score", "btts")):
        if home_score > 0 and away_score > 0:
            suffix = "both teams scored"
        elif home_score == 0 and away_score == 0:
            suffix = "goalless draw"
        else:
            loser = home_name if away_score > home_score else away_name
            suffix = f"{loser} kept a clean sheet"
        return f"{score} ({suffix})"

    if home_score > away_score:
        base = f"{home_name} won {score}"
    elif away_score > home_score:
        base = f"{away_name} won {score}"
    else:
        base = f"{score} draw"

    if any(x in bt for x in ("asian handicap", "handicap")):
        m = re.search(r'([+-]?\d+\.?\d*)\s*$', pick.strip())
        if m:
            hc      = float(m.group(1))
            team_q  = pick[:m.start()].strip().lower()
            if team_q in home_name.lower():
                base += f" (handicap adjusted: {home_score + hc:.1f}-{away_score})"
            elif team_q in away_name.lower():
                base += f" (handicap adjusted: {home_score}-{away_score + hc:.1f})"

    return base


def _format_result_notification(r: dict) -> str:
    _EMOJI  = {"WIN": "✅", "HALF WIN": "🟡", "HALF LOSS": "🟠", "LOSS": "❌"}
    emoji   = _EMOJI.get(r["result"], "⬜")
    pnl_str = f"+{r['pnl']:.2f}" if r["pnl"] >= 0 else f"{r['pnl']:.2f}"
    desc    = _score_description(
        r["bet_type"], r["pick"],
        r["home_name"], r["away_name"],
        r["home_score"], r["away_score"],
    )
    if r.get("penalties"):
        desc += " (decided on penalties)"
    elif r.get("extra_time"):
        desc += " (after extra time)"
    return (
        f"{emoji} {r['result']} — {r['match']}\n"
        f"Bet: {r['bet_type']} | Odds: {r['odds']:.2f}\n"
        f"Pick: {r['pick']}\n"
        f"Result: {desc}\n"
        f"P&L: {pnl_str} units"
    )


# ── API ───────────────────────────────────────────────────────────────────────

def _fetch_matches(dt: date) -> list[dict]:
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < 2.0:
        time.sleep(2.0 - elapsed)
    headers = {"x-rapidapi-host": HOST, "x-rapidapi-key": os.environ.get("RAPIDAPI_KEY")}
    r = requests.get(
        f"https://{HOST}/football-get-matches-by-date",
        headers=headers,
        params={"date": dt.strftime("%Y%m%d")},
        timeout=15,
    )
    _last_api_call = time.time()
    r.raise_for_status()
    return r.json().get("response", {}).get("matches", [])


def _fetch_matches_cached(dt: date) -> list[dict]:
    now = datetime.now()
    if dt in _matches_cache:
        fetched_at, matches = _matches_cache[dt]
        if now - fetched_at < _CACHE_TTL:
            log.info("  Cache hit for %s (%d matches)", dt, len(matches))
            return matches
    matches = _fetch_matches(dt)
    _matches_cache[dt] = (now, matches)
    return matches


def _find_api_match(matches: list[dict], home_q: str, away_q: str) -> dict | None:
    hq = home_q.lower().strip()
    aq = away_q.lower().strip()
    for m in matches:
        h = m["home"]["longName"].lower()
        a = m["away"]["longName"].lower()
        if (hq in h or h in hq) and (aq in a or a in aq):
            return m
    return None


# ── Bet-type evaluation ───────────────────────────────────────────────────────

def _parse_handicap(pick: str) -> tuple[str, float] | None:
    m = re.search(r'([+-]?\d+\.?\d*)\s*$', pick.strip())
    if not m:
        return None
    return pick[:m.start()].strip().lower(), float(m.group(1))


def _is_quarter_line(hc: float) -> bool:
    """Quarter handicaps (±0.25, ±0.75, ±1.25 …) produce half results."""
    return (hc * 2) % 1 > 0.01


def _eval_ah_line(adj: float, opp: float) -> str:
    if adj > opp: return "WIN"
    if adj < opp: return "LOSS"
    return "VOID"


def _combine_ah_halves(r1: str, r2: str) -> str:
    pair = frozenset([r1, r2])
    if pair == frozenset(["WIN"]):           return "WIN"
    if pair == frozenset(["WIN",  "VOID"]):  return "HALF WIN"
    if pair == frozenset(["VOID"]):          return "VOID"
    if pair == frozenset(["VOID", "LOSS"]):  return "HALF LOSS"
    if pair == frozenset(["LOSS"]):          return "LOSS"
    return "VOID"  # WIN + LOSS edge case — treat as push


def evaluate_pick(
    bet_type: str,
    pick: str,
    home_name: str,
    away_name: str,
    home_score: int,
    away_score: int,
    *,
    extra_time: bool = False,
    penalties: bool = False,
) -> str:
    """
    Return WIN, LOSS, VOID, or PENDING (unrecognised bet type / data missing).

    Handles both generic terms ('Home Win', 'Away or Draw') and team-name
    picks generated by the updated Claude prompt ('Sweden Win', 'Ivory Coast or Draw').

    Knockout picks may carry a time-scope suffix since 12 Jul 2026:
    '(90 min)' or '(Full-Time incl. ET/Pens)'. The API score includes extra
    time, so extra_time/penalties (from the match status) decide how each
    scope settles. Unscoped picks follow the bookmaker default for EVERY bet
    type: 90 minutes only. When a match went past 90 minutes the regulation
    score itself is unknown (the API only returns the final score), but the
    match can only reach extra time level after 90 minutes — each bet type
    derives what it can from that fact and returns PENDING (manual settlement
    via update_result.py) when the regulation outcome is genuinely ambiguous.
    """
    bt    = bet_type.lower()
    pk    = pick.lower().strip()
    hn    = home_name.lower()
    an    = away_name.lower()
    total = home_score + away_score
    hw    = home_score > away_score
    aw    = away_score > home_score
    dr    = home_score == away_score

    # Detect and strip a trailing time-scope marker from the pick text
    scope = None
    m = re.search(r'\s*\(([^)]*)\)$', pk)
    if m:
        inner = m.group(1)
        if "90" in inner:
            scope = "90"
        elif any(x in inner for x in ("full", "et", "pen")):
            scope = "ft"
        if scope:
            pk = pk[:m.start()].strip()

    # Match went past 90 minutes and the pick settles on regulation time
    # (the default). The API score includes ET goals, but regulation is
    # guaranteed to have ended level — that is the only way to reach ET.
    reg_draw = (extra_time or penalties) and scope != "ft"

    # ── Match Winner ─────────────────────────────────────────────────────────
    if any(x in bt for x in ("match winner", "1x2", "result", "moneyline")):
        # Generic terms OR team name anywhere in pick string
        home_pick = pk in ("home", "home win", "1") or hn in pk or pk in hn
        away_pick = pk in ("away", "away win", "2") or an in pk or pk in an
        draw_pick = pk in ("draw", "x", "tie")

        if reg_draw:
            # A single-match knockout only reaches extra time when level after
            # 90 minutes, so the regulation result is a draw regardless of the
            # final API score (which includes ET goals).
            if draw_pick:                          return "WIN"
            if home_pick != away_pick:             return "LOSS"
        if penalties and scope == "ft":
            # After a shootout the API score is level — the winner cannot be
            # derived here. Settle manually via update_result.py.
            log.warning("Pick '%s' was decided on penalties — settle manually "
                        "via update_result.py", pick)
            return "PENDING"

        if home_pick and not away_pick: return "WIN" if hw else "LOSS"
        if away_pick and not home_pick: return "WIN" if aw else "LOSS"
        if draw_pick:                   return "WIN" if dr else "LOSS"

    # ── Both Teams to Score ──────────────────────────────────────────────────
    elif any(x in bt for x in ("both teams to score", "btts", "gg/ng", "goal goal")):
        if reg_draw:
            if min(home_score, away_score) == 0:
                # Level after 90' with one side scoreless overall → reg was 0-0
                both = False
            else:
                log.warning("Pick '%s' (%s) went to extra time — the 90-minute "
                            "BTTS outcome cannot be derived from the AET score; "
                            "settle manually via update_result.py", pick, bet_type)
                return "PENDING"
        else:
            both = home_score > 0 and away_score > 0
        if pk in ("yes", "true", "gg", "yes (gg)"): return "WIN" if both     else "LOSS"
        if pk in ("no",  "false", "ng", "no (ng)"): return "WIN" if not both else "LOSS"

    # ── Over / Under goals ───────────────────────────────────────────────────
    elif any(x in bt for x in ("over", "under", "total goals", "goals over", "o/u")):
        nums      = re.findall(r'\d+\.?\d*', bt)
        threshold = float(nums[0]) if nums else 2.5
        if reg_draw:
            # Regulation ended h-h with h ≤ min(final scores), so the highest
            # possible 90-minute total is 2 × min(home, away).
            if 2 * min(home_score, away_score) < threshold:
                if "over"  in pk: return "LOSS"
                if "under" in pk: return "WIN"
            log.warning("Pick '%s' (%s) went to extra time — the 90-minute "
                        "total cannot be derived from the AET score; settle "
                        "manually via update_result.py", pick, bet_type)
            return "PENDING"
        if "over"  in pk: return "WIN" if total >  threshold else "LOSS"
        if "under" in pk: return "WIN" if total <  threshold else "LOSS"

    # ── Asian Handicap ───────────────────────────────────────────────────────
    elif any(x in bt for x in ("asian handicap", "handicap", " ah ")):
        parsed = _parse_handicap(pk)  # pk, not pick: the scope suffix is stripped
        if parsed:
            team_q, hc = parsed
            if team_q in hn or hn in team_q:
                score, opp = home_score, away_score
            elif team_q in an or an in team_q:
                score, opp = away_score, home_score
            else:
                score, opp = None, None
            if score is not None and reg_draw:
                # AH settles on the goal difference, which was exactly 0 after
                # 90 minutes no matter how many ET goals the API score holds.
                score, opp = 0, 0
            if score is not None:
                if _is_quarter_line(hc):
                    h_low  = math.floor(hc * 2) / 2
                    h_high = math.ceil(hc * 2) / 2
                    return _combine_ah_halves(
                        _eval_ah_line(score + h_low,  opp),
                        _eval_ah_line(score + h_high, opp),
                    )
                else:
                    adj = score + hc
                    if   adj > opp: return "WIN"
                    elif adj < opp: return "LOSS"
                    else:           return "VOID"

    # ── Double Chance ────────────────────────────────────────────────────────
    elif "double chance" in bt:
        if reg_draw:
            # Regulation result was a draw: 'or draw' sides win, '12' loses.
            hw, aw, dr = False, False, True
        # "or draw" picks: home team name or "home" must appear alongside "or draw"
        if "or draw" in pk:
            home_side = hn in pk or "home" in pk or "1x" in pk
            away_side = an in pk or "away" in pk or "x2" in pk
            if home_side and not away_side: return "WIN" if hw or dr else "LOSS"
            if away_side and not home_side: return "WIN" if aw or dr else "LOSS"
        # Both-wins double chance
        if any(x in pk for x in ("home or away", "12")) or (hn in pk and an in pk):
            return "WIN" if hw or aw else "LOSS"

    log.warning("evaluate_pick: unhandled bet_type='%s' pick='%s'", bet_type, pick)
    return "PENDING"


# ── Core checker ─────────────────────────────────────────────────────────────

def run_auto_results(
    lookback_days: int = LOOKBACK_DAYS,
    *,
    pending_source=None,
    row_writer=None,
    finalizer=None,
) -> tuple[dict, list[dict]]:
    """
    Scan pending Google Sheets rows, fetch API scores, update the sheet.
    Returns (stats_dict, list_of_newly_resolved_picks).

    The three hooks default to the production football tab
    (get_pending_picks_rows / update_row_result / finalize_workbook); a
    caller may pass its own tab's reader/writer plus a different finalizer
    to settle another tab with the identical evaluation logic.
    """
    pending_source = pending_source or get_pending_picks_rows
    row_writer     = row_writer or update_row_result
    finalizer      = finalizer if finalizer is not None else finalize_workbook

    init_excel()

    stats   = {"checked": 0, "updated": 0, "not_finished": 0,
               "no_match": 0, "too_old": 0, "errors": 0}
    resolved: list[dict] = []

    # ── 1. Collect pending rows from Google Sheets ────────────────────────────
    pending = pending_source(lookback_days)

    if not pending:
        log.info("No pending picks in the lookback window.")
        return stats, resolved

    log.info("Found %d pending pick(s) to check.", len(pending))

    # ── 2. Batch football API calls by date ───────────────────────────────────
    api_cache: dict[date, list[dict]] = {}
    for p in pending:
        for dt in (p["date"], p["date"] + timedelta(days=1)):
            if dt in api_cache:
                continue
            try:
                api_cache[dt] = _fetch_matches_cached(dt)
                log.info("  API: fetched %d matches for %s", len(api_cache[dt]), dt)
            except Exception as exc:
                log.error("  API fetch failed for %s: %s", dt, exc)
                api_cache[dt] = []

    # ── 3. Evaluate each pick and write results ───────────────────────────────
    changed = False
    for p in pending:
        stats["checked"] += 1
        sheet_row = p["sheet_row"]
        match     = p["match"]
        bet_type  = p["bet_type"]
        pick      = p["pick"]
        odds      = p["odds"]

        if " vs " not in match:
            log.warning("Cannot parse match name '%s'", match)
            stats["errors"] += 1
            continue

        home_q, away_q = [s.strip() for s in match.split(" vs ", 1)]

        api_match = None
        for dt in (p["date"], p["date"] + timedelta(days=1)):
            api_match = _find_api_match(api_cache.get(dt, []), home_q, away_q)
            if api_match:
                break

        if api_match is None:
            log.info("'%s' — not found in API yet", match)
            stats["no_match"] += 1
            continue

        if not api_match["status"].get("finished"):
            log.info("'%s' — match not finished yet", match)
            stats["not_finished"] += 1
            continue

        home_score = int(api_match["home"].get("score") or 0)
        away_score = int(api_match["away"].get("score") or 0)
        home_name  = api_match["home"]["longName"]
        away_name  = api_match["away"]["longName"]

        # Knockout finishes: the API's status.reason says how the match ended
        # (FT / AET / Pen) while the score always includes extra time.
        reason     = (api_match.get("status") or {}).get("reason") or {}
        fin_txt    = f"{reason.get('short', '')} {reason.get('long', '')}".lower()
        penalties  = fin_txt.startswith("pen") or "penalt" in fin_txt
        extra_time = penalties or "aet" in fin_txt or "extra time" in fin_txt

        result = evaluate_pick(bet_type, pick, home_name, away_name, home_score, away_score,
                               extra_time=extra_time, penalties=penalties)

        if result == "PENDING":
            log.warning("Could not evaluate bet_type='%s' pick='%s'", bet_type, pick)
            stats["errors"] += 1
            continue

        if result == "WIN":
            pnl = round(odds - 1, 2)
        elif result == "HALF WIN":
            pnl = round(0.5 * (odds - 1), 2)
        elif result == "HALF LOSS":
            pnl = -0.50
        elif result == "LOSS":
            pnl = -1.0
        else:
            pnl = 0.0
        row_writer(sheet_row, result, pnl)
        changed = True
        stats["updated"] += 1

        log.info("%s [%s→%s]  score %d-%d  P&L %+.2f",
                 match, pick, result, home_score, away_score, pnl)

        resolved.append({
            "match":      match,
            "bet_type":   bet_type,
            "pick":       pick,
            "odds":       odds,
            "result":     result,
            "pnl":        pnl,
            "home_name":  home_name,
            "away_name":  away_name,
            "home_score": home_score,
            "away_score": away_score,
            "extra_time": extra_time,
            "penalties":  penalties,
        })

    # ── 4. Recalculate running totals + refresh Summary ───────────────────────
    if changed:
        finalizer()
        log.info("Google Sheets updated — %d row(s) written.", stats["updated"])
    else:
        log.info("No changes.")

    return stats, resolved


# ── Entry point ───────────────────────────────────────────────────────────────

def _print_stats(stats: dict) -> None:
    print(f"\n  Checked     : {stats.get('checked', 0)}")
    print(f"  Updated     : {stats.get('updated', 0)}")
    print(f"  Not finished: {stats.get('not_finished', 0)}")
    print(f"  No API match: {stats.get('no_match', 0)}")
    print(f"  Too old     : {stats.get('too_old', 0)}")
    print(f"  Errors      : {stats.get('errors', 0)}")


if __name__ == "__main__":
    live_mode     = "--live"     in sys.argv
    schedule_mode = "--schedule" in sys.argv

    if live_mode:
        # Track which picks have already been notified this session
        notified: set[tuple] = set()

        def _live_check() -> None:
            print("\n--- Checking results ---")
            stats, resolved = run_auto_results(lookback_days=2)
            _print_stats(stats)
            for r in resolved:
                key = (r["match"], r["bet_type"], r["pick"])
                if key in notified:
                    continue
                msg = _format_result_notification(r)
                print(f"\nSending notification:\n{msg}")
                _telegram_send(msg)
                send_to_discord("results-cards", message=msg)  # same trigger as Telegram
                notified.add(key)

        _live_check()

        scheduler = BlockingScheduler()
        scheduler.add_job(_live_check, "interval", minutes=30)
        print("\nLive result checker running — checks every 30 minutes.")
        print("Press Ctrl+C to stop.")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            print("Live checker stopped.")

    elif schedule_mode:
        scheduler = BlockingScheduler(timezone="Europe/Brussels")
        scheduler.add_job(
            lambda: run_auto_results(),
            "cron",
            hour=0,
            minute=15,
        )
        print("Auto-result checker started — runs nightly at 00:15 Brussels.")
        print("Press Ctrl+C to stop.")
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            print("Scheduler stopped.")

    elif "--results" in sys.argv:
        print(f"Checking results for pending picks (last {LOOKBACK_DAYS} days)...\n")

        pending_before = get_pending_picks_rows(LOOKBACK_DAYS)
        stats, resolved = run_auto_results(LOOKBACK_DAYS)

        resolved_map: dict[tuple, dict] = {
            (r["match"], r["bet_type"], r["pick"]): r for r in resolved
        }

        C_MATCH  = 36
        C_PICK   = 30
        C_ODDS   =  6
        C_RESULT =  9
        C_SCORE  =  7
        C_PNL    =  7

        header = (
            f"{'Match':<{C_MATCH}}  {'Pick':<{C_PICK}}  {'Odds':>{C_ODDS}}"
            f"  {'Result':<{C_RESULT}}  {'Score':<{C_SCORE}}  {'P&L':>{C_PNL}}"
        )
        sep = "-" * len(header)
        print(header)
        print(sep)

        for p in pending_before:
            key = (p["match"], p["bet_type"], p["pick"])
            r   = resolved_map.get(key)

            pick_label = f"{p['bet_type']} / {p['pick']}"
            if r:
                result = r["result"]
                score  = f"{r['home_score']}-{r['away_score']}"
                pnl    = f"{r['pnl']:+.2f}"
            else:
                result = "PENDING"
                score  = "-"
                pnl    = "-"

            print(
                f"{p['match']:<{C_MATCH}}  {pick_label:<{C_PICK}}  {p['odds']:>{C_ODDS}.2f}"
                f"  {result:<{C_RESULT}}  {score:<{C_SCORE}}  {pnl:>{C_PNL}}"
            )

        print(sep)
        _print_stats(stats)

        # ── Per-pick Telegram notifications for yesterday ─────────────────────
        yesterday = date.today() - timedelta(days=1)
        yesterday_picks = get_picks_for_date(yesterday)
        settled = [p for p in yesterday_picks if p["result"] in ("WIN", "HALF WIN", "HALF LOSS", "LOSS")]

        if not settled:
            print(f"\nNo settled picks for {yesterday} — skipping Telegram notifications.")
        else:
            print(f"\nSending {len(settled)} individual result notification(s) to Telegram...")
            total_pnl = 0.0
            for p in settled:
                key = (p["match"], p["bet_type"], p["pick"])
                r   = resolved_map.get(key)
                if r:
                    notif = r
                else:
                    # Pick was settled before this run — no score data available
                    parts = p["match"].split(" vs ", 1)
                    notif = {
                        "match":      p["match"],
                        "bet_type":   p["bet_type"],
                        "pick":       p["pick"],
                        "odds":       p["odds"],
                        "result":     p["result"],
                        "pnl":        p["pnl"] if p["pnl"] is not None else 0.0,
                        "home_name":  parts[0].strip() if len(parts) == 2 else p["match"],
                        "away_name":  parts[1].strip() if len(parts) == 2 else "",
                        "home_score": 0,
                        "away_score": 0,
                    }
                total_pnl += notif["pnl"] if notif.get("pnl") is not None else 0.0
                _telegram_send(_format_result_notification(notif))

            total_str = f"+{total_pnl:.2f}" if total_pnl >= 0 else f"{total_pnl:.2f}"
            _telegram_send(
                f"Results {yesterday.strftime('%d %b %Y')} — "
                f"{total_str} units P&L ({len(settled)} settled picks)"
            )
            print("Done.")

            try:
                from card_generator import generate_results_card
                card_path = generate_results_card(settled, card_date=yesterday)
                _telegram_send_photo(card_path)
                log.info("Results card sent: %s", card_path.name)
                send_to_discord("results-cards", image_path=card_path)
            except Exception as exc:
                log.warning("Results card failed (non-fatal): %s", exc)

    elif "--fix-brazil-japan" in sys.argv:
        from excel_tracker import fix_brazil_japan_picks
        fix_brazil_japan_picks()

    else:
        print("Running auto-result check now...")
        stats, _ = run_auto_results()
        _print_stats(stats)
        print(f"\nDone.  Excel: {EXCEL_PATH}")
