"""
Backtest the analysis logic against API Football historical data (2023-2024 season).
Outputs results to backtest_results.csv.

Usage:
    python backtest.py
    python backtest.py --league 39          # Premier League only
    python backtest.py --matchday 1 38      # matchdays 1-38
"""

import argparse
import csv
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = "api-football-v1.p.rapidapi.com"
HEADERS = {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": RAPIDAPI_KEY}

LEAGUES = {
    "Premier League": 39,
    "Jupiler Pro League": 144,
}

SEASON = 2023
OUTPUT_CSV = Path(__file__).parent / "backtest_results.csv"

# Import analysis helpers from main (Claude call is reused as-is)
from main import analyse_with_claude, build_fixture_summary


# ── API helpers ───────────────────────────────────────────────────────────────

def fetch_fixtures_by_round(league_id: int, season: int, round_str: str) -> list[dict]:
    url = f"https://{RAPIDAPI_HOST}/v3/fixtures"
    params = {"league": league_id, "season": season, "round": round_str}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("response", [])


def fetch_rounds(league_id: int, season: int) -> list[str]:
    url = f"https://{RAPIDAPI_HOST}/v3/fixtures/rounds"
    params = {"league": league_id, "season": season}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("response", [])


def fetch_odds(fixture_id: int) -> list[dict]:
    url = f"https://{RAPIDAPI_HOST}/v3/odds"
    params = {"fixture": fixture_id, "bookmaker": 6, "season": SEASON}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("response", [])


def get_actual_result(fixture: dict) -> dict:
    goals = fixture.get("goals", {})
    home_goals = goals.get("home", 0) or 0
    away_goals = goals.get("away", 0) or 0
    total_goals = home_goals + away_goals

    if home_goals > away_goals:
        match_winner = fixture["teams"]["home"]["name"]
    elif away_goals > home_goals:
        match_winner = fixture["teams"]["away"]["name"]
    else:
        match_winner = "Draw"

    return {
        "home_goals": home_goals,
        "away_goals": away_goals,
        "total_goals": total_goals,
        "match_winner": match_winner,
        "btts": home_goals > 0 and away_goals > 0,
        "over_2_5": total_goals > 2,
        "over_3_5": total_goals > 3,
    }


def evaluate_pick(pick: dict, result: dict) -> str:
    """Return WIN, LOSS, or VOID."""
    bet_type = pick["bet_type"].lower()
    selection = pick["pick"].lower()

    if "match winner" in bet_type or "1x2" in bet_type:
        winner = result["match_winner"].lower()
        if selection in ("home", "1") and winner == pick.get("home", "").lower():
            return "WIN"
        if selection in ("away", "2") and winner == pick.get("away", "").lower():
            return "WIN"
        if selection in ("draw", "x") and winner == "draw":
            return "WIN"
        return "LOSS"

    if "both teams to score" in bet_type or "btts" in bet_type:
        expected = selection in ("yes", "true", "1")
        return "WIN" if result["btts"] == expected else "LOSS"

    if "over 2.5" in bet_type or "over/under 2.5" in bet_type:
        if "over" in selection:
            return "WIN" if result["over_2_5"] else "LOSS"
        return "WIN" if not result["over_2_5"] else "LOSS"

    if "over 3.5" in bet_type or "over/under 3.5" in bet_type:
        if "over" in selection:
            return "WIN" if result["over_3_5"] else "LOSS"
        return "WIN" if not result["over_3_5"] else "LOSS"

    log.warning("Unknown bet type for evaluation: %s", bet_type)
    return "VOID"


# ── Backtest runner ───────────────────────────────────────────────────────────

def run_backtest(league_filter: int | None = None, matchday_range: tuple[int, int] | None = None):
    rows = []

    for league_name, league_id in LEAGUES.items():
        if league_filter and league_id != league_filter:
            continue

        log.info("Backtesting %s (season %d)", league_name, SEASON)

        try:
            rounds = fetch_rounds(league_id, SEASON)
        except Exception as exc:
            log.error("Failed to fetch rounds: %s", exc)
            continue

        # Filter to numbered matchdays only
        numbered = [r for r in rounds if "Regular Season" in r]
        if matchday_range:
            lo, hi = matchday_range
            numbered = [r for r in numbered if lo <= _round_number(r) <= hi]

        for round_str in numbered:
            log.info("  Round: %s", round_str)
            try:
                fixtures = fetch_fixtures_by_round(league_id, SEASON, round_str)
                time.sleep(0.5)  # respect rate limit
            except Exception as exc:
                log.warning("  Skipping round %s: %s", round_str, exc)
                continue

            # Only use finished matches
            finished = [f for f in fixtures if f["fixture"]["status"]["short"] == "FT"]
            if not finished:
                continue

            summaries = []
            for fixture in finished:
                fid = fixture["fixture"]["id"]
                try:
                    odds_data = fetch_odds(fid)
                    time.sleep(0.3)
                except Exception:
                    odds_data = []
                summary = build_fixture_summary(fixture, odds_data)
                # Attach team names so evaluate_pick can resolve "Home"/"Away"
                summary["home"] = fixture["teams"]["home"]["name"]
                summary["away"] = fixture["teams"]["away"]["name"]
                summaries.append(summary)

            if not summaries:
                continue

            try:
                picks = analyse_with_claude({league_name: summaries})
            except Exception as exc:
                log.error("  Claude failed for %s %s: %s", league_name, round_str, exc)
                continue

            # Map fixture results by match string
            result_map = {}
            for fixture in finished:
                key = f"{fixture['teams']['home']['name']} vs {fixture['teams']['away']['name']}"
                result_map[key] = get_actual_result(fixture)

            for pick in picks:
                match_key = pick.get("match", "")
                actual = result_map.get(match_key)
                if actual is None:
                    outcome = "VOID"
                    profit = 0.0
                else:
                    outcome = evaluate_pick(pick, actual)
                    profit = float(pick["odds"]) - 1 if outcome == "WIN" else (-1 if outcome == "LOSS" else 0)

                rows.append({
                    "date": round_str,
                    "league": league_name,
                    "match": match_key,
                    "bet_type": pick.get("bet_type"),
                    "pick": pick.get("pick"),
                    "odds": pick.get("odds"),
                    "confidence": pick.get("confidence"),
                    "result": outcome,
                    "profit": round(profit, 2),
                })

            log.info("  %d picks evaluated for %s", len(picks), round_str)
            time.sleep(1)

    return rows


def _round_number(round_str: str) -> int:
    try:
        return int(round_str.split("-")[-1].strip())
    except (ValueError, IndexError):
        return 0


def write_csv(rows: list[dict]):
    if not rows:
        log.warning("No rows to write")
        return
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    log.info("Results written to %s (%d rows)", OUTPUT_CSV, len(rows))


def print_summary(rows: list[dict]):
    settled = [r for r in rows if r["result"] != "VOID"]
    wins = [r for r in settled if r["result"] == "WIN"]
    total_profit = sum(r["profit"] for r in settled)
    win_rate = len(wins) / len(settled) * 100 if settled else 0

    print("\n=== Backtest Summary ===")
    print(f"Total picks   : {len(rows)}")
    print(f"Settled       : {len(settled)}")
    print(f"Wins          : {len(wins)}")
    print(f"Win rate      : {win_rate:.1f}%")
    print(f"Total profit  : {total_profit:+.2f} units (1 unit stake per pick)")
    print(f"Output CSV    : {OUTPUT_CSV}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest football betting bot (2023-24 season)")
    parser.add_argument("--league", type=int, help="Filter to a single league ID (39 or 144)")
    parser.add_argument("--matchday", type=int, nargs=2, metavar=("FROM", "TO"),
                        help="Matchday range, e.g. --matchday 1 10")
    args = parser.parse_args()

    md_range = tuple(args.matchday) if args.matchday else None
    rows = run_backtest(league_filter=args.league, matchday_range=md_range)
    write_csv(rows)
    print_summary(rows)
