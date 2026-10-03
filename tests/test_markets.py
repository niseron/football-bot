"""
Regression suite for market recognition and the Opus-only extra markets
(4 Oct 2026). Pure — no network, no Sheets, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

Measured 3 Oct 2026 against the evaluator as it then stood: 'Over/Under / Over
1.5 Goals' settled against a defaulted 2.5 line; 'Team Total Goals / Arsenal
Over 1.5' and 'Arsenal Over 1.5 Goals' settled on the MATCH total; 'Match Winner
/ Arsenal Win to Nil' paid as a plain win; and 'Corners Over 9.5', 'Over 4.5
Cards' and 'Shots on Target Over 8.5' all settled on GOALS. Every one silently.
Each case from that table is pinned below, for both callers: production
(extended_markets left False) and the Opus shadow (True).

An audit of every settled row on the Picks and Opus Shadow tabs the same day
(490 + 244) found none had actually been settled that way — no corrections.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_results as ar  # noqa: E402
from tests.test_settlement import _fx  # noqa: E402

H, A = "Arsenal", "Leeds United"


def prod(bt, pk, hs, as_, **kw):
    return ar.evaluate_pick(bt, pk, H, A, hs, as_, **kw)


def opus(bt, pk, hs, as_, **kw):
    return ar.evaluate_pick(bt, pk, H, A, hs, as_, extended_markets=True, **kw)


class TheThreeOctoberTable(unittest.TestCase):
    """Every row of the 3 Oct 2026 table, production then shadow."""

    def test_draw_no_bet_is_pending_in_production_and_void_on_a_draw_for_opus(self):
        self.assertEqual(prod("Draw No Bet", "Arsenal", 1, 1), "PENDING")
        self.assertEqual(opus("Draw No Bet", "Arsenal", 1, 1), "VOID")

    def test_over_under_label_reads_the_line_from_the_pick(self):
        # Was LOSS: the line defaulted to 2.5.
        self.assertEqual(prod("Over/Under", "Over 1.5 Goals", 2, 0), "WIN")
        self.assertEqual(opus("Over/Under", "Over 1.5 Goals", 2, 0), "WIN")

    def test_total_goals_label_reads_the_line_from_the_pick(self):
        # Was WIN: 3 goals cleared the defaulted 2.5, not the stated 3.5.
        self.assertEqual(prod("Total Goals", "Over 3.5 Goals", 2, 1), "LOSS")
        self.assertEqual(prod("Over/Under", "Under 3.5 Goals", 2, 1), "WIN")

    def test_team_total_never_settles_on_the_match_total(self):
        # Was WIN: 3 match goals cleared 1.5; Arsenal scored 1.
        for bt, pk in (("Team Total Goals", "Arsenal Over 1.5"),
                       ("Arsenal Over 1.5 Goals", "Arsenal Over 1.5 Goals")):
            with self.subTest(bt=bt):
                self.assertEqual(prod(bt, pk, 1, 2), "PENDING")
                self.assertEqual(opus(bt, pk, 1, 2), "LOSS")

    def test_win_to_nil_never_settles_as_a_plain_win(self):
        # Was WIN under a Match Winner bet type.
        self.assertEqual(prod("Match Winner", "Arsenal Win to Nil", 2, 1), "PENDING")
        self.assertEqual(opus("Match Winner", "Arsenal Win to Nil", 2, 1), "LOSS")
        self.assertEqual(prod("Win to Nil", "Arsenal", 2, 0), "PENDING")
        self.assertEqual(opus("Win to Nil", "Arsenal", 2, 0), "WIN")

    def test_stat_markets_are_pending_for_every_caller(self):
        # Were LOSS: settled on goals.
        for bt, pk in (("Corners Over 9.5", "Over 9.5 Corners"),
                       ("Total Corners", "Over 9.5 Corners"),
                       ("Over 4.5 Cards", "Over 4.5 Cards"),
                       ("Shots on Target Over 8.5", "Over 8.5"),
                       ("Total Bookings", "Over 40.5 Booking Points"),
                       ("Half Time Result", "Arsenal")):
            with self.subTest(bt=bt):
                self.assertEqual(prod(bt, pk, 2, 1), "PENDING")
                self.assertEqual(opus(bt, pk, 2, 1), "PENDING")


class OverUnderLine(unittest.TestCase):

    def test_well_formed_production_labels_settle_exactly_as_before(self):
        self.assertEqual(prod("Over 2.5 Goals", "Over 2.5 Goals", 2, 1), "WIN")
        self.assertEqual(prod("Over 2.5 Goals", "Over 2.5 Goals", 1, 1), "LOSS")
        self.assertEqual(prod("Under 2.5 Goals", "Under 2.5 Goals", 1, 1), "WIN")
        self.assertEqual(prod("Over/Under 2.5 Goals", "Over 2.5 Goals", 3, 0), "WIN")
        self.assertEqual(prod("Under/Over", "Under 2.5 Goals", 3, 0), "LOSS")

    def test_line_in_the_bet_type_alone_still_works(self):
        self.assertEqual(prod("Over 1.5 Goals", "Over", 2, 0), "WIN")
        self.assertEqual(prod("Under 3.5 Goals", "Under 3.5 Goals", 3, 1), "LOSS")

    def test_no_line_anywhere_is_pending_never_a_default(self):
        self.assertEqual(prod("Over/Under", "Over", 3, 0), "PENDING")

    def test_conflicting_lines_are_pending(self):
        self.assertEqual(prod("Over 2.5 Goals", "Over 3.5 Goals", 4, 0), "PENDING")

    def test_both_or_neither_side_is_pending(self):
        self.assertEqual(prod("Over/Under 2.5", "Over or Under 2.5", 3, 0), "PENDING")
        self.assertEqual(prod("Over 2.5 Goals", "Yes", 3, 0), "PENDING")

    def test_club_name_with_digits_is_not_read_as_a_line(self):
        self.assertEqual(
            ar.evaluate_pick("Over 2.5 Goals", "Over 2.5 Goals", "Schalke 04", "Mainz", 2, 1),
            "WIN")

    def test_cards_inside_a_club_name_is_not_a_cards_market(self):
        self.assertEqual(
            ar.evaluate_pick("Match Winner", "Cardiff City Win", "Cardiff City", "Hull", 2, 0),
            "WIN")

    def test_extra_time_bound_still_applies_on_a_1_5_line(self):
        # 1-1 aet in a single match: 90' total is 0 or 2 — straddles 1.5.
        self.assertEqual(prod("Over/Under", "Over 1.5 Goals", 1, 1, extra_time=True), "PENDING")
        # 0-0 aet: 90' total was 0, under any line.
        self.assertEqual(prod("Over/Under", "Under 1.5 Goals", 0, 0, extra_time=True), "WIN")


class DrawNoBet(unittest.TestCase):

    def test_win_loss_void(self):
        self.assertEqual(opus("Draw No Bet", "Arsenal", 2, 0), "WIN")
        self.assertEqual(opus("Draw No Bet", "Arsenal DNB", 0, 1), "LOSS")
        self.assertEqual(opus("Draw No Bet", "Leeds United", 0, 1), "WIN")
        self.assertEqual(opus("Draw No Bet", "Leeds United", 1, 1), "VOID")

    def test_single_match_extra_time_was_level_at_90_so_it_voids(self):
        self.assertEqual(opus("Draw No Bet", "Arsenal", 2, 1, extra_time=True), "VOID")

    def test_two_legged_extra_time_uses_the_derived_margin(self):
        # 1st leg 1-0 to the away side; this leg 2-1 aet, agg 3-2 → 90' margin
        # (2-1) - (1-0)... derived by _regulation_goal_difference.
        gd = ar._regulation_goal_difference(2, 1, "3-2", two_legged=True)
        self.assertIsNotNone(gd)
        expect = "VOID" if gd == 0 else ("WIN" if gd > 0 else "LOSS")
        self.assertEqual(opus("Draw No Bet", "Arsenal", 2, 1, extra_time=True,
                              two_legged=True, aggregate="3-2"), expect)

    def test_untrusted_margin_is_pending(self):
        self.assertEqual(opus("Draw No Bet", "Arsenal", 2, 1, extra_time=True,
                              two_legged=True, aggregate="garbage"), "PENDING")

    def test_shootout_without_extra_time_settles_on_the_score(self):
        self.assertEqual(opus("Draw No Bet", "Arsenal", 1, 1, penalties=True), "VOID")

    def test_full_time_scope_is_refused(self):
        self.assertEqual(opus("Draw No Bet", "Arsenal (Full-Time incl. ET/Pens)", 2, 1),
                         "PENDING")
        self.assertEqual(opus("Draw No Bet", "Arsenal (90 min)", 2, 1), "WIN")

    def test_naming_neither_side_or_the_draw_is_pending(self):
        self.assertEqual(opus("Draw No Bet", "Chelsea", 2, 1), "PENDING")
        self.assertEqual(opus("Draw No Bet", "Draw", 1, 1), "PENDING")

    def test_bare_draw_bet_type_is_still_the_match_winner_draw(self):
        self.assertEqual(prod("Draw", "Draw", 1, 1), "WIN")


class TeamTotal(unittest.TestCase):

    def test_over_and_under_on_either_side(self):
        self.assertEqual(opus("Team Total Goals", "Arsenal Over 1.5 Goals", 2, 0), "WIN")
        self.assertEqual(opus("Team Total Goals", "Leeds United Under 0.5 Goals", 3, 0), "WIN")
        self.assertEqual(opus("Team Total", "Leeds Under 0.5", 3, 1), "LOSS")

    def test_whole_line_push_is_void_and_quarter_line_is_pending(self):
        self.assertEqual(opus("Team Total Goals", "Arsenal Over 2 Goals", 2, 0), "VOID")
        self.assertEqual(opus("Team Total Goals", "Arsenal Over 1.25 Goals", 2, 0), "PENDING")

    def test_must_name_exactly_one_side(self):
        self.assertEqual(opus("Team Total Goals", "Over 1.5 Goals", 2, 0), "PENDING")
        self.assertEqual(opus("Team Total Goals", "Chelsea Over 1.5 Goals", 2, 0), "PENDING")

    def test_extra_time_settles_only_when_the_whole_range_clears_the_line(self):
        # 1-1 aet single match: Arsenal's 90' goals lie in [0, 1].
        self.assertEqual(opus("Team Total Goals", "Arsenal Under 1.5 Goals", 1, 1,
                              extra_time=True), "WIN")
        self.assertEqual(opus("Team Total Goals", "Arsenal Over 1.5 Goals", 1, 1,
                              extra_time=True), "LOSS")
        # 2-2 aet: [0, 2] straddles 0.5.
        self.assertEqual(opus("Team Total Goals", "Arsenal Over 0.5 Goals", 2, 2,
                              extra_time=True), "PENDING")

    def test_full_time_scope_is_refused(self):
        self.assertEqual(opus("Team Total Goals",
                              "Arsenal Over 0.5 Goals (Full-Time incl. ET/Pens)", 2, 0),
                         "PENDING")


class WinToNil(unittest.TestCase):

    def test_win_and_clean_sheet(self):
        self.assertEqual(opus("Win to Nil", "Arsenal to Win to Nil", 2, 0), "WIN")
        self.assertEqual(opus("Win to Nil", "Arsenal to Win to Nil", 2, 1), "LOSS")
        self.assertEqual(opus("Win to Nil", "Arsenal to Win to Nil", 0, 0), "LOSS")
        self.assertEqual(opus("Win to Nil", "Leeds United to Win to Nil", 0, 1), "WIN")

    def test_single_match_extra_time_was_level_at_90_so_it_loses(self):
        self.assertEqual(opus("Win to Nil", "Arsenal to Win to Nil", 1, 0,
                              extra_time=True), "LOSS")

    def test_two_legged_extra_time(self):
        # 90' margin derived from the aggregate; opponent scoreless over 120'
        # means scoreless over 90'.
        gd = ar._regulation_goal_difference(1, 0, "1-1", two_legged=True)
        self.assertIsNotNone(gd)
        expect = "WIN" if gd > 0 else "LOSS"
        self.assertEqual(opus("Win to Nil", "Arsenal to Win to Nil", 1, 0, extra_time=True,
                              two_legged=True, aggregate="1-1"), expect)

    def test_untrusted_margin_is_pending(self):
        self.assertEqual(opus("Win to Nil", "Arsenal to Win to Nil", 1, 0, extra_time=True,
                              two_legged=True, aggregate="nonsense"), "PENDING")


class MarketClassification(unittest.TestCase):

    def test_kinds(self):
        cases = {
            ("Match Winner", "Arsenal Win"): "other",
            ("Over 2.5 Goals", "Over 2.5 Goals"): "total",
            ("Draw No Bet", "Arsenal"): "draw_no_bet",
            ("Team Total Goals", "Arsenal Over 1.5"): "team_total",
            ("Win to Nil", "Arsenal to Win to Nil"): "win_to_nil",
            ("Total Corners", "Over 9.5"): "stat",
            ("1st Half Over/Under", "Over 0.5"): "half",
        }
        for (bt, pk), kind in cases.items():
            with self.subTest(bt=bt):
                self.assertEqual(ar.classify_market(bt, pk, H, A), kind)

    def test_extended_markets_tag(self):
        self.assertFalse(ar.is_extended_market("Over 2.5 Goals", "Over 2.5 Goals"))
        self.assertFalse(ar.is_extended_market("Match Winner", "Arsenal Win"))
        self.assertTrue(ar.is_extended_market("Over/Under 1.5 Goals", "Over 1.5 Goals"))
        self.assertTrue(ar.is_extended_market("Over/Under 3.5 Goals", "Under 3.5 Goals"))
        self.assertTrue(ar.is_extended_market("Draw No Bet", "Arsenal"))
        self.assertTrue(ar.is_extended_market("Team Total Goals", "Arsenal Over 1.5 Goals"))
        self.assertTrue(ar.is_extended_market("Win to Nil", "Arsenal to Win to Nil"))

    def test_pending_reason_names_the_market(self):
        kw = dict(home_name=H, away_name=A, extended_markets=False, scope_ft=False,
                  extra_time=False, penalties=False, two_legged=False)
        self.assertIn("stats market", ar._pending_reason("Total Corners", "Over 9.5", **kw))
        self.assertIn("does not settle", ar._pending_reason("Draw No Bet", "Arsenal", **kw))
        self.assertIn("never assumed", ar._pending_reason("Over/Under", "Over", **kw))


class FlagOnlyReachesTheShadow(unittest.TestCase):
    """run_auto_results end to end: same DNB row, two callers."""

    ROW = {"sheet_row": 9, "date": date(2026, 10, 4), "match": "Arsenal vs Leeds United",
           "bet_type": "Draw No Bet", "pick": "Arsenal", "odds": 1.4,
           "est_odds": 1.4, "market_odds": None}

    def _run(self, scope, **kw):
        writes = []
        game = _fx(77, H, A, hs=2, as_=0, finished=True, utc="2026-10-04T14:00:00.000Z")
        with mock.patch.object(ar, "init_excel"), \
             mock.patch.object(ar, "_fetch_matches_cached",
                               side_effect=lambda d: [game] if d == date(2026, 10, 4) else []):
            stats, _ = ar.run_auto_results(
                7, pending_source=lambda _d: [dict(self.ROW)],
                row_writer=lambda row, res, pnl: writes.append((row, res, pnl)),
                finalizer=lambda: None, alert_scope=scope, **kw)
        return stats, writes

    def test_production_default_leaves_it_pending_with_an_alert(self):
        stats, writes = self._run("test-markets-prod")
        self.assertEqual(writes, [])
        self.assertEqual(stats["pending"], 1)
        self.assertIn("does not settle", stats["pending_alerts"][0]["reason"])

    def test_shadow_flag_settles_it(self):
        stats, writes = self._run("test-markets-opus", extended_markets=True)
        self.assertEqual(writes, [(9, "WIN", ar.pnl_for_result("WIN", 1.4))])


class ShadowGeneration(unittest.TestCase):
    """The Opus prompt addendum, tag and Core bar — and production untouched."""

    def test_production_prompts_carry_no_extra_markets(self):
        import main as M
        for prompt in (M.SYSTEM_PROMPT, M.LEAGUE_SYSTEM_PROMPT):
            self.assertNotIn("ADDITIONAL MARKETS", prompt)
            self.assertNotIn("Win to Nil", prompt)

    def test_production_settlement_default_is_off(self):
        import inspect
        sig = inspect.signature(ar.run_auto_results)
        self.assertIs(sig.parameters["extended_markets"].default, False)
        self.assertIs(inspect.signature(ar.evaluate_pick)
                      .parameters["extended_markets"].default, False)

    def test_extended_market_picks_are_tagged_and_never_core(self):
        import main as M
        import opus_shadow as O

        picks = [
            {"match": "Arsenal vs Leeds United", "league": "Premier League",
             "bet_type": "Draw No Bet", "pick": "Arsenal", "odds": 1.4},
            {"match": "Inter vs Roma", "league": "Serie A",
             "bet_type": "Match Winner", "pick": "Inter Win", "odds": 1.8},
            {"match": "Lens vs Lyon", "league": "Ligue 1",
             "bet_type": "Over/Under 3.5 Goals", "pick": "Under 3.5 Goals", "odds": 1.3},
            {"match": "Bayern vs Mainz", "league": "Bundesliga",
             "bet_type": "Over 2.5 Goals", "pick": "Over 2.5 Goals", "odds": 1.5},
        ]
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps({"picks": picks}))],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1), stop_reason="end_turn")
        create = mock.Mock(return_value=msg)
        with mock.patch.object(M, "claude", SimpleNamespace(messages=SimpleNamespace(create=create))), \
             mock.patch("usage_tracker.record_anthropic_usage"):
            out = O.analyse_with_opus({"x": []})

        sent_system = create.call_args.kwargs["system"]
        self.assertIn("ADDITIONAL MARKETS", sent_system)
        self.assertTrue(sent_system.startswith(M.SYSTEM_PROMPT))

        by_pick = {p["pick"]: p for p in out}
        self.assertEqual(by_pick["Arsenal"]["market_tag"], O.OPUS_MARKET_TAG)
        self.assertEqual(by_pick["Under 3.5 Goals"]["market_tag"], O.OPUS_MARKET_TAG)
        self.assertEqual(by_pick["Inter Win"]["market_tag"], "")
        self.assertEqual(by_pick["Over 2.5 Goals"]["market_tag"], "")
        from excel_tracker import PICK_TIER_CORE, PICK_TIER_EXTENDED
        self.assertEqual(by_pick["Arsenal"]["pick_tier"], PICK_TIER_EXTENDED)
        self.assertEqual(by_pick["Under 3.5 Goals"]["pick_tier"], PICK_TIER_EXTENDED)
        self.assertEqual(by_pick["Inter Win"]["pick_tier"], PICK_TIER_CORE)
        self.assertEqual(by_pick["Over 2.5 Goals"]["pick_tier"], PICK_TIER_CORE)


if __name__ == "__main__":
    unittest.main()
