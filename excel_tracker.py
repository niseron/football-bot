"""
Google Sheets tracking layer for the football betting bot.
Drop-in replacement for the openpyxl-based version — same public API.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread

log = logging.getLogger(__name__)

# Kept so any script that imports EXCEL_PATH still compiles
EXCEL_PATH = Path(__file__).parent / "picks_tracker.xlsx"

PICKS_HEADERS = [
    "Date", "Match", "Bet Type", "Pick", "Odds",
    "Confidence", "Result", "Profit/Loss", "Running Total P&L",
]

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client: gspread.Client | None = None


# ── Connection ────────────────────────────────────────────────────────────────

def _get_client() -> gspread.Client:
    global _client
    if _client is None:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS_JSON", "{}"))
        _client = gspread.service_account_from_dict(creds_dict, scopes=_SCOPES)
    return _client


def _get_spreadsheet() -> gspread.Spreadsheet:
    return _get_client().open_by_key(os.environ.get("GOOGLE_SHEETS_ID", ""))


def _picks_ws() -> gspread.Worksheet:
    return _get_spreadsheet().worksheet("Picks")


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def init_excel() -> None:
    """Ensure Picks and Summary sheets exist with correct headers."""
    try:
        ss = _get_spreadsheet()
    except Exception as exc:
        log.error("Cannot connect to Google Sheets: %s", exc)
        return

    titles = {ws.title for ws in ss.worksheets()}

    if "Picks" not in titles:
        ws = ss.add_worksheet("Picks", rows=1000, cols=len(PICKS_HEADERS))
        ws.append_row(PICKS_HEADERS, value_input_option="RAW")
        log.info("Created 'Picks' sheet with headers")

    if "Summary" not in titles:
        ss.add_worksheet("Summary", rows=30, cols=2)
        log.info("Created 'Summary' sheet")

    # Remove the default blank sheet if it's still there
    for ws in ss.worksheets():
        if ws.title == "Sheet1" and len(ss.worksheets()) > 2:
            try:
                ss.del_worksheet(ws)
            except Exception:
                pass


# ── Write a new pick ──────────────────────────────────────────────────────────

def log_to_excel(
    match: str,
    league: str,
    bet_type: str,
    pick: str,
    odds: float,
    confidence: str,
    pick_date: str | None = None,
) -> None:
    dt = datetime.fromisoformat(pick_date) if pick_date else datetime.now()
    date_str = dt.strftime("%d-%b-%Y")
    target_date = dt.date()

    try:
        ws = _picks_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
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
            log.info("Sheets: skipping duplicate '%s — %s'", match, pick)
            return

    new_row = [date_str, match, bet_type, pick, round(float(odds), 2), confidence, "", "", ""]
    try:
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        log.info("Sheets: logged '%s — %s'", match, pick)
    except Exception as exc:
        log.error("Sheets write failed: %s", exc)


# ── Pending rows (used by auto_results) ──────────────────────────────────────

def get_pending_picks_rows(lookback_days: int = 7) -> list[dict]:
    """Return rows with no Result within the lookback window, each with its 1-based sheet row."""
    try:
        ws = _picks_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return []

    cutoff = date.today() - timedelta(days=lookback_days)
    pending = []

    for i, row in enumerate(rows[1:], start=2):  # row 1 = header; Sheets rows are 1-based
        if not row or not row[0]:
            continue
        if len(row) >= 7 and row[6]:  # Result column already set
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


# ── Write result for one row ──────────────────────────────────────────────────

def update_row_result(sheet_row: int, result: str, pnl: float) -> None:
    """Write Result and Profit/Loss to a specific sheet row. Call finalize_workbook() when done."""
    try:
        ws = _picks_ws()
        ws.batch_update([
            {"range": f"G{sheet_row}", "values": [[result]]},
            {"range": f"H{sheet_row}", "values": [[round(pnl, 2)]]},
        ])
    except Exception as exc:
        log.error("Sheets update_row_result failed: %s", exc)


# ── Recalculate running totals + Summary ──────────────────────────────────────

def _recalculate_running_total(ws: gspread.Worksheet) -> None:
    rows = ws.get_all_values()
    running = 0.0
    updates = []
    for i, row in enumerate(rows[1:], start=2):
        result  = row[6] if len(row) > 6 else ""
        pnl_str = row[7] if len(row) > 7 else ""
        if result in ("WIN", "LOSS", "VOID") and pnl_str:
            try:
                running += float(pnl_str)
                updates.append({"range": f"I{i}", "values": [[round(running, 2)]]})
                continue
            except ValueError:
                pass
        updates.append({"range": f"I{i}", "values": [[""]]})
    if updates:
        ws.batch_update(updates)


def _refresh_summary(ss: gspread.Spreadsheet) -> None:
    rows = ss.worksheet("Picks").get_all_values()[1:]

    def _pnl(row: list) -> float:
        try:
            return float(row[7]) if len(row) > 7 and row[7] else 0.0
        except ValueError:
            return 0.0

    settled = [r for r in rows if len(r) > 6 and r[6] in ("WIN", "LOSS", "VOID")]
    wins    = [r for r in settled if r[6] == "WIN"]
    losses  = [r for r in settled if r[6] == "LOSS"]
    voids   = [r for r in settled if r[6] == "VOID"]
    pending = [r for r in rows if not (len(r) > 6 and r[6])]

    total_pnl = round(sum(_pnl(r) for r in settled), 2)
    win_rate  = round(len(wins) / len(settled) * 100, 1) if settled else 0.0

    bt_pnl: dict[str, float] = defaultdict(float)
    for r in settled:
        bt_pnl[r[2]] += _pnl(r)
    best_bt     = max(bt_pnl, key=bt_pnl.get) if bt_pnl else "N/A"
    best_bt_pnl = round(bt_pnl.get(best_bt, 0), 2)

    conf_pnl: dict[str, float] = defaultdict(float)
    for r in settled:
        conf_pnl[r[5]] += _pnl(r)
    best_conf     = max(conf_pnl, key=conf_pnl.get) if conf_pnl else "N/A"
    best_conf_pnl = round(conf_pnl.get(best_conf, 0), 2)

    data = [
        ["PERFORMANCE SUMMARY", ""],
        ["", ""],
        ["Total picks",           len(rows)],
        ["  Wins",                len(wins)],
        ["  Losses",              len(losses)],
        ["  Voids",               len(voids)],
        ["  Pending",             len(pending)],
        ["", ""],
        ["Win rate",              f"{win_rate}%"],
        ["Total P&L (units)",     total_pnl],
        ["", ""],
        ["Best bet type",         best_bt],
        ["  P&L from this type",  best_bt_pnl],
        ["", ""],
        ["Best confidence level",   best_conf],
        ["  P&L from this level",   best_conf_pnl],
    ]

    ws_sum = ss.worksheet("Summary")
    ws_sum.clear()
    ws_sum.update("A1", data, value_input_option="USER_ENTERED")


def finalize_workbook(wb=None) -> None:
    """Recalculate running totals and refresh Summary. wb param accepted for backwards compat."""
    try:
        ss = _get_spreadsheet()
        _recalculate_running_total(ss.worksheet("Picks"))
        _refresh_summary(ss)
    except Exception as exc:
        log.error("finalize_workbook failed: %s", exc)


# ── Manual result update ──────────────────────────────────────────────────────

def update_result(match_query: str, pick_query: str, result: str) -> bool:
    result = result.upper()
    if result not in ("WIN", "LOSS", "VOID"):
        raise ValueError(f"Result must be WIN, LOSS or VOID — got '{result}'")

    try:
        ws = _picks_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return False

    mq = match_query.lower().strip()
    pq = pick_query.lower().strip()

    target_row = None
    for i in range(len(rows) - 1, 0, -1):  # bottom-up → most recent first
        row    = rows[i]
        m_val  = (row[1] if len(row) > 1 else "").lower()
        bt_val = (row[2] if len(row) > 2 else "").lower()
        p_val  = (row[3] if len(row) > 3 else "").lower()
        res    =  row[6] if len(row) > 6 else ""

        match_ok = mq in m_val or m_val in mq
        pick_ok  = (pq in p_val or p_val in pq or
                    pq in bt_val or bt_val in pq or
                    pq in f"{bt_val} {p_val}")

        if match_ok and pick_ok and not res:
            target_row = i + 1  # convert to 1-based sheet row
            break

    if target_row is None:
        print(f"No pending pick found matching '{match_query}' / '{pick_query}'")
        return False

    try:
        odds = float(rows[target_row - 1][4]) if len(rows[target_row - 1]) > 4 and rows[target_row - 1][4] else 1.0
    except ValueError:
        odds = 1.0
    pnl = round(odds - 1, 2) if result == "WIN" else (-1.0 if result == "LOSS" else 0.0)

    update_row_result(target_row, result, pnl)
    finalize_workbook()

    match_name = rows[target_row - 1][1] if len(rows[target_row - 1]) > 1 else "?"
    sign = "+" if pnl >= 0 else ""
    print(f"Updated  : {match_name}")
    print(f"Result   : {result}")
    print(f"P&L      : {sign}{pnl:.2f} units  (odds {odds})")
    return True


# ── Weekly data ───────────────────────────────────────────────────────────────

def get_weekly_data() -> dict:
    try:
        ws = _picks_ws()
        rows = ws.get_all_values()[1:]
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return {}

    today    = date.today()
    week_mon = today - timedelta(days=today.weekday())
    week_sun = week_mon + timedelta(days=6)

    all_rows:  list[dict] = []
    week_rows: list[dict] = []

    for row in rows:
        if not row or not row[0]:
            continue
        try:
            dt = datetime.strptime(row[0], "%d-%b-%Y").date()
        except ValueError:
            continue

        def _safe_float(val: str) -> float | None:
            try:
                return float(val) if val else None
            except ValueError:
                return None

        r = {
            "date":       dt,
            "match":      row[1] if len(row) > 1 else "",
            "bet_type":   row[2] if len(row) > 2 else "",
            "pick":       row[3] if len(row) > 3 else "",
            "odds":       _safe_float(row[4] if len(row) > 4 else "") or 0.0,
            "confidence": row[5] if len(row) > 5 else "",
            "result":     row[6] if len(row) > 6 else "",
            "pnl":        _safe_float(row[7] if len(row) > 7 else ""),
            "running":    _safe_float(row[8] if len(row) > 8 else ""),
        }
        all_rows.append(r)
        if week_mon <= dt <= week_sun:
            week_rows.append(r)

    settled  = [r for r in week_rows if r["result"] in ("WIN", "LOSS", "VOID")]
    wins     = [r for r in settled if r["result"] == "WIN"]
    losses   = [r for r in settled if r["result"] == "LOSS"]
    pending  = [r for r in week_rows if not r["result"]]
    pnl_week = round(sum(r["pnl"] or 0 for r in settled if r["pnl"] is not None), 2)
    win_rate = round(len(wins) / len(settled) * 100, 1) if settled else 0.0
    best_pick = max(wins, key=lambda r: r["pnl"] or 0) if wins else None
    running_total = next(
        (r["running"] for r in reversed(all_rows) if r.get("running") is not None), 0.0
    )

    return {
        "week_start":    week_mon.strftime("%d %b"),
        "week_end":      week_sun.strftime("%d %b %Y"),
        "total_picks":   len(week_rows),
        "wins":          len(wins),
        "losses":        len(losses),
        "pending":       len(pending),
        "win_rate":      win_rate,
        "pnl_week":      pnl_week,
        "best_pick":     best_pick,
        "running_total": running_total or 0.0,
        "pending_picks": pending,
    }


# ── Deduplicate ───────────────────────────────────────────────────────────────

def cleanup_duplicates() -> int:
    try:
        ws = _picks_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return 0

    seen: set[tuple] = set()
    to_delete: list[int] = []

    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        key = (
            row[0],
            row[1] if len(row) > 1 else "",
            row[2] if len(row) > 2 else "",
            row[3] if len(row) > 3 else "",
        )
        if key in seen:
            to_delete.append(i)
        else:
            seen.add(key)

    for row_idx in reversed(to_delete):
        ws.delete_rows(row_idx)

    if to_delete:
        finalize_workbook()
        log.info("Removed %d duplicate row(s)", len(to_delete))
    else:
        log.info("No duplicates found")

    return len(to_delete)


# ── Compatibility stub ────────────────────────────────────────────────────────

def _style_picks_row(ws, row: int, result: str | None) -> None:
    """No-op: row colouring is handled via Google Sheets conditional formatting, not Python."""
    pass
