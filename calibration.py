"""
calibration.py — probability calibration engine.

Measures how well Claude's stated probabilities ('Claude Prob %' column) match
reality, and whether the Claude-vs-market edge actually predicts profit.

Picks logged before the probability columns existed have no data and are
skipped — the engine only evaluates picks logged from the day the columns
were introduced onward. Both report functions return None on any failure so
callers can fall back to existing behaviour silently.

Run manually:
    python calibration.py
"""
from __future__ import annotations

import logging

from excel_tracker import _picks_ws, PICKS_HEADERS

log = logging.getLogger(__name__)

# Calibration buckets: (label, lower bound inclusive, upper bound exclusive).
# The last bucket includes 100 exactly. Picks below 50% land in "<50%" so no
# probability data is silently dropped.
_BUCKETS = [
    ("<50%",    0.0,  50.0),
    ("50-60%",  50.0, 60.0),
    ("60-70%",  60.0, 70.0),
    ("70-80%",  70.0, 80.0),
    ("80-90%",  80.0, 90.0),
    ("90-100%", 90.0, 100.000001),
]

# Sample sizes below this are too small to draw calibration conclusions from
MIN_MEANINGFUL_SAMPLE = 300


def _safe_float(val: str) -> float | None:
    try:
        return float(val) if val not in ("", None) else None
    except (ValueError, TypeError):
        return None


def _settled_prob_rows() -> list[dict] | None:
    """
    Return settled picks that carry a Claude Prob value, as dicts with:
    claude_prob, market_prob (may be None), result, pnl (may be None),
    outcome (1.0 WIN / 0.0 LOSS / None for HALF WIN, HALF LOSS, VOID).
    None on any read failure.
    """
    try:
        rows = _picks_ws().get_all_values()
    except Exception as exc:
        log.warning("calibration: Sheets read failed: %s", exc)
        return None
    if not rows:
        return []

    # Locate columns by header name so the report survives future column moves
    header = rows[0]
    try:
        result_col = header.index("Result")
        pnl_col    = header.index("Profit/Loss")
        cp_col     = header.index("Claude Prob %")
        mp_col     = header.index("Market Prob %")
    except ValueError:
        # Sheet pre-dates the prob columns entirely — nothing to evaluate yet
        return []

    out = []
    for row in rows[1:]:
        result = row[result_col].strip().upper() if len(row) > result_col else ""
        if result not in ("WIN", "HALF WIN", "HALF LOSS", "LOSS", "VOID"):
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


def calibration_report() -> dict | None:
    """
    Bucket settled picks by Claude's stated probability and compare the
    average stated probability against the actual win rate per bucket.
    A well-calibrated bot has actual win rate ≈ stated probability.

    Only WIN/LOSS picks enter the buckets and the Brier score — half results
    and voids have no binary outcome to score against.

    Returns {"buckets": [...], "brier_score": float | None,
             "sample_size": int, "meaningful": bool} or None on failure.
    """
    rows = _settled_prob_rows()
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


def edge_report() -> dict | None:
    """
    For settled picks with both Claude Prob and Market Prob:
      - average edge (Claude Prob − Market Prob) for winning vs losing picks
      - ROI of picks where Claude's probability exceeded the market's vs not
        (ROI = total P&L units / picks staked × 100, 1 unit per pick;
        includes half results and voids since they carry real P&L)

    Returns None on failure.
    """
    rows = _settled_prob_rows()
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


def clv_report() -> dict | None:
    """
    Closing Line Value (CLV) — the gold-standard measure of genuine edge,
    independent of whether a pick actually won. For every settled pick with
    both an original 'Odds' value and a logged 'Closing Odds' value:

        CLV % = (original_odds / closing_odds − 1) × 100

    Positive CLV means the price shortened after the pick was made (the
    market moved toward our side) — a real signal of edge that a single
    win/loss can't fake. Requires closing_odds.py to have run near kickoff;
    picks with no Closing Odds value yet are skipped, not counted as zero.

    Returns {"avg_clv", "pct_positive", "sample_size", "meaningful",
             "positive_clv_roi", "negative_clv_roi"} or None on read failure.
    An empty/pre-migration sheet returns the zeroed report above, not None,
    so callers can distinguish "nothing to report yet" from "read failed".
    """
    try:
        rows = _picks_ws().get_all_values()
    except Exception as exc:
        log.warning("clv_report: Sheets read failed: %s", exc)
        return None
    if not rows:
        return dict(_EMPTY_CLV_REPORT)

    header = rows[0]
    try:
        result_col  = header.index("Result")
        odds_col    = header.index("Odds")
        pnl_col     = header.index("Profit/Loss")
        closing_col = header.index("Closing Odds")
    except ValueError:
        # Sheet pre-dates the Closing Odds column — nothing to evaluate yet
        return dict(_EMPTY_CLV_REPORT)

    scored = []
    for row in rows[1:]:
        result = row[result_col].strip().upper() if len(row) > result_col else ""
        if result not in ("WIN", "HALF WIN", "HALF LOSS", "LOSS", "VOID"):
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

    print("Calibration report:")
    print(_json.dumps(calibration_report(), indent=2))
    print("\nEdge report:")
    print(_json.dumps(edge_report(), indent=2))
    print("\nCLV report:")
    print(_json.dumps(clv_report(), indent=2))
