"""
Google Sheets tracking layer for the football betting bot.
Drop-in replacement for the openpyxl-based version — same public API.
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import gspread

log = logging.getLogger(__name__)

# ── Sheets quota / transient-failure retry ───────────────────────────────────
# Google Sheets allows 60 READ requests per minute per user (the service
# account), counted across the whole spreadsheet. Exceeding it returns a 429
# that gspread raises as APIError, and every read in this module is wrapped in
# a try/except that logs and returns a neutral value — so before 1 Sep 2026 a
# 429 was completely silent.
#
# It was not hypothetical. From 20 Aug 2026 the picks run read the full Picks
# tab ONCE PER PICK (calculate_kelly_stake -> get_bet_type_breakdown), so a
# slate of 21+ picks exhausted the minute's read budget and the batch write
# that followed got the 429. Six days lost their entire slate to the sheet
# while Discord showed a full card: 20, 22, 26, 27, 28 and 29 Aug, 128 picks.
#
# The per-pick read is gone (calculate_kelly_stake now takes a precomputed
# breakdown), which is the actual fix. This retry is the belt to that braces:
# the quota is per MINUTE, so sleeping through the window is usually enough to
# turn a lost slate into a slow one.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
SHEETS_RETRY_ATTEMPTS = 4
SHEETS_RETRY_BASE_DELAY = 20.0   # seconds; 429s clear on a per-minute window


def _is_retryable_api_error(exc: Exception) -> bool:
    """True for a Sheets error that is worth waiting out (quota / 5xx)."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    if code in _RETRY_STATUSES:
        return True
    # gspread does not always expose the status; the rendered message does.
    text = str(exc).lower()
    return "quota exceeded" in text or "[429]" in text or "rate_limit" in text


def with_sheets_retry(what: str, fn, *args, **kwargs):
    """
    Run a Sheets call, retrying quota/5xx failures with exponential backoff.

    Re-raises the last exception when the retries are used up, so the CALLER
    still decides what a permanent failure means — this helper never converts a
    failure into a silent success. Non-retryable errors propagate immediately.
    """
    delay = SHEETS_RETRY_BASE_DELAY
    for attempt in range(1, SHEETS_RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == SHEETS_RETRY_ATTEMPTS or not _is_retryable_api_error(exc):
                raise
            # Jitter so concurrently-throttled jobs do not retry in lockstep and
            # re-exhaust the same window together.
            wait = delay + random.uniform(0, delay * 0.25)
            log.warning(
                "Sheets %s hit a retryable error (attempt %d/%d) — retrying in %.0fs: %s",
                what, attempt, SHEETS_RETRY_ATTEMPTS, wait, exc,
            )
            time.sleep(wait)
            delay *= 2
    raise RuntimeError("unreachable")


class BatchLogResult(NamedTuple):
    """
    Outcome of one batch pick-logging call.

    Three buckets, because they mean different things to an operator:
      written — rows appended to the sheet
      skipped — rows deliberately NOT written (duplicate guard). Expected.
      failed  — rows that SHOULD have been written and were not. Never expected;
                this is the number that has to raise an alarm.

    `int(result)` is the written count, so the historical `written = log_picks_batch(...)`
    shape still reads correctly at a glance.
    """
    written: int
    skipped: int
    failed: int

    def __int__(self) -> int:
        return self.written

    @property
    def attempted(self) -> int:
        return self.written + self.skipped + self.failed

# Kept so any script that imports EXCEL_PATH still compiles
EXCEL_PATH = Path(__file__).parent / "picks_tracker.xlsx"

# New columns must be appended at the END — result/P&L/running-total logic
# addresses columns G/H/I/J by letter throughout this module.
PICKS_HEADERS = [
    "Date", "Match", "Bet Type", "Pick", "Odds",
    "Confidence", "Result", "Profit/Loss", "Running Total P&L", "Bankroll (€)",
    "Claude Prob %", "Market Prob %",
    "League", "Kickoff UTC", "Closing Odds", "Market Odds",
    "Pick Tier",
]

# ── Pick tiers (13 Aug 2026) ─────────────────────────────────────────────────
# Football picks are conviction-ranked within their own competition (up to
# main.MAX_PICKS_PER_LEAGUE each), and a global selection step then marks the
# best 5 of the day CORE — the continuous series collected since 30 Jun 2026 and
# the only rows that may reach calibration, edge and CLV. Every other pick is
# EXTENDED — tracked and settled identically so its real P&L is measurable, but
# excluded from every Core aggregation.
#
# Tier does NOT follow from a rank number. It did until 15 Aug 2026 (one global
# list of 10: ranks 1-5 Core, 6-10 Extended); since then a league's rank-1 pick
# is Extended whenever it loses the global cut, and Extended volume is several
# times Core's. Read the column, never infer the tier.
#
# A BLANK 'Pick Tier' means CORE, and that is load-bearing, not a convenience:
# every row logged before this column existed keeps its exact meaning with no
# backfill and no rewrite, so the baseline collected since 30 Jun cannot drift.
# Never "migrate" old rows by filling this column in.
PICK_TIER_CORE     = "Core"
PICK_TIER_EXTENDED = "Extended"

# A third tier, added 13 Aug 2026 for the retroactive duplicate correction.
# Rows tagged Duplicate are neither Core nor Extended, so _core_rows and
# _extended_rows both drop them and they fall out of EVERY metric — running
# total, bankroll, Summary, calibration, edge, CLV, the weekly card — without a
# single new filter anywhere.
#
# Chosen over deleting the rows because it is reversible and auditable: the bet,
# its odds and its settled result stay on the sheet, and undoing an exclusion is
# editing one cell back to 'Core'. Applied to fixtures that were logged on more
# than one date before the match-level guard existed; the FIRST pick on a
# fixture keeps its tier, every later one is tagged.
PICK_TIER_DUPLICATE = "Duplicate"


def _row_tier(row: list[str], tier_idx: int | None) -> str:
    """This row's tier. Missing column, short row or blank cell all mean Core."""
    if tier_idx is None or len(row) <= tier_idx:
        return PICK_TIER_CORE
    return (row[tier_idx] or "").strip() or PICK_TIER_CORE


def _core_rows(rows: list[list[str]], header: list[str] | None = None) -> list[list[str]]:
    """
    Core-tier rows only — the single filter every Core aggregation goes through.

    Deliberately ONE helper rather than an inline tier check per call site: the
    Core series feeds calibration, CLV, edge, the Summary tab, the card footer
    and the weekly card, and a site that silently forgot to filter would fold
    Extended P&L into the baseline without anything visibly breaking.

    `header` is optional so callers holding only the data rows (the common
    `get_all_values()[1:]` shape) can still filter; without it the tier column
    is located at its fixed position in PICKS_HEADERS. Rows shorter than that
    index — i.e. every row logged before 13 Aug 2026 — are Core by definition.

    NOT applied to settlement (`get_pending_picks_rows`) or closing-odds
    collection (`get_unsettled_picks_with_kickoff`): both tiers must settle and
    both collect closing odds. Only the reporting layer is Core-only.
    """
    if header:
        tier_idx = _col(header, "Pick Tier")
    else:
        tier_idx = PICKS_HEADERS.index("Pick Tier")
    return [r for r in rows if _row_tier(r, tier_idx) == PICK_TIER_CORE]


def _extended_rows(rows: list[list[str]], header: list[str] | None = None) -> list[list[str]]:
    """Extended-tier rows only — the mirror of _core_rows, for tier reporting."""
    if header:
        tier_idx = _col(header, "Pick Tier")
    else:
        tier_idx = PICKS_HEADERS.index("Pick Tier")
    return [r for r in rows if _row_tier(r, tier_idx) == PICK_TIER_EXTENDED]

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

# Competitions always shown in the Summary tab's LEAGUE BREAKDOWN, even at zero
# picks. These strings must match what main.py writes to the 'League' column
# exactly — note "FIFA World Cup 2026", not "World Cup". Leagues found in the
# sheet but absent here (e.g. Conference League) are appended automatically, so
# this list is a display floor, not a filter.
TRACKED_LEAGUES: list[str] = [
    "FIFA World Cup 2026",
    "Premier League",
    "Jupiler Pro League",
    "Champions League",
    "La Liga",
    "Bundesliga",
    "Serie A",
    "Ligue 1",
]

# Bucket for picks logged before the 'League' column was added to PICKS_HEADERS.
# Keeps the league breakdown's P&L reconcilable with the headline total.
_NO_LEAGUE = "(no league recorded)"

# Minimum decided picks (wins + losses) before a Bet Type × League cell is
# allowed to show a win rate. Below this it reads 'insufficient data' — a
# 2-pick cell at 100% reads as a finding when it is noise. Slicing two ways at
# once splits an already-small sample hard, so most cells sit under this for a
# long time; that is the honest state, not a gap to be filled.
_MIN_CELL_SAMPLE = 10


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


# ── Settlement price resolution ───────────────────────────────────────────────
#
# The 'Odds' column holds Claude's ESTIMATED price. When the pick was matched
# to a real bookmaker line, the picks card shows that market price instead —
# so the market price is what a follower actually got, and the only correct
# basis for P&L. Settling a 1.75 market price at a 2.00 estimate books a
# payout nobody received.
#
# 'Market Odds' (added 9 Aug 2026) stores the matched price verbatim. Rows
# logged before that carry only 'Market Prob %', which main.py writes as
# 100/market_odds rounded to 1 dp — invertible, and exact after rounding to
# 2 dp at the short prices this bot picks, but lossy past roughly 6.00 (an
# 8.50 price round-trips to 8.47). Prefer the explicit column; derive only
# when it is absent.


def _col(header: list[str], name: str) -> int | None:
    try:
        return header.index(name)
    except ValueError:
        return None  # sheet pre-dates this column


def _cell_float(row: list[str], idx: int | None) -> float | None:
    if idx is None or len(row) <= idx:
        return None
    val = (row[idx] or "").strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def market_odds_from_row(row: list[str], header: list[str]) -> float | None:
    """The matched market price for a row, or None when no market line was matched."""
    explicit = _cell_float(row, _col(header, "Market Odds"))
    if explicit is not None and explicit > 1.0:
        return round(explicit, 2)
    prob = _cell_float(row, _col(header, "Market Prob %"))
    if prob is not None and prob > 0:
        return round(100.0 / prob, 2)
    return None


def settlement_odds_from_row(row: list[str], header: list[str]) -> float:
    """
    The price this pick settles at: the market price when one was matched,
    otherwise Claude's estimate. Falls back to 1.0 (P&L 0) on an unreadable
    Odds cell, matching the previous behaviour of every caller.
    """
    market = market_odds_from_row(row, header)
    if market is not None:
        return market
    return _cell_float(row, _col(header, "Odds")) or 1.0


def pnl_for_result(result: str, odds: float) -> float:
    """
    Units won/lost on a 1-unit stake. Single definition shared by the
    automatic (auto_results.py) and manual (update_result) settlement paths
    so the two can never drift apart on which price they pay out at.
    """
    if result == "WIN":
        return round(odds - 1, 2)
    if result == "HALF WIN":
        return round(0.5 * (odds - 1), 2)
    if result == "HALF LOSS":
        return -0.50
    if result == "LOSS":
        return -1.0
    return 0.0


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
        # Add any column headers the sheet pre-dates (Bankroll, Claude/Market Prob %)
        ws = ss.worksheet("Picks")
        header = ws.row_values(1)
        if len(header) < len(PICKS_HEADERS):
            ws.resize(cols=len(PICKS_HEADERS))
            for idx in range(len(header), len(PICKS_HEADERS)):
                ws.update_cell(1, idx + 1, PICKS_HEADERS[idx])
            log.info("Added missing column header(s) to existing Picks sheet: %s",
                     PICKS_HEADERS[len(header):])

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

def _migrate_picks_header(ws: gspread.Worksheet, header: list[str]) -> None:
    """Widen the sheet and fill in headers if it pre-dates the newer columns."""
    try:
        if len(header) < len(PICKS_HEADERS):
            if ws.col_count < len(PICKS_HEADERS):
                ws.resize(cols=len(PICKS_HEADERS))
            for idx in range(len(header), len(PICKS_HEADERS)):
                ws.update_cell(1, idx + 1, PICKS_HEADERS[idx])
    except Exception as exc:
        log.warning("Picks header migration failed (non-fatal): %s", exc)


def _duplicate_skip_reason(
    body_rows: list[list[str]],
    header: list[str],
    match: str,
    bet_type: str,
    pick: str,
    target_date: date,
) -> str | None:
    """
    Why this pick must NOT be written, or None if it may be. Two guards, and the
    second is the one that matters.

    (a) SAME-DAY exact repeat — a re-run of the same job. Date-scoped.

    (b) CROSS-DAY repeat of an UNSETTLED bet on the same fixture, added
        13 Aug 2026. fetch_upcoming_matches pulls a 48-hour window, so a fixture
        kicking off tomorrow appears in today's run AND tomorrow's. The analysis
        dedupes (match, bet_type) but only WITHIN one batch, and guard (a) is
        keyed on the date — so the second day's pick sailed through and the
        fixture was logged twice. One match then settled both rows: P&L booked
        twice and the pick counted twice in calibration. Measured on 13 Aug 2026:
        16 duplicated groups across 223 rows, +4.20u double-counted, 13.3% of
        reported Core P&L.

    Keyed on MATCH ALONE (widened from (match, bet_type) on 13 Aug 2026). One
    fixture gets at most one unsettled bet, full stop. The narrower keys both
    leaked: (match, bet_type, pick) let the 3-4 Jul Canada vs Morocco BTTS market
    through as 'Yes' one day and 'No' the next — opposite sides of one market —
    and (match, bet_type) still allowed two different bet types on one fixture,
    which is two stakes riding on a single result. Nothing downstream treats
    those as correlated, so the sheet's P&L and calibration both read them as
    independent samples when they are not.

    Only UNSETTLED rows block: once a row has a Result the fixture is over, so a
    later row on it is a data problem to see, not one to hide.

    `body_rows` must exclude the header. Callers writing several picks in one
    pass append each accepted row to that list, so the guards see picks staged
    earlier in the same batch exactly as if they were already on the sheet.
    """
    result_idx = _col(header, "Result")
    result_idx = 6 if result_idx is None else result_idx
    for row in body_rows:
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
            return "already logged today (exact duplicate)"
        settled = len(row) > result_idx and bool((row[result_idx] or "").strip())
        if not settled and len(row) > 1 and row[1] == match:
            return (
                f"this fixture already has an UNSETTLED pick logged ({row[0]}: "
                f"'{row[3] if len(row) > 3 else '?'}' [{row[2] if len(row) > 2 else '?'}]). "
                "One fixture carries at most one open bet; a second would settle "
                "off the same result."
            )
    return None


def _build_pick_row(
    date_str: str,
    match: str,
    league: str,
    bet_type: str,
    pick: str,
    odds: float,
    confidence: str,
    claude_prob: float | None,
    market_prob: float | None,
    kickoff_utc: str | None,
    market_odds: float | None,
    pick_tier: str,
) -> list:
    """One Picks row, in PICKS_HEADERS order."""
    return [
        date_str, match, bet_type, pick, round(float(odds), 2), confidence, "", "", "", "",
        round(float(claude_prob), 1) if claude_prob is not None else "",
        round(float(market_prob), 1) if market_prob is not None else "",
        league or "",
        kickoff_utc or "",
        "",  # Closing Odds — populated later by the closing-odds job, if at all
        # The price shown on the picks card and paid out at settlement. Empty
        # when no market line was matched — settlement then falls back to the
        # 'Odds' estimate.
        round(float(market_odds), 2) if market_odds is not None else "",
        # Core or Extended. Written explicitly on every new row; blank on
        # historical rows, which _row_tier reads as Core.
        pick_tier or PICK_TIER_CORE,
    ]


def log_picks_batch(entries: list[dict]) -> BatchLogResult:
    """
    Log a whole run's picks in ONE round-trip.

    Returns a BatchLogResult splitting the outcome into written / skipped /
    failed. The split is the point: before 1 Sep 2026 this returned a bare int
    and the caller logged "Logged 0 of 29" as INFO, so a 429 that dropped a
    whole slate looked exactly like a day where the duplicate guard legitimately
    skipped everything. `failed` is never expected and must be alerted on.

    Same guards, same row shape and same ordering as log_to_excel — the only
    difference is that the sheet is read once, appended to once and repainted
    once for the entire batch instead of once PER PICK. That mattered from
    15 Aug 2026, when the per-league cap took a busy run from 10 picks to 30+:
    log_to_excel costs ~4 API calls per pick and _apply_formatting repaints
    every row of the sheet each time, so a 30-pick run would have made ~120
    calls and 30 full repaints in a couple of minutes — comfortably into Sheets'
    per-minute quota, where the failure mode is a pick delivered to Discord but
    missing from the sheet.

    Entries are written in list order, so pass Core first: the fixture-level
    guard then resolves any collision in Core's favour.
    """
    if not entries:
        return BatchLogResult(0, 0, 0)

    try:
        ws = _picks_ws()
        rows = with_sheets_retry("read (picks batch)", ws.get_all_values)
    except Exception as exc:
        # EVERY entry is unaccounted for: the guard never ran, so none of these
        # are 'skipped'. They are failures and must be alerted on.
        log.error("Sheets read failed: %s", exc)
        return BatchLogResult(0, 0, len(entries))

    header = rows[0] if rows else []
    _migrate_picks_header(ws, header)

    known: list[list[str]] = list(rows[1:])
    staged: list[list] = []
    skipped = 0
    failed  = 0
    for e in entries:
        try:
            dt = datetime.fromisoformat(e["pick_date"]) if e.get("pick_date") else datetime.now()
            match, bet_type, pick = e["match"], e["bet_type"], e["pick"]
            reason = _duplicate_skip_reason(known, header, match, bet_type, pick, dt.date())
            if reason:
                log.info("Sheets: skipping '%s — %s [%s]' — %s", match, pick, bet_type, reason)
                skipped += 1
                continue
            row = _build_pick_row(
                dt.strftime("%d-%b-%Y"), match, e.get("league", ""), bet_type, pick,
                e["odds"], e.get("confidence", "N/A"),
                e.get("claude_prob"), e.get("market_prob"), e.get("kickoff_utc"),
                e.get("market_odds"), e.get("pick_tier", PICK_TIER_CORE),
            )
        except Exception as exc:
            # A row we could not build is a LOST pick, not a skipped one — it
            # was eligible and nothing downstream will ever see it again.
            log.error("Sheets: could not build row for %r (lost): %s", e.get("match"), exc)
            failed += 1
            continue
        staged.append(row)
        known.append([str(c) for c in row])

    if not staged:
        log.info("Sheets: nothing to log — %d skipped, %d failed of %d pick(s)",
                 skipped, failed, len(entries))
        return BatchLogResult(0, skipped, failed)

    try:
        with_sheets_retry(
            "append (picks batch)",
            ws.append_rows, staged, value_input_option="USER_ENTERED",
        )
        log.info("Sheets: logged %d pick(s) in one batch", len(staged))
    except Exception as exc:
        log.error("Sheets batch write failed: %s", exc)
        return BatchLogResult(0, skipped, failed + len(staged))

    _apply_formatting(_get_spreadsheet())
    return BatchLogResult(len(staged), skipped, failed)


def log_to_excel(
    match: str,
    league: str,
    bet_type: str,
    pick: str,
    odds: float,
    confidence: str,
    pick_date: str | None = None,
    claude_prob: float | None = None,
    market_prob: float | None = None,
    kickoff_utc: str | None = None,
    market_odds: float | None = None,
    pick_tier: str = PICK_TIER_CORE,
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

    header = rows[0] if rows else []
    _migrate_picks_header(ws, header)

    reason = _duplicate_skip_reason(rows[1:], header, match, bet_type, pick, target_date)
    if reason:
        log.info("Sheets: skipping '%s — %s [%s]' — %s", match, pick, bet_type, reason)
        return

    new_row = _build_pick_row(
        date_str, match, league, bet_type, pick, odds, confidence,
        claude_prob, market_prob, kickoff_utc, market_odds, pick_tier,
    )
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
    header = rows[0] if rows else []
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
            # 'odds' is the SETTLEMENT price — the market price when one was
            # matched, else the estimate. The raw estimate stays available as
            # 'est_odds' for calibration; 'market_odds' is None when unmatched.
            "odds":        settlement_odds_from_row(row, header),
            "est_odds":    _cell_float(row, _col(header, "Odds")) or 1.0,
            "market_odds": market_odds_from_row(row, header),
        })
    return pending


# ── Pending rows with kickoff time (used by closing_odds job) ────────────────

def get_unsettled_picks_with_kickoff() -> list[dict]:
    """
    Return unsettled (no Result) picks that have a logged Kickoff UTC value,
    each with its 1-based sheet row, league, bet_type, pick, and kickoff_utc.
    Empty list (never raises) if the sheet can't be read or pre-dates the
    League/Kickoff UTC columns — callers should treat that as "nothing to do".
    """
    try:
        ws = _picks_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return []

    if not rows:
        return []

    header = rows[0]
    try:
        result_col  = header.index("Result")
        league_col  = header.index("League")
        kickoff_col = header.index("Kickoff UTC")
    except ValueError:
        return []  # sheet pre-dates these columns — nothing to check yet

    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        if len(row) > result_col and row[result_col]:
            continue  # already settled
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


def update_closing_odds(sheet_row: int, closing_odds: float) -> None:
    """
    Write (overwriting any prior value) the Closing Odds cell for one row.
    Called repeatedly as kickoff approaches — the last write before kickoff
    is the closing price. Column is located by header name, not a hardcoded
    letter, so it survives future column reordering.
    """
    try:
        ws = _picks_ws()
        header = ws.row_values(1)
        col = header.index("Closing Odds") + 1  # gspread columns are 1-based
        ws.update_cell(sheet_row, col, round(float(closing_odds), 2))
    except Exception as exc:
        log.error("Sheets update_closing_odds failed (row %d): %s", sheet_row, exc)


# ── Recalculate running totals + Summary ──────────────────────────────────────

def _recalculate_running_total(ws: gspread.Worksheet) -> None:
    # CORE ONLY. The running total and bankroll columns (I/J) are the headline
    # baseline series, so an Extended row must never move them — it is skipped
    # here exactly as an unsettled row is, leaving I/J blank on its line while
    # the Core accumulation carries straight past it. This is the single most
    # important application of the tier filter: without it, doubling pick
    # volume on 13 Aug 2026 would have silently doubled the tracked bankroll's
    # inputs and broken continuity with everything logged since 30 Jun.
    rows = ws.get_all_values()
    header = rows[0] if rows else []
    tier_idx = _col(header, "Pick Tier")
    running_units = 0.0
    bankroll      = STARTING_BANKROLL
    updates  = []
    fmt_reqs = []
    ws_id    = ws.id
    for i, row in enumerate(rows[1:], start=2):
        result  = row[6] if len(row) > 6 else ""
        pnl_str = row[7] if len(row) > 7 else ""
        row_idx = i - 1  # 0-based for Sheets API
        is_core = _row_tier(row, tier_idx) == PICK_TIER_CORE
        if is_core and result in _SETTLED_RESULTS and pnl_str:
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
    # CORE ONLY — this drives the Summary tab, which in turn feeds the picks
    # card footer win rate. The Extended tier gets its own separate block,
    # appended below by _append_tier_breakdown(), never mixed into these totals.
    all_picks_rows = ss.worksheet("Picks").get_all_values()
    picks_header   = all_picks_rows[0] if all_picks_rows else []
    rows = _core_rows(all_picks_rows[1:], picks_header)

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

    # ── League breakdown (uses the League column, index 12) ──────────────────
    # Every tracked competition gets a row even with no picks yet: several open
    # their 2026-27 season mid-to-late August, so zeros are the expected state
    # for now, and an absent row would be indistinguishable from a routing bug.
    # Any league present in the sheet but not listed here is appended rather
    # than dropped, and picks logged before the League column existed group
    # under _NO_LEAGUE — without that bucket this section's P&L would silently
    # fail to reconcile with 'Total P&L (units)' above.
    lg_groups: dict[str, dict] = {
        name: {"wins": 0, "losses": 0, "pnl": 0.0, "picks": 0}
        for name in TRACKED_LEAGUES
    }
    for r in rows:
        name = r[12].strip() if len(r) > 12 and r[12].strip() else _NO_LEAGUE
        g = lg_groups.setdefault(name, {"wins": 0, "losses": 0, "pnl": 0.0, "picks": 0})
        g["picks"] += 1                      # every logged pick, settled or not
        result = r[6] if len(r) > 6 else ""
        if result not in _SETTLED_RESULTS or result == "VOID":
            continue
        g["pnl"] += _pnl(r)                  # half wins/losses count here...
        if result == "WIN":
            g["wins"] += 1                   # ...but not in the W/L win rate,
        elif result == "LOSS":
            g["losses"] += 1                 # matching the bet-type section

    lg_rows = []
    for name, g in lg_groups.items():
        decided = g["wins"] + g["losses"]
        wr = round(g["wins"] / decided * 100, 1) if decided else 0.0
        lg_rows.append([name, g["wins"], g["losses"], f"{wr:.1f}%",
                        round(g["pnl"], 2), g["picks"]])
    # P&L descending, as asked. Ties break on pick count then name so the
    # not-yet-in-season leagues — all identically zero — hold a stable order
    # instead of shuffling between refreshes.
    lg_rows.sort(key=lambda x: (-x[4], -x[5], x[0]))

    # ── Bet type × league cross-breakdown ────────────────────────────────────
    # Win rate is suppressed below _MIN_CELL_SAMPLE and replaced with
    # 'insufficient data': a 2-pick cell reading 100% is worse than no number,
    # because it invites exactly the conclusion the sample cannot support.
    #
    # The gate is on the rate's OWN denominator (wins + losses), not on the
    # count of settled picks. A cell holding 10 settled picks that are 8 VOIDs
    # and 2 decided would pass a settled>=10 test and then print the very
    # 2-sample rate this rule exists to hide. P&L and pick count are always
    # shown — only the rate is unsafe at low n.
    #
    # Only leagues with at least one settled pick get rows, so the seven
    # competitions still waiting for their 2026-27 season open contribute
    # nothing here (they remain visible, at zero, in the League Breakdown).
    xl_cells: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "picks": 0}
    )
    league_settled: dict[str, int] = defaultdict(int)
    for r in rows:
        league = r[12].strip() if len(r) > 12 and r[12].strip() else _NO_LEAGUE
        result = r[6] if len(r) > 6 else ""
        if result in _SETTLED_RESULTS:
            league_settled[league] += 1
        bt = r[2].strip() if len(r) > 2 else ""
        if not bt:
            continue                         # no bet type = no cell to file it under
        cell = xl_cells[(league, bt)]
        cell["picks"] += 1                   # every logged pick, settled or not
        if result not in _SETTLED_RESULTS or result == "VOID":
            continue
        cell["pnl"] += _pnl(r)
        if result == "WIN":
            cell["wins"] += 1
        elif result == "LOSS":
            cell["losses"] += 1

    xl_by_league: dict[str, list] = defaultdict(list)
    xl_league_pnl: dict[str, float] = defaultdict(float)
    for (league, bt), g in xl_cells.items():
        xl_by_league[league].append((bt, g))
        xl_league_pnl[league] += g["pnl"]

    xl_rows = []
    # League order matches the League Breakdown above — P&L descending — so the
    # two sections read in the same sequence.
    for league in sorted(
        (lg for lg in xl_by_league if league_settled.get(lg, 0) > 0),
        key=lambda lg: (-xl_league_pnl[lg], lg),
    ):
        cells = sorted(xl_by_league[league],
                       key=lambda c: (-c[1]["pnl"], -c[1]["picks"], c[0]))
        for bt, g in cells:
            decided = g["wins"] + g["losses"]
            win_rate_cell = (f"{g['wins'] / decided * 100:.1f}%"
                             if decided >= _MIN_CELL_SAMPLE else "insufficient data")
            xl_rows.append([league, bt, win_rate_cell, round(g["pnl"], 2), g["picks"]])

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
    ] + bt_rows + [
        ["", ""],
        ["LEAGUE BREAKDOWN", "", "", "", "", ""],
        # 'Picks' counts every logged pick incl. pending and void, unlike the
        # bet-type table's 'Total Picks' (settled wins + losses only) — it is
        # what shows a competition is tracked but not yet in season.
        ["League", "Wins", "Losses", "Win Rate %", "Total P&L", "Picks"],
    ] + lg_rows + [
        ["", ""],
        ["BET TYPE × LEAGUE BREAKDOWN", "", "", "", ""],
        # Spelled out in the sheet so 'insufficient data' needs no explaining
        # to whoever is reading it.
        [f"Win rate shown only at {_MIN_CELL_SAMPLE}+ decided picks "
         f"(wins + losses); smaller cells read 'insufficient data'", "", "", "", ""],
        ["League", "Bet Type", "Win Rate %", "Total P&L", "Picks"],
    ] + xl_rows + [
        ["", ""],
        ["PICK TIER BREAKDOWN", "", "", "", "", ""],
        ["Core = the 5 highest-conviction picks across all competitions that day "
         "(the baseline series, and the ONLY tier feeding calibration / edge / CLV "
         "and every figure above). Extended = every other pick, up to 10 per "
         "competition per day; tracked since 13 Aug 2026. Extended was ranks 6-10 "
         "of a single 10-pick list until 15 Aug 2026, so it accrues far faster "
         "from that date and the two tiers are no longer similar in size.",
         "", "", "", "", ""],
        ["Tier", "Wins", "Losses", "Win Rate %", "Total P&L", "Picks"],
    ] + _tier_breakdown_rows(all_picks_rows)

    ws_sum = ss.worksheet("Summary")
    ws_sum.clear()
    ws_sum.update("A1", data, value_input_option="RAW")


def _tier_stats(rows: list[list[str]]) -> dict:
    """Wins/losses/win rate/P&L/pick count for a set of Picks rows."""
    def _pnl(row: list) -> float:
        try:
            return float(row[7]) if len(row) > 7 and row[7] else 0.0
        except ValueError:
            return 0.0

    settled = [r for r in rows if len(r) > 6 and r[6] in _SETTLED_RESULTS]
    wins    = [r for r in settled if r[6] == "WIN"]
    losses  = [r for r in settled if r[6] == "LOSS"]
    decided = len(wins) + len(losses)
    return {
        "picks":    len(rows),
        "settled":  len(settled),
        "wins":     len(wins),
        "losses":   len(losses),
        "win_rate": round(len(wins) / decided * 100, 1) if decided else 0.0,
        "pnl":      round(sum(_pnl(r) for r in settled), 2),
        "pnl_eur":  round(sum(_pnl(r) for r in settled) * UNIT_STAKE, 2),
    }


def _tier_breakdown_rows(all_rows: list[list[str]]) -> list[list]:
    """Summary-tab rows comparing Core vs Extended P&L and win rate."""
    header = all_rows[0] if all_rows else []
    body   = all_rows[1:]
    out = []
    tier_idx = _col(header, "Pick Tier")
    for label, subset in (
        (PICK_TIER_CORE,     _core_rows(body, header)),
        (PICK_TIER_EXTENDED, _extended_rows(body, header)),
        # Shown so the retroactive duplicate correction is visible in the sheet
        # rather than silently missing from the totals above.
        (PICK_TIER_DUPLICATE,
         [r for r in body if _row_tier(r, tier_idx) == PICK_TIER_DUPLICATE]),
    ):
        s = _tier_stats(subset)
        out.append([label, s["wins"], s["losses"], f"{s['win_rate']:.1f}%",
                    s["pnl"], s["picks"]])
    return out


def get_tier_breakdown() -> dict:
    """
    Core vs Extended performance, for the tier-comparison report.

    Reads the Picks tab directly rather than reusing any Core-filtered helper —
    this is the one reporting path that is deliberately allowed to see both
    tiers, because comparing them is its entire purpose.
    """
    try:
        rows = _picks_ws().get_all_values()
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return {}

    header, body = (rows[0] if rows else []), rows[1:]
    tier_idx = _col(header, "Pick Tier")
    return {
        PICK_TIER_CORE:     _tier_stats(_core_rows(body, header)),
        PICK_TIER_EXTENDED: _tier_stats(_extended_rows(body, header)),
        # Reported alongside the live tiers so the retroactive duplicate
        # correction stays visible instead of just missing from the totals.
        PICK_TIER_DUPLICATE: _tier_stats(
            [r for r in body if _row_tier(r, tier_idx) == PICK_TIER_DUPLICATE]
        ),
    }


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

    # Same price rule as the automatic checker: pay out at the market price
    # when one was matched, since that is what the card showed.
    odds = settlement_odds_from_row(rows[target_row - 1], rows[0] if rows else [])
    if pnl is None:
        pnl = pnl_for_result(result, odds)

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

    # CORE ONLY — drives the results card and the per-pick result notifications.
    # Extended results are reported through the tier breakdown instead, so the
    # daily result posts keep showing the same 5-pick book they always have.
    header = rows[0] if rows else []
    result = []
    for row in _core_rows(rows[1:], header):
        if not row or not row[0]:
            continue
        try:
            row_date = datetime.strptime(row[0], "%d-%b-%Y").date()
        except ValueError:
            continue
        if row_date != dt:
            continue
        # Settlement price, so the result notification quotes the same odds the
        # P&L was actually paid at (and the same figure the picks card showed).
        odds = settlement_odds_from_row(row, header)
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

def _safe_float(val: str) -> float | None:
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _weekly_row(row: list[str]) -> dict | None:
    """One Picks row as the dict shape the weekly aggregations work on."""
    if not row or not row[0]:
        return None
    try:
        dt = datetime.strptime(row[0], "%d-%b-%Y").date()
    except ValueError:
        return None
    return {
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


def _weekly_tier_stats(week_rows: list[dict]) -> dict:
    """
    Picks / wins / losses / pending / win rate / P&L for one tier's week.

    Extracted from get_weekly_data on 1 Sep 2026 so the Extended section of the
    weekly summary is computed by the SAME arithmetic as the Core section — two
    differently-derived win rates printed side by side in one message would
    invite a comparison that is not valid.

    Deliberately NOT _tier_stats(), which divides by wins+losses only. This
    divides by every settled row (VOID and the half results included) and
    honours _WIN_RATE_EXCLUDE, which is what the Core figure has always done and
    must keep doing.
    """
    settled  = [r for r in week_rows if r["result"] in _SETTLED_RESULTS]
    wins     = [r for r in settled if r["result"] == "WIN"]
    losses   = [r for r in settled if r["result"] == "LOSS"]
    pending  = [r for r in week_rows if not r["result"]]
    pnl_week = round(sum(r["pnl"] or 0 for r in settled if r["pnl"] is not None), 2)
    wr_settled = [r for r in settled if r["date"] not in _WIN_RATE_EXCLUDE]
    wr_wins    = [r for r in wr_settled if r["result"] == "WIN"]
    win_rate   = round(len(wr_wins) / len(wr_settled) * 100, 1) if wr_settled else 0.0
    best_pick  = max(wins, key=lambda r: r["pnl"] or 0) if wins else None
    return {
        "total_picks":   len(week_rows),
        "wins":          len(wins),
        "losses":        len(losses),
        "pending":       len(pending),
        "win_rate":      win_rate,
        "pnl_week":      pnl_week,
        "best_pick":     best_pick,
        "pending_picks": pending,
    }


def get_weekly_data() -> dict:
    """
    Last week performance. The top-level figures are CORE ONLY — they feed the
    weekly summary card and the update_result.py recap, and they are the
    headline series, so nothing here may fold Extended into them.

    Extended is reported SEPARATELY under the "extended" key (added 1 Sep 2026),
    computed from the same sheet read and the same arithmetic. Consumers that
    predate it — generate_weekly_card, update_result.py — read named Core keys
    through .get(), so the extra key is inert for them and the card stays
    Core-only.

    ONE sheet read serves both tiers. Do not add a second read for Extended: the
    whole Picks tab is one full read and the quota is 60/minute across the
    spreadsheet (see the 20-29 Aug 2026 data loss).
    """
    try:
        ws = _picks_ws()
        sheet_rows = with_sheets_retry("read (weekly data)", ws.get_all_values)
    except Exception as exc:
        log.error("Sheets read failed: %s", exc)
        return {}

    header = sheet_rows[0] if sheet_rows else []
    body   = sheet_rows[1:]
    core_src = _core_rows(body, header)
    ext_src  = _extended_rows(body, header)

    today    = date.today()
    week_mon = today - timedelta(days=today.weekday() + 7)
    week_sun = week_mon + timedelta(days=6)

    def _split(src: list[list[str]]) -> tuple[list[dict], list[dict]]:
        parsed = [x for x in (_weekly_row(r) for r in src) if x is not None]
        return parsed, [r for r in parsed if week_mon <= r["date"] <= week_sun]

    core_all, core_week = _split(core_src)
    _, ext_week         = _split(ext_src)

    core = _weekly_tier_stats(core_week)
    ext  = _weekly_tier_stats(ext_week)

    # Core only: the running-total and bankroll columns are never written for an
    # Extended row (_recalculate_running_total skips them), so there is no
    # Extended equivalent to read and inventing one would imply a second book.
    running_total = next(
        (r["running"] for r in reversed(core_all) if r.get("running") is not None), 0.0
    )

    return {
        "week_start":    week_mon.strftime("%d %b"),
        "week_end":      week_sun.strftime("%d %b %Y"),
        "total_picks":   core["total_picks"],
        "wins":          core["wins"],
        "losses":        core["losses"],
        "pending":       core["pending"],
        "win_rate":      core["win_rate"],
        "pnl_week":      core["pnl_week"],
        "best_pick":     core["best_pick"],
        "running_total": running_total or 0.0,
        "pending_picks": core["pending_picks"],
        # Reported alongside Core, never merged into it.
        "extended":      ext,
    }


def get_bet_type_breakdown() -> list[dict]:
    """
    Settled CORE picks grouped by bet type, sorted by win rate descending.
    Core-only: this feeds the weekly card, which reports the baseline book.
    """
    try:
        ws = _picks_ws()
        all_rows = ws.get_all_values()
        rows = _core_rows(all_rows[1:], all_rows[0] if all_rows else [])
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

def calculate_kelly_stake(
    bet_type: str,
    odds: float,
    confidence: str,
    breakdown: list[dict] | None = None,
) -> dict:
    """
    Return {"stake": euros, "note": str} for a half-Kelly recommendation.

    Uses historical win rate for this bet type from settled Sheets data.
    Stake is based on REAL_BANKROLL, capped at 5%.
    Returns a flat UNIT_STAKE with note="insufficient data" when fewer than
    10 settled picks exist for this bet type.

    PASS `breakdown` WHEN SIZING MORE THAN ONE PICK. It is the result of
    get_bet_type_breakdown(), which reads the ENTIRE Picks tab — one Sheets
    read per call. Left to fetch its own, this function costs one full-sheet
    read per pick, and callers size every pick of a run in a loop.

    That is not a theoretical cost. Google allows 60 reads per minute per
    service account across the whole spreadsheet; once the per-league cap
    (15 Aug 2026) took a slate to 20-30 picks, the loop alone exhausted the
    minute and the batch sheet write that ran straight afterwards took the
    429 — silently. Six slates were lost that way (20/22/26/27/28/29 Aug 2026,
    128 picks) before the cause was found. One read per RUN, not per pick.
    """
    if breakdown is None:
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
