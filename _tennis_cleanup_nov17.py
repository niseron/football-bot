"""
One-off tennis Sheet cleanup, 17 Nov 2026. DRY RUN unless --apply is passed.
Importing this file does nothing (run only as a script).

  --stuck    Pending rows older than the settlement lookback (stranded: the
             automatic checker never revisits them). Settle each one whose
             result is CERTAIN from the feed (both players' ids known, the
             match found in a player's past-matches feed on the right day, a
             score that evaluates); VOID the rest with a Settlement Note saying
             it was voided by review, not by the match.
             Rows with no stored ids borrow them from other rows of the tab
             where the same exact player name was logged with an id.

  --reprice  Settled WIN rows that carried a market price but were paid at
             the model's estimate (11 Jul - 6 Aug 2026): rewrite the P&L at the
             market price and note the old figure. LOSS/VOID rows do not change.

    python _tennis_cleanup_nov17.py --stuck --reprice            # dry run
    python _tennis_cleanup_nov17.py --stuck --reprice --apply    # write
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta

log = logging.getLogger("tennis_cleanup")

TODAY = date(2026, 11, 17)


def _id_map(rows: list[list[str]], ids_col: int) -> dict[str, tuple[str, int]]:
    """Exact player name -> (tour, id), from every row that logged ids."""
    out: dict[str, tuple[str, int]] = {}
    for r in rows:
        ids = r[ids_col] if len(r) > ids_col else ""
        if ":" not in ids or " vs " not in (r[1] if len(r) > 1 else ""):
            continue
        try:
            tour, pair = ids.split(":", 1)
            i1, i2 = (int(x) for x in pair.split("|", 1))
        except ValueError:
            continue
        n1, n2 = (s.strip() for s in r[1].split(" vs ", 1))
        out.setdefault(n1, (tour, i1))
        out.setdefault(n2, (tour, i2))
    return out


def main(argv: list[str]) -> None:
    from env_loader import load_env
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    import tennis_auto_results as tar
    from tennis_excel_tracker import (
        TENNIS_HEADERS, TENNIS_SHEET_NAME, _col_letter, _tennis_ws,
        finalize_tennis_workbook, init_tennis_sheet, settlement_odds, settlement_pnl,
    )

    apply = "--apply" in argv
    if apply:
        init_tennis_sheet()  # adds the 'Market Odds' / 'Settlement Note' headers
    ws = _tennis_ws()
    rows = ws.get_all_values()
    body = rows[1:]
    H = {h: TENNIS_HEADERS.index(h) for h in TENNIS_HEADERS}
    note_a1 = _col_letter(H["Settlement Note"] + 1)
    cell = lambda r, k: (r[H[k]].strip() if len(r) > H[k] else "")
    updates: list[dict] = []
    report: list[str] = []

    def price_of(r):
        return settlement_odds(cell(r, "Odds"), cell(r, "Market Odds"), cell(r, "Market Prob %"))

    if "--stuck" in argv:
        ids_by_name = _id_map(body, H["Player IDs"])
        cutoff = TODAY - timedelta(days=tar.LOOKBACK_DAYS)
        tar._past_matches_cache.clear()
        for i, r in enumerate(body, start=2):
            if not r or not r[0] or cell(r, "Result"):
                continue
            d = datetime.strptime(r[0], "%d-%b-%Y").date()
            if d >= cutoff:
                continue  # still inside the automatic window
            match, bet, pick = cell(r, "Match"), cell(r, "Bet Type"), cell(r, "Pick")
            p = {"date": d, "start_utc": cell(r, "Kickoff/Start Time")}
            ids = tar._parse_player_ids(cell(r, "Player IDs"))
            source = "stored ids"
            if ids is None and " vs " in match:
                n1, n2 = (s.strip() for s in match.split(" vs ", 1))
                a, b = ids_by_name.get(n1), ids_by_name.get(n2)
                if a and b and a[0] == b[0]:
                    ids, source = (a[0], a[1], b[1]), "ids borrowed from other rows"
            past = None
            if ids is not None:
                dates = {x.isoformat() for x in tar._candidate_dates(p)}
                past = tar._find_past_match_by_ids(ids[0], ids[1], ids[2], dates)
            result, why = None, ""
            if past is not None:
                res_str = (past.get("result") or "").strip()
                rtype = str(past.get("result_type") or "").strip().lower()
                n1 = (past.get("player1") or {}).get("name", "")
                n2 = (past.get("player2") or {}).get("name", "")
                if rtype and rtype != "completed":
                    result, why = "VOID", f"{rtype} ({res_str or 'no score'}) — from feed"
                elif res_str:
                    ev = tar.evaluate_tennis_pick(bet, pick, n1, n2, res_str)
                    if ev in ("WIN", "LOSS", "VOID"):
                        result, why = ev, f"{n1} vs {n2} {res_str} — from feed"
            if result is None:
                reason = ("no player ids for both names" if ids is None else
                          "match no longer in either player's last-10 past-matches feed"
                          if past is None else
                          "score found but the bet could not be evaluated from it")
                note = (f"VOID by review 17 Nov 2026 — result not recoverable ({reason}); "
                        f"stranded past the {tar.LOOKBACK_DAYS}-day settlement window. "
                        f"Not a real void.")
                result, pnl = "VOID", 0.0
                report.append(f"row {i:3d} {r[0]} {match} | {pick} -> VOID (unrecoverable: {reason})")
            else:
                price, basis = price_of(r)
                pnl = settlement_pnl(result, price)
                note = (f"Settled by review 17 Nov 2026 ({source}): {why}; "
                        f"paid at {price:.2f} ({basis}).")
                report.append(f"row {i:3d} {r[0]} {match} | {pick} -> {result} {pnl:+.2f} "
                              f"({why}; {source}; {basis} {price:.2f})")
            updates += [
                {"range": f"G{i}", "values": [[result]]},
                {"range": f"H{i}", "values": [[round(pnl, 2)]]},
                {"range": f"{note_a1}{i}", "values": [[note]]},
            ]

    if "--reprice" in argv:
        for i, r in enumerate(body, start=2):
            if cell(r, "Result") != "WIN":
                continue
            price, basis = price_of(r)
            if basis == "estimate":
                continue
            new = settlement_pnl("WIN", price)
            try:
                old = float(cell(r, "P&L"))
            except ValueError:
                old = None
            if old is not None and abs(old - new) < 0.005:
                continue
            note = (f"Re-priced 17 Nov 2026: paid at market {price:.2f} ({basis}); "
                    f"was {old:+.2f} at the {cell(r, 'Odds')} estimate.")
            report.append(f"row {i:3d} {r[0]} {cell(r, 'Match')} | {cell(r, 'Pick')} -> "
                          f"P&L {old:+.2f} -> {new:+.2f} (market {price:.2f})")
            updates += [
                {"range": f"H{i}", "values": [[new]]},
                {"range": f"{note_a1}{i}", "values": [[note]]},
            ]

    print("\n".join(report) or "nothing to do")
    print(f"\n{len(report)} row(s); {'APPLYING' if apply else 'DRY RUN — nothing written'}")
    if apply and updates:
        ws.batch_update(updates)
        finalize_tennis_workbook()
        print(f"Written to '{TENNIS_SHEET_NAME}' and the summary rebuilt.")


if __name__ == "__main__":
    main(sys.argv[1:])
