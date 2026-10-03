"""
Automatic result checker for the football betting bot.

Usage:
    python auto_results.py              # run once immediately and exit
    python auto_results.py --schedule   # nightly daemon at 00:15 Brussels
    python auto_results.py --live       # check every 30 min + Discord alerts
"""
from __future__ import annotations

import logging
import math
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

from discord_bot import send_to_discord
from env_loader import load_env
from excel_tracker import (
    EXCEL_PATH,
    PICK_TIER_CORE,
    PICK_TIER_EXTENDED,
    finalize_workbook,
    get_pending_picks_rows,
    get_picks_for_date,
    init_excel,
    pnl_for_result,
    settlement_pnl_mismatch,
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

# PENDING picks need a human (evaluate_pick could not derive the outcome). Before
# 12 Aug 2026 they were counted under stats['errors'] and nothing else happened,
# so a row could sit unsettled until it fell out of the lookback window and was
# stranded forever — which is exactly what happened to the 10 Aug Bodø/Glimt BTTS
# pick. Now every PENDING is announced once when first seen, and again if it is
# still unsettled 24h after kickoff.
#
# Keyed by (alert_scope, match, bet_type, pick) — the scope is NOT decorative.
# These sets are module-level and every caller of run_auto_results shares them,
# so without it a second pipeline settling a different tab (the Opus 5 shadow,
# 13 Aug 2026) would consume football's alert slot for any pick both models
# happened to make: the shadow's caller discards its alerts, so the football
# row's alert would then never be raised anywhere and the row could strand
# silently — reintroducing the exact 10 Aug failure this block exists to
# prevent. Collisions are expected, not rare: both pipelines analyse the same
# fixtures with the same prompt, and PENDING is a property of the fixture (a
# two-legged tie, an unhandled bet type), not of the model.
#
# In-process only: a Railway restart re-sends the first alert, which is the safe
# direction to fail.
_pending_alerted:     set[tuple] = set()
_pending_followed_up: set[tuple] = set()
PENDING_FOLLOWUP_HOURS = 24


def _score_description(
    bet_type: str,
    pick: str,
    home_name: str,
    away_name: str,
    home_score: int,
    away_score: int,
) -> str:
    """Human-readable result line for a Discord result notification."""
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


_RESULT_EMOJI = {"WIN": "✅", "HALF WIN": "🟡", "HALF LOSS": "🟠", "LOSS": "❌"}


def _result_notification_body(r: dict) -> str:
    """
    The tier-neutral part of a settled-pick notification: verdict, match, bet,
    price, pick and the score line. Both tier formatters build on this so the
    two channels can never drift apart in anything but the money line.
    """
    emoji = _RESULT_EMOJI.get(r["result"], "⬜")
    desc  = _score_description(
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
        f"Result: {desc}"
    )


def _format_result_notification(r: dict) -> str:
    """Core result for 'results-cards': the body plus the flat-unit P&L line."""
    pnl_str = f"+{r['pnl']:.2f}" if r["pnl"] >= 0 else f"{r['pnl']:.2f}"
    return f"{_result_notification_body(r)}\nP&L: {pnl_str} units"


def _format_extended_result_notification(r: dict) -> str:
    """
    Extended result for 'extended-results': verdict and score ONLY — no P&L,
    no bankroll, no units (20 Sep 2026). Extended picks carry no stake and sit
    outside the tracked book, so a unit figure here would read as money that
    was bet. The sheet still books a flat-unit P&L on these rows for the tier
    comparison; that figure is deliberately not published.
    """
    return _result_notification_body(r)


def result_notification(r: dict) -> tuple[str, str]:
    """
    (channel_key, message) for one newly settled row, routed by tier. THE one
    place that decides where a settled football pick is announced — every
    30-minute poller (run_all.live_results_check, `--live` here) goes through
    it, so the two can never disagree.

    Extended → 'extended-results', without a money line. Anything else — Core,
    the blank-tier rows logged before 13 Aug 2026, and the handful of
    Duplicate-tagged rows — → 'results-cards' with P&L, exactly as before.
    Until 20 Sep 2026 every tier went to 'results-cards' with a P&L line, so
    the results feed was announcing ~4x the Core volume with units on picks
    that were never staked.
    """
    if r.get("pick_tier", PICK_TIER_CORE) == PICK_TIER_EXTENDED:
        return "extended-results", _format_extended_result_notification(r)
    return "results-cards", _format_result_notification(r)


def _pick_scope(pick: str) -> str | None:
    """
    The time-scope suffix on a knockout pick, added 12 Jul 2026:
    '90' for '(90 min)', 'ft' for '(Full-Time incl. ET/Pens)', None if absent.
    Shared by evaluate_pick() and the PENDING alert builder so the two can
    never disagree about how a pick was scoped.
    """
    m = re.search(r'\s*\(([^)]*)\)$', pick.lower().strip())
    if not m:
        return None
    inner = m.group(1)
    if "90" in inner:
        return "90"
    if any(x in inner for x in ("full", "et", "pen")):
        return "ft"
    return None


def _is_draw_bet_type(bet_type: str) -> bool:
    """
    True when the BET TYPE itself is a bare 'Draw' label (5 Sep 2026: row 340,
    Fiorentina vs Torino, bet type 'Draw' / pick 'Draw'). The prompt offers
    Draw only as a Match Winner PICK, so this is Claude's own label — but it is
    the same bet, and it stranded because no branch below dispatched on it.
    Matched on the whole label, never by substring, so 'Draw No Bet' (a
    different market with a refund leg) can never be swept in.
    """
    label = re.sub(r'\s*\([^)]*\)$', '', bet_type.lower()).strip()
    return re.fullmatch(r'(?:the\s+|match\s+)?draw(?:\s*/\s*x)?', label) is not None


def _pending_reason(
    bet_type: str,
    pick: str,
    *,
    extra_time: bool,
    penalties: bool,
    two_legged: bool,
    scope_ft: bool,
    margin_known: bool = False,
    home_name: str = "",
    away_name: str = "",
    extended_markets: bool = False,
) -> str:
    """
    Why evaluate_pick() could not settle this pick, in one human sentence.

    Kept in step with evaluate_pick deliberately — the two used to be able to
    disagree about WHY a pick was pending, and an alert that misstates the
    reason sends whoever settles it manually looking at the wrong thing. Since
    1 Sep 2026 the 90-minute MARGIN is derivable on ties that went to extra
    time, so 'the 90-minute outcome cannot be derived' is no longer true of
    Match Winner, Double Chance or Asian Handicap — only of the markets that
    need the 90-minute TOTAL.
    """
    # The market itself comes first: no score detail makes these settleable.
    market = classify_market(bet_type, pick, home_name, away_name)
    if market in ("stat", "half"):
        return (f"'{bet_type}' is a {'stats' if market == 'stat' else 'half-time'} "
                f"market — the feed's final score cannot settle it")
    if market == "unknown_total":
        return (f"'{bet_type}' / '{pick}' looks like a total but names something "
                f"other than this fixture's match goals or one of its sides")
    if market in EXTENDED_MARKETS and not extended_markets:
        return (f"'{bet_type}' is a {market.replace('_', ' ')} market, which this "
                f"pipeline does not settle")
    if market == "total" and _total_line(bet_type, pick, home_name, away_name) is None:
        return ("the goals line is missing, or the bet type and the pick state "
                "different lines — a line is never assumed")
    if penalties and scope_ft:
        return ("decided on a penalty shootout — the API score is level, so the "
                "winner cannot be read from it")
    if extra_time:
        if margin_known:
            return ("went past 90 minutes — the 90-minute margin is known, but "
                    "this market pays on the TOTAL, and the published score "
                    "includes extra-time goals so the 90-minute total is "
                    "ambiguous")
        if two_legged:
            return ("went past 90 minutes in a two-legged tie and the aggregate "
                    "could not be reconciled with the final score, so the "
                    "90-minute margin could not be derived")
        return ("went past 90 minutes — the API publishes no period scores, so "
                "the 90-minute outcome cannot be derived")
    if home_name and away_name and _is_match_winner_bet(bet_type.lower()):
        pk = re.sub(r'\s*\([^)]*\)$', '', pick.lower().strip()).strip()
        home_pick, away_pick, draw_pick = _match_winner_sides(
            pk, home_name.lower(), away_name.lower()
        )
        if not (home_pick or away_pick or draw_pick):
            # 8 Sep 2026, row 357: the fixture was 'FC Porto U19 vs Manchester
            # City U19'. Say which fixture the row was matched to, so the reader
            # checks THAT before suspecting the settlement rule.
            return (f"the pick names neither side of the fixture the row was "
                    f"matched to, '{home_name} vs {away_name}' — check that this "
                    f"is the right game (a youth, reserve or women's fixture "
                    f"carries the same club names)")
        if home_pick and away_pick:
            return (f"the pick matches both sides of '{home_name} vs {away_name}', "
                    f"so the side it backs cannot be read")
    return f"no settlement rule matched bet type '{bet_type}' / pick '{pick}'"


def _format_pending_notification(p: dict) -> str:
    """Alert for a pick that needs manual settlement."""
    head = ("⏳ NEEDS MANUAL SETTLEMENT"
            if p["stage"] == "initial"
            else f"🔁 STILL UNSETTLED after {p['hours_since_kickoff']:.0f}h")
    lines = [
        f"{head} — {p['match']}",
        f"Bet: {p['bet_type']}",
        f"Pick: {p['pick']}",
        f"Score: {p['score']}{' ' + p['status_note'] if p['status_note'] else ''}",
        f"Fixture: {p.get('fixture') or p['match']} (as listed by the API)",
        f"Why: {p['reason']}",
        f"Sheet row {p['sheet_row']} — settle with: "
        f"python update_result.py \"{p['match']}\" \"{p['pick']}\" WIN|LOSS",
    ]
    if p["stage"] != "initial":
        lines.append("⚠️ This will be stranded permanently once it leaves the "
                     f"{LOOKBACK_DAYS}-day lookback window.")
    return "\n".join(lines)


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


# Latin letters that do NOT decompose under NFKD. Unicode classifies these as
# distinct letters rather than accented forms, so stripping combining marks
# leaves them untouched and 'Lillestrom' never matches 'Lillestrøm'.
_TEAM_TRANSLIT = {
    "ø": "o", "æ": "ae", "å": "a", "ð": "d", "þ": "th",
    "đ": "d", "ł": "l", "ß": "ss", "œ": "oe", "ı": "i",
}


def _normalise_team(name: str) -> str:
    """
    Fold a team name to a comparable form: lowercase, transliterated, stripped
    of diacritics, whitespace collapsed.

    The pick's match string comes back through Claude, which silently
    transliterates — the sheet carries 'Lillestrom' and 'Nordsjaelland' where
    the API says 'Lillestrøm' and 'Nordsjælland'. Substring matching cannot
    bridge that, so those rows never matched a fixture, never settled, and
    stranded permanently once they aged out of the lookback window (rows 246
    and 251, found 1 Sep 2026).

    Deliberately limited to case, diacritics and whitespace. Punctuation is left
    alone: loosening further would start matching genuinely different clubs, and
    a wrong fixture settles a real bet off someone else's result.
    """
    s = (name or "").strip().lower()
    for src, dst in _TEAM_TRANSLIT.items():
        s = s.replace(src, dst)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


# Words that mark a DIFFERENT squad of the same club — youth, reserve, B and
# women's sides. The feed lists them beside the senior fixture with the club name
# intact: on 8 Sep 2026 its 8 Sep bucket carried the UEFA Youth League game
# 'FC Porto U19 vs Manchester City U19' while the senior 'FC Porto vs Manchester
# City' sat in the 9 Sep bucket. Plain containment cannot tell them apart —
# 'fc porto' sits inside 'fc porto u19' — so row 357 was matched to the youth
# game and alerted as 'no settlement rule matched', rows 358 and 359 were on
# the same path, and the senior fixture would never have been looked at.
#
# Checked ONLY against the words left over once the shorter name is removed
# from the longer one, so a club whose real name carries one of these words is
# never affected. 'u-19', 'u 19' and 'under 19' are folded to 'u19' first.
_SQUAD_QUALIFIER = re.compile(
    r"^(?:u\d{1,2}|b|c|ii|iii|iv|w|xi|women|womens|ladies|female|feminine|"
    r"feminino|femenino|femenina|femini|frauen|kvinner|kvinnor|damer|dames|"
    r"reserves?|youth|academy|jong|castilla|amateurs?)$"
)

_SIDE_EXACT   = 3   # normalised names identical
_SIDE_CONTAIN = 1   # one name inside the other; the remainder is not a squad marker


def _side_score(query: str, api_name: str) -> int:
    """
    How well a pick's team name fits an API team name, on normalised strings:
    _SIDE_EXACT for the same name, _SIDE_CONTAIN when one sits inside the
    other and the words left over do not mark another squad of the club, and 0
    when they do not fit at all.

    Containment is still needed — the sheet says 'Porto' where the feed says
    'FC Porto' — but until 8 Sep 2026 it was the ONLY rule, and it accepted
    'FC Porto U19' for 'FC Porto'. Row 357 only escaped a wrong settlement
    because its Match Winner pick named 'Manchester City' and the youth fixture
    did not; an Over/Under, BTTS or handicap pick on the same fixture would have
    paid out on the youth score without a word of warning.
    """
    if not query or not api_name:
        return 0
    if query == api_name:
        return _SIDE_EXACT
    if query in api_name:
        longer, shorter = api_name, query
    elif api_name in query:
        longer, shorter = query, api_name
    else:
        return 0
    remainder = longer.replace(shorter, " ", 1)
    remainder = re.sub(r"\b(?:u|under)[\s-]*(\d{1,2})\b", r"u\1", remainder)
    for word in re.findall(r"[^\W_]+", remainder):
        if _SQUAD_QUALIFIER.fullmatch(word):
            return 0
    return _SIDE_CONTAIN


def _side_matches(query: str, api_name: str) -> bool:
    """True when the two normalised names fit at all — exact or clean containment."""
    return _side_score(query, api_name) > 0


def _find_api_match(
    matches: list[dict],
    home_q: str,
    away_q: str,
    *,
    reversed_sides: bool = False,
) -> dict | None:
    """
    The fixture best matching '<home_q> vs <away_q>' among `matches`, or None.

    Every candidate is scored (_side_score per side, summed) and the highest
    wins, so an exact fixture beats a containment hit wherever it sits in the
    list. Since 8 Sep 2026 the caller passes BOTH date buckets in one list for
    the same reason: searched bucket by bucket, the first bucket's youth game
    was returned before the next bucket's senior fixture was ever looked at.
    Two DIFFERENT fixtures tied at the top score are ambiguous, and an
    ambiguous match is no match — a warning is logged and None returned,
    because a wrong fixture settles a real bet off someone else's result.

    `reversed_sides` looks for the SAME pair with the sides swapped. Claude
    occasionally emits a fixture with home and away the wrong way round (row
    243: 'Bodø/Glimt vs NEC Nijmegen' for a fixture the API lists as 'NEC
    Nijmegen vs Bodø/Glimt'), which is a naming error, not a different match.

    Callers must exhaust the correct orientation across EVERY candidate date
    before trying this one. In a two-legged tie both orientations exist as real
    fixtures on different dates, so a reversed match found greedily could settle
    a pick against the wrong leg.

    Settlement then uses the API's own home/away, not the pick's — scores, names
    and handicap sides all come from the matched fixture, so a reversed match
    still settles correctly.
    """
    hq = _normalise_team(home_q)
    aq = _normalise_team(away_q)
    if not hq or not aq:
        return None
    best: dict | None = None
    best_score = 0
    tied_with: dict | None = None
    for m in matches:
        h = _normalise_team(m["home"]["longName"])
        a = _normalise_team(m["away"]["longName"])
        if reversed_sides:
            h, a = a, h
        hs, as_ = _side_score(hq, h), _side_score(aq, a)
        if not hs or not as_:
            continue
        score = hs + as_
        if score > best_score:
            best, best_score, tied_with = m, score, None
        elif score == best_score and m.get("id") != best.get("id"):
            tied_with = m
    if best is not None and tied_with is not None:
        log.warning(
            "'%s vs %s' matches two different fixtures equally well — '%s vs %s' "
            "and '%s vs %s' — so neither is used; settle manually via update_result.py",
            home_q, away_q,
            best["home"]["longName"], best["away"]["longName"],
            tied_with["home"]["longName"], tied_with["away"]["longName"],
        )
        return None
    return best


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


def _parse_aggregate(aggregate: str | None) -> tuple[int, int] | None:
    """'5 - 4' -> (5, 4). None when absent or unparseable — never a guess."""
    if not aggregate:
        return None
    nums = re.findall(r"\d+", str(aggregate))
    if len(nums) != 2:
        return None
    return int(nums[0]), int(nums[1])


def _regulation_goal_difference(
    home_score: int,
    away_score: int,
    aggregate: str | None,
    *,
    two_legged: bool,
) -> int | None:
    """
    Home-minus-away goal difference after 90 minutes, or None if not derivable.

    The API publishes no period scores at all — checked exhaustively 1 Sep 2026:
    football-get-match-detail returns 792 bytes of metadata with no score,
    football-get-match-score returns only the final score, and every
    events/statistics/timeline endpoint 404s. status.halfs carries period START
    TIMESTAMPS, never period scores. So the 90' SCORE genuinely cannot be read.

    The 90' goal DIFFERENCE can be computed, though, and that is enough to
    settle every market that pays on the margin:

    * Single match — extra time is only reachable from a level score, so
      regulation ended a draw. Difference is 0.

    * Two-legged tie — extra time is triggered by the AGGREGATE being level at
      the end of 90 minutes of the second leg. Each side's first-leg goals are
      (aggregate - this leg's goals), so aggregate parity at 90' pins the margin
      exactly:

          h90 - a90 = (agg_away - away_score) - (agg_home - home_score)

      Validated 1 Sep 2026 against all 15 finished two-legged AET/shootout ties
      in the 19-27 Aug feeds, UEFA and CONMEBOL, with and without extra time.
      LASK 5-1 Celtic (agg 5-4) yields +3: LASK led by exactly three at 90',
      whether the night's score was 3-0 or 4-1.

    Two sanity gates, because a wrong margin settles a real bet:
      * each side's aggregate must be at least its goals in this leg (goals only
        accumulate), which also catches an aggregate published the other way round
      * the margin must be reachable given the final score

    Either gate failing returns None, and the caller leaves the pick PENDING.
    """
    if not two_legged:
        # ET in a single match implies a level score after 90 minutes.
        return 0

    agg = _parse_aggregate(aggregate)
    if agg is None:
        return None
    agg_home, agg_away = agg

    # Goals accumulate across legs, so the aggregate cannot be below this leg's
    # score for either side. A failure here means bad data or an aggregate
    # oriented against the fixture — either way, do not derive from it.
    if agg_home < home_score or agg_away < away_score:
        log.warning(
            "Aggregate '%s' is inconsistent with this leg's score %d-%d — "
            "not deriving a 90-minute margin from it", aggregate, home_score, away_score,
        )
        return None

    reg_gd = (agg_away - away_score) - (agg_home - home_score)

    # Reachability: some (h90, a90) with h90 - a90 = reg_gd must sit inside the
    # final score. a90 ranges over [max(0, -reg_gd), min(away_score, home_score - reg_gd)].
    lo = max(0, -reg_gd)
    hi = min(away_score, home_score - reg_gd)
    if lo > hi:
        log.warning(
            "Derived 90-minute margin %+d is unreachable inside final score %d-%d "
            "(aggregate '%s') — leaving the pick PENDING",
            reg_gd, home_score, away_score, aggregate,
        )
        return None

    return reg_gd


def _combine_ah_halves(r1: str, r2: str) -> str:
    pair = frozenset([r1, r2])
    if pair == frozenset(["WIN"]):           return "WIN"
    if pair == frozenset(["WIN",  "VOID"]):  return "HALF WIN"
    if pair == frozenset(["VOID"]):          return "VOID"
    if pair == frozenset(["VOID", "LOSS"]):  return "HALF LOSS"
    if pair == frozenset(["LOSS"]):          return "LOSS"
    return "VOID"  # WIN + LOSS edge case — treat as push


_MATCH_WINNER_LABELS = ("match winner", "1x2", "result", "moneyline")


def _is_match_winner_bet(bt: str) -> bool:
    """True for a lower-cased bet type that settles as a 3-way match result."""
    return any(x in bt for x in _MATCH_WINNER_LABELS)


def _match_winner_sides(pk: str, hn: str, an: str) -> tuple[bool, bool, bool]:
    """
    (home_pick, away_pick, draw_pick) for a lower-cased, scope-stripped pick
    against the matched fixture's lower-cased names. Generic labels ('Home Win',
    '2') or a team name anywhere in the pick. A draw label is a draw and nothing
    else — 'X' must never read as a home pick because the letter sits inside
    'Ajax'. Shared by evaluate_pick() and _pending_reason() so the alert can
    never disagree with the verdict about which side the pick names.
    """
    if pk in ("draw", "x", "tie"):
        return False, False, True
    home_pick = pk in ("home", "home win", "1") or hn in pk or pk in hn
    away_pick = pk in ("away", "away win", "2") or an in pk or pk in an
    return home_pick, away_pick, False


# ── Market recognition (4 Oct 2026) ──────────────────────────────────────────
# evaluate_pick dispatches on substrings of the bet type, so a market it has no
# rule for could fall into one that merely shares a word. Measured 3 Oct 2026:
# 'Corners Over 9.5', 'Over 4.5 Cards' and 'Shots on Target Over 8.5' all entered
# the goals Over/Under branch and settled on GOALS; 'Team Total Goals / Arsenal
# Over 1.5' settled on the MATCH total; 'Match Winner / Arsenal Win to Nil' paid
# as a plain win; and 'Over/Under / Over 1.5 Goals' settled against a defaulted
# 2.5 line. All of them silently. The final score can settle none of the stat or
# half-time markets, so they are recognised up front and return PENDING with a
# reason — a pick nobody can settle automatically must reach a human, never a
# goals verdict.

# Stat and period markets: the final score carries no corners, cards, shots or
# half-time score, so these can never settle here. Whole words only — 'Cardiff'
# must not read as a cards market.
_STAT_MARKET_RE = re.compile(
    r"\b(?:corners?|cards?|bookings?|booking points|shots?|sot|fouls?|"
    r"offsides?|throw[\s-]?ins?|tackles?|saves?|xg|possession|clean sheets?)\b"
)
_HALF_MARKET_RE = re.compile(
    r"\b(?:1st|2nd|first|second)[\s-]*half\b|\bhalf[\s-]*time\b|\bht\b|\bh[12]\b"
)
_WIN_TO_NIL_RE   = re.compile(r"\bto\s+nil\b")
_DRAW_NO_BET_RE  = re.compile(r"\bdraw\s+no\s+bet\b|\bdnb\b")
_TEAM_TOTAL_RE   = re.compile(r"\bteam\s+(?:total|goals)\b")
_LINE_RE         = re.compile(r"\d+(?:\.\d+)?")
_OU_BET_LABELS   = ("over", "under", "total goals", "goals over", "o/u")

# Words a match-total label may carry besides the line. Anything else left in an
# Over/Under bet type or pick names something — a team (a team total) or a
# market this branch does not know — and the match total must not settle it.
_TOTAL_WORDS = frozenset({
    "over", "under", "o", "u", "total", "totals", "goal", "goals", "match",
    "line", "alt", "alternate", "full", "time", "ft", "and", "or", "to", "score",
})


def _strip_scope(text: str) -> str:
    """Lower-cased text without a trailing '(…)' time-scope marker."""
    return re.sub(r"\s*\([^)]*\)$", "", text.lower().strip()).strip()


def _without_names(text: str, *names: str) -> str:
    """`text` normalised, with each (normalised) team name cut out of it."""
    out = _normalise_team(text)
    for name in names:
        n = _normalise_team(name)
        if n:
            out = out.replace(n, " ")
    return out


def _total_side_name(bt: str, pk: str, hn: str, an: str) -> tuple[str, str | None]:
    """
    Read an Over/Under-shaped label: ('match', None) for a plain match total,
    ('team', 'home'|'away') when it names one side, ('unknown', None) when it
    carries words that are neither a total nor a side of this fixture.
    """
    leftover: list[str] = []
    for text in (_strip_scope(bt), _strip_scope(pk)):
        words = re.findall(r"[^\W\d_]+", _normalise_team(text))
        leftover += [w for w in words if w not in _TOTAL_WORDS and w != "team"]
    if not leftover and not _TEAM_TOTAL_RE.search(bt + " " + pk):
        return "match", None
    rest = " ".join(leftover)
    home_fit = _side_matches(rest, _normalise_team(hn)) if rest else False
    away_fit = _side_matches(rest, _normalise_team(an)) if rest else False
    if home_fit and not away_fit:
        return "team", "home"
    if away_fit and not home_fit:
        return "team", "away"
    # Fall back to the full name inside the text — 'leeds united over 0.5' leaves
    # 'leeds united' but a pick may also add words the side does not carry.
    pk_n = _normalise_team(_strip_scope(pk) + " " + _strip_scope(bt))
    h_in = bool(_normalise_team(hn)) and _normalise_team(hn) in pk_n
    a_in = bool(_normalise_team(an)) and _normalise_team(an) in pk_n
    if h_in != a_in:
        return "team", "home" if h_in else "away"
    return "unknown", None


def _total_line(bt: str, pk: str, hn: str, an: str) -> float | None:
    """
    The goals line a total pick is on, read from the pick AND the bet type.

    None when no line is stated anywhere, or when the two state DIFFERENT lines.
    Until 4 Oct 2026 the line came from the bet type alone and defaulted to 2.5,
    so 'Over/Under / Over 1.5 Goals' settled against 2.5. A guessed line settles
    a real bet on the wrong market, so there is no default any more. Team names
    are cut out first so a club name with digits ('Schalke 04') is not read as a
    line.
    """
    lines = {
        float(n)
        for text in (_strip_scope(bt), _strip_scope(pk))
        for n in _LINE_RE.findall(_without_names(text, hn, an))
    }
    return lines.pop() if len(lines) == 1 else None


def classify_market(bet_type: str, pick: str, home_name: str = "", away_name: str = "") -> str:
    """
    Which market a pick is on, from its own text:

      'stat'        corners, cards, shots, … — never settleable from the score
      'half'        a half-time / single-half market — same
      'win_to_nil'  '<Team> to Win to Nil'
      'draw_no_bet' Draw No Bet
      'team_total'  an Over/Under naming one side of the fixture
      'total'       a match goals Over/Under
      'other'       everything evaluate_pick already dispatches on

    `home_name` / `away_name` are optional: without them a team total is still
    recognised by its words, and the Opus shadow uses that at generation time,
    before any fixture has been matched. Shared by evaluate_pick, the PENDING
    reason and the shadow's Core filter so they can never disagree.
    """
    bt = _strip_scope(bet_type)
    pk = _strip_scope(pick)
    both = f"{bt} {pk}"
    if _STAT_MARKET_RE.search(both):
        return "stat"
    if _HALF_MARKET_RE.search(both):
        return "half"
    if _WIN_TO_NIL_RE.search(both):
        return "win_to_nil"
    if _DRAW_NO_BET_RE.search(both):
        return "draw_no_bet"
    if _TEAM_TOTAL_RE.search(both):
        return "team_total"
    if any(x in bt for x in _OU_BET_LABELS) and not any(
        x in bt for x in ("both teams to score", "btts", "handicap")
    ):
        if home_name and away_name:
            kind, _side = _total_side_name(bt, pk, home_name.lower(), away_name.lower())
            return {"match": "total", "team": "team_total"}.get(kind, "unknown_total")
        words = [w for w in re.findall(r"[^\W\d_]+", _normalise_team(both))
                 if w not in _TOTAL_WORDS]
        return "team_total" if words else "total"
    return "other"


# Markets the Opus shadow may offer on top of the production prompt's set, and
# that evaluate_pick settles ONLY when the caller passes extended_markets=True.
# Production never passes it, so for Sonnet these stay PENDING — exactly what
# they were before 4 Oct 2026 for Draw No Bet, and a correction for the rest.
EXTENDED_MARKETS = ("draw_no_bet", "team_total", "win_to_nil")


def is_extended_market(bet_type: str, pick: str) -> bool:
    """
    True for a pick on a market outside the production prompt's set: Draw No
    Bet, a team total, win to nil, or a goals total on any line other than 2.5.
    The Opus shadow tags these and keeps them out of Core.
    """
    kind = classify_market(bet_type, pick)
    if kind in EXTENDED_MARKETS:
        return True
    if kind == "total":
        return _total_line(bet_type, pick, "", "") != 2.5
    return False


def _goal_range_90(home_score: int, away_score: int, reg_gd: int | None,
                   past_90: bool, *, home: bool) -> tuple[int, int]:
    """
    The range one side's 90-minute goals can lie in.

    Exact (g, g) when the published score is the 90-minute one. After extra time
    with a trusted margin, a90 lies in [max(0, -gd), min(away, home - gd)] and
    h90 = a90 + gd — the same bound the Over/Under branch uses. Without a
    trusted margin all that is left is that goals accumulate: [0, final].
    """
    final = home_score if home else away_score
    if not past_90:
        return final, final
    if reg_gd is None:
        return 0, final
    a_lo, a_hi = max(0, -reg_gd), min(away_score, home_score - reg_gd)
    return (a_lo + reg_gd, a_hi + reg_gd) if home else (a_lo, a_hi)


def _line_outcome(goals: int, line: float, over: bool) -> str:
    if goals == line:
        return "VOID"
    return "WIN" if (goals > line) == over else "LOSS"


# Words that carry the market, not the side, in a Draw No Bet / win-to-nil pick.
_SIDE_NOISE_RE = re.compile(r"\b(?:draw\s+no\s+bet|dnb|to\s+win\s+to\s+nil|"
                            r"wins?\s+to\s+nil|to\s+nil|to\s+win|wins?|nil)\b")


def _extended_side(pk: str, hn: str, an: str) -> str | None:
    """'home' / 'away' for the one side a DNB or win-to-nil pick names, else None."""
    core = " ".join(_SIDE_NOISE_RE.sub(" ", pk).split())
    if not core or core in ("draw", "x", "tie"):
        return None
    home, away, _draw = _match_winner_sides(core, hn, an)
    if home == away:
        return None
    return "home" if home else "away"


def _evaluate_extended_market(
    market: str, bt: str, pk: str, hn: str, an: str,
    home_score: int, away_score: int, *,
    scope: str | None, past_90: bool, reg_gd: int | None, pick: str,
) -> str:
    """
    Draw No Bet, team total goals and win to nil (4 Oct 2026, Opus shadow only).

    All three are 90-minute markets at every bookmaker, so a '(Full-Time incl.
    ET/Pens)' scope is refused. After extra time they settle only on what the
    derived 90-minute margin and the goal bounds make certain, exactly like
    the production branches — never on the published score.

    * Draw No Bet pays on the margin: level → VOID. A single match that went to
      extra time was level at 90', so it VOIDs; a two-legged tie uses the
      derived margin; no trusted margin → PENDING.
    * Team total compares one side's 90-minute goals with the line, settling
      only when the whole possible range (_goal_range_90) lands on one side.
    * Win to nil needs a 90-minute win AND a scoreless opponent. A single match
      that went to extra time was level at 90', so it LOSES; otherwise the
      opponent's goal range decides, PENDING while it straddles zero.
    """
    if scope == "ft":
        log.warning("Pick '%s' is a 90-minute market with a full-time scope — "
                    "settle manually via update_result.py", pick)
        return "PENDING"

    if market == "team_total":
        kind, side = _total_side_name(bt, pk, hn, an)
        line = _total_line(bt, pk, hn, an)
        over_pick, under_pick = "over" in pk, "under" in pk
        if kind != "team" or line is None or over_pick == under_pick:
            log.warning("Team total pick '%s' (%s) does not name exactly one side, "
                        "one line and one of Over/Under — settle manually", pick, bt)
            return "PENDING"
        if (line * 2) != int(line * 2):
            log.warning("Team total pick '%s' is on a quarter line — settle manually",
                        pick)
            return "PENDING"
        lo, hi = _goal_range_90(home_score, away_score, reg_gd, past_90,
                                home=(side == "home"))
        first, last = _line_outcome(lo, line, over_pick), _line_outcome(hi, line, over_pick)
        if first == last:
            return first
        log.warning("Team total pick '%s' went past 90 minutes and the side's "
                    "90-minute goals (%d-%d) straddle the line — settle manually",
                    pick, lo, hi)
        return "PENDING"

    side = _extended_side(pk, hn, an)
    if side is None:
        log.warning("%s pick '%s' names neither or both sides of '%s vs %s' — "
                    "settle manually via update_result.py", market, pick, hn, an)
        return "PENDING"

    if past_90 and reg_gd is None:
        log.warning("Pick '%s' (%s) went past 90 minutes and the 90-minute margin "
                    "could not be derived — settle manually", pick, market)
        return "PENDING"
    gd = reg_gd if past_90 else home_score - away_score
    margin = gd if side == "home" else -gd

    if market == "draw_no_bet":
        if margin == 0:
            return "VOID"
        return "WIN" if margin > 0 else "LOSS"

    # win_to_nil
    if margin <= 0:
        return "LOSS"
    lo, hi = _goal_range_90(home_score, away_score, reg_gd, past_90,
                            home=(side == "away"))   # the OPPONENT's goals
    if hi == 0:
        return "WIN"
    if lo > 0:
        return "LOSS"
    log.warning("Win-to-nil pick '%s' went past 90 minutes and the opponent's "
                "90-minute goals (%d-%d) are unknown — settle manually", pick, lo, hi)
    return "PENDING"


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
    two_legged: bool = False,
    aggregate: str | None = None,
    extended_markets: bool = False,
) -> str:
    """
    Return WIN, LOSS, VOID, or PENDING (unrecognised bet type / data missing).

    `extended_markets` (4 Oct 2026) enables Draw No Bet, team totals and win to
    nil. Only the Opus shadow passes it; production never does, so for Sonnet
    those markets stay PENDING. Stat and half-time markets (corners, cards,
    shots, …) are PENDING for every caller: the final score cannot settle them.
    See classify_market.

    Handles both generic terms ('Home Win', 'Away or Draw') and team-name
    picks generated by the updated Claude prompt ('Sweden Win', 'Ivory Coast or Draw').

    Knockout picks may carry a time-scope suffix since 12 Jul 2026:
    '(90 min)' or '(Full-Time incl. ET/Pens)'. Unscoped picks follow the
    bookmaker default for EVERY bet type: 90 minutes only.

    `extra_time` means EXTRA TIME WAS ACTUALLY PLAYED, not merely that the tie
    needed separating (changed 1 Sep 2026). The two are different: a shootout
    that follows straight after 90 minutes — the CONMEBOL format, and many
    domestic cups — leaves the published score EQUAL to the 90-minute score, so
    every market settles exactly. Treating 'went to penalties' as 'went past 90'
    sent all of those to manual settlement for nothing. The caller reads
    status.halfs.firstExtraHalfStarted to tell them apart.

    When extra time WAS played the published score includes it, so the 90-minute
    score is unknown — the API publishes no period scores anywhere. The 90-minute
    goal DIFFERENCE is still derivable (see _regulation_goal_difference), and
    that settles every market paying on the margin:

    * Match Winner, Double Chance, Asian Handicap — settled from the derived
      margin. For a single match the margin is 0 (extra time implies a level
      score); for a two-legged tie it follows from the aggregate being level at
      90 minutes of the second leg. Before 1 Sep 2026 the two-legged case
      returned PENDING for all three, which is why LASK vs Celtic and Rapid Wien
      vs Hearts sat unsettled — both were derivable all along.
    * Over/Under and BTTS — the margin does not pin the TOTAL, so these stay
      PENDING unless a bound settles them outright: a side scoreless over 120'
      was scoreless over 90' (BTTS), or the largest reachable 90-minute total
      still sits under the line (Under).

    A derivation that cannot be trusted returns None from the helper and the
    pick stays PENDING. Never settle a margin that was guessed.
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
    scope = _pick_scope(pk)
    if scope:
        pk = re.sub(r'\s*\([^)]*\)$', '', pk).strip()

    # A bare 'Draw' bet type is the Match Winner draw outcome, so it is folded
    # into that branch rather than given a second copy of its rule: WIN when
    # the 90-minute score is level, LOSS otherwise, with the identical
    # two-legged extra-time handling — settled off the derived margin, PENDING
    # when that margin cannot be derived. Only when the pick text agrees it is
    # a draw (or is blank): a pick naming a side under a 'Draw' bet type is
    # contradictory and stays unhandled, so it alerts for manual settlement
    # instead of being settled on a guess.
    if _is_draw_bet_type(bt) and (
        pk in ("", "draw", "x", "tie", "the draw")
        or ("draw" in pk and "no bet" not in pk and hn not in pk and an not in pk)
    ):
        bt, pk = "match winner", "draw"

    # Extra time was PLAYED and the pick settles on regulation time (the
    # default), so the published score overshoots the 90-minute one. A shootout
    # with no extra time does not qualify: its score IS the 90-minute score.
    past_90 = extra_time and scope != "ft"

    # Home-minus-away margin after 90 minutes: 0 for a single match, derived
    # from the aggregate for a two-legged tie, None when it cannot be trusted.
    reg_gd = (
        _regulation_goal_difference(
            home_score, away_score, aggregate, two_legged=two_legged
        )
        if past_90 else None
    )
    reg_known   = past_90 and reg_gd is not None
    reg_unknown = past_90 and reg_gd is None

    # ── Market recognition — before any substring dispatch ───────────────────
    market = classify_market(bet_type, pick, home_name, away_name)
    if market in ("stat", "half", "unknown_total"):
        log.warning("Pick '%s' (%s) is on a market the final score cannot settle "
                    "(%s) — settle manually via update_result.py",
                    pick, bet_type, market)
        return "PENDING"
    if market in EXTENDED_MARKETS:
        if not extended_markets:
            log.warning("Pick '%s' (%s) is a %s pick and this pipeline does not "
                        "settle that market — settle manually via update_result.py",
                        pick, bet_type, market)
            return "PENDING"
        return _evaluate_extended_market(
            market, bt, pk, hn, an, home_score, away_score,
            scope=scope, past_90=past_90, reg_gd=reg_gd, pick=pick,
        )

    # ── Match Winner ─────────────────────────────────────────────────────────
    if _is_match_winner_bet(bt):
        home_pick, away_pick, draw_pick = _match_winner_sides(pk, hn, an)

        # The pick names a side this fixture does not have. Either the pick text
        # is unreadable or — the 8 Sep 2026 case — the fixture is the wrong one:
        # a youth, reserve or women's game carrying the same club names. There
        # is nothing to settle and no side to guess, and the reason must say so
        # (it used to fall through to "no settlement rule matched bet type
        # Match Winner", which sent the fix hunting in the wrong place).
        if not (home_pick or away_pick or draw_pick):
            log.warning("Match Winner pick '%s' names neither side of '%s vs %s' — "
                        "settle manually via update_result.py",
                        pick, home_name, away_name)
            return "PENDING"
        if home_pick and away_pick:
            log.warning("Match Winner pick '%s' matches BOTH sides of '%s vs %s' — "
                        "settle manually via update_result.py",
                        pick, home_name, away_name)
            return "PENDING"

        if reg_unknown:
            log.warning("Pick '%s' (%s) went past 90 minutes and the 90-minute "
                        "margin could not be derived; settle manually via "
                        "update_result.py", pick, bet_type)
            return "PENDING"
        if reg_known:
            # Settle on the derived 90-minute margin, not the published score
            # (which includes extra-time goals).
            if draw_pick: return "WIN" if reg_gd == 0 else "LOSS"
            if home_pick: return "WIN" if reg_gd > 0 else "LOSS"
            if away_pick: return "WIN" if reg_gd < 0 else "LOSS"
        if penalties and scope == "ft":
            # After a shootout the API score is level — the winner cannot be
            # derived here. Settle manually via update_result.py.
            log.warning("Pick '%s' was decided on penalties — settle manually "
                        "via update_result.py", pick)
            return "PENDING"

        if home_pick: return "WIN" if hw else "LOSS"
        if away_pick: return "WIN" if aw else "LOSS"
        if draw_pick: return "WIN" if dr else "LOSS"

    # ── Both Teams to Score ──────────────────────────────────────────────────
    elif any(x in bt for x in ("both teams to score", "btts", "gg/ng", "goal goal")):
        if past_90:
            if min(home_score, away_score) == 0:
                # Goals are monotonic: a side that failed to score across the
                # full 120' also failed to score in the first 90'. Holds for a
                # two-legged tie too — it needs no draw inference.
                both = False
            else:
                log.warning("Pick '%s' (%s) went past 90 minutes%s — the "
                            "90-minute BTTS outcome cannot be derived from the "
                            "final score; settle manually via update_result.py",
                            pick, bet_type,
                            " in a TWO-LEGGED tie" if two_legged else "")
                return "PENDING"
        else:
            both = home_score > 0 and away_score > 0
        if pk in ("yes", "true", "gg", "yes (gg)"): return "WIN" if both     else "LOSS"
        if pk in ("no",  "false", "ng", "no (ng)"): return "WIN" if not both else "LOSS"

    # ── Over / Under goals ───────────────────────────────────────────────────
    elif any(x in bt for x in _OU_BET_LABELS):
        # The line comes from the pick AND the bet type, and they must agree.
        # Never a default: 'Over/Under / Over 1.5 Goals' settled against 2.5
        # until 4 Oct 2026.
        threshold = _total_line(bet_type, pick, home_name, away_name)
        if threshold is None:
            log.warning("Pick '%s' (%s) states no goals line, or two different "
                        "ones — settle manually via update_result.py", pick, bet_type)
            return "PENDING"
        over_pick, under_pick = "over" in pk, "under" in pk
        if over_pick == under_pick:
            log.warning("Pick '%s' (%s) names neither or both of Over and Under — "
                        "settle manually via update_result.py", pick, bet_type)
            return "PENDING"
        if past_90:
            # Largest 90-minute total consistent with the derived margin. With
            # h90 - a90 = reg_gd and neither side above its final score, a90 tops
            # out at min(away_score, home_score - reg_gd), giving 2*a90 + reg_gd.
            # For a single match reg_gd is 0 and this reduces to the old
            # 2 × min(final scores). Without a trusted margin, fall back to the
            # only fact left — goals accumulate, so the 90' total cannot exceed
            # the final one.
            if reg_known:
                max_reg_total = 2 * min(away_score, home_score - reg_gd) + reg_gd
            else:
                max_reg_total = total
            if max_reg_total < threshold:
                if "over"  in pk: return "LOSS"
                if "under" in pk: return "WIN"
            log.warning("Pick '%s' (%s) went past 90 minutes%s — the 90-minute "
                        "total cannot be derived from the final score; settle "
                        "manually via update_result.py", pick, bet_type,
                        " in a TWO-LEGGED tie" if two_legged else "")
            return "PENDING"
        if "over"  in pk: return "WIN" if total >  threshold else "LOSS"
        if "under" in pk: return "WIN" if total <  threshold else "LOSS"

    # ── Asian Handicap ───────────────────────────────────────────────────────
    elif any(x in bt for x in ("asian handicap", "handicap", " ah ")):
        parsed = _parse_handicap(pk)  # pk, not pick: the scope suffix is stripped
        if parsed:
            team_q, hc = parsed
            picked_home = None
            if team_q in hn or hn in team_q:
                score, opp, picked_home = home_score, away_score, True
            elif team_q in an or an in team_q:
                score, opp, picked_home = away_score, home_score, False
            else:
                score, opp = None, None
            if score is not None and reg_unknown:
                log.warning("Pick '%s' (%s) went past 90 minutes and the 90-minute "
                            "goal difference could not be derived; settle manually "
                            "via update_result.py", pick, bet_type)
                return "PENDING"
            if score is not None and reg_known:
                # AH pays on the goal difference alone, and that is exactly what
                # the derivation gives — so the handicap settles even when the
                # 90-minute SCORE is unknown. Expressed from the picked side.
                margin = reg_gd if picked_home else -reg_gd
                score, opp = margin, 0
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
        if reg_unknown:
            log.warning("Pick '%s' (%s) went past 90 minutes and the 90-minute "
                        "result could not be derived; settle manually via "
                        "update_result.py", pick, bet_type)
            return "PENDING"
        if reg_known:
            # Re-express the outcome from the derived 90-minute margin.
            hw, aw, dr = reg_gd > 0, reg_gd < 0, reg_gd == 0
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
    alert_scope: str = "football",
    extended_markets: bool = False,
) -> tuple[dict, list[dict]]:
    """
    Scan pending Google Sheets rows, fetch API scores, update the sheet.
    Returns (stats_dict, list_of_newly_resolved_picks).

    The three hooks default to the production football tab
    (get_pending_picks_rows / update_row_result / finalize_workbook); a
    caller may pass its own tab's reader/writer plus a different finalizer
    to settle another tab with the identical evaluation logic.

    `alert_scope` namespaces the module-level PENDING-alert dedup sets. ANY
    caller settling a tab other than the football one MUST pass its own scope,
    or its PENDING rows will consume football's alert slots — see the comment
    on _pending_alerted above.

    `extended_markets` is passed straight to evaluate_pick: True only for the
    Opus shadow (Draw No Bet, team totals, win to nil). Production never sets it.

    `row_writer` may return False to signal the write failed; that row is then
    not counted as updated and not reported as resolved. A writer returning
    None (the football one) is treated as success, so this is backward
    compatible.
    """
    pending_source = pending_source or get_pending_picks_rows
    row_writer     = row_writer or update_row_result
    finalizer      = finalizer if finalizer is not None else finalize_workbook

    init_excel()

    # 'pending_alerts' rides along in stats so the return signature stays a
    # 2-tuple — run_all.py owns delivery, the same way it does for 'resolved'.
    # Keeping the send out of here means a caller settling a different tab via
    # the hooks below never posts to the football results channel.
    stats   = {"checked": 0, "updated": 0, "not_finished": 0,
               "no_match": 0, "too_old": 0, "errors": 0, "pending": 0,
               "pending_alerts": []}
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

        # Both date buckets are searched TOGETHER, not one after the other, so
        # the best-fitting fixture wins wherever it sits. On 8 Sep 2026 the
        # feed's 8 Sep bucket held only the Youth League game 'FC Porto U19 vs
        # Manchester City U19' and the senior fixture sat in the 9 Sep bucket; a
        # bucket-by-bucket search took the youth game and never looked further.
        # The correct orientation across both buckets still comes before the
        # reversed one: both orientations are real fixtures in a two-legged
        # tie, so a greedy reversed match could settle against the wrong leg.
        candidate_dates = (p["date"], p["date"] + timedelta(days=1))
        pool = [m for dt in candidate_dates for m in api_cache.get(dt, [])]
        api_match = _find_api_match(pool, home_q, away_q)

        if api_match is None:
            api_match = _find_api_match(pool, home_q, away_q, reversed_sides=True)
            if api_match:
                log.warning(
                    "'%s' matched with home/away REVERSED — the API lists it as "
                    "'%s vs %s'. Settling on the API's orientation.",
                    match, api_match["home"]["longName"], api_match["away"]["longName"],
                )

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
        status     = api_match.get("status") or {}
        reason     = status.get("reason") or {}
        fin_txt    = f"{reason.get('short', '')} {reason.get('long', '')}".lower()
        penalties  = fin_txt.startswith("pen") or "penalt" in fin_txt
        aet_reason = "aet" in fin_txt or "extra time" in fin_txt

        # Did extra time actually get PLAYED? A shootout straight after 90
        # minutes (CONMEBOL, many domestic cups) leaves the published score
        # equal to the 90-minute score, so every market settles exactly — but
        # until 1 Sep 2026 'penalties' alone was read as 'went past 90' and
        # those all went to manual settlement for nothing. status.halfs carries
        # a start timestamp per period, so an extra-time half appears there iff
        # one was played.
        halfs = status.get("halfs") or {}
        if aet_reason:
            extra_time = True
        elif not halfs:
            # No period data at all: cannot rule extra time out, so assume the
            # cautious side for a tie that needed separating. Wrongly assuming
            # extra time costs a manual settlement; wrongly assuming none
            # settles a bet off a score that is not the 90-minute one.
            extra_time = bool(penalties)
        else:
            extra_time = bool(halfs.get("firstExtraHalfStarted"))

        # A two-legged tie carries an aggregate score, and that aggregate is
        # what pins the 90-minute margin — see _regulation_goal_difference().
        aggregate  = status.get("aggregatedStr")
        two_legged = bool(aggregate)

        result = evaluate_pick(bet_type, pick, home_name, away_name, home_score, away_score,
                               extra_time=extra_time, penalties=penalties,
                               two_legged=two_legged, aggregate=aggregate,
                               extended_markets=extended_markets)

        if result == "PENDING":
            log.warning("Could not evaluate bet_type='%s' pick='%s'", bet_type, pick)
            stats["errors"] += 1
            stats["pending"] += 1

            key = (alert_scope, match, bet_type, pick)
            kickoff = status.get("utcTime") or ""
            hours = 0.0
            try:
                ko = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                hours = (datetime.now(timezone.utc) - ko).total_seconds() / 3600
            except ValueError:
                log.warning("Unparseable kickoff '%s' for pending pick '%s'", kickoff, match)

            stage = None
            if key not in _pending_alerted:
                _pending_alerted.add(key)
                if hours >= PENDING_FOLLOWUP_HOURS:
                    # Already past the follow-up window on first sighting — a
                    # restart cleared the in-process state, or the API resolved
                    # the fixture late. Send the louder 'still unsettled' alert
                    # once instead of both variants 30 minutes apart.
                    stage = "followup"
                    _pending_followed_up.add(key)
                else:
                    stage = "initial"
            elif hours >= PENDING_FOLLOWUP_HOURS and key not in _pending_followed_up:
                stage = "followup"
                _pending_followed_up.add(key)

            if stage:
                status_note = ("(after penalties)" if penalties
                               else "(after extra time)" if extra_time else "")
                if two_legged:
                    status_note += f" two-legged, agg {status['aggregatedStr']}"
                stats["pending_alerts"].append({
                    "stage":       stage,
                    "sheet_row":   sheet_row,
                    "match":       match,
                    "bet_type":    bet_type,
                    "pick":        pick,
                    "score":       f"{home_score}-{away_score}",
                    "status_note": status_note.strip(),
                    "kickoff_utc": kickoff,
                    "hours_since_kickoff": hours,
                    "fixture":     f"{home_name} vs {away_name}",
                    "reason": _pending_reason(
                        bet_type, pick,
                        home_name=home_name, away_name=away_name,
                        extra_time=extra_time, penalties=penalties,
                        two_legged=two_legged,
                        scope_ft=_pick_scope(pick) == "ft",
                        extended_markets=extended_markets,
                        margin_known=_regulation_goal_difference(
                            home_score, away_score, aggregate, two_legged=two_legged
                        ) is not None,
                    ),
                })
            continue

        # 'odds' is the settlement price resolved by the pending-rows reader:
        # the matched market price when there is one, else Claude's estimate.
        # Paying out at the estimate inflated P&L on every pick whose card
        # showed a shorter market price (fixed 9 Aug 2026).
        pnl = pnl_for_result(result, odds)

        # The payout must follow from the result and the price this row settles
        # at. A breach means the row is corrupt — a stale write, a hand edit, or
        # a price that moved under a settlement nobody recomputed — and it must
        # never reach a subscriber-facing message. Cheap, and it also catches the
        # degenerate case of a WIN paying 0.00 because the price fell back to 1.0.
        breach = settlement_pnl_mismatch(result, odds, pnl)
        if breach:
            log.error("Row %s (%s | %s): IMPOSSIBLE PAYOUT — %s",
                      sheet_row, match, pick, breach)
            stats.setdefault("integrity_breaches", []).append(
                f"row {sheet_row}: {match} | {pick} — {breach}"
            )

        # A writer that returns False failed to write. Counting it as settled
        # anyway is how the tennis pipeline can announce '✅ settled' for a row
        # that is still PENDING on the sheet (tennis_excel_tracker.py:368 +
        # tennis_auto_results.py:415) — not reproduced here. None means success,
        # so the football writer is unaffected.
        if row_writer(sheet_row, result, pnl) is False:
            log.error("Row %s: result write FAILED — leaving PENDING, not reporting settled",
                      sheet_row)
            stats["errors"] += 1
            continue
        changed = True
        stats["updated"] += 1

        log.info("%s [%s→%s]  score %d-%d  @ %.2f (%s)  P&L %+.2f",
                 match, pick, result, home_score, away_score, odds,
                 "market" if p.get("market_odds") is not None else "estimate", pnl)

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
            # Routing key for result_notification(). A pending_source that
            # predates the field (the Opus shadow's, whose resolved list is
            # dropped anyway) reads as Core, which is the pre-20-Sep behaviour.
            "pick_tier":  p.get("pick_tier", PICK_TIER_CORE),
        })

    # ── 4. Recalculate running totals + refresh Summary ───────────────────────
    _alert_integrity_breaches(stats, alert_scope)

    if changed:
        finalizer()
        log.info("Google Sheets updated — %d row(s) written.", stats["updated"])
    else:
        log.info("No changes.")

    return stats, resolved


# ── Entry point ───────────────────────────────────────────────────────────────

def _alert_integrity_breaches(stats: dict, alert_scope: str) -> None:
    """
    Announce impossible payouts found while settling. Ops channel, not the
    subscriber-facing results feed: the picks are fine, the arithmetic in the
    sheet is not. One alert per run listing every breach, rather than one per
    row — the same reasoning that keeps the credit-balance alert deduped.
    """
    breaches = stats.get("integrity_breaches") or []
    if not breaches:
        return
    try:
        from usage_tracker import alert_data_integrity
        alert_data_integrity(
            f"settlement-payout ({alert_scope})",
            f"**{len(breaches)}** settled row(s) carry a P&L that cannot follow "
            f"from their result and price.",
            rows=breaches,
        )
    except Exception as exc:
        log.error("Could not raise the settlement integrity alert: %s", exc)


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
                channel_key, msg = result_notification(r)
                print(f"\nSending notification to '{channel_key}':\n{msg}")
                send_to_discord(channel_key, message=msg)
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

        # ── Per-pick Discord notifications for yesterday ─────────────────────
        yesterday = date.today() - timedelta(days=1)
        yesterday_picks = get_picks_for_date(yesterday)
        settled = [p for p in yesterday_picks if p["result"] in ("WIN", "HALF WIN", "HALF LOSS", "LOSS")]

        if not settled:
            print(f"\nNo settled picks for {yesterday} — skipping notifications.")
        else:
            print(f"\nSending {len(settled)} individual result notification(s) to Discord...")
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
                # Discord-only since 18 Aug 2026. Both of these sends were
                # Telegram-only before that — the ONLY two in the repo with no
                # Discord equivalent at all, so removing Telegram without adding
                # them here would have silently deleted the manual settlement
                # report rather than moved it.
                send_to_discord("results-cards", message=_format_result_notification(notif))

            total_str = f"+{total_pnl:.2f}" if total_pnl >= 0 else f"{total_pnl:.2f}"
            send_to_discord(
                "results-cards",
                message=(
                    f"Results {yesterday.strftime('%d %b %Y')} — "
                    f"{total_str} units P&L ({len(settled)} settled picks)"
                ),
            )
            print("Done.")

            try:
                from card_generator import generate_results_card
                card_path = generate_results_card(settled, card_date=yesterday)
                send_to_discord("results-cards", image_path=card_path)
                log.info("Results card sent: %s", card_path.name)
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
