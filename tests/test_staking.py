"""
Regression suite for Kelly staking (rebuilt 8 Sep 2026). Pure — no network,
no Sheets. Run from football-bot/:

    python -m unittest discover -s tests -t . -v

Pins: a bucket that shrinks below break-even returns EXACTLY 0.0; the 5%
per-pick cap binds at €175 on a €3,500 bankroll; the 15% daily cap scales five
capped picks proportionally to €105 each; market odds are sized on when
matched and the estimate otherwise; shrinkage arithmetic; the club-only
breakdown's exclusions; and the flat fallback surviving only below 50 club
picks.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import excel_tracker as et  # noqa: E402


# The settled club-football Core picks as of 8 Sep 2026 (World Cup and
# friendlies excluded): 56 of 87 overall, by settlement-odds bucket.
BREAKDOWN = {
    "overall": {"wins": 56, "total": 87},
    "buckets": {
        "<1.30":     {"wins": 10, "total": 11},
        "1.30-1.50": {"wins": 9,  "total": 14},
        "1.50-1.75": {"wins": 14, "total": 22},
        "1.75-2.00": {"wins": 14, "total": 21},
        "2.00+":     {"wins": 9,  "total": 19},
    },
}


def _core(stake: float, tier: str = et.PICK_TIER_CORE) -> dict:
    return {"pick_tier": tier, "kelly": {"stake": stake, "note": ""}}


class Constants(unittest.TestCase):

    def test_bankroll_and_caps(self):
        self.assertEqual(et.REAL_BANKROLL, 3500.0)
        self.assertEqual(et.KELLY_FRACTION, 0.5)
        self.assertEqual(et.KELLY_MAX_FRACTION, 0.05)
        self.assertEqual(et.DAILY_STAKE_CAP_FRACTION, 0.15)
        self.assertEqual(et.KELLY_SHRINK_K, 50)
        self.assertEqual(et.KELLY_MIN_CLUB_SAMPLE, 50)
        self.assertEqual(round(et.KELLY_MAX_FRACTION * et.REAL_BANKROLL, 2), 175.0)
        self.assertEqual(round(et.DAILY_STAKE_CAP_FRACTION * et.REAL_BANKROLL, 2), 525.0)


class OddsBuckets(unittest.TestCase):

    def test_edges_are_lower_inclusive_upper_exclusive(self):
        self.assertEqual(et.kelly_odds_bucket(1.20), "<1.30")
        self.assertEqual(et.kelly_odds_bucket(1.2999), "<1.30")
        self.assertEqual(et.kelly_odds_bucket(1.30), "1.30-1.50")
        self.assertEqual(et.kelly_odds_bucket(1.50), "1.50-1.75")
        self.assertEqual(et.kelly_odds_bucket(1.75), "1.75-2.00")
        self.assertEqual(et.kelly_odds_bucket(1.99), "1.75-2.00")
        self.assertEqual(et.kelly_odds_bucket(2.00), "2.00+")
        self.assertEqual(et.kelly_odds_bucket(9.50), "2.00+")


class Shrinkage(unittest.TestCase):

    def test_bucket_rate_is_weighted_n_over_n_plus_k(self):
        # <1.30: 10/11 raw, weight 11/61 -> 0.6915 with the 56/87 prior.
        k = et.calculate_kelly_stake(1.20, BREAKDOWN)
        self.assertEqual(k["bucket"], "<1.30")
        self.assertEqual(k["sample"], 11)
        self.assertAlmostEqual(k["win_rate"], 0.6915, places=3)

    def test_empty_bucket_uses_the_overall_rate(self):
        thin = {"overall": {"wins": 56, "total": 87}, "buckets": {}}
        k = et.calculate_kelly_stake(1.85, thin)
        self.assertEqual(k["sample"], 0)
        self.assertAlmostEqual(k["win_rate"], 56 / 87, places=4)


class BelowBreakEven(unittest.TestCase):
    """A bucket that shrinks under its break-even line returns exactly 0.0."""

    def test_short_odds_bucket_returns_exactly_zero(self):
        for odds in (1.10, 1.20, 1.29):
            k = et.calculate_kelly_stake(odds, BREAKDOWN)
            self.assertEqual(k["stake"], 0.0, odds)
            self.assertIsInstance(k["stake"], float)
            self.assertEqual(k["note"], "negative edge")
        # 90.9% raw would have justified 1.20 (break-even 83.3%); the shrunk
        # 69.2% does not — that is the point of shrinking.
        self.assertGreater(10 / 11, 1 / 1.20)
        self.assertLess(et.calculate_kelly_stake(1.20, BREAKDOWN)["win_rate"], 1 / 1.20)

    def test_1_30_to_1_50_bucket_returns_exactly_zero(self):
        for odds in (1.30, 1.38, 1.49):
            self.assertEqual(et.calculate_kelly_stake(odds, BREAKDOWN)["stake"], 0.0, odds)

    def test_break_even_edge_inside_a_bucket(self):
        # 1.50-1.75 shrinks to 64.1%, break-even odds 1.56: nothing at 1.50,
        # a real stake at 1.63.
        self.assertEqual(et.calculate_kelly_stake(1.50, BREAKDOWN)["stake"], 0.0)
        self.assertGreater(et.calculate_kelly_stake(1.63, BREAKDOWN)["stake"], 0.0)


class PerPickCap(unittest.TestCase):

    def test_five_percent_cap_binds(self):
        # 1.75-2.00 at 65.0%: full Kelly ~26%, half ~13%, capped to 5% = €175.
        k = et.calculate_kelly_stake(1.90, BREAKDOWN)
        self.assertEqual(k["fraction"], et.KELLY_MAX_FRACTION)
        self.assertEqual(k["stake"], 175.0)
        # And every longer price in the book caps at the same figure.
        for odds in (1.75, 2.00, 2.56, 3.00):
            self.assertEqual(et.calculate_kelly_stake(odds, BREAKDOWN)["stake"], 175.0, odds)

    def test_half_kelly_below_the_cap(self):
        # 1.50-1.75 at 1.63: p = 0.6414, full Kelly 7.23%, half 3.62% -> €126.53.
        k = et.calculate_kelly_stake(1.63, BREAKDOWN)
        self.assertLess(k["fraction"], et.KELLY_MAX_FRACTION)
        self.assertAlmostEqual(k["stake"], 126.53, delta=0.02)


class DailyCap(unittest.TestCase):

    def test_five_capped_picks_scale_proportionally_to_the_cap(self):
        picks = [_core(175.0) for _ in range(5)]
        factor = et.apply_daily_stake_cap(picks)
        self.assertAlmostEqual(factor, 0.6, places=6)
        self.assertEqual([p["kelly"]["stake"] for p in picks], [105.0] * 5)
        self.assertEqual(sum(p["kelly"]["stake"] for p in picks), 525.0)
        self.assertEqual(picks[0]["kelly"]["stake_uncapped"], 175.0)
        self.assertIn("daily cap", picks[0]["kelly"]["note"])

    def test_unequal_stakes_keep_their_proportions(self):
        picks = [_core(175.0), _core(175.0), _core(175.0), _core(125.0), _core(50.0)]  # 700
        et.apply_daily_stake_cap(picks)
        self.assertEqual([p["kelly"]["stake"] for p in picks], [131.25, 131.25, 131.25, 93.75, 37.5])

    def test_three_capped_picks_sit_on_the_cap_untouched(self):
        picks = [_core(175.0) for _ in range(3)]
        self.assertEqual(et.apply_daily_stake_cap(picks), 1.0)
        self.assertEqual([p["kelly"]["stake"] for p in picks], [175.0] * 3)
        self.assertNotIn("stake_uncapped", picks[0]["kelly"])

    def test_zero_stakes_and_extended_picks_are_ignored(self):
        picks = [_core(175.0) for _ in range(5)]
        picks.append(_core(0.0))
        picks.append(_core(175.0, tier=et.PICK_TIER_EXTENDED))
        picks.append({"pick_tier": et.PICK_TIER_CORE})  # no kelly at all
        et.apply_daily_stake_cap(picks)
        self.assertEqual([p["kelly"]["stake"] for p in picks[:5]], [105.0] * 5)
        self.assertEqual(picks[5]["kelly"]["stake"], 0.0)
        self.assertEqual(picks[6]["kelly"]["stake"], 175.0)

    def test_bankroll_argument_sets_the_cap(self):
        picks = [_core(100.0) for _ in range(5)]  # 500 against a 15% cap of 150
        et.apply_daily_stake_cap(picks, bankroll=1000.0)
        self.assertEqual([p["kelly"]["stake"] for p in picks], [30.0] * 5)


class OddsBasis(unittest.TestCase):
    """Size on the market price when one was matched, else the estimate."""

    def test_market_odds_preferred(self):
        self.assertEqual(et.kelly_odds_for_pick({"odds": 1.8, "market_odds": 1.68}), 1.68)
        self.assertEqual(et.kelly_odds_for_pick({"odds": 1.8, "market_odds": "1.68"}), 1.68)

    def test_falls_back_to_the_estimate(self):
        self.assertEqual(et.kelly_odds_for_pick({"odds": 1.8}), 1.8)
        self.assertEqual(et.kelly_odds_for_pick({"odds": 1.8, "market_odds": None}), 1.8)
        self.assertEqual(et.kelly_odds_for_pick({"odds": 1.8, "market_odds": 0}), 1.8)
        self.assertEqual(et.kelly_odds_for_pick({"odds": "1.8", "market_odds": "n/a"}), 1.8)

    def test_the_stake_follows_the_price_it_was_sized_on(self):
        # Estimate 1.45 would be zero-staked; the matched 1.63 is not.
        est = et.calculate_kelly_stake(et.kelly_odds_for_pick({"odds": 1.45}), BREAKDOWN)
        mkt = et.calculate_kelly_stake(et.kelly_odds_for_pick({"odds": 1.45, "market_odds": 1.63}), BREAKDOWN)
        self.assertEqual(est["stake"], 0.0)
        self.assertGreater(mkt["stake"], 0.0)
        self.assertEqual(mkt["odds_used"], 1.63)


class FlatFallback(unittest.TestCase):

    def test_only_below_the_minimum_club_sample(self):
        thin = {"overall": {"wins": 30, "total": 49}, "buckets": {}}
        k = et.calculate_kelly_stake(1.90, thin)
        self.assertEqual(k["stake"], et.UNIT_STAKE)
        self.assertIn("insufficient data", k["note"])
        enough = {"overall": {"wins": 31, "total": 50}, "buckets": {}}
        self.assertNotIn("insufficient data", et.calculate_kelly_stake(1.90, enough)["note"])

    def test_empty_breakdown_from_a_failed_read_is_flat_not_a_crash(self):
        self.assertEqual(et.calculate_kelly_stake(1.90, {})["stake"], et.UNIT_STAKE)
        self.assertEqual(et.calculate_kelly_stake(1.90, et.kelly_breakdown_from_rows([]))["stake"],
                         et.UNIT_STAKE)


class BreakdownFromRows(unittest.TestCase):
    """The club-only Core sample: what is in, what is out."""

    @staticmethod
    def _row(**cells) -> list[str]:
        row = [""] * len(et.PICKS_HEADERS)
        for name, value in cells.items():
            row[et.PICKS_HEADERS.index(name)] = str(value)
        return row

    def test_exclusions_and_bucketing(self):
        R = self._row
        rows = [list(et.PICKS_HEADERS),
                R(Date="30-Aug-2026", Match="a", Odds="1.20", Result="WIN",  League="Premier League"),
                R(Date="30-Aug-2026", Match="b", Odds="2.40", Result="LOSS", League="Serie A", **{"Pick Tier": "Core"}),
                R(Date="21-Jun-2026", Match="c", Odds="1.20", Result="WIN",  League="FIFA World Cup 2026"),
                R(Date="21-Jun-2026", Match="d", Odds="1.20", Result="WIN",  League="Friendlies"),
                R(Date="30-Aug-2026", Match="e", Odds="1.20", Result="WIN",  League="La Liga", **{"Pick Tier": "Extended"}),
                R(Date="30-Aug-2026", Match="f", Odds="1.20", Result="WIN",  League="La Liga", **{"Pick Tier": "Duplicate"}),
                R(Date="30-Aug-2026", Match="g", Odds="1.20", Result="HALF WIN", League="La Liga"),
                R(Date="30-Aug-2026", Match="h", Odds="1.20", Result="VOID", League="La Liga"),
                R(Date="30-Aug-2026", Match="i", Odds="1.20", Result="",     League="La Liga"),
                R(Date="15-Jun-2026", Match="j", Odds="1.20", Result="WIN",  League=""),
                ]
        b = et.kelly_breakdown_from_rows(rows)
        self.assertEqual(b["overall"], {"wins": 1, "total": 2})
        self.assertEqual(b["buckets"]["<1.30"], {"wins": 1, "total": 1})
        self.assertEqual(b["buckets"]["2.00+"], {"wins": 0, "total": 1})
        self.assertEqual(b["buckets"]["1.75-2.00"], {"wins": 0, "total": 0})

    def test_bucket_uses_the_settlement_price(self):
        R = self._row
        rows = [list(et.PICKS_HEADERS),
                R(Date="30-Aug-2026", Match="a", Odds="1.80", Result="WIN", League="Ligue 1", **{"Market Odds": "1.68"}),
                R(Date="30-Aug-2026", Match="b", Odds="1.80", Result="WIN", League="Ligue 1")]
        b = et.kelly_breakdown_from_rows(rows)
        self.assertEqual(b["buckets"]["1.50-1.75"]["total"], 1)  # market 1.68
        self.assertEqual(b["buckets"]["1.75-2.00"]["total"], 1)  # estimate 1.80

    def test_club_football_predicate(self):
        for label in ("Premier League", "Champions League", "Jupiler Pro League", "Conference League"):
            self.assertTrue(et.is_club_football(label), label)
        for label in ("FIFA World Cup 2026", "Friendlies", "UEFA Nations League", "Euro 2028 Qualifiers", ""):
            self.assertFalse(et.is_club_football(label), label)


if __name__ == "__main__":
    unittest.main()
