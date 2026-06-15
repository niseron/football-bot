"""
CLI to update a pick's result in picks_tracker.xlsx.

Usage:
    python update_result.py "Brazil vs Morocco" "BTTS No" WIN
    python update_result.py "Germany vs Curacao" "Asian Handicap" LOSS
    python update_result.py "Haiti vs Scotland" "Scotland" VOID

Arguments:
    match   — full or partial match name (case-insensitive)
    pick    — full or partial pick / bet type (case-insensitive)
    result  — WIN | LOSS | VOID
"""
import sys
from excel_tracker import update_result, get_weekly_data, EXCEL_PATH


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    match_query = sys.argv[1].strip()
    pick_query  = sys.argv[2].strip()
    result      = sys.argv[3].strip().upper()

    if result not in ("WIN", "LOSS", "VOID"):
        print(f"Error: result must be WIN, LOSS, or VOID — got '{result}'")
        sys.exit(1)

    print(f"\nSearching for:  '{match_query}'  |  '{pick_query}'  |  {result}")
    print("-" * 60)

    ok = update_result(match_query, pick_query, result)

    if ok:
        stats = get_weekly_data()
        if stats:
            pnl = stats["pnl_week"]
            sign = "+" if pnl >= 0 else ""
            print(f"\nThis week     : {stats['wins']}W / {stats['losses']}L / {stats['pending']} pending")
            print(f"Week P&L      : {sign}{pnl:.2f} units")
            print(f"Running total : {stats['running_total']:+.2f} units")
        print(f"\nSaved to      : {EXCEL_PATH}")
    else:
        print("\nNo matching pending pick found.")
        print("Tip: check the match name and pick exactly as logged.")
        sys.exit(1)


if __name__ == "__main__":
    main()
