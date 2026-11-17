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


def _summary(p1, p2, r1, r2, i1, i2, start="2026-08-05T21:05:00.000Z"):
    """A fixture summary in build_tennis_fixture_summary's shape."""
    return {"player1": p1, "player2": p2, "player1_rank": r1, "player2_rank": r2,
            "player1_id": i1, "player2_id": i2, "start_utc": start}


class PickFixtureRouting(unittest.TestCase):
    """17 Nov 2026: 88 of 562 picks named their match differently from the feed
    — almost always the pair reversed — so the exact-string lookup missed and
    they got no ranks, no player ids and no start time, and went to
    'tennis-picks-lower' whatever the ranks. Real misrouted rows below."""

    def setUp(self):
        from tennis_main import TENNIS_LOWER_TIER_LABEL, TENNIS_TOP_TIER_LABEL
        self.TOP, self.LOW = TENNIS_TOP_TIER_LABEL, TENNIS_LOWER_TIER_LABEL
        # Pool order as the feed lists it; the model wrote each pair reversed
        self.pool = {
            "ATP": [
                _summary("Thiago Agustin Tirante", "Taylor Fritz", 98, 4, 70001, 24008),
                _summary("Casper Ruud", "Joao Fonseca", 12, 30, 30001, 30002),
                _summary("Bryce Nakashima", "Quinn Vandecasteele", 260, 410, 40001, 40002),
                _summary("Arthur Rinderknech", "Brandon Nakashima", 25, 33, 40003, 40004),
            ],
            "WTA": [_summary("Ann Li", "Elena Rybakina", 60, 5, 50001, 50002,
                             start="2026-08-07T23:00:00.000Z")],
        }

    def _attach(self, match):
        from tennis_main import attach_fixture_context
        pick = {"match": match, "bet_type": "Match Winner", "pick": "x Win"}
        ok = attach_fixture_context(pick, self.pool)
        return ok, pick

    def test_fritz_reversed_gets_ranks_ids_start_and_top_tier(self):
        ok, p = self._attach("Taylor Fritz vs Thiago Agustin Tirante")
        self.assertTrue(ok)
        self.assertEqual(p["match"], "Thiago Agustin Tirante vs Taylor Fritz")
        self.assertEqual(p["ranks"], "#98 vs #4")
        self.assertEqual(p["rank_tier"], self.TOP)
        self.assertEqual(p["player_ids"], "atp:70001|24008")
        self.assertEqual(p["start_utc"], "2026-08-05T21:05:00.000Z")

    def test_fonseca_vs_ruud_reversed(self):
        ok, p = self._attach("Joao Fonseca vs Casper Ruud")
        self.assertTrue(ok)
        self.assertEqual(p["player_ids"], "atp:30001|30002")
        self.assertEqual(p["rank_tier"], self.TOP)

    def test_rybakina_reversed_resolves_on_the_wta_list(self):
        ok, p = self._attach("Elena Rybakina vs Ann Li")
        self.assertTrue(ok)
        self.assertEqual(p["player_ids"], "wta:50001|50002")
        self.assertEqual(p["rank_tier"], self.TOP)

    def test_accents_and_case_still_resolve(self):
        ok, p = self._attach("joão fonseca vs CASPER RUUD")
        self.assertTrue(ok)
        self.assertEqual(p["match"], "Casper Ruud vs Joao Fonseca")

    def test_exact_name_beats_a_namesake(self):
        # Brandon and Bryce Nakashima are both fuzzy matches for each other;
        # the opponent and the exact name must decide, never list order
        ok, p = self._attach("Brandon Nakashima vs Arthur Rinderknech")
        self.assertTrue(ok)
        self.assertEqual(p["player_ids"], "atp:40003|40004")

    def test_unknown_match_stays_lower_tier_without_ids(self):
        ok, p = self._attach("Novak Djokovic vs Rafael Nadal")
        self.assertFalse(ok)
        self.assertEqual(p["rank_tier"], self.LOW)
        self.assertEqual(p["player_ids"], "")

    def test_negative_control_the_old_exact_lookup_misses_the_reversal(self):
        exact = {f"{f['player1']} vs {f['player2']}" for fs in self.pool.values() for f in fs}
        self.assertNotIn("Taylor Fritz vs Thiago Agustin Tirante", exact)

    def test_ambiguity_is_refused(self):
        from tennis_main import best_fixture_match
        cands = [("A. Smith", "Jan Novak", 1), ("B. Smith", "Jan Novak", 2)]
        self.assertIsNone(best_fixture_match("Smith", "Jan Novak", cands))

    def test_settlement_fallback_uses_the_same_matcher(self):
        from tennis_auto_results import _find_fixture
        slate = [
            {"player1": {"name": "Bryce Nakashima"}, "player2": {"name": "Arthur Rinderknech"}, "id": 1},
            {"player1": {"name": "Arthur Rinderknech"}, "player2": {"name": "Brandon Nakashima"}, "id": 2},
        ]
        self.assertEqual(_find_fixture(slate, "Brandon Nakashima", "Arthur Rinderknech")["id"], 2)


def _row(bet, conf, result, pnl, bank=""):
    from tennis_excel_tracker import TENNIS_HEADERS
    r = [""] * len(TENNIS_HEADERS)
    r[0], r[1], r[2], r[3], r[4] = "10-Oct-2026", "A vs B", bet, "A Win", "1.80"
    r[5], r[6], r[7] = conf, result, str(pnl)
    r[TENNIS_HEADERS.index("Bankroll € (SIM)")] = bank
    return r


class TennisSummaryLabels(unittest.TestCase):
    """17 Nov 2026: 'ROI' showed the SIM bankroll drawdown (-55.3%) while the
    per-bet ROI was -5.6%, and 'best bet type' / 'best confidence' crowned
    12-pick groups because they lost least."""

    def _cells(self, rows):
        from tennis_excel_tracker import _tennis_summary_data
        return {str(r[0]).strip(): r[1] for r in _tennis_summary_data(rows) if r and len(r) > 1}

    def test_bankroll_change_is_not_called_roi(self):
        rows = [_row("Match Winner", "High", "LOSS", -1, bank="90")] * 10
        cells = self._cells(rows)
        self.assertNotIn("ROI", cells)
        self.assertEqual(cells["Bankroll change"], "-10.0%")
        self.assertEqual(cells["ROI per unit staked"], "-100.0%")

    def test_best_group_needs_twenty_settled_picks(self):
        rows = ([_row("Match Winner", "Medium", "LOSS", -1)] * 25
                + [_row("Total Games", "Low", "WIN", 0.9)] * 12)
        cells = self._cells(rows)
        self.assertEqual(cells["Best bet type (20+ settled)"], "Match Winner (25 settled)")
        self.assertEqual(cells["Best confidence level (20+ settled)"], "Medium (25 settled)")

    def test_no_group_large_enough_says_so(self):
        cells = self._cells([_row("Total Games", "Low", "WIN", 0.9)] * 12)
        self.assertTrue(cells["Best bet type (20+ settled)"].startswith("n/a"))
        self.assertTrue(cells["Best confidence level (20+ settled)"].startswith("n/a"))


class PickTimeOdds(unittest.TestCase):
    """17 Nov 2026: pick-time market prices back on for tennis, closing-odds
    polling still off, the monthly reserve respected, ranked matching only."""

    EVENT = {"home_team": "Taylor Fritz", "away_team": "Thiago Agustin Tirante",
             "bookmakers": [{"markets": [{"key": "h2h", "outcomes": [
                 {"name": "Taylor Fritz", "price": 1.25},
                 {"name": "Thiago Agustin Tirante", "price": 4.10}]}]}]}

    def _pick(self, odds=1.30):
        return {"match": "Thiago Agustin Tirante vs Taylor Fritz", "bet_type": "Match Winner",
                "pick": "Taylor Fritz Win", "odds": odds}

    def _run(self, picks, exhausted=False, events=None):
        from unittest import mock
        import tennis_main as tm
        with mock.patch("usage_tracker.odds_budget_exhausted", return_value=exhausted), \
             mock.patch.object(tm, "fetch_active_tennis_sport_keys", return_value=["tennis_atp_x"]), \
             mock.patch.object(tm, "fetch_tennis_odds_events",
                               return_value=[self.EVENT] if events is None else events) as fetch:
            tm.enrich_tennis_picks_with_real_odds(picks)
        return fetch

    def test_switches(self):
        import tennis_main as tm
        self.assertTrue(tm.TENNIS_ODDS_API_ENABLED)
        self.assertFalse(tm.TENNIS_CLOSING_ODDS_ENABLED)

    def test_closing_odds_poller_makes_no_call(self):
        from unittest import mock
        import tennis_closing_odds as tco
        with mock.patch.object(tco, "get_unsettled_tennis_picks_with_start") as read, \
             mock.patch.object(tco, "fetch_tennis_odds_events") as fetch:
            tco.run_tennis_closing_odds_check()
        read.assert_not_called()
        fetch.assert_not_called()

    def test_market_price_attached_with_reversed_event_order(self):
        p = self._pick()
        self._run([p])
        self.assertEqual(p["market_odds"], 1.25)
        self.assertEqual(p["market_prob"], 80.0)

    def test_reserve_reached_means_no_billed_call(self):
        p = self._pick()
        fetch = self._run([p], exhausted=True)
        fetch.assert_not_called()
        self.assertNotIn("market_odds", p)

    def test_suspicious_price_is_dropped(self):
        p = self._pick(odds=4.50)  # 1.25 vs 4.50 is a 3.6x gap: a mismatch
        self._run([p])
        self.assertNotIn("market_odds", p)

    def test_h2h_side_is_ranked_not_first_match(self):
        from tennis_main import match_tennis_market_odds
        real = {"h2h": {"Juan Manuel Cerundolo": 2.40, "Francisco Cerundolo": 1.60}}
        pick = {"bet_type": "Match Winner", "pick": "Francisco Cerundolo Win"}
        self.assertEqual(match_tennis_market_odds(pick, real), 1.60)


class PickRules(unittest.TestCase):
    """New regime from 17 Nov 2026: no Handicap (games), no Total Games, no
    Match Winner at 2.00+ (on the price that will be paid); honest probability."""

    def _p(self, bet, odds, market=None, pick="A Win"):
        d = {"match": "A vs B", "bet_type": bet, "pick": pick, "odds": odds}
        if market:
            d["market_odds"] = market
        return d

    def test_cut_bet_types_are_dropped(self):
        from tennis_main import apply_tennis_pick_rules
        kept, dropped = apply_tennis_pick_rules([
            self._p("Handicap (games)", 1.85, pick="A -4.5"),
            self._p("Total Games", 1.90, pick="Over 21.5 Games"),
            self._p("Set Betting", 2.10, pick="A 2-0"),
        ])
        self.assertEqual([k["bet_type"] for k in kept], ["Set Betting"])
        self.assertEqual(len(dropped), 2)

    def test_match_winner_two_or_more_is_dropped(self):
        from tennis_main import apply_tennis_pick_rules
        kept, _ = apply_tennis_pick_rules([self._p("Match Winner", 2.00),
                                           self._p("Match Winner", 1.99)])
        self.assertEqual([k["odds"] for k in kept], [1.99])

    def test_market_price_decides_not_the_estimate(self):
        from tennis_main import apply_tennis_pick_rules
        kept, dropped = apply_tennis_pick_rules([
            self._p("Match Winner", 1.85, market=2.15),   # market says underdog
            self._p("Match Winner", 2.05, market=1.90),   # market says favourite
        ])
        self.assertEqual([k["market_odds"] for k in kept], [1.90])
        self.assertIn("market", dropped[0][1])

    def test_prompt_offers_only_the_two_markets_and_honest_probability(self):
        from tennis_main import TENNIS_SYSTEM_PROMPT as P
        self.assertNotIn("Handicap", P)
        self.assertNotIn("Total Games", P)
        self.assertNotIn("a value bet is precisely one where your probability", P)
        self.assertIn("2.00 or higher", P)
        self.assertIn("Do not derive it from the odds", P)


class TennisModelSwitch(unittest.TestCase):
    """17 Nov 2026: Opus 5.5 makes the tennis picks into its own 'Tennis Opus'
    tab; TENNIS_MODEL=claude-sonnet-4-6 switches back to Sonnet and the
    'Tennis Picks' tab with no code change."""

    FIXTURES = {"ATP": [_summary("Casper Ruud", "Joao Fonseca", 12, 30, 30001, 30002)]}
    TEXT = ('{"picks": [{"match": "Casper Ruud vs Joao Fonseca", "tour": "ATP", '
            '"bet_type": "Match Winner", "pick": "Casper Ruud Win", "odds": 1.6, '
            '"probability": 62, "confidence": "Medium", "reasoning": "x"}]}')

    def _env(self, value):
        import os
        from unittest import mock
        env = dict(os.environ)
        env.pop("TENNIS_MODEL", None)
        if value is not None:
            env["TENNIS_MODEL"] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def _message(self, stop="end_turn"):
        from types import SimpleNamespace as NS
        return NS(
            model="claude-opus-5-5", stop_reason=stop,
            usage=NS(input_tokens=1, output_tokens=1),
            content=[NS(type="thinking", thinking=""), NS(type="text", text=self.TEXT)],
        )

    def _call(self, model_env, stop="end_turn"):
        from unittest import mock
        import tennis_main as tm
        with self._env(model_env), \
             mock.patch.object(tm.claude.messages, "create", return_value=self._message(stop)) as create, \
             mock.patch("usage_tracker.record_anthropic_usage"):
            picks = tm.analyse_tennis_with_claude(self.FIXTURES)
        return picks, create.call_args.kwargs

    def test_default_is_opus_into_its_own_tab(self):
        from tennis_excel_tracker import active_tennis_tab, tennis_model
        with self._env(None):
            self.assertEqual(tennis_model(), "claude-opus-5-5")
            self.assertEqual(active_tennis_tab(), "Tennis Opus")

    def test_one_setting_switches_back_to_sonnet(self):
        from tennis_excel_tracker import active_tennis_tab, tennis_model
        with self._env("claude-sonnet-4-6"):
            self.assertEqual(tennis_model(), "claude-sonnet-4-6")
            self.assertEqual(active_tennis_tab(), "Tennis Picks")

    def test_unknown_model_falls_back_to_the_default(self):
        from tennis_excel_tracker import tennis_model
        with self._env("claude-something-else"):
            self.assertEqual(tennis_model(), "claude-opus-5-5")

    def test_opus_call_shape(self):
        picks, kw = self._call(None)
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["max_tokens"], 16000)
        self.assertEqual(kw["output_config"], {"effort": "high"})
        self.assertNotIn("temperature", kw)
        self.assertNotIn("extra_body", kw)       # no temperature 0 smuggled in
        self.assertNotIn("thinking", kw)         # always on; cannot be disabled
        # parsed from the first TEXT block, behind a thinking block
        self.assertEqual(picks[0]["pick"], "Casper Ruud Win")

    def test_sonnet_call_keeps_its_old_shape(self):
        _, kw = self._call("claude-sonnet-4-6")
        self.assertEqual(kw["model"], "claude-sonnet-4-6")
        self.assertEqual(kw["max_tokens"], 2048)
        self.assertEqual(kw["extra_body"], {"temperature": 0})

    def test_refusal_fails_the_run(self):
        with self.assertRaises(RuntimeError):
            self._call(None, stop="refusal")

    def test_opus_5_5_is_priced(self):
        from usage_tracker import ANTHROPIC_PRICING
        self.assertEqual(ANTHROPIC_PRICING["claude-opus-5-5"], (4.00, 20.00))

    def test_settlement_covers_both_tabs(self):
        from unittest import mock
        import tennis_auto_results as tar
        with mock.patch.object(tar, "get_pending_tennis_picks", return_value=[]) as pending:
            tar.run_tennis_auto_results()
        tabs = [c.kwargs.get("sheet_name") for c in pending.call_args_list]
        self.assertEqual(sorted(tabs), ["Tennis Opus", "Tennis Picks"])

    def test_alert_text_never_names_the_model(self):
        from unittest import mock
        import tennis_main as tm
        with mock.patch.object(tm, "send_to_discord", return_value=True) as send:
            tm._notify_tennis_picks_failed(
                "the analysis could not be completed",
                detail="Error code: 400 - claude-opus-5-5 (Anthropic) rejected the request")
        text = send.call_args.kwargs["message"].lower()
        for word in ("claude", "opus", "anthropic", "sonnet"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
