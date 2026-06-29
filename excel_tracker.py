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
    "Confidence", "Result", "Profit/Loss", "Running Total P&L", "Bankroll (€)",
]

STARTING_BANKROLL = 100.0    # € tracked bankroll (used for running P&L in the sheet)
UNIT_STAKE        = 10.0     # € per pick (1 unit)
REAL_BANKROLL     = 1500.0   # € actual bankroll on the betting site (used for Kelly sizing)

# Dates excluded from win-rate calculations (backfill / bad-data days)
_WIN_RATE_EXCLUDE: frozenset[date] = frozenset([date(2026, 6, 15)])

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


# ── Formatting helpers ────────────────────────────────────────────────────────

def _rgb(hex_str: str) -> dict:
    h = hex_str.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


_WHITE            = _rgb("#ffffff")
_LIGHT_GREEN      = _rgb("#e8f5e9")
_LIGHT_RED        = _rgb("#ffebee")
_DARK_GREEN       = _rgb("#1a5c38")
_WIN_GREEN        = _rgb("#00c853")
_LOSS_RED         = _rgb("#d50000")
_HALF_WIN_AMBER   = _rgb("#ffab00")   # half win — amber/gold
_HALF_LOSS_ORANGE = _rgb("#ff6d00")   # half loss — deep orange
_WHITE_TEXT  = {"red": 1.0, "green": 1.0, "blue": 1.0}
_BLACK_TEXT  = {"red": 0.0, "green": 0.0, "blue": 0.0}

# All result values that count as settled (not pending)
_SETTLED_RESULTS: frozenset[str] = frozenset(["WIN", "HALF WIN", "HALF LOSS", "LOSS", "VOID"])


def _apply_formatting(ss: gspread.Spreadsheet) -> None:
    try:
        ws       = ss.worksheet("Picks")
        sid      = ws.id
        all_rows = ws.get_all_values()
        nrows    = len(all_rows)
        ncols    = len(PICKS_HEADERS)
        result_col = PICKS_HEADERS.index("Result")

        if nrows < 1:
            return

        reqs: list[dict] = []

        # Freeze header row
        reqs.append({
            "updateSheetProperties": {
                "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        })

        # Header: bold, dark green background, white text
        reqs.append({
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                           "startColumnIndex": 0, "endColumnIndex": ncols},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _DARK_GREEN,
                    "textFormat": {"bold": True, "foregroundColor": _WHITE_TEXT},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

        # Data rows: alternating white / light green + WIN/LOSS result cell override
        wins_found = losses_found = skipped_short = 0
        for i in range(1, nrows):
            row_bg = _WHITE if i % 2 == 1 else _LIGHT_GREEN
            reqs.append({
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1,
                               "startColumnIndex": 0, "endColumnIndex": ncols},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": row_bg,
                        "textFormat": {"bold": False, "foregroundColor": _BLACK_TEXT},
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            })
            if len(all_rows[i]) <= result_col:
                skipped_short += 1
                result_val = ""
            else:
                result_val = all_rows[i][result_col]
            _RESULT_COLORS = {
                "WIN":       _WIN_GREEN,
                "HALF WIN":  _HALF_WIN_AMBER,
                "HALF LOSS": _HALF_LOSS_ORANGE,
                "LOSS":      _LOSS_RED,
            }
            if result_val in _RESULT_COLORS:
                if result_val == "WIN":
                    wins_found += 1
                elif result_val == "LOSS":
                    losses_found += 1
                reqs.append({
                    "repeatCell": {
                        "range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1,
                                   "startColumnIndex": result_col, "endColumnIndex": result_col + 1},
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": _RESULT_COLORS[result_val],
                            "textFormat": {"bold": True, "foregroundColor": _WHITE_TEXT},
                        }},
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                })
            elif result_val.strip() not in ("", "VOID"):
                print(f"[_apply_formatting] row {i+1}: unexpected result value {result_val!r} (len={len(all_rows[i])})")

        print(f"[_apply_formatting] repainting {nrows - 1} data rows — WIN={wins_found}, LOSS={losses_found}, skipped_short={skipped_short}")

        # Thick border around the full data range
        thick = {"style": "SOLID_THICK", "colorStyle": {"rgbColor": _BLACK_TEXT}}
        reqs.append({
            "updateBorders": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": nrows,
                           "startColumnIndex": 0, "endColumnIndex": ncols},
                "top": thick, "bottom": thick, "left": thick, "right": thick,
            }
        })

        # Auto-resize all columns to fit content
        reqs.append({
            "autoResizeDimensions": {
                "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                                "startIndex": 0, "endIndex": ncols},
            }
        })

        ss.batch_update({"requests": reqs})
        log.info("Formatting applied to Picks sheet (%d rows)", nrows)

    except Exception as exc:
        log.warning("Sheet formatting failed (non-fatal): %s", exc)


def _format_result_row(
    ss: gspread.Spreadsheet,
    ws_id: int,
    sheet_row: int,
    result: str,
    pnl: float,
    bankroll: float | None = None,
) -> None:
    """batchUpdate formatting for G (Result), H (Profit/Loss), and optionally J (Bankroll)."""
    print(f"[_format_result_row] called — sheet_row={sheet_row}, row_idx={sheet_row - 1}, result={result!r}, pnl={pnl}")
    row_idx = sheet_row - 1  # Sheets API uses 0-based row indices
    reqs: list[dict] = []

    # G: Result cell — coloured by outcome
    _RESULT_BG = {
        "WIN":       _WIN_GREEN,
        "HALF WIN":  _HALF_WIN_AMBER,
        "HALF LOSS": _HALF_LOSS_ORANGE,
        "LOSS":      _LOSS_RED,
    }
    if result in _RESULT_BG:
        reqs.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws_id,
                    "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                    "startColumnIndex": 6, "endColumnIndex": 7,
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _RESULT_BG[result],
                    "textFormat": {"bold": True, "foregroundColor": _WHITE_TEXT},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

    # H: Profit/Loss cell — green background if positive, red if negative
    if pnl > 0:
        h_bg = _WIN_GREEN
    elif pnl < 0:
        h_bg = _LOSS_RED
    else:
        h_bg = None
    if h_bg is not None:
        reqs.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws_id,
                    "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                    "startColumnIndex": 7, "endColumnIndex": 8,
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": h_bg,
                    "textFormat": {"foregroundColor": _WHITE_TEXT},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat)",
            }
        })

    # J: Bankroll cell — light green if above 100, light red if below
    if bankroll is not None:
        j_bg = _LIGHT_GREEN if bankroll >= 100 else _LIGHT_RED
        reqs.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws_id,
                    "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                    "startColumnIndex": 9, "endColumnIndex": 10,
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": j_bg,
                }},
                "fields": "userEnteredFormat(backgroundColor)",
            }
        })

    if reqs:
        try:
            ss.batch_update({"requests": reqs})
        except Exception as exc:
            log.error("Row %d formatting batchUpdate failed — %r", sheet_row, exc, exc_info=True)


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
    else:
        # Add Bankroll column header if the sheet pre-dates this feature
        ws = ss.worksheet("Picks")
        header = ws.row_values(1)
        if len(header) < len(PICKS_HEADERS):
            ws.resize(cols=len(PICKS_HEADERS))
            ws.update_cell(1, len(PICKS_HEADERS), PICKS_HEADERS[-1])
            log.info("Added 'Bankroll (€)' column header to existing Picks sheet")

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

    new_row = [date_str, match, bet_type, pick, round(float(odds), 2), confidence, "", "", "", ""]
    try:
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        log.info("Sheets: logged '%s — %s'", match, pick)
        _apply_formatting(_get_spreadsheet())
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
        ss = _get_spreadsheet()
        ws = ss.worksheet("Picks")
        ws.batch_update([
            {"range": f"G{sheet_row}", "values": [[result]]},
            {"range": f"H{sheet_row}", "values": [[round(pnl, 2)]]},
        ])
        _format_result_row(ss, ws.id, sheet_row, result, pnl)
    except Exception as exc:
        log.error("Sheets update_row_result failed: %s", exc)


# ── Recalculate running totals + Summary ──────────────────────────────────────

def _recalculate_running_total(ws: gspread.Worksheet) -> None:
    rows = ws.get_all_values()
    running_units = 0.0
    bankroll      = STARTING_BANKROLL
    updates  = []
    fmt_reqs = []
    ws_id    = ws.id
    for i, row in enumerate(rows[1:], start=2):
        result  = row[6] if len(row) > 6 else ""
        pnl_str = row[7] if len(row) > 7 else ""
        row_idx = i - 1  # 0-based for Sheets API
        if result in _SETTLED_RESULTS and pnl_str:
            try:
                pnl_units      = float(pnl_str)
                running_units += pnl_units
                bankroll      += pnl_units * UNIT_STAKE
                updates.append({"range": f"I{i}", "values": [[round(running_units, 2)]]})
                updates.append({"range": f"J{i}", "values": [[round(bankroll, 2)]]})
                fmt_reqs.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": ws_id,
                            "startRowIndex": row_idx, "endRowIndex": row_idx + 1,
                            "startColumnIndex": 9, "endColumnIndex": 10,
                        },
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": _LIGHT_GREEN if bankroll >= 100 else _LIGHT_RED,
                        }},
                        "fields": "userEnteredFormat(backgroundColor)",
                    }
                })
                continue
            except ValueError:
                pass
        updates.append({"range": f"I{i}", "values": [[""]]})
        updates.append({"range": f"J{i}", "values": [[""]]})
    if updates:
        ws.batch_update(updates)
    if fmt_reqs:
        try:
            ws.spreadsheet.batch_update({"requests": fmt_reqs})
        except Exception as exc:
            log.warning("Bankroll formatting failed (non-fatal): %s", exc)


def _refresh_summary(ss: gspread.Spreadsheet) -> None:
    rows = ss.worksheet("Picks").get_all_values()[1:]

    def _pnl(row: list) -> float:
        try:
            return float(row[7]) if len(row) > 7 and row[7] else 0.0
        except ValueError:
            return 0.0

    settled     = [r for r in rows if len(r) > 6 and r[6] in _SETTLED_RESULTS]
    wins        = [r for r in settled if r[6] == "WIN"]
    half_wins   = [r for r in settled if r[6] == "HALF WIN"]
    half_losses = [r for r in settled if r[6] == "HALF LOSS"]
    losses      = [r for r in settled if r[6] == "LOSS"]
    voids       = [r for r in settled if r[6] == "VOID"]
    pending     = [r for r in rows if not (len(r) > 6 and r[6])]

    total_pnl_units  = round(sum(_pnl(r) for r in settled), 2)
    total_pnl_euros  = round(total_pnl_units * UNIT_STAKE, 2)
    full_wr_settled  = [r for r in settled if r[6] in ("WIN", "LOSS")]
    win_rate         = round(len(wins) / len(full_wr_settled) * 100, 1) if full_wr_settled else 0.0

    # Current bankroll = last non-empty J column value
    current_bankroll = STARTING_BANKROLL
    for r in reversed(rows):
        if len(r) > 9 and r[9]:
            try:
                current_bankroll = float(r[9])
                break
            except ValueError:
                pass
    roi = round((current_bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100, 1)

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

    pnl_str = f"+EUR {total_pnl_euros:.2f}" if total_pnl_euros >= 0 else f"-EUR {abs(total_pnl_euros):.2f}"
    roi_str = f"+{roi:.1f}%" if roi >= 0 else f"{roi:.1f}%"

    # ── Bet type breakdown (reuse settled rows already in memory) ────────────
    bt_groups: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for r in settled:
        bt = r[2].strip() if len(r) > 2 else ""
        if not bt or r[6] == "VOID":
            continue
        bt_groups[bt]["pnl"] += _pnl(r)
        if r[6] == "WIN":
            bt_groups[bt]["wins"] += 1
        elif r[6] == "LOSS":
            bt_groups[bt]["losses"] += 1

    bt_rows = []
    for bt, g in bt_groups.items():
        total    = g["wins"] + g["losses"]
        win_rate_bt = round(g["wins"] / total * 100, 1) if total else 0.0
        bt_rows.append([bt, g["wins"], g["losses"], f"{win_rate_bt:.1f}%", round(g["pnl"], 2), total])
    bt_rows.sort(key=lambda x: float(x[3].rstrip("%")), reverse=True)

    data = [
        ["PERFORMANCE SUMMARY", ""],
        ["", ""],
        ["Total picks",           len(rows)],
        ["  Wins",                len(wins)],
        ["  Half Wins",           len(half_wins)],
        ["  Half Losses",         len(half_losses)],
        ["  Losses",              len(losses)],
        ["  Voids",               len(voids)],
        ["  Pending",             len(pending)],
        ["", ""],
        ["Win rate",              f"{win_rate:.1f}%"],
        ["Total P&L (units)",     total_pnl_units],
        ["", ""],
        ["BANKROLL", ""],
        ["  Starting bankroll",   f"EUR {STARTING_BANKROLL:.2f}"],
        ["  Current bankroll",    f"EUR {current_bankroll:.2f}"],
        ["  Total profit/loss",   pnl_str],
        ["  ROI",                 roi_str],
        ["", ""],
        ["Best bet type",         best_bt],
        ["  P&L from this type",  best_bt_pnl],
        ["", ""],
        ["Best confidence level",   best_conf],
        ["  P&L from this level",   best_conf_pnl],
        ["", ""],
        ["BET TYPE BREAKDOWN", "", "", "", "", ""],
        ["Bet Type", "Wins", "Losses", "Win Rate %", "Total P&L", "Total Picks"],
    ] + bt_rows

    ws_sum = ss.worksheet("Summary")
    ws_sum.clear()
    ws_sum.update("A1", data, value_input_option="RAW")


def finalize_workbook(wb=None) -> None:
    """Recalculate running totals, refresh Summary, and re-apply formatting."""
    try:
        ss = _get_spreadsheet()
    except Exception as exc:
        log.error("finalize_workbook: cannot connect to Sheets: %s", exc)
        return

    try:
        _recalculate_running_total(ss.worksheet("Picks"))
    except Exception as exc:
        log.error("finalize_workbook: running total recalculation failed: %s", exc)

    try:
        _refresh_summary(ss)
    except Exception as exc:
        log.error("finalize_workbook: summary refresh failed: %s", exc)

    _apply_formatting(ss)  # has its own try/except — always runs


# ── Manual result update ──────────────────────────────────────────────────────

def update_result(match_query: str, pick_query: str, result: str, pnl: float | None = None) -> bool:
    result = result.upper()
    if result not in ("WIN", "HALF WIN", "HALF LOSS", "LOSS", "VOID"):
        raise ValueError(f"Result must be WIN, HALF WIN, HALF LOSS, LOSS or VOID — got '{result}'")

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
    if pnl is None:
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

    update_row_result(target_row, result, pnl)
    finalize_workbook()

    match_name = rows[target_row - 1][1] if len(rows[target_row - 1]) > 1 else "?"
    sign = "+" if pnl >= 0 else ""
    print(f"Updated  : {match_name}")
    print(f"Result   : {result}")
    print(f"P&L      : {sign}{pnl:.2f} units  (odds {odds})")
    return True


# ── Picks for a specific date ─────────────────────────────────────────────────

def get_picks_for_date(dt: date) -> list[dict]:
    """Return all logged picks for a specific date with their current result/P&L."""
    try:
        ws = _picks_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return []

    result = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        try:
            row_date = datetime.strptime(row[0], "%d-%b-%Y").date()
        except ValueError:
            continue
        if row_date != dt:
            continue
        try:
            odds = float(row[4]) if len(row) > 4 and row[4] else 1.0
        except ValueError:
            odds = 1.0
        try:
            pnl = float(row[7]) if len(row) > 7 and row[7] else None
        except ValueError:
            pnl = None
        result.append({
            "match":    row[1] if len(row) > 1 else "",
            "bet_type": row[2] if len(row) > 2 else "",
            "pick":     row[3] if len(row) > 3 else "",
            "odds":     odds,
            "result":   row[6] if len(row) > 6 else "",
            "pnl":      pnl,
        })
    return result


# ── Weekly data ───────────────────────────────────────────────────────────────

def get_weekly_data() -> dict:
    try:
        ws = _picks_ws()
        rows = ws.get_all_values()[1:]
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return {}

    today    = date.today()
    week_mon = today - timedelta(days=today.weekday() + 7)
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

    settled  = [r for r in week_rows if r["result"] in _SETTLED_RESULTS]
    wins     = [r for r in settled if r["result"] == "WIN"]
    losses   = [r for r in settled if r["result"] == "LOSS"]
    pending  = [r for r in week_rows if not r["result"]]
    pnl_week = round(sum(r["pnl"] or 0 for r in settled if r["pnl"] is not None), 2)
    wr_settled = [r for r in settled if r["date"] not in _WIN_RATE_EXCLUDE]
    wr_wins    = [r for r in wr_settled if r["result"] == "WIN"]
    win_rate   = round(len(wr_wins) / len(wr_settled) * 100, 1) if wr_settled else 0.0
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


def get_bet_type_breakdown() -> list[dict]:
    """Return settled picks grouped by bet type, sorted by win rate descending."""
    try:
        ws = _picks_ws()
        rows = ws.get_all_values()[1:]
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return []

    groups: dict[str, dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})

    for row in rows:
        if not row or not row[0]:
            continue
        result   = row[6].strip().upper() if len(row) > 6 else ""
        bet_type = row[2].strip()         if len(row) > 2 else ""
        if result not in ("WIN", "HALF WIN", "HALF LOSS", "LOSS") or not bet_type:
            continue
        try:
            pnl = float(row[7]) if len(row) > 7 and row[7] else 0.0
        except ValueError:
            pnl = 0.0
        g = groups[bet_type]
        if result == "WIN":
            g["wins"] += 1
        elif result == "LOSS":
            g["losses"] += 1
        # HALF WIN / HALF LOSS count toward P&L but not win/loss totals
        g["pnl"] += pnl

    breakdown = []
    for bet_type, g in groups.items():
        total    = g["wins"] + g["losses"]
        win_rate = round(g["wins"] / total * 100, 1) if total else 0.0
        breakdown.append({
            "bet_type": bet_type,
            "total":    total,
            "wins":     g["wins"],
            "losses":   g["losses"],
            "win_rate": win_rate,
            "pnl":      round(g["pnl"], 2),
        })

    breakdown.sort(key=lambda x: x["win_rate"], reverse=True)
    return breakdown


# ── Kelly stake recommendation ────────────────────────────────────────────────

def calculate_kelly_stake(bet_type: str, odds: float, confidence: str) -> dict:
    """
    Return {"stake": euros, "note": str} for a half-Kelly recommendation.

    Uses historical win rate for this bet type from settled Sheets data.
    Stake is based on REAL_BANKROLL, capped at 5%.
    Returns a flat UNIT_STAKE with note="insufficient data" when fewer than
    10 settled picks exist for this bet type.
    """
    breakdown = get_bet_type_breakdown()
    record = next(
        (b for b in breakdown if b["bet_type"].strip().lower() == bet_type.strip().lower()),
        None,
    )

    if record is None or record["total"] < 10:
        count = record["total"] if record else 0
        return {"stake": UNIT_STAKE, "note": f"insufficient data ({count} settled picks)"}

    win_rate = record["win_rate"] / 100.0
    kelly = (win_rate * (odds - 1) - (1 - win_rate)) / (odds - 1)

    if kelly <= 0:
        return {"stake": 0.0, "note": "negative edge"}

    fraction = min(kelly * 0.5, 0.05)  # half-Kelly, capped at 5% of bankroll
    stake = round(fraction * REAL_BANKROLL, 2)
    return {"stake": stake, "note": ""}


def get_overall_win_rate() -> float:
    """Overall historical WIN rate (%) across all settled picks."""
    breakdown  = get_bet_type_breakdown()
    total_wins = sum(b["wins"]  for b in breakdown)
    total_set  = sum(b["total"] for b in breakdown)
    return round(total_wins / total_set * 100, 1) if total_set else 0.0


def get_summary_win_rate() -> float:
    """Read the all-time win rate from the Summary sheet by scanning for the 'Win rate' label."""
    try:
        ws   = _get_spreadsheet().worksheet("Summary")
        rows = ws.get_all_values()
        for row in rows:
            if row and row[0].strip().lower() == "win rate":
                val = row[1].strip() if len(row) > 1 else ""
                if val.endswith("%"):
                    return float(val.rstrip("%"))
    except Exception as exc:
        log.warning("Could not read win rate from Summary sheet: %s", exc)
    return 0.0


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


# ── One-time manual fixes ─────────────────────────────────────────────────────

def fix_brazil_japan_picks() -> None:
    """
    One-time manual fix for the Brazil vs Japan picks:
      Match Winner / Brazil Win @ 1.80  →  WIN,       P&L +0.80
      Asian Handicap / Brazil -1.5      →  HALF LOSS, P&L -0.50
    Scans bottom-up for the most recent unfilled rows matching each pick.
    """
    try:
        ws   = _picks_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return

    targets = [
        {
            "match_key": "brazil", "opp_key": "japan",
            "bt_key":    "match winner",
            "pick_key":  "brazil win",
            "result":    "WIN",
            "pnl":       0.80,
        },
        {
            "match_key": "brazil", "opp_key": "japan",
            "bt_key":    "asian handicap",
            "pick_key":  "brazil -1.5",
            "result":    "HALF LOSS",
            "pnl":       -0.50,
        },
    ]

    changed = 0
    for fix in targets:
        for i in range(len(rows) - 1, 0, -1):  # bottom-up → most recent first
            row = rows[i]
            if not row or not row[0]:
                continue
            match_val  = (row[1] if len(row) > 1 else "").lower()
            bt_val     = (row[2] if len(row) > 2 else "").lower()
            pick_val   = (row[3] if len(row) > 3 else "").lower()
            result_val =  row[6] if len(row) > 6 else ""

            if (fix["match_key"] in match_val and fix["opp_key"] in match_val
                    and fix["bt_key"]   in bt_val
                    and fix["pick_key"] in pick_val
                    and not result_val):
                sheet_row = i + 1  # rows list is 0-based; sheet rows are 1-based
                update_row_result(sheet_row, fix["result"], fix["pnl"])
                print(
                    f"Fixed row {sheet_row}: {row[1]} | {row[2]} | {row[3]}"
                    f" → {fix['result']} ({fix['pnl']:+.2f})"
                )
                changed += 1
                break

    if changed:
        finalize_workbook()
        print(f"Done. Fixed {changed} row(s) — running totals and Summary refreshed.")
    else:
        print("No matching unfilled Brazil vs Japan rows found.")


# ── Compatibility stub ────────────────────────────────────────────────────────

def _style_picks_row(ws, row: int, result: str | None) -> None:
    """No-op: row colouring is handled via Google Sheets conditional formatting, not Python."""
    pass


# ── Smoke test ────────────────────────────────────────────────────────────────

def test_format_row() -> None:
    """Paint cell G2 green via batchUpdate to verify the Sheets API write path works."""
    import pprint
    ss = _get_spreadsheet()
    ws = ss.worksheet("Picks")
    req = {
        "requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": 1, "endRowIndex": 2,   # row 2 (0-based: index 1)
                    "startColumnIndex": 6, "endColumnIndex": 7,  # column G
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.0, "green": 0.784, "blue": 0.325},
                }},
                "fields": "userEnteredFormat(backgroundColor)",
            }
        }]
    }
    print("Sending batchUpdate request:")
    pprint.pprint(req)
    try:
        resp = ss.batch_update(req)
        print("\nbatchUpdate succeeded. Response:")
        pprint.pprint(resp)
    except Exception as exc:
        print(f"\nbatchUpdate FAILED — {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    test_format_row()
