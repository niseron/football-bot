"""
One-off Picks-tab corrections, 17 Nov 2026. DRY RUN unless --apply is passed.
Importing this file does nothing (run only as a script).

  --tag-578    Row 578 'Lech Poznan vs Bayer Leverkusen' (Extended, 15 Oct 2026)
               is the same fixture and bet as row 559 'Lech Poznań vs Bayer
               Leverkusen' (Core, 14 Oct), published twice because the duplicate
               guard compared raw strings. Set its Pick Tier to Duplicate — the
               audit tag every metric already excludes. The row is NOT deleted.

  --settle-579 Row 579 'Hoffenheim vs Besiktas' / 'Besiktas Win' (Extended,
               15 Oct 2026) never settled because the verdict compared
               'besiktas' with 'beşiktaş'. Re-reads the final score from the
               feed with settlement's own matcher and evaluate_pick, and writes
               the result ONLY if the feed shows Hoffenheim 2-3 Beşiktaş at
               full time, paid at the row's 3.43 market price.

Each step first checks the row still holds exactly what it is meant to, and
re-reads it after writing.

    python _rows_578_579_nov17.py --tag-578 --settle-579            # dry run
    python _rows_578_579_nov17.py --tag-578 --settle-579 --apply    # write
"""
from __future__ import annotations

import sys
from datetime import date


def main(argv: list[str]) -> None:
    from env_loader import load_env
    load_env()
    import logging
    logging.basicConfig(level=logging.WARNING)

    import auto_results as ar
    from excel_tracker import (
        PICK_TIER_DUPLICATE, _get_spreadsheet, finalize_workbook,
        pnl_for_result, settlement_odds_from_row, update_row_result,
    )
    from feed_names import same_fixture

    apply = "--apply" in argv
    ws = _get_spreadsheet().worksheet("Picks")
    rows = ws.get_all_values()
    header = rows[0]
    col = {h: i for i, h in enumerate(header)}
    tier_a1 = chr(ord("A") + col["Pick Tier"])
    changed = False

    if "--tag-578" in argv:
        r559, r578 = rows[558], rows[577]
        ok = (r578[1] == "Lech Poznan vs Bayer Leverkusen"
              and same_fixture(r559[1], r578[1])
              and r559[2] == r578[2] == "Match Winner"
              and r559[3] == r578[3] == "Bayer Leverkusen Win"
              and r578[col["Pick Tier"]] == "Extended")
        print(f"row 578: {ascii(r578[1])} | {r578[3]} | tier {r578[col['Pick Tier']]} "
              f"-> {PICK_TIER_DUPLICATE} (duplicate of row 559 {ascii(r559[1])}); checks {'OK' if ok else 'FAILED'}")
        if ok and apply:
            ws.update(f"{tier_a1}578", [[PICK_TIER_DUPLICATE]])
            changed = True

    if "--settle-579" in argv:
        r = rows[578]
        ok_row = (r[1] == "Hoffenheim vs Besiktas" and r[3] == "Besiktas Win"
                  and not r[col["Result"]].strip() and r[col["Market Odds"]] == "3.43")
        m = ar._find_api_match(ar._fetch_matches(date(2026, 10, 16)), "Hoffenheim", "Besiktas")
        status = (m or {}).get("status") or {}
        hs, as_ = ((m or {}).get("home") or {}).get("score"), ((m or {}).get("away") or {}).get("score")
        an = ((m or {}).get("away") or {}).get("name", "")
        ok_feed = (m is not None and status.get("finished") and not status.get("cancelled")
                   and not status.get("awarded") and (hs, as_) == (2, 3)
                   and not (status.get("halfs") or {}).get("firstExtraHalfStarted"))
        verdict = ar.evaluate_pick("Match Winner", "Besiktas Win", "Hoffenheim", an, hs, as_) if m else None
        odds = settlement_odds_from_row(r, header)
        pnl = pnl_for_result(verdict, odds) if verdict in ("WIN", "LOSS") else None
        print(f"row 579: {r[1]} | {r[3]} | feed id {(m or {}).get('id')} "
              f"{status.get('reason', {}).get('long')} {hs}-{as_} ({ascii(an)}) -> {verdict} at {odds} "
              f"= {pnl}; row checks {'OK' if ok_row else 'FAILED'}, feed checks {'OK' if ok_feed else 'FAILED'}")
        if ok_row and ok_feed and verdict == "WIN" and abs(odds - 3.43) < 1e-9 and apply:
            update_row_result(579, verdict, pnl)
            changed = True

    if changed:
        finalize_workbook()
        after = ws.get_all_values()
        print("after: row 578 tier =", after[577][col["Pick Tier"]],
              "| row 579 result =", after[578][col["Result"]], "P&L =", after[578][col["Profit/Loss"]])
    elif apply:
        print("nothing written")
    else:
        print("DRY RUN — nothing written")


if __name__ == "__main__":
    main(sys.argv[1:])
