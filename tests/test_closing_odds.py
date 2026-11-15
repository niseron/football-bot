"""
Regression suite for closing-odds event matching. Pure: no network, no Sheets.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

Until 16 Nov 2026 `closing_odds.run_closing_odds_check` chose a pick's Odds API
event with `next(e for e in events if _team_match(home) and _team_match(away))`,
which takes the FIRST event that passes the permissive recall filter. That is
the pattern CLAUDE.md bans after the 23 Aug 2026 Club Brugge mispricing.
`_normalize_team("Club Brugge")` is bare "brugge", a substring of
"cercle brugge ksv", so for the derby pick "Club Brugge vs Cercle Brugge" the
RETURN fixture (Cercle at home) passes on both sides too. When the API listed
it first, the closing price was read off the wrong match: here Club Brugge's
AWAY price, 1.70, instead of its home price, 1.30. CLV is scored on that number.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import closing_odds as co  # noqa: E402
import main as M  # noqa: E402

LEAGUE = "Jupiler Pro League"


def _event(home: str, away: str, prices: dict[str, float]) -> dict:
    """One Odds API event in the live response shape, one bookmaker, h2h only."""
    return {
        "home_team": home, "away_team": away,
        "bookmakers": [{"key": "book", "markets": [{"key": "h2h", "outcomes": [
            {"name": n, "price": p} for n, p in prices.items()]}]}],
    }


# Derby fixture and its return leg, both listed by the API. Same h2h names,
# different prices: Club Brugge is the 1.30 favourite at home, 1.70 away.
CLUB_HOME = _event("Club Brugge", "Cercle Brugge KSV",
                   {"Club Brugge": 1.30, "Cercle Brugge KSV": 9.00, "Draw": 5.50})
CERCLE_HOME = _event("Cercle Brugge KSV", "Club Brugge",
                     {"Cercle Brugge KSV": 4.50, "Club Brugge": 1.70, "Draw": 3.90})


def _pick(match: str, pick: str) -> dict:
    kickoff = datetime.now(timezone.utc) + timedelta(minutes=30)   # inside the 5-65 min window
    return {"sheet_row": 900, "match": match, "bet_type": "Match Winner", "pick": pick,
            "league": LEAGUE, "kickoff_utc": kickoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")}


class ClosingOddsUseTheRankedMatcher(unittest.TestCase):
    def setUp(self):
        mock.patch.object(co, "_request_counts", {}).start()
        mock.patch("usage_tracker.odds_budget_exhausted", return_value=False).start()
        mock.patch.object(requests, "get",
                          side_effect=AssertionError("unexpected network call")).start()
        self.addCleanup(mock.patch.stopall)

    def _run(self, pick: dict, events: list[dict]) -> mock.Mock:
        writer = mock.Mock()
        with mock.patch.object(co, "_fetch_odds_events", return_value=events) as fetch:
            co.run_closing_odds_check(picks_source=lambda: [pick], odds_writer=writer)
        fetch.assert_called_once_with(M.ODDS_API_SPORT_KEYS[LEAGUE], job="closing-odds")
        return writer

    def test_the_premise_both_legs_pass_the_recall_filter(self):
        # Negative control: if this stopped holding, the tests below would pass
        # on first-match code too and prove nothing.
        for e in (CLUB_HOME, CERCLE_HOME):
            self.assertTrue(M._team_match(e["home_team"], "Club Brugge"))
            self.assertTrue(M._team_match(e["away_team"], "Cercle Brugge"))
        # The old code's choice with the return leg listed first: the WRONG fixture.
        first = next(e for e in [CERCLE_HOME, CLUB_HOME]
                     if M._team_match(e["home_team"], "Club Brugge")
                     and M._team_match(e["away_team"], "Cercle Brugge"))
        self.assertIs(first, CERCLE_HOME)

    def test_the_return_leg_listed_first_does_not_price_the_derby(self):
        writer = self._run(_pick("Club Brugge vs Cercle Brugge", "Club Brugge Win"),
                           [CERCLE_HOME, CLUB_HOME])
        writer.assert_called_once_with(900, 1.30)      # first-match wrote 1.70

    def test_listing_order_does_not_matter(self):
        writer = self._run(_pick("Club Brugge vs Cercle Brugge", "Club Brugge Win"),
                           [CLUB_HOME, CERCLE_HOME])
        writer.assert_called_once_with(900, 1.30)

    def test_the_return_leg_pick_gets_the_return_leg(self):
        writer = self._run(_pick("Cercle Brugge vs Club Brugge", "Club Brugge Win"),
                           [CLUB_HOME, CERCLE_HOME])
        writer.assert_called_once_with(900, 1.70)

    def test_an_ambiguous_fixture_writes_no_closing_price(self):
        # Two listings indistinguishable by name: refusing beats guessing.
        twin = _event("Club Brugge", "Cercle Brugge KSV",
                      {"Club Brugge": 1.45, "Cercle Brugge KSV": 7.00, "Draw": 4.80})
        with self.assertLogs(M.log, level="WARNING"):
            writer = self._run(_pick("Club Brugge vs Cercle Brugge", "Club Brugge Win"),
                               [CLUB_HOME, twin])
        writer.assert_not_called()

    def test_no_matching_fixture_writes_nothing(self):
        other = _event("Anderlecht", "Genk", {"Anderlecht": 2.1, "Genk": 3.2, "Draw": 3.4})
        writer = self._run(_pick("Club Brugge vs Cercle Brugge", "Club Brugge Win"), [other])
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
