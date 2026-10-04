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


# ── Stats markets: corners, cards, shots on target (4 Oct 2026) ──────────────

# Marseille vs PSG, 20 Sep 2026, as the feed reported it: Weah, sent off for a
# second yellow, shows ONLY as a red — yellow_cards [2, 1] is the plain bookings.
MARSEILLE_PSG = {
    "full": {"corners": [3, 7], "yellow_cards": [2, 1], "red_cards": [1, 0],
             "ShotsOnTarget": [4, 10]},
    "h1": None, "h2": None, "second_yellows": [1, 0],
}
MH, MA = "Marseille", "Paris Saint-Germain"


def stat(bt, pk, ms, *, home=MH, away=MA, **kw):
    return ar.evaluate_pick(bt, pk, home, away, 1, 2, extended_markets=True,
                            match_stats=ms, **kw)


def stat_reason(bt, pk, ms, *, extra_time=False, extended=True):
    return ar._pending_reason(bt, pk, home_name=MH, away_name=MA, extra_time=extra_time,
                              penalties=False, two_legged=False, scope_ft=False,
                              extended_markets=extended, match_stats=ms)


class StatsMarkets(unittest.TestCase):

    def test_corners_match_and_team(self):
        self.assertEqual(stat("Total Corners", "Over 9.5 Corners", MARSEILLE_PSG), "WIN")    # 10
        self.assertEqual(stat("Total Corners", "Under 9.5 Corners", MARSEILLE_PSG), "LOSS")
        self.assertEqual(stat("Team Corners", "Paris Saint-Germain Over 6.5 Corners",
                              MARSEILLE_PSG), "WIN")                                      # 7
        self.assertEqual(stat("Team Corners", "Marseille Over 3.5 Corners", MARSEILLE_PSG), "LOSS")
        self.assertEqual(stat("Total Corners", "Over 10 Corners", MARSEILLE_PSG), "VOID")
        self.assertEqual(stat("Total Corners", "Over 9.25 Corners", MARSEILLE_PSG), "PENDING")

    def test_second_yellow_counts_as_one_yellow_plus_one_red(self):
        # 2 + 1 yellows, 1 red, +1 for Weah's first yellow the feed leaves out = 5.
        self.assertEqual(stat("Total Cards", "Over 4.5 Cards", MARSEILLE_PSG), "WIN")
        self.assertEqual(stat("Total Cards", "Over 5.5 Cards", MARSEILLE_PSG), "LOSS")
        self.assertEqual(stat("Team Cards", "Marseille Over 3.5 Cards", MARSEILLE_PSG), "WIN")  # 4
        self.assertEqual(stat("Team Cards", "Paris Saint-Germain Under 1.5 Cards",
                              MARSEILLE_PSG), "WIN")                                       # 1

    def test_straight_red_counts_once(self):
        straight = dict(MARSEILLE_PSG, second_yellows=[0, 0])
        self.assertEqual(stat("Total Cards", "Over 4.5 Cards", straight), "LOSS")   # 4

    def test_red_card_without_readable_lineups_is_pending(self):
        unknown = dict(MARSEILLE_PSG, second_yellows=None)
        self.assertEqual(stat("Total Cards", "Over 4.5 Cards", unknown), "PENDING")
        self.assertIn("second yellow", stat_reason("Total Cards", "Over 4.5 Cards", unknown))
        # Corners do not need the lineups at all.
        self.assertEqual(stat("Total Corners", "Over 9.5 Corners", unknown), "WIN")

    def test_shots_on_target(self):
        self.assertEqual(stat("Total Shots on Target", "Over 13.5 Shots on Target",
                              MARSEILLE_PSG), "WIN")                                       # 14
        self.assertEqual(stat("Team Shots on Target", "Marseille Under 4.5 Shots on Target",
                              MARSEILLE_PSG), "WIN")                                       # 4

    def test_partial_stats_are_pending_with_a_reason_never_settled(self):
        # The 6-of-316 case: the stats block exists but a needed figure is missing.
        partial = {"full": {"corners": [7, 8], "ShotsOnTarget": [6, 3]},
                   "h1": None, "h2": None, "second_yellows": [0, 0]}
        self.assertEqual(stat("Total Cards", "Over 3.5 Cards", partial), "PENDING")
        reason = stat_reason("Total Cards", "Over 3.5 Cards", partial)
        self.assertIn("yellow_cards", reason)
        self.assertIn("never settled on partial data", reason)
        self.assertEqual(stat("Total Corners", "Over 9.5 Corners", partial), "WIN")
        # Nothing readable at all.
        self.assertEqual(stat("Total Corners", "Over 9.5 Corners", None), "PENDING")
        self.assertEqual(stat("Total Corners", "Over 9.5 Corners",
                              {"full": {}, "h1": None, "h2": None, "second_yellows": [0, 0]}),
                         "PENDING")

    def test_extra_time_uses_the_half_split(self):
        # Saudi Arabia vs Qatar, 3 Oct 2026 (0-0, pens): corners full [12, 2],
        # halves [6, 1] + [3, 1] — the 90-minute total is 11, not 14.
        saudi = {"full": {"corners": [12, 2], "yellow_cards": [2, 0], "red_cards": [0, 0],
                          "ShotsOnTarget": [7, 0]},
                 "h1": {"corners": [6, 1], "yellow_cards": [1, 0], "red_cards": [0, 0],
                        "ShotsOnTarget": [4, 0]},
                 "h2": {"corners": [3, 1], "yellow_cards": [0, 0], "red_cards": [0, 0],
                        "ShotsOnTarget": [1, 0]},
                 "second_yellows": [0, 0]}
        args = dict(home="Saudi Arabia", away="Qatar", extra_time=True, penalties=True)
        self.assertEqual(stat("Total Corners", "Over 11.5 Corners", saudi, **args), "LOSS")
        self.assertEqual(stat("Total Corners", "Over 10.5 Corners", saudi, **args), "WIN")
        self.assertEqual(stat("Total Cards", "Under 1.5 Cards", saudi, **args), "WIN")   # 1

    def test_extra_time_without_a_half_split_is_pending(self):
        # Kilmarnock vs Aberdeen, 12 Sep 2026: full-match figures only.
        kilmarnock = {"full": {"corners": [7, 8], "ShotsOnTarget": [6, 3]},
                      "h1": {}, "h2": {}, "second_yellows": [0, 0]}
        self.assertEqual(stat("Total Corners", "Over 9.5 Corners", kilmarnock,
                              extra_time=True), "PENDING")
        self.assertIn("half split", stat_reason("Total Corners", "Over 9.5 Corners",
                                                kilmarnock, extra_time=True))

    def test_extra_time_dismissal_with_a_second_yellow_is_pending(self):
        ms = {"full": {"corners": [5, 5], "yellow_cards": [3, 2], "red_cards": [1, 0],
                       "ShotsOnTarget": [4, 4]},
              "h1": {"corners": [2, 2], "yellow_cards": [1, 1], "red_cards": [0, 0],
                     "ShotsOnTarget": [2, 2]},
              "h2": {"corners": [2, 2], "yellow_cards": [1, 1], "red_cards": [0, 0],
                     "ShotsOnTarget": [1, 1]},
              "second_yellows": [1, 0]}
        self.assertEqual(stat("Total Cards", "Over 3.5 Cards", ms, extra_time=True), "PENDING")

    def test_full_time_scope_and_unsupported_stats_are_pending(self):
        for bt, pk in (("Total Corners", "Over 9.5 Corners (Full-Time incl. ET/Pens)"),
                       ("Total Shots", "Over 25.5 Shots"),
                       ("Total Bookings", "Over 40.5 Booking Points"),
                       ("Total Yellow Cards", "Over 3.5 Yellow Cards"),
                       ("1st Half Corners", "Over 4.5 Corners"),
                       ("Corner Handicap", "Marseille -1.5 Corners"),
                       ("Most Corners", "Paris Saint-Germain")):
            with self.subTest(bt=bt):
                self.assertEqual(stat(bt, pk, MARSEILLE_PSG), "PENDING")

    def test_a_club_named_red_is_not_a_red_card_market(self):
        self.assertEqual(ar.stat_market_kind("Team Corners", "Red Bull Salzburg Over 4.5 Corners",
                                             "Red Bull Salzburg", "Sturm Graz"), "corners")

    def test_production_never_settles_stats_even_when_handed_them(self):
        self.assertEqual(ar.evaluate_pick("Total Corners", "Over 9.5 Corners", MH, MA, 1, 2,
                                          match_stats=MARSEILLE_PSG), "PENDING")
        self.assertIn("stats market", stat_reason("Total Corners", "Over 9.5 Corners",
                                                   MARSEILLE_PSG, extended=False))

    def test_stats_markets_are_tagged_extended(self):
        for bt, pk in (("Total Corners", "Over 9.5 Corners"),
                       ("Team Cards", "Arsenal Over 1.5 Cards"),
                       ("Total Shots on Target", "Over 8.5 Shots on Target"),
                       ("Total Bookings", "Over 40.5 Booking Points")):
            with self.subTest(bt=bt):
                self.assertTrue(ar.is_extended_market(bt, pk))


class StatsSettlementLoop(unittest.TestCase):
    """The wait after kickoff, the fetch, and that production never fetches."""

    ROW = {"sheet_row": 11, "date": date(2026, 9, 20), "match": "Marseille vs Paris Saint-Germain",
           "bet_type": "Total Corners", "pick": "Over 9.5 Corners", "odds": 1.9,
           "est_odds": 1.9, "market_odds": None}

    def _run(self, scope, minutes_after_kickoff, **kw):
        from datetime import datetime, timedelta, timezone
        ko = datetime.now(timezone.utc) - timedelta(minutes=minutes_after_kickoff)
        game = _fx(5802945, MH, MA, hs=1, as_=2, finished=True,
                   utc=ko.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        writes = []
        with mock.patch.object(ar, "init_excel"), \
             mock.patch.object(ar, "_fetch_matches_cached",
                               side_effect=lambda d: [game] if d == date(2026, 9, 20) else []), \
             mock.patch.object(ar, "fetch_match_stats", return_value=MARSEILLE_PSG) as fetch:
            stats, _ = ar.run_auto_results(
                7, pending_source=lambda _d: [dict(self.ROW)],
                row_writer=lambda row, res, pnl: writes.append((row, res, pnl)),
                finalizer=lambda: None, alert_scope=scope, **kw)
        return stats, writes, fetch

    def test_before_the_settle_delay_it_waits_without_fetching_or_alerting(self):
        stats, writes, fetch = self._run("test-stats-early",
                                         ar.STATS_SETTLE_AFTER_KICKOFF_MIN - 10,
                                         extended_markets=True)
        fetch.assert_not_called()
        self.assertEqual(writes, [])
        self.assertEqual(stats["not_finished"], 1)
        self.assertEqual(stats["pending_alerts"], [])

    def test_after_the_delay_the_shadow_fetches_once_and_settles(self):
        stats, writes, fetch = self._run("test-stats-ready",
                                         ar.STATS_SETTLE_AFTER_KICKOFF_MIN + 10,
                                         extended_markets=True)
        fetch.assert_called_once_with(5802945, halves=False, dismissals=False)
        self.assertEqual(writes, [(11, "WIN", ar.pnl_for_result("WIN", 1.9))])

    def test_production_never_fetches_stats(self):
        stats, writes, fetch = self._run("test-stats-prod",
                                         ar.STATS_SETTLE_AFTER_KICKOFF_MIN + 10)
        fetch.assert_not_called()
        self.assertEqual(writes, [])
        self.assertEqual(stats["pending"], 1)

    def test_shadow_prompt_states_the_card_counting_rule(self):
        import opus_shadow as O
        self.assertIn("second yellow counts 2", O._OPUS_EXTENDED_MARKETS_PROMPT)
        self.assertIn("Total Corners", O._OPUS_EXTENDED_MARKETS_PROMPT)


if __name__ == "__main__":
    unittest.main()
