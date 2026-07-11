"""
fable_calibration.py — calibration engine for the FABLE 5 SHADOW EXPERIMENT.

Thin wrapper over calibration.py's report functions, pointed at the
'Fable Picks' tab instead of the production 'Picks' tab, so Fable 5 gets its
own Brier score, calibration buckets, edge and CLV numbers — completely
separate from Sonnet 4.6's. The Fable tab uses the same header names
("Claude Prob %", "Market Prob %", "Result", "Profit/Loss", "Odds",
"Closing Odds"), so the header-located column logic works unchanged.

Independent calibration timeline: starts 12 Jul 2026 (the experiment's first
day) — sample sizes are judged on the same MIN_MEANINGFUL_SAMPLE bar as
football's, counted from zero.

Run manually:
    python fable_calibration.py
"""
from __future__ import annotations

import logging

from calibration import calibration_report, clv_report, edge_report
from fable_tracker import _fable_ws

log = logging.getLogger(__name__)


def fable_calibration_report() -> dict | None:
    """Brier score + probability buckets for Fable 5's settled picks."""
    return calibration_report(ws_getter=_fable_ws)


def fable_edge_report() -> dict | None:
    """Claude-vs-market edge analysis for Fable 5's settled picks."""
    return edge_report(ws_getter=_fable_ws)


def fable_clv_report() -> dict | None:
    """Closing Line Value for Fable 5's settled picks."""
    return clv_report(ws_getter=_fable_ws)


if __name__ == "__main__":
    import json as _json

    from env_loader import load_env
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    print("Fable 5 calibration report:")
    print(_json.dumps(fable_calibration_report(), indent=2))
    print("\nFable 5 edge report:")
    print(_json.dumps(fable_edge_report(), indent=2))
    print("\nFable 5 CLV report:")
    print(_json.dumps(fable_clv_report(), indent=2))
