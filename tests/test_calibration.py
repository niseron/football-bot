"""
CLV is measured from the price PAID, and reported all-Core beside club-only
(22 Oct 2026). Pure — no Sheets, no network, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

Until 22 Oct 2026 clv_report() divided the 'Odds' ESTIMATE by the closing
price on every row. Settlement has paid the matched market price since 9 Aug
2026, so the report was scoring the model's own price against the close
rather than the price a follower got — on the 22 Oct sheet it read +1.35%
average CLV where the price paid gave +0.27%. These tests pin the price, the
two columns and the sample-size warning.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import calibration  # noqa: E402
import excel_tracker as et  # noqa: E402

H = list(et.PICKS_HEADERS)
I = {name: n for n, name in enumerate(H)}


def _row(*, odds="2.00", market_odds="", market_prob="", closing="1.80", result="WIN",
         pnl=None, league="Premier League", tier=""):
    r = [""] * len(H)
    r[I["Date"]] = "10-Oct-2026"
    r[I["Match"]] = "Arsenal vs Chelsea"
    r[I["Bet Type"]] = "Match Winner"
    r[I["Pick"]] = "Arsenal Win"
    r[I["Odds"]] = odds
    r[I["Result"]] = result
    if pnl is None:
        pnl = str(et.pnl_for_result(result, float(market_odds or odds)))
    r[I["Profit/Loss"]] = pnl
    r[I["Market Odds"]] = market_odds
    r[I["Market Prob %"]] = market_prob
    r[I["Closing Odds"]] = closing
    r[I["League"]] = league
    r[I["Pick Tier"]] = tier
    return r


def _clv(rows):
    ws = mock.Mock()
    ws.get_all_values.return_value = [H] + rows
    return calibration.clv_report(ws_getter=lambda: ws)


class ClvIsMeasuredFromThePricePaid(unittest.TestCase):

    def test_matched_market_price_not_the_estimate(self):
        # Estimate 2.00, market 1.80, close 1.80: the follower got the close
        # exactly. The old estimate basis read this as +11.1%.
        clv = _clv([_row(odds="2.00", market_odds="1.80", closing="1.80")])
        self.assertEqual(clv["avg_clv"], 0.0)
        self.assertEqual(clv["market_priced"], 1)

    def test_estimate_when_no_market_price_was_matched(self):
        # No market line: settlement paid the estimate, so CLV is measured from it.
        clv = _clv([_row(odds="2.00", market_odds="", closing="1.60")])
        self.assertEqual(clv["avg_clv"], 25.0)
        self.assertEqual(clv["market_priced"], 0)

    def test_pre_9_aug_row_uses_the_price_derived_from_market_prob(self):
        # Rows before the 'Market Odds' column carry only 'Market Prob %'.
        clv = _clv([_row(odds="2.30", market_odds="", market_prob="55.6", closing="1.80")])
        self.assertAlmostEqual(clv["avg_clv"], (1.80 / 1.80 - 1) * 100, places=2)
        self.assertEqual(clv["market_priced"], 1)

    def test_unreadable_estimate_with_no_market_is_skipped_not_priced_at_1(self):
        # settlement_odds_from_row falls back to 1.0; measured at 1.0 this would
        # read as a large negative CLV.
        clv = _clv([_row(odds="?", market_odds="", closing="1.80", pnl="0.00")])
        self.assertEqual(clv["sample_size"], 0)

    def test_no_closing_price_is_skipped(self):
        self.assertEqual(_clv([_row(closing="")])["sample_size"], 0)

    def test_unsettled_row_is_skipped(self):
        self.assertEqual(_clv([_row(result="")])["sample_size"], 0)


class ClvAllCoreBesideClubOnly(unittest.TestCase):

    def setUp(self):
        self.clv = _clv([
            _row(market_odds="2.00", closing="1.60", league="Premier League"),        # +25%
            _row(market_odds="1.50", closing="2.00", result="LOSS", league="La Liga"),  # -25%
            _row(market_odds="2.20", closing="2.00", league="FIFA World Cup 2026"),   # +10%
            _row(market_odds="3.00", closing="1.50", league="Premier League",
                 tier=et.PICK_TIER_EXTENDED),                                          # never counted
        ])

    def test_all_core_counts_every_core_competition(self):
        self.assertEqual(self.clv["sample_size"], 3)
        self.assertAlmostEqual(self.clv["avg_clv"], round((25 - 25 + 10) / 3, 2))

    def test_club_drops_the_world_cup(self):
        club = self.clv["club"]
        self.assertEqual(club["sample_size"], 2)
        self.assertEqual(club["avg_clv"], 0.0)
        self.assertEqual(club["pct_positive"], 50.0)
        self.assertEqual(club["positive_clv_roi"], {"picks": 1, "pnl": 1.0, "roi": 100.0})
        self.assertEqual(club["negative_clv_roi"], {"picks": 1, "pnl": -1.0, "roi": -100.0})

    def test_extended_reaches_neither_column(self):
        self.assertEqual(self.clv["market_priced"], 3)
        self.assertEqual(self.clv["club"]["market_priced"], 2)

    def test_empty_sheet_still_carries_both_columns(self):
        ws = mock.Mock()
        ws.get_all_values.return_value = []
        clv = calibration.clv_report(ws_getter=lambda: ws)
        self.assertEqual(clv["sample_size"], 0)
        self.assertEqual(clv["club"]["sample_size"], 0)

    def test_read_failure_is_none(self):
        def boom():
            raise RuntimeError("quota")
        self.assertIsNone(calibration.clv_report(ws_getter=boom))

    def test_table_puts_the_columns_side_by_side(self):
        lines = calibration.clv_table_lines(self.clv)
        self.assertIn("All Core", lines[0])
        self.assertIn("Club only", lines[0])
        picks = next(l for l in lines if l.startswith("Picks"))
        self.assertEqual(picks.split()[1:], ["3", "2"])
        self.assertEqual(len({len(l) for l in lines}), 1, "fixed-width rows")


class ClvSampleSizeWarning(unittest.TestCase):

    def _report(self, core_n, club_n):
        rows = [_row(league="Premier League") for _ in range(club_n)]
        rows += [_row(league="FIFA World Cup 2026") for _ in range(core_n - club_n)]
        return _clv(rows)

    def test_warns_below_the_threshold(self):
        warning = calibration.clv_sample_warning(self._report(3, 2))
        self.assertIn(f"below {calibration.MIN_MEANINGFUL_SAMPLE} settled picks", warning)
        self.assertIn("directional only", warning)

    def test_warns_on_the_club_column_alone_once_core_clears_it(self):
        with mock.patch.object(calibration, "MIN_MEANINGFUL_SAMPLE", 3):
            warning = calibration.clv_sample_warning(self._report(3, 2))
        self.assertTrue(warning.startswith("Club-only CLV (2 picks)"))

    def test_silent_once_both_columns_clear_it(self):
        with mock.patch.object(calibration, "MIN_MEANINGFUL_SAMPLE", 2):
            self.assertIsNone(calibration.clv_sample_warning(self._report(3, 2)))


class MonthlyReportShowsBothColumns(unittest.TestCase):

    def test_discord_message_carries_the_table_and_the_warning(self):
        import weekly_summary as W

        clv = _clv([
            _row(market_odds="2.00", closing="1.60", league="Premier League"),
            _row(market_odds="2.20", closing="2.00", league="FIFA World Cup 2026"),
        ])
        cal = {"buckets": [{"range": "60-70%", "picks": 2, "avg_stated": 64.0,
                            "actual_win_rate": 100.0}],
               "brier_score": 0.13, "sample_size": 2, "meaningful": False}
        with mock.patch.object(calibration, "calibration_report", return_value=cal), \
             mock.patch.object(calibration, "edge_report", return_value=None), \
             mock.patch.object(calibration, "clv_report", return_value=clv):
            text = W._to_discord_markdown(W.build_calibration_message())

        section = text[text.index("Closing Line Value"):]
        self.assertIn("All Core", section)
        self.assertIn("Club only", section)
        self.assertIn("price paid", section)
        for row in calibration.clv_table_lines(clv):
            self.assertIn(f"`{row}`", section)
        self.assertIn("CLV results are not statistically meaningful below", section)
        self.assertNotIn("\\", section, "MarkdownV2 escapes must not reach Discord")


if __name__ == "__main__":
    unittest.main()
