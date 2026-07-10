"""
tennis_calibration.py — probability calibration engine for the TENNIS system.

Same logic as the football calibration.py, but reads EXCLUSIVELY from the
'Tennis Picks' tab via tennis_excel_tracker. Its Brier score, edge report,
CLV report, and 300-settled-pick meaningfulness threshold are all computed
independently of the football system — the two datasets are never merged.

Tennis data collection start: 9 Jul 2026. Do not draw conclusions from these
reports before ~300 settled tennis picks with probability data exist.

Run manually:
    python tennis_calibration.py
"""
from __future__ import annotations

import logging

from tennis_excel_tracker import _tennis_ws, TENNIS_SETTLED_RESULTS

log = logging.getLogger(__name__)

# Calibration buckets: (label, lower bound inclusive, upper bound exclusive).
_BUCKETS = [
    ("<50%",    0.0,  50.0),
    ("50-60%",  50.0, 60.0),
    ("60-70%",  60.0, 70.0),
    ("70-80%",  70.0, 80.0),
    ("80-90%",  80.0, 90.0),
    ("90-100%", 90.0, 100.000001),
]

# Independent tennis threshold — never combined with football sample counts
MIN_MEANINGFUL_SAMPLE = 300


def _safe_float(val: str) -> float | None:
    try:
        return float(val) if val not in ("", None) else None
    except (ValueError, TypeError):
        return None


def _settled_tennis_prob_rows() -> list[dict] | None:
    """
    Settled tennis picks that carry a Claude Prob value, as dicts with:
    claude_prob, market_prob (may be None), result, pnl (may be None),
    outcome (1.0 WIN / 0.0 LOSS / None VOID). None on any read failure.
    """
    try:
        rows = _tennis_ws().get_all_values()
    except Exception as exc:
        log.warning("tennis_calibration: Sheets read failed: %s", exc)
        return None
    if not rows:
        return []

    header = rows[0]
    try:
        result_col = header.index("Result")
        pnl_col    = header.index("P&L")
        cp_col     = header.index("Claude Prob %")
        mp_col     = header.index("Market Prob %")
    except ValueError:
        return []  # tab pre-dates the prob columns — nothing to evaluate yet

    out = []
    for row in rows[1:]:
        result = row[result_col].strip().upper() if len(row) > result_col else ""
        if result not in TENNIS_SETTLED_RESULTS:
            continue
        claude_prob = _safe_float(row[cp_col]) if len(row) > cp_col else None
        if claude_prob is None:
            continue
        out.append({
            "claude_prob": claude_prob,
            "market_prob": _safe_float(row[mp_col]) if len(row) > mp_col else None,
            "result":      result,
            "pnl":         _safe_float(row[pnl_col]) if len(row) > pnl_col else None,
            "outcome":     1.0 if result == "WIN" else 0.0 if result == "LOSS" else None,
        })
    return out


def tennis_calibration_report() -> dict | None:
    """
    Bucket settled tennis picks by Claude's stated probability and compare the
    average stated probability against the actual win rate per bucket, plus a
    Brier score. Only WIN/LOSS picks are scored — voids have no binary outcome.

    Returns {"buckets": [...], "brier_score": float | None,
             "sample_size": int, "meaningful": bool} or None on failure.
    """
    rows = _settled_tennis_prob_rows()
    if rows is None:
        return None

    scored = [r for r in rows if r["outcome"] is not None]

    buckets = []
    for label, lo, hi in _BUCKETS:
        in_bucket = [r for r in scored if lo <= r["claude_prob"] < hi]
        n = len(in_bucket)
        buckets.append({
            "range":           label,
            "picks":           n,
            "avg_stated":      round(sum(r["claude_prob"] for r in in_bucket) / n, 1) if n else None,
            "actual_win_rate": round(sum(r["outcome"] for r in in_bucket) / n * 100, 1) if n else None,
        })

    brier = None
    if scored:
        brier = round(
            sum((r["claude_prob"] / 100.0 - r["outcome"]) ** 2 for r in scored) / len(scored), 4
        )

    return {
        "buckets":     buckets,
        "brier_score": brier,
        "sample_size": len(scored),
        "meaningful":  len(scored) >= MIN_MEANINGFUL_SAMPLE,
    }


def tennis_edge_report() -> dict | None:
    """
    For settled tennis picks with both Claude Prob and Market Prob:
      - average edge (Claude Prob − Market Prob) for winners vs losers
      - ROI of picks where Claude's probability exceeded the market's vs not

    Returns None on failure.
    """
    rows = _settled_tennis_prob_rows()
    if rows is None:
        return None

    both = [r for r in rows if r["market_prob"] is not None]

    winners = [r for r in both if r["result"] == "WIN"]
    losers  = [r for r in both if r["result"] == "LOSS"]

    def _avg_edge(group: list[dict]) -> float | None:
        if not group:
            return None
        return round(sum(r["claude_prob"] - r["market_prob"] for r in group) / len(group), 1)

    def _roi_group(group: list[dict]) -> dict:
        staked = [r for r in group if r["pnl"] is not None]
        pnl = round(sum(r["pnl"] for r in staked), 2)
        return {
            "picks": len(staked),
            "pnl":   pnl,
            "roi":   round(pnl / len(staked) * 100, 1) if staked else None,
        }

    return {
        "avg_edge_winners": _avg_edge(winners),
        "avg_edge_losers":  _avg_edge(losers),
        "positive_edge":    _roi_group([r for r in both if r["claude_prob"] > r["market_prob"]]),
        "negative_edge":    _roi_group([r for r in both if r["claude_prob"] <= r["market_prob"]]),
        "sample_size":      len(both),
    }


_EMPTY_CLV_REPORT: dict = {
    "avg_clv": None, "pct_positive": None, "sample_size": 0, "meaningful": False,
    "positive_clv_roi": {"picks": 0, "pnl": 0.0, "roi": None},
    "negative_clv_roi": {"picks": 0, "pnl": 0.0, "roi": None},
}


def tennis_clv_report() -> dict | None:
    """
    Closing Line Value for settled tennis picks with both an original 'Odds'
    and a logged 'Closing Odds' value:

        CLV % = (original_odds / closing_odds − 1) × 100

    Requires tennis_closing_odds.py to have run near match start; picks with
    no Closing Odds value are skipped, not counted as zero. An empty tab
    returns the zeroed report (not None) so callers can distinguish "nothing
    yet" from "read failed".
    """
    try:
        rows = _tennis_ws().get_all_values()
    except Exception as exc:
        log.warning("tennis_clv_report: Sheets read failed: %s", exc)
        return None
    if not rows:
        return dict(_EMPTY_CLV_REPORT)

    header = rows[0]
    try:
        result_col  = header.index("Result")
        odds_col    = header.index("Odds")
        pnl_col     = header.index("P&L")
        closing_col = header.index("Closing Odds")
    except ValueError:
        return dict(_EMPTY_CLV_REPORT)

    scored = []
    for row in rows[1:]:
        result = row[result_col].strip().upper() if len(row) > result_col else ""
        if result not in TENNIS_SETTLED_RESULTS:
            continue
        odds    = _safe_float(row[odds_col])    if len(row) > odds_col    else None
        closing = _safe_float(row[closing_col]) if len(row) > closing_col else None
        if odds is None or closing is None or closing == 0:
            continue
        scored.append({
            "clv": (odds / closing - 1) * 100,
            "pnl": _safe_float(row[pnl_col]) if len(row) > pnl_col else None,
        })

    n = len(scored)
    if n == 0:
        return dict(_EMPTY_CLV_REPORT)

    def _roi_group(group: list[dict]) -> dict:
        staked = [r for r in group if r["pnl"] is not None]
        pnl = round(sum(r["pnl"] for r in staked), 2)
        return {
            "picks": len(staked),
            "pnl":   pnl,
            "roi":   round(pnl / len(staked) * 100, 1) if staked else None,
        }

    positive = [r for r in scored if r["clv"] > 0]
    negative = [r for r in scored if r["clv"] <= 0]

    return {
        "avg_clv":          round(sum(r["clv"] for r in scored) / n, 2),
        "pct_positive":     round(len(positive) / n * 100, 1),
        "sample_size":      n,
        "meaningful":       n >= MIN_MEANINGFUL_SAMPLE,
        "positive_clv_roi": _roi_group(positive),
        "negative_clv_roi": _roi_group(negative),
    }


if __name__ == "__main__":
    import json as _json

    from env_loader import load_env
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print("Tennis calibration report:")
    print(_json.dumps(tennis_calibration_report(), indent=2))
    print("\nTennis edge report:")
    print(_json.dumps(tennis_edge_report(), indent=2))
    print("\nTennis CLV report:")
    print(_json.dumps(tennis_clv_report(), indent=2))
