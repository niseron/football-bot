"""
Regression suite for the Summary tab layout: the Core/Extended split of the
bet-type and league breakdowns (20 Sep 2026). Pure — no Sheets, no network.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

Pins three things. The two table builders are tier-agnostic and identical for
both tiers (VOIDs skipped, half results in P&L only, the sort orders). The
headline figures and the first table of each pair read Core rows only — a
blank tier is Core, Duplicate is neither. And the Extended tables read
Extended rows only, sit directly under their Core twin, and label their P&L
as unstaked units so a reader cannot sum a paper book into a real one.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import excel_tracker as et  # noqa: E402

H = list(et.PICKS_HEADERS)
_I = {name: i for i, name in enumerate(H)}


def _row(*, match="A vs B", bet_type="Match Winner", result="", pnl="",
         league="Premier League", tier="", confidence="High", date="19-Sep-2026"):
    r = [""] * len(H)
    r[_I["Date"]] = date
    r[_I["Match"]] = match
    r[_I["Bet Type"]] = bet_type
    r[_I["Pick"]] = "Home Win"
    r[_I["Odds"]] = "1.80"
    r[_I["Confidence"]] = confidence
    r[_I["Result"]] = result
    r[_I["Profit/Loss"]] = pnl
    r[_I["League"]] = league
    r[_I["Pick Tier"]] = tier
    return r


def _section(data: list[list], header: str) -> list[list]:
    """Rows of one Summary section: from its header row to the next blank row."""
    start = next(i for i, r in enumerate(data) if r and r[0] == header)
    out = []
    for r in data[start:]:
        if r == ["", ""]:
            break
        out.append(r)
    return out


class BetTypeBreakdownRows(unittest.TestCase):

    def test_void_is_skipped_and_half_results_count_only_in_pnl(self):
        rows = [
            _row(bet_type="Over/Under", result="WIN", pnl="0.8"),
            _row(bet_type="Over/Under", result="VOID", pnl="0"),
            _row(bet_type="Asian Handicap", result="HALF WIN", pnl="0.4"),
            _row(bet_type="Asian Handicap", result="LOSS", pnl="-1"),
            _row(bet_type="BTTS", result=""),                 # pending
            _row(bet_type="", result="WIN", pnl="0.5"),       # no bet type
        ]
        out = et._bet_type_breakdown_rows(rows)
        self.assertEqual(out, [
            ["Over/Under", 1, 0, "100.0%", 0.8, 1],
            ["Asian Handicap", 0, 1, "0.0%", -0.6, 1],
        ])

    def test_sorted_by_win_rate_descending(self):
        rows = [
            _row(bet_type="X", result="LOSS", pnl="-1"),
            _row(bet_type="Y", result="WIN", pnl="0.7"),
            _row(bet_type="Y", result="LOSS", pnl="-1"),
            _row(bet_type="Z", result="WIN", pnl="0.9"),
        ]
        self.assertEqual([r[0] for r in et._bet_type_breakdown_rows(rows)], ["Z", "Y", "X"])

    def test_it_is_tier_blind(self):
        # The helper never looks at the tier column: filtering is the caller's job.
        rows = [_row(bet_type="X", result="WIN", pnl="0.8", tier="Extended"),
                _row(bet_type="X", result="WIN", pnl="0.8", tier="Core")]
        self.assertEqual(et._bet_type_breakdown_rows(rows), [["X", 2, 0, "100.0%", 1.6, 2]])


class LeagueBreakdownRows(unittest.TestCase):

    def test_tracked_leagues_always_present_and_extra_ones_appended(self):
        rows = [_row(league="Conference League", result="WIN", pnl="0.9"),
                _row(league="", result="LOSS", pnl="-1")]
        out = et._league_breakdown_rows(rows)
        names = [r[0] for r in out]
        for name in et.TRACKED_LEAGUES:
            self.assertIn(name, names)
        self.assertIn("Conference League", names)
        self.assertIn(et._NO_LEAGUE, names)
        self.assertEqual(out[0], ["Conference League", 1, 0, "100.0%", 0.9, 1])

    def test_picks_counts_pending_and_sort_is_pnl_then_picks_then_name(self):
        rows = [
            _row(league="La Liga", result="WIN", pnl="0.5"),
            _row(league="La Liga", result=""),                      # pending
            _row(league="Serie A", result="WIN", pnl="0.5"),
            _row(league="Ligue 1", result="HALF LOSS", pnl="-0.5"),
        ]
        out = et._league_breakdown_rows(rows)
        self.assertEqual(out[0], ["La Liga", 1, 0, "100.0%", 0.5, 2])   # same P&L, more picks
        self.assertEqual(out[1], ["Serie A", 1, 0, "100.0%", 0.5, 1])
        self.assertEqual(out[-1], ["Ligue 1", 0, 0, "0.0%", -0.5, 1])  # half loss: P&L only
        zeros = [r[0] for r in out if r[4] == 0 and r[5] == 0]
        self.assertEqual(zeros, sorted(zeros))                          # stable, by name


class SummaryLayout(unittest.TestCase):

    CORE_BLANK = _row(match="C1 vs X", bet_type="Match Winner", result="WIN", pnl="0.8",
                      league="Premier League", tier="")
    CORE       = _row(match="C2 vs X", bet_type="Over/Under", result="LOSS", pnl="-1",
                      league="La Liga", tier="Core")
    EXT_1      = _row(match="E1 vs X", bet_type="Over/Under", result="LOSS", pnl="-1",
                      league="Serie A", tier="Extended")
    EXT_2      = _row(match="E2 vs X", bet_type="BTTS", result="WIN", pnl="0.7",
                      league="Serie A", tier="Extended")
    EXT_PEND   = _row(match="E3 vs X", bet_type="BTTS", result="",
                      league="Bundesliga", tier="Extended")
    DUP        = _row(match="D1 vs X", bet_type="BTTS", result="WIN", pnl="9.9",
                      league="Ligue 1", tier="Duplicate")

    def setUp(self):
        self.data = et._summary_data(
            [H, self.CORE_BLANK, self.CORE, self.EXT_1, self.EXT_2, self.EXT_PEND, self.DUP]
        )

    def _table(self, header):
        sec = _section(self.data, header)
        # header row, note row, column-header row, then the data rows
        return sec[1], sec[2], sec[3:]

    def test_sections_sit_in_order_each_extended_under_its_core_twin(self):
        headers = [r[0] for r in self.data if r and r[0] in (
            "BET TYPE BREAKDOWN", "EXTENDED BET TYPE BREAKDOWN",
            "LEAGUE BREAKDOWN", "EXTENDED LEAGUE BREAKDOWN",
            "BET TYPE × LEAGUE BREAKDOWN", "PICK TIER BREAKDOWN",
        )]
        self.assertEqual(headers, [
            "BET TYPE BREAKDOWN", "EXTENDED BET TYPE BREAKDOWN",
            "LEAGUE BREAKDOWN", "EXTENDED LEAGUE BREAKDOWN",
            "BET TYPE × LEAGUE BREAKDOWN", "PICK TIER BREAKDOWN",
        ])

    def test_core_bet_type_table_is_core_only_and_unchanged_in_shape(self):
        note, cols, body = self._table("BET TYPE BREAKDOWN")
        self.assertEqual(note[0], et._CORE_SECTION_NOTE)
        self.assertEqual(cols, ["Bet Type", "Wins", "Losses", "Win Rate %", "Total P&L", "Total Picks"])
        self.assertEqual(body, et._bet_type_breakdown_rows([self.CORE_BLANK, self.CORE]))
        self.assertEqual(body, [["Match Winner", 1, 0, "100.0%", 0.8, 1],
                                ["Over/Under", 0, 1, "0.0%", -1.0, 1]])
        self.assertNotIn("BTTS", [r[0] for r in body])          # Extended + Duplicate only

    def test_extended_bet_type_table_is_extended_only_with_unstaked_label(self):
        note, cols, body = self._table("EXTENDED BET TYPE BREAKDOWN")
        self.assertEqual(note[0], et._EXTENDED_SECTION_NOTE)
        self.assertIn("NO stake", note[0])
        self.assertEqual(cols, ["Bet Type", "Wins", "Losses", "Win Rate %",
                                "Total P&L (unstaked units)", "Total Picks"])
        self.assertEqual(body, et._bet_type_breakdown_rows([self.EXT_1, self.EXT_2, self.EXT_PEND]))
        self.assertEqual(body, [["BTTS", 1, 0, "100.0%", 0.7, 1],
                                ["Over/Under", 0, 1, "0.0%", -1.0, 1]])
        self.assertNotIn("Match Winner", [r[0] for r in body])  # Core only
        self.assertNotIn(9.9, [r[4] for r in body])             # Duplicate in neither

    def test_core_league_table_is_core_only(self):
        note, cols, body = self._table("LEAGUE BREAKDOWN")
        self.assertEqual(note[0], et._CORE_SECTION_NOTE)
        self.assertEqual(cols, ["League", "Wins", "Losses", "Win Rate %", "Total P&L", "Picks"])
        self.assertEqual(body, et._league_breakdown_rows([self.CORE_BLANK, self.CORE]))
        by_name = {r[0]: r for r in body}
        self.assertEqual(by_name["Premier League"], ["Premier League", 1, 0, "100.0%", 0.8, 1])
        self.assertEqual(by_name["Serie A"], ["Serie A", 0, 0, "0.0%", 0, 0])   # Extended's league: zero here
        self.assertEqual(by_name["Ligue 1"], ["Ligue 1", 0, 0, "0.0%", 0, 0])   # Duplicate's league: zero here

    def test_extended_league_table_shows_where_the_paper_tier_is_weak(self):
        note, cols, body = self._table("EXTENDED LEAGUE BREAKDOWN")
        self.assertEqual(note[0], et._EXTENDED_SECTION_NOTE)
        self.assertEqual(cols, ["League", "Wins", "Losses", "Win Rate %",
                                "Total P&L (unstaked units)", "Picks"])
        self.assertEqual(body, et._league_breakdown_rows([self.EXT_1, self.EXT_2, self.EXT_PEND]))
        by_name = {r[0]: r for r in body}
        self.assertEqual(by_name["Serie A"], ["Serie A", 1, 1, "50.0%", -0.3, 2])
        self.assertEqual(by_name["Bundesliga"], ["Bundesliga", 0, 0, "0.0%", 0, 1])  # pending counts as a pick
        self.assertEqual(by_name["Premier League"], ["Premier League", 0, 0, "0.0%", 0, 0])
        self.assertEqual(by_name["Ligue 1"], ["Ligue 1", 0, 0, "0.0%", 0, 0])

    def test_headline_figures_stay_core_only(self):
        as_dict = {r[0]: r[1] for r in self.data if len(r) > 1 and r[0]}
        self.assertEqual(as_dict["Total picks"], 2)
        self.assertEqual(as_dict["  Wins"], 1)
        self.assertEqual(as_dict["Total P&L (units)"], -0.2)
        self.assertEqual(as_dict["Win rate"], "50.0%")

    def test_no_extended_section_ever_says_plain_total_pnl(self):
        for header in ("EXTENDED BET TYPE BREAKDOWN", "EXTENDED LEAGUE BREAKDOWN"):
            _, cols, _ = self._table(header)
            self.assertNotIn("Total P&L", cols)
            self.assertIn("Total P&L (unstaked units)", cols)


if __name__ == "__main__":
    unittest.main()
