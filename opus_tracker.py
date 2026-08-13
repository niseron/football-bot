"""
opus_tracker.py — Google Sheets layer for the Opus 5 shadow experiment.

Owns exactly one tab, "Opus Shadow Picks", and nothing else touches it. The
columns mirror the football Picks tab (including 'Pick Tier') so the two are
read side by side, plus two SIMULATED staking columns modelled on the tennis
tab: no money is placed on an Opus pick, so every stake and bankroll figure
here is labelled SIM and must stay that way.

ISOLATION — the point of the whole experiment:
  * Nothing in this module reads or writes the football 'Picks' or 'Summary'
    tabs, and nothing in calibration.py / closing_odds.py / excel_tracker.py
    reads this one. calibration's three reports default to
    excel_tracker._picks_ws, so an Opus row cannot reach calibration, edge or
    CLV by construction — there is no code path, not merely a filter.
  * The football Core baseline (30 Jun 2026 onward) is therefore untouched by
    anything the shadow does, including a bug in it.

The readers below are shape-compatible with excel_tracker's, which is what
lets settlement reuse auto_results.run_auto_results via its
pending_source / row_writer / finalizer hooks rather than growing a second
evaluation engine. run_auto_results reads exactly six keys off a pending row —
sheet_row, date, match, bet_type, pick, odds — verified against its source
before this module was written, not assumed.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import gspread

from excel_tracker import PICK_TIER_CORE, PICK_TIER_EXTENDED, _get_spreadsheet

log = logging.getLogger(__name__)

OPUS_SHEET_NAME = "Opus Shadow Picks"

# Mirrors excel_tracker.PICKS_HEADERS (including 'Pick Tier') so the two tabs
# line up column-for-column, with the two SIM staking columns appended at the
# END. New columns must also go at the end — the row writer addresses the
# Result and Profit/Loss cells by letter.
OPUS_HEADERS = [
    "Date", "Match", "Bet Type", "Pick", "Odds",
    "Confidence", "Result", "Profit/Loss", "Running Total P&L", "Bankroll EUR (SIM)",
    "Claude Prob %", "Market Prob %",
    "League", "Kickoff UTC", "Closing Odds", "Market Odds", "Pick Tier",
    "Stake EUR (SIM)",
]

# Simulated money only. €100 start, matching the tennis experiment's scale
# rather than football's real €1500 bankroll — nothing here is staked for real,
# and sizing it like the real book would invite reading it as if it were.
OPUS_STARTING_BANKROLL = 100.0   # EUR, start of the running bankroll column
OPUS_REAL_BANKROLL     = 100.0   # EUR, base for Kelly sizing
OPUS_UNIT_STAKE        = 2.0     # EUR flat fallback (2% of bankroll)

_SETTLED_RESULTS = ("WIN", "HALF WIN", "HALF LOSS", "LOSS", "VOID")


# ── Connection ────────────────────────────────────────────────────────────────

def _opus_ws() -> gspread.Worksheet:
    """The Opus Shadow Picks worksheet, created with headers if missing."""
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(OPUS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        # Narrow deliberately: a bare `except Exception` here would read a
        # transient 429 or network blip as "tab missing" and try to create a
        # second one.
        ws = ss.add_worksheet(OPUS_SHEET_NAME, rows=2000, cols=len(OPUS_HEADERS))
        ws.update(values=[OPUS_HEADERS], range_name="A1", value_input_option="RAW")
        log.info("opus_tracker: created '%s' sheet", OPUS_SHEET_NAME)
        return ws


def init_opus_sheet() -> None:
    """Ensure the tab exists and carries every current header."""
    try:
        ws = _opus_ws()
    except Exception as exc:
        log.error("opus_tracker: cannot reach Sheets: %s", exc)
        return
    try:
        header = ws.row_values(1)
        if len(header) < len(OPUS_HEADERS):
            ws.resize(cols=len(OPUS_HEADERS))
            for idx in range(len(header), len(OPUS_HEADERS)):
                ws.update_cell(1, idx + 1, OPUS_HEADERS[idx])
            log.info("opus_tracker: added header(s) %s", OPUS_HEADERS[len(header):])
    except Exception as exc:
        log.warning("opus_tracker: header migration failed (non-fatal): %s", exc)


# ── Write a pick ──────────────────────────────────────────────────────────────

def log_opus_pick(
    match: str,
    league: str,
    bet_type: str,
    pick: str,
    odds: float,
    confidence: str = "N/A",
    pick_date: str | None = None,
    claude_prob: float | None = None,
    market_prob: float | None = None,
    kickoff_utc: str | None = None,
    market_odds: float | None = None,
    pick_tier: str = PICK_TIER_CORE,
    stake: float | None = None,
) -> None:
    """
    Append one Opus pick. Duplicate-guarded on (date, match, bet_type, pick)
    exactly like log_to_excel, so a re-run is a no-op rather than a second row.
    Never raises — a shadow experiment must not be able to break a real run.
    """
    dt = datetime.fromisoformat(pick_date) if pick_date else datetime.now()
    date_str = dt.strftime("%d-%b-%Y")
    target_date = dt.date()

    try:
        ws = _opus_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("opus_tracker: Sheets read failed: %s", exc)
        return

    # Same two guards as excel_tracker.log_to_excel — see the long comment there.
    # (a) same-day exact repeat; (b) any UNSETTLED pick already logged for this
    # MATCH, regardless of bet type. The shadow analyses the same 48-hour window
    # as production, so it has the identical exposure: a fixture kicking off
    # tomorrow appears in today's run and tomorrow's, and without (b) one match
    # would settle two shadow rows and book its P&L twice.
    header = rows[0] if rows else []
    try:
        result_idx = header.index("Result")
    except ValueError:
        result_idx = 6
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        try:
            existing = datetime.strptime(row[0], "%d-%b-%Y").date()
        except ValueError:
            continue
        if (existing == target_date and len(row) > 3
                and row[1] == match and row[2] == bet_type and row[3] == pick):
            log.info("opus_tracker: skipping duplicate '%s — %s'", match, pick)
            return
        settled = len(row) > result_idx and bool((row[result_idx] or "").strip())
        if not settled and len(row) > 1 and row[1] == match:
            log.info(
                "opus_tracker: skipping '%s — %s [%s]' — this fixture already has "
                "an UNSETTLED pick logged (%s: '%s')",
                match, pick, bet_type, row[0], row[3] if len(row) > 3 else "?",
            )
            return

    new_row = [
        date_str, match, bet_type, pick, round(float(odds), 2), confidence,
        "", "", "", "",                      # Result, P&L, Running Total, Bankroll
        round(float(claude_prob), 1) if claude_prob is not None else "",
        round(float(market_prob), 1) if market_prob is not None else "",
        league or "",
        kickoff_utc or "",
        "",                                   # Closing Odds — not polled for the shadow
        round(float(market_odds), 2) if market_odds is not None else "",
        pick_tier or PICK_TIER_CORE,
        round(float(stake), 2) if stake is not None else "",
    ]
    try:
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        log.info("opus_tracker: logged '%s — %s' [%s]", match, pick, pick_tier)
    except Exception as exc:
        log.error("opus_tracker: Sheets write failed: %s", exc)


# ── Readers used by settlement (shape-compatible with excel_tracker's) ────────

def get_pending_opus_rows(lookback_days: int = 7) -> list[dict]:
    """
    Unsettled Opus rows, in the dict shape run_auto_results expects.

    Returns exactly the six keys that function reads — sheet_row, date, match,
    bet_type, pick, odds. 'odds' is the settlement price: the matched market
    price when there is one, else Opus's own estimate, mirroring
    excel_tracker.settlement_odds_from_row.
    """
    try:
        rows = _opus_ws().get_all_values()
    except Exception as exc:
        log.error("opus_tracker: Sheets read failed: %s", exc)
        return []
    if not rows:
        return []

    header = rows[0]

    def _col(name: str) -> int | None:
        try:
            return header.index(name)
        except ValueError:
            return None

    def _f(row: list, idx: int | None) -> float | None:
        if idx is None or len(row) <= idx:
            return None
        val = (row[idx] or "").strip()
        try:
            return float(val) if val else None
        except ValueError:
            return None

    result_col = _col("Result")
    odds_col, market_col = _col("Odds"), _col("Market Odds")

    cutoff = date.today() - timedelta(days=lookback_days)
    pending: list[dict] = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0]:
            continue
        if result_col is not None and len(row) > result_col and row[result_col]:
            continue
        try:
            pick_date = datetime.strptime(row[0], "%d-%b-%Y").date()
        except ValueError:
            continue
        if pick_date < cutoff:
            continue
        market = _f(row, market_col)
        pending.append({
            "sheet_row": i,
            "date":      pick_date,
            "match":     row[1] if len(row) > 1 else "",
            "bet_type":  row[2] if len(row) > 2 else "",
            "pick":      row[3] if len(row) > 3 else "",
            "odds":      market if market and market > 1.0 else (_f(row, odds_col) or 1.0),
            # Not read by run_auto_results' settlement logic — it only uses this
            # to label the log line 'market' vs 'estimate'. Omitting it made
            # every Opus settlement claim '(estimate)' even when it paid out at
            # the market price.
            "market_odds": market if market and market > 1.0 else None,
        })
    return pending


def update_opus_row_result(sheet_row: int, result: str, pnl: float) -> bool:
    """
    Write Result + Profit/Loss for one row. Returns True on success.

    Returns a BOOL deliberately: the tennis equivalent swallows every exception
    and returns None, and its caller then counts the row as settled anyway — so
    a failed Sheets write still fires a '✅ settled' notification while the row
    stays PENDING forever (a live bug in that pipeline, 13 Aug 2026). The Opus
    caller gates on this return value instead.
    """
    try:
        ws = _opus_ws()
        ws.update(values=[[result, round(float(pnl), 2)]],
                  range_name=f"G{sheet_row}:H{sheet_row}",
                  value_input_option="USER_ENTERED")
        return True
    except Exception as exc:
        log.error("opus_tracker: result write failed for row %s: %s", sheet_row, exc)
        return False


# ── Running P&L + simulated bankroll ─────────────────────────────────────────

def recalculate_opus_running_totals() -> None:
    """
    Rebuild the Running Total P&L and SIM Bankroll columns bottom-up.

    Unlike the football tab this is tier-BLIND: every Opus pick, Core and
    Extended alike, moves the shadow's running total. The tier split exists
    here for comparability with the football tab, not to protect a baseline —
    the shadow has no baseline to protect.
    """
    try:
        ws = _opus_ws()
        rows = ws.get_all_values()
    except Exception as exc:
        log.error("opus_tracker: Sheets read failed: %s", exc)
        return
    if len(rows) < 2:
        return

    header = rows[0]
    try:
        res_i, pnl_i = header.index("Result"), header.index("Profit/Loss")
        stake_i = header.index("Stake EUR (SIM)")
    except ValueError as exc:
        log.error("opus_tracker: header missing a required column: %s", exc)
        return

    running, bankroll = 0.0, OPUS_STARTING_BANKROLL
    updates = []
    for i, row in enumerate(rows[1:], start=2):
        result = row[res_i] if len(row) > res_i else ""
        pnl_str = row[pnl_i] if len(row) > pnl_i else ""
        if result in _SETTLED_RESULTS and pnl_str:
            try:
                units = float(pnl_str)
                try:
                    stake = float(row[stake_i]) if len(row) > stake_i and row[stake_i] else OPUS_UNIT_STAKE
                except ValueError:
                    stake = OPUS_UNIT_STAKE
                running += units
                bankroll += units * stake
                updates.append({"range": f"I{i}", "values": [[round(running, 2)]]})
                updates.append({"range": f"J{i}", "values": [[round(bankroll, 2)]]})
                continue
            except ValueError:
                pass
        updates.append({"range": f"I{i}", "values": [[""]]})
        updates.append({"range": f"J{i}", "values": [[""]]})

    if updates:
        try:
            ws.batch_update(updates)
        except Exception as exc:
            log.error("opus_tracker: running-total write failed: %s", exc)


def finalize_opus_sheet() -> None:
    """Finalizer hook for run_auto_results — recalculates totals only."""
    recalculate_opus_running_totals()


# ── Reporting ────────────────────────────────────────────────────────────────

def get_opus_bet_type_breakdown() -> list[dict]:
    """Settled Opus picks grouped by bet type — feeds the SIM Kelly sizing."""
    try:
        rows = _opus_ws().get_all_values()
    except Exception as exc:
        log.error("opus_tracker: Sheets read failed: %s", exc)
        return []
    if not rows:
        return []

    header = rows[0]
    try:
        res_i, pnl_i, bt_i = (header.index("Result"), header.index("Profit/Loss"),
                              header.index("Bet Type"))
    except ValueError:
        return []

    groups: dict[str, dict] = {}
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        result = (row[res_i].strip().upper() if len(row) > res_i else "")
        bet_type = (row[bt_i].strip() if len(row) > bt_i else "")
        if result not in ("WIN", "HALF WIN", "HALF LOSS", "LOSS") or not bet_type:
            continue
        try:
            pnl = float(row[pnl_i]) if len(row) > pnl_i and row[pnl_i] else 0.0
        except ValueError:
            pnl = 0.0
        g = groups.setdefault(bet_type, {"wins": 0, "losses": 0, "pnl": 0.0})
        if result == "WIN":
            g["wins"] += 1
        elif result == "LOSS":
            g["losses"] += 1
        g["pnl"] += pnl

    out = []
    for bet_type, g in groups.items():
        total = g["wins"] + g["losses"]
        out.append({
            "bet_type": bet_type, "total": total,
            "wins": g["wins"], "losses": g["losses"],
            "win_rate": round(g["wins"] / total * 100, 1) if total else 0.0,
            "pnl": round(g["pnl"], 2),
        })
    out.sort(key=lambda b: b["win_rate"], reverse=True)
    return out


def get_opus_tier_breakdown() -> dict:
    """Core vs Extended performance within the shadow, for tier comparison."""
    try:
        rows = _opus_ws().get_all_values()
    except Exception as exc:
        log.error("opus_tracker: Sheets read failed: %s", exc)
        return {}
    if not rows:
        return {}

    header = rows[0]
    try:
        res_i, pnl_i, tier_i = (header.index("Result"), header.index("Profit/Loss"),
                                header.index("Pick Tier"))
    except ValueError:
        return {}

    def _tier(row: list) -> str:
        if len(row) <= tier_i:
            return PICK_TIER_CORE
        return (row[tier_i] or "").strip() or PICK_TIER_CORE

    out: dict[str, dict] = {}
    for label in (PICK_TIER_CORE, PICK_TIER_EXTENDED):
        subset = [r for r in rows[1:] if r and r[0] and _tier(r) == label]
        settled = [r for r in subset
                   if len(r) > res_i and r[res_i] in _SETTLED_RESULTS]
        wins = [r for r in settled if r[res_i] == "WIN"]
        losses = [r for r in settled if r[res_i] == "LOSS"]
        decided = len(wins) + len(losses)

        def _pnl(r: list) -> float:
            try:
                return float(r[pnl_i]) if len(r) > pnl_i and r[pnl_i] else 0.0
            except ValueError:
                return 0.0

        out[label] = {
            "picks": len(subset), "settled": len(settled),
            "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / decided * 100, 1) if decided else 0.0,
            "pnl": round(sum(_pnl(r) for r in settled), 2),
        }
    return out


# ── SIM staking ──────────────────────────────────────────────────────────────

def calculate_opus_kelly_stake(bet_type: str, odds: float, confidence: str) -> dict:
    """
    {"stake": euros, "note": str} — the same half-Kelly / 5%-cap logic tennis
    uses, sized against OPUS_REAL_BANKROLL (€100). SIMULATED: no Opus pick is
    ever staked for real, so callers must render this with a SIM tag.
    Flat OPUS_UNIT_STAKE below 10 settled picks for the bet type.
    """
    breakdown = get_opus_bet_type_breakdown()
    record = next(
        (b for b in breakdown if b["bet_type"].strip().lower() == bet_type.strip().lower()),
        None,
    )
    if record is None or record["total"] < 10:
        count = record["total"] if record else 0
        return {"stake": OPUS_UNIT_STAKE, "note": f"insufficient data ({count} settled picks)"}

    win_rate = record["win_rate"] / 100.0
    kelly = (win_rate * (odds - 1) - (1 - win_rate)) / (odds - 1)
    if kelly <= 0:
        return {"stake": 0.0, "note": "negative edge"}

    fraction = min(kelly * 0.5, 0.05)   # half-Kelly, capped at 5% of bankroll
    return {"stake": round(fraction * OPUS_REAL_BANKROLL, 2), "note": ""}
