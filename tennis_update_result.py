"""
CLI to manually settle a TENNIS pick in the 'Tennis Picks' Google Sheet tab.

Tennis results are currently settled manually (no tennis auto-results job yet).

Usage:
    python tennis_update_result.py "Sinner vs Alcaraz" "Match Winner" WIN
    python tennis_update_result.py "Swiatek vs Gauff" "Over 21.5 Games" LOSS
    python tennis_update_result.py "Rybakina vs Sabalenka" "Set Betting" VOID

Arguments:
    match   — full or partial match name (case-insensitive)
    pick    — full or partial pick / bet type (case-insensitive)
    result  — WIN | LOSS | VOID
"""
import sys

from dotenv import load_dotenv

from tennis_excel_tracker import update_tennis_result


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    load_dotenv()

    match_query = sys.argv[1].strip()
    pick_query  = sys.argv[2].strip()
    result      = sys.argv[3].strip().upper()

    if result not in ("WIN", "LOSS", "VOID"):
        print(f"Error: result must be WIN, LOSS, or VOID — got '{result}'")
        sys.exit(1)

    print(f"\nSearching for:  '{match_query}'  |  '{pick_query}'  |  {result}")
    print("-" * 60)

    if not update_tennis_result(match_query, pick_query, result):
        print("\nNo matching pending tennis pick found.")
        print("Tip: check the match name and pick exactly as logged in the Tennis Picks tab.")
        sys.exit(1)


if __name__ == "__main__":
    main()
