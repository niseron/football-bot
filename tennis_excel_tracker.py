"""
Google Sheets tracking layer for the TENNIS picks system.

Fully independent of excel_tracker.py (the football layer): it opens its own
gspread client, reads and writes ONLY the 'Tennis Picks' worksheet, and shares
no functions, headers, or state with the football data path. Every write in
this module is hard-targeted at TENNIS_SHEET_NAME so tennis data can never
land in football rows or vice versa.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta

import gspread

log = logging.getLogger(__name__)

TENNIS_SHEET_NAME = "Tennis Picks"

# New columns must be appended at the END — result/P&L logic addresses
# columns G/H by letter throughout this module.
TENNIS_HEADERS = [
    "Date", "Match", "Bet Type", "Pick", "Odds",
    "Confidence", "Result", "P&L",
    "Claude Prob %", "Market Prob %",
    "Kickoff/Start Time", "Closing Odds",
]

# Tennis results have no half outcomes (no quarter-line handicaps in games)
TENNIS_SETTLED_RESULTS: frozenset[str] = frozenset(["WIN", "LOSS", "VOID"])

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


def _tennis_ws() -> gspread.Worksheet:
    """Return the Tennis Picks worksheet, creating it (with headers) if missing."""
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(TENNIS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(TENNIS_SHEET_NAME, rows=1000, cols=len(TENNIS_HEADERS))
        ws.append_row(TENNIS_HEADERS, value_input_option="RAW")
        log.info("Created '%s' sheet with headers", TENNIS_SHEET_NAME)
        return ws


def init_tennis_sheet() -> None:
    """Ensure the Tennis Picks tab exists with the full current header row."""
    try:
        ws = _tennis_ws()
    except Exception as exc:
        log.error("Cannot connect to Google Sheets (tennis): %s", exc)
        return
    try:
        header = ws.row_values(1)
        if len(header) < len(TENNIS_HEADERS):
            if ws.col_count < len(TENNIS_HEADERS):
                ws.resize(cols=len(TENNIS_HEADERS))
            for idx in range(len(header), len(TENNIS_HEADERS)):
                ws.update_cell(1, idx + 1, TENNIS_HEADERS[idx])
            log.info("Added missing column header(s) to '%s': %s",
                     TENNIS_SHEET_NAME, TENNIS_HEADERS[len(header):])
    except Exception as exc:
        log.warning("Tennis header migration failed (non-fatal): %s", exc)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _rgb(hex_str: str) -> dict:
    h = hex_str.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


_WHITE       = _rgb("#ffffff")
_LIGHT_BLUE  = _rgb("#e3f2fd")
_DARK_BLUE   = _rgb("#0d47a1")   # tennis header — blue, visually distinct from football's green
_WIN_GREEN   = _rgb("#00c853")
_LOSS_RED    = _rgb("#d50000")
_WHITE_TEXT  = {"red": 1.0, "green": 1.0, "blue": 1.0}
_BLACK_TEXT  = {"red": 0.0, "green": 0.0, "blue": 0.0}


def _apply_tennis_formatting() -> None:
    try:
        ss = _get_spreadsheet()
        ws = ss.worksheet(TENNIS_SHEET_NAME)
        sid = ws.id
        all_rows = ws.get_all_values()
        nrows = len(all_rows)
        ncols = len(TENNIS_HEADERS)
        result_col = TENNIS_HEADERS.index("Result")

        if nrows < 1:
            return

        reqs: list[dict] = [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                              "startColumnIndex": 0, "endColumnIndex": ncols},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": _DARK_BLUE,
                        "textFormat": {"bold": True, "foregroundColor": _WHITE_TEXT},
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            },
        ]

        for i in range(1, nrows):
            row_bg = _WHITE if i % 2 == 1 else _LIGHT_BLUE
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
            result_val = all_rows[i][result_col] if len(all_rows[i]) > result_col else ""
            colour = {"WIN": _WIN_GREEN, "LOSS": _LOSS_RED}.get(result_val)
            if colour is not None:
                reqs.append({
                    "repeatCell": {
                        "range": {"sheetId": sid, "startRowIndex": i, "endRowIndex": i + 1,
                                  "startColumnIndex": result_col, "endColumnIndex": result_col + 1},
                        "cell": {"userEnteredFormat": {
                            "backgroundColor": colour,
                            "textFormat": {"bold": True, "foregroundColor": _WHITE_TEXT},
                        }},
                        "fields": "userEnteredFormat(backgroundColor,textFormat)",
                    }
                })

        reqs.append({
            "autoResizeDimensions": {
                "dimensions": {"sheetId": sid, "dimension": "COLUMNS",
                               "startIndex": 0, "endIndex": ncols},
            }
        })

        ss.batch_update({"requests": reqs})
        log.info("Formatting applied to '%s' sheet (%d rows)", TENNIS_SHEET_NAME, nrows)
    except Exception as exc:
        log.warning("Tennis sheet formatting failed (non-fatal): %s", exc)


# ── Write a new pick ──────────────────────────────────────────────────────────

def log_tennis_pick(
    match: str,
    bet_type: str,
    pick: str,
    odds: float,
    confidence: str,
    pick_date: str | None = None,
    claude_prob: float | None = None,
    market_prob: float | None = None,
    start_time_utc: str | None = None,
) -> None:
    dt = datetime.fromisoformat(pick_date) if pick_date else datetime.now()
    date_str = dt.strftime("%d-%b-%Y")
    target_date = dt.date()

    init_tennis_sheet()
    try:
        ws = _tennis_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Tennis Sheets read failed: %s", exc)
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
            log.info("Tennis Sheets: skipping duplicate '%s — %s'", match, pick)
            return

    new_row = [
        date_str, match, bet_type, pick, round(float(odds), 2), confidence, "", "",
        round(float(claude_prob), 1) if claude_prob is not None else "",
        round(float(market_prob), 1) if market_prob is not None else "",
        start_time_utc or "",
        "",  # Closing Odds — populated later by the tennis closing-odds job, if at all
    ]
    try:
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        log.info("Tennis Sheets: logged '%s — %s'", match, pick)
        _apply_tennis_formatting()
    except Exception as exc:
        log.error("Tennis Sheets write failed: %s", exc)


def tennis_picks_exist_for_today() -> bool:
    """True if any pick dated today is already in the Tennis Picks tab.

    Sheet-based duplicate-run guard — the tennis system deliberately does not
    touch the football picks.db SQLite file.
    """
    try:
        rows = _tennis_ws().get_all_values()
    except Exception as exc:
        log.warning("tennis_picks_exist_for_today: read failed, assuming no picks: %s", exc)
        return False
    today = date.today()
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        try:
            if datetime.strptime(row[0], "%d-%b-%Y").date() == today:
                return True
        except ValueError:
            continue
    return False


# ── Pending rows ──────────────────────────────────────────────────────────────

def get_pending_tennis_picks(lookback_days: int = 7) -> list[dict]:
    """Rows with no Result within the lookback window, each with its 1-based sheet row."""
    try:
        rows = _tennis_ws().get_all_values()
    except Exception as exc:
        log.error("Tennis Sheets read failed: %s", exc)
        return []

    cutoff = date.today() - timedelta(days=lookback_days)
    result_col = TENNIS_HEADERS.index("Result")
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


def get_unsettled_tennis_picks_with_start() -> list[dict]:
    """
    Unsettled (no Result) tennis picks that have a logged Kickoff/Start Time,
    each with its 1-based sheet row. Empty list (never raises) on any failure —
    callers treat that as "nothing to do".
    """
    try:
        rows = _tennis_ws().get_all_values()
    except Exception as exc:
        log.error("Tennis Sheets read failed: %s", exc)
        return []
    if not rows:
        return []

    header = rows[0]
    try:
        result_col = header.index("Result")
        start_col  = header.index("Kickoff/Start Time")
    except ValueError:
        return []

    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        if len(row) > result_col and row[result_col]:
            continue
        start_utc = row[start_col] if len(row) > start_col else ""
        if not start_utc:
            continue
        out.append({
            "sheet_row": i,
            "match":     row[1] if len(row) > 1 else "",
            "bet_type":  row[2] if len(row) > 2 else "",
            "pick":      row[3] if len(row) > 3 else "",
            "start_utc": start_utc,
        })
    return out


# ── Write result / closing odds for one row ───────────────────────────────────

def update_tennis_row_result(sheet_row: int, result: str, pnl: float) -> None:
    """Write Result (G) and P&L (H) to a specific Tennis Picks row."""
    try:
        ss = _get_spreadsheet()
        ws = ss.worksheet(TENNIS_SHEET_NAME)
        ws.batch_update([
            {"range": f"G{sheet_row}", "values": [[result]]},
            {"range": f"H{sheet_row}", "values": [[round(pnl, 2)]]},
        ])
        _apply_tennis_formatting()
    except Exception as exc:
        log.error("Tennis Sheets update_tennis_row_result failed: %s", exc)


def update_tennis_closing_odds(sheet_row: int, closing_odds: float) -> None:
    """
    Write (overwriting any prior value) the Closing Odds cell for one tennis row.
    Called repeatedly as the match start approaches — the last write before the
    start becomes the closing price. Column located by header name.
    """
    try:
        ws = _tennis_ws()
        header = ws.row_values(1)
        col = header.index("Closing Odds") + 1  # gspread columns are 1-based
        ws.update_cell(sheet_row, col, round(float(closing_odds), 2))
    except Exception as exc:
        log.error("Tennis Sheets update_tennis_closing_odds failed (row %d): %s", sheet_row, exc)


# ── Manual result update ──────────────────────────────────────────────────────

def update_tennis_result(match_query: str, pick_query: str, result: str,
                         pnl: float | None = None) -> bool:
    result = result.upper()
    if result not in TENNIS_SETTLED_RESULTS:
        raise ValueError(f"Result must be WIN, LOSS or VOID — got '{result}'")

    try:
        rows = _tennis_ws().get_all_values()
    except Exception as exc:
        log.error("Tennis Sheets read failed: %s", exc)
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
        print(f"No pending tennis pick found matching '{match_query}' / '{pick_query}'")
        return False

    try:
        odds = float(rows[target_row - 1][4]) if len(rows[target_row - 1]) > 4 and rows[target_row - 1][4] else 1.0
    except ValueError:
        odds = 1.0
    if pnl is None:
        if result == "WIN":
            pnl = round(odds - 1, 2)
        elif result == "LOSS":
            pnl = -1.0
        else:
            pnl = 0.0

    update_tennis_row_result(target_row, result, pnl)

    match_name = rows[target_row - 1][1] if len(rows[target_row - 1]) > 1 else "?"
    sign = "+" if pnl >= 0 else ""
    print(f"Updated  : {match_name}")
    print(f"Result   : {result}")
    print(f"P&L      : {sign}{pnl:.2f} units  (odds {odds})")
    return True


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_tennis_sheet()
    print(f"'{TENNIS_SHEET_NAME}' tab ready.")
