"""
Tennis pipeline regression tests. Pure: no network, no Sheets, no Discord.

Run from football-bot/:
    python -m unittest discover -s tests -t . -v
"""
import unittest

from tennis_excel_tracker import TENNIS_HEADERS, settlement_odds, settlement_pnl


class SettlementPrice(unittest.TestCase):
    """17 Nov 2026: a tennis pick settles at the market price whenever one was
    captured, never at the model's estimate (row 'Joao Fonseca vs Casper Ruud',
    6 Aug 2026, was paid at the 2.10 estimate against a 1.69 market)."""

    def test_market_odds_column_wins(self):
        self.assertEqual(settlement_odds("2.10", "1.69", "59.2"), (1.69, "market"))

    def test_market_prob_rebuilds_the_price_for_older_rows(self):
        price, basis = settlement_odds("2.10", "", "59.2")
        self.assertAlmostEqual(price, 100 / 59.2)
        self.assertTrue(basis.startswith("market"))

    def test_estimate_only_without_any_market_price(self):
        self.assertEqual(settlement_odds("1.75", "", ""), (1.75, "estimate"))

    def test_garbage_market_cells_fall_back_to_the_estimate(self):
        self.assertEqual(settlement_odds("1.75", "n/a", "abc"), (1.75, "estimate"))
        self.assertEqual(settlement_odds("1.75", "0.9", "0"), (1.75, "estimate"))

    def test_pnl_at_the_settlement_price(self):
        price, _ = settlement_odds("2.10", "", "59.2")
        self.assertEqual(settlement_pnl("WIN", price), 0.69)
        self.assertEqual(settlement_pnl("LOSS", price), -1.0)
        self.assertEqual(settlement_pnl("VOID", price), 0.0)

    def test_new_columns_only_ever_append(self):
        # Result/P&L are written to G/H by letter — nothing may move them
        self.assertEqual(TENNIS_HEADERS.index("Result"), 6)
        self.assertEqual(TENNIS_HEADERS.index("P&L"), 7)
        self.assertGreater(TENNIS_HEADERS.index("Market Odds"),
                           TENNIS_HEADERS.index("Player IDs"))


if __name__ == "__main__":
    unittest.main()
