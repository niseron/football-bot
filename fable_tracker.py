"""
Google Sheets tracking layer for the FABLE 5 SHADOW EXPERIMENT (football).

Fable 5 (claude-fable-5) runs as a side-by-side comparison against the
production Sonnet 4.6 pipeline: same daily fixture pool, its own independent
picks, its own 'Fable Picks' tab, its own calibration numbers. It is NOT a
replacement for Sonnet and never touches the production 'Picks' tab.

The tab mirrors the football Picks tab structure (minus the staking columns —
the experiment tracks units P&L only, there is no Fable bankroll). 'League'
is included because the closing-odds job batches its Odds API calls per
league, and CLV needs closing odds.

Row dicts returned by the readers here are shape-compatible with
excel_tracker's get_pending_picks_rows / get_unsettled_picks_with_kickoff so
auto_results.run_auto_results and closing_odds.run_closing_odds_check can run
against this tab through their source/writer hooks.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import gspread

from excel_tracker import _get_spreadsheet

log = logging.getLogger(__name__)

FABLE_SHEET_NAME = "Fable Picks"

FABLE_HEADERS = [
    "Date", "Match", "Bet Type", "Pick", "Odds",
    "Confidence", "Result", "Profit/Loss",
    "Claude Prob %", "Market Prob %",
    "League", "Kickoff UTC", "Closing Odds",
]

_SETTLED_RESULTS = ("WIN", "HALF WIN", "HALF LOSS", "LOSS", "VOID")


def _fable_ws() -> gspread.Worksheet:
    """Return the Fable Picks worksheet, creating it (with headers) if missing."""
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(FABLE_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(FABLE_SHEET_NAME, rows=1000, cols=len(FABLE_HEADERS))
        ws.append_row(FABLE_HEADERS, value_input_option="RAW")
        log.info("Created '%s' sheet with headers", FABLE_SHEET_NAME)
        return ws


def init_fable_sheet() -> None:
    """Ensure the Fable Picks tab exists with the full current header row."""
    try:
        ws = _fable_ws()
    except Exception as exc:
        log.error("Cannot connect to Google Sheets (fable): %s", exc)
        return
    try:
        header = ws.row_values(1)
        if len(header) < len(FABLE_HEADERS):
            if ws.col_count < len(FABLE_HEADERS):
                ws.resize(cols=len(FABLE_HEADERS))
            for idx in range(len(header), len(FABLE_HEADERS)):
                ws.update_cell(1, idx + 1, FABLE_HEADERS[idx])
            log.info("Added missing column header(s) to '%s': %s",
                     FABLE_SHEET_NAME, FABLE_HEADERS[len(header):])
    except Exception as exc:
        log.warning("Fable header migration failed (non-fatal): %s", exc)


# ── Write a new pick ──────────────────────────────────────────────────────────

def log_fable_pick(
    match: str,
    league: str,
    bet_type: str,
    pick: str,
    odds: float,
    confidence: str,
    claude_prob: float | None = None,
    market_prob: float | None = None,
    kickoff_utc: str | None = None,
) -> None:
    date_str    = datetime.now().strftime("%d-%b-%Y")
    target_date = date.today()

    init_fable_sheet()
    try:
        ws = _fable_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Fable Sheets read failed: %s", exc)
        return

    for row in rows[1:]:
        if not row or not row[0]:
            continue
        try:
            existing = datetime.strptime(row[0], "%d-%b-%Y").date()
        except ValueError:
            continue
        if (
            existing == target_date
            and len(row) > 3
            and row[1] == match
            and row[2] == bet_type
            and row[3] == pick
        ):
            log.info("Fable Sheets: skipping duplicate '%s — %s'", match, pick)
            return

    new_row = [
        date_str, match, bet_type, pick, round(float(odds), 2), confidence, "", "",
        round(float(claude_prob), 1) if claude_prob is not None else "",
        round(float(market_prob), 1) if market_prob is not None else "",
        league or "",
        kickoff_utc or "",
        "",  # Closing Odds — populated near kickoff by the closing-odds job
    ]
    try:
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        log.info("Fable Sheets: logged '%s — %s'", match, pick)
    except Exception as exc:
        log.error("Fable Sheets write failed: %s", exc)


# ── Readers (shape-compatible with excel_tracker's) ───────────────────────────

def get_pending_fable_rows(lookback_days: int = 7) -> list[dict]:
    """Pending Fable rows in the same dict shape as get_pending_picks_rows."""
    try:
        rows = _fable_ws().get_all_values()
    except Exception as exc:
        log.error("Fable Sheets read failed: %s", exc)
        return []

    cutoff = date.today() - timedelta(days=lookback_days)
    result_col = FABLE_HEADERS.index("Result")
    pending = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        if len(row) > result_col and row[result_col]:
            continue
        try:
            pick_date = datetime.strptime(row[0], "%d-%b-%Y").date()
        except ValueError:
            continue
        if pick_date < cutoff:
            continue
        pending.append({
            "sheet_row": i,
            "date":      pick_date,
            "match":     row[1] if len(row) > 1 else "",
            "bet_type":  row[2] if len(row) > 2 else "",
            "pick":      row[3] if len(row) > 3 else "",
            "odds":      float(row[4]) if len(row) > 4 and row[4] else 1.0,
        })
    return pending


def get_unsettled_fable_with_kickoff() -> list[dict]:
    """Unsettled Fable rows with a kickoff, same shape as football's reader."""
    try:
        rows = _fable_ws().get_all_values()
    except Exception as exc:
        log.error("Fable Sheets read failed: %s", exc)
        return []
    if not rows:
        return []

    result_col  = FABLE_HEADERS.index("Result")
    league_col  = FABLE_HEADERS.index("League")
    kickoff_col = FABLE_HEADERS.index("Kickoff UTC")

    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        if len(row) > result_col and row[result_col]:
            continue
        kickoff_utc = row[kickoff_col] if len(row) > kickoff_col else ""
        if not kickoff_utc:
            continue
        out.append({
            "sheet_row":   i,
            "match":       row[1] if len(row) > 1 else "",
            "bet_type":    row[2] if len(row) > 2 else "",
            "pick":        row[3] if len(row) > 3 else "",
            "league":      row[league_col] if len(row) > league_col else "",
            "kickoff_utc": kickoff_utc,
        })
    return out


# ── Writers ───────────────────────────────────────────────────────────────────

def update_fable_row_result(sheet_row: int, result: str, pnl: float) -> None:
    """Write Result (G) and Profit/Loss (H) to a specific Fable Picks row."""
    try:
        ws = _fable_ws()
        ws.batch_update([
            {"range": f"G{sheet_row}", "values": [[result]]},
            {"range": f"H{sheet_row}", "values": [[round(pnl, 2)]]},
        ])
    except Exception as exc:
        log.error("Fable Sheets update_fable_row_result failed: %s", exc)


def update_fable_closing_odds(sheet_row: int, closing_odds: float) -> None:
    """Write the Closing Odds cell for one Fable row (last write pre-kickoff wins)."""
    try:
        ws = _fable_ws()
        col = FABLE_HEADERS.index("Closing Odds") + 1
        ws.update_cell(sheet_row, col, round(float(closing_odds), 2))
    except Exception as exc:
        log.error("Fable Sheets update_fable_closing_odds failed (row %d): %s", sheet_row, exc)


if __name__ == "__main__":
    from env_loader import load_env
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_fable_sheet()
    print(f"'{FABLE_SHEET_NAME}' tab ready.")
