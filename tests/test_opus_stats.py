"""
Opus shadow ONLY (4 Oct 2026): the pre-match stat averages fed to Opus, and the
'Estimated odds' handling of corners / cards / shots-on-target picks on the Opus
tab. Pure — no network, no Sheets, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402
import opus_shadow as OS  # noqa: E402
import opus_stats as ST  # noqa: E402
import opus_tracker as OT  # noqa: E402


def _stats(corners, yellow, red, sot):
    return {"corners": corners, "yellow_cards": yellow, "red_cards": red, "ShotsOnTarget": sot}


def _feed_match(mid, home_id, away_id, ko="2026-09-20T14:00:00.000Z", **status):
    return {"id": mid, "home": {"id": home_id}, "away": {"id": away_id},
            "status": {"finished": True, "utcTime": ko, **status}}


class NinetyMinuteFigures(unittest.TestCase):
    def test_regular_match_reads_the_full_figures(self):
        fig = ST.ninety_minute_figures(
            {"full": _stats([6, 3], [2, 1], [0, 0], [5, 2]), "second_yellows": [0, 0]}, False)
        self.assertEqual(fig, {"corners": [6, 3], "cards": [2, 1], "shots_on_target": [5, 2]})

    def test_extra_time_uses_the_halves_never_the_full_figure(self):
        fig = ST.ninety_minute_figures({
            "full": _stats([9, 5], [3, 3], [0, 0], [8, 4]),
            "h1": _stats([3, 2], [1, 1], [0, 0], [2, 1]),
            "h2": _stats([4, 2], [1, 1], [0, 0], [3, 2]),
            "second_yellows": [0, 0]}, True)
        self.assertEqual(fig["corners"], [7, 4])
        self.assertEqual(fig["shots_on_target"], [5, 3])

    def test_extra_time_without_a_half_split_does_not_count(self):
        fig = ST.ninety_minute_figures(
            {"full": _stats([9, 5], [3, 3], [0, 0], [8, 4]), "h1": {}, "h2": {}}, True)
        self.assertIn("incomplete", fig)

    def test_second_yellow_counts_two_like_the_cards_market(self):
        # Feed books a second yellow as 1 red + 0 yellows; the lineup event adds it back.
        fig = ST.ninety_minute_figures(
            {"full": _stats([4, 4], [1, 2], [1, 0], [3, 3]), "second_yellows": [1, 0]}, False)
        self.assertEqual(fig["cards"], [3, 2])

    def test_red_card_with_unreadable_lineups_does_not_count(self):
        fig = ST.ninety_minute_figures(
            {"full": _stats([4, 4], [1, 2], [1, 0], [3, 3]), "second_yellows": None}, False)
        self.assertIn("incomplete", fig)

    def test_partial_stats_do_not_count(self):
        full = _stats([4, 4], [1, 2], [0, 0], [3, 3])
        del full["corners"]
        self.assertIn("incomplete", ST.ninety_minute_figures({"full": full}, False))


class TeamSummary(unittest.TestCase):
    def _figs(self, n, incomplete=0):
        matches, figures = [], {}
        for i in range(n):
            home = i % 2 == 0                      # team 1 alternates home / away
            m = _feed_match(i, 1 if home else 9, 9 if home else 1)
            matches.append(m)
            figures[i] = ({"incomplete": "x"} if i < incomplete else
                          {"corners": [6, 2] if home else [2, 6],
                           "cards": [1, 3] if home else [3, 1],
                           "shots_on_target": [5, 1] if home else [1, 5]})
        return matches, figures

    def test_for_and_against_follow_the_teams_side(self):
        matches, figures = self._figs(10)
        s = ST.team_summary(1, matches, figures)
        self.assertEqual(s["matches_averaged"], 10)
        self.assertEqual(s["per_match_90min"], {
            "corners_for": 6.0, "corners_against": 2.0, "cards_for": 1.0,
            "cards_against": 3.0, "shots_on_target_for": 5.0, "shots_on_target_against": 1.0})

    def test_fewer_than_five_complete_matches_says_so_instead_of_averaging(self):
        matches, figures = self._figs(10, incomplete=6)
        s = ST.team_summary(1, matches, figures)
        self.assertIsInstance(s, str)
        self.assertTrue(s.startswith("INSUFFICIENT DATA"))
        self.assertIn("only 4 of", s)

    def test_five_complete_matches_is_enough(self):
        matches, figures = self._figs(10, incomplete=5)
        self.assertEqual(ST.team_summary(1, matches, figures)["matches_averaged"], 5)


class MatchIdCache(unittest.TestCase):
    def setUp(self):
        ST._match_cache.clear()

    def test_a_match_is_fetched_once(self):
        raw = {"full": _stats([4, 4], [1, 2], [0, 0], [3, 3]), "h1": None, "h2": None,
               "second_yellows": [0, 0]}
        m = _feed_match(77, 1, 2, halfs={"firstHalfStarted": "x"})
        c = {"hits": 0, "fetched": 0, "failed": 0}
        with mock.patch("auto_results.fetch_match_stats", return_value=raw) as f:
            ST._match_figures(m, c)
            ST._match_figures(m, c)
        self.assertEqual(f.call_count, 1)
        self.assertEqual(c["hits"], 1)

    def test_a_failed_fetch_is_not_cached(self):
        m = _feed_match(78, 1, 2, halfs={"firstHalfStarted": "x"})
        c = {"hits": 0, "fetched": 0, "failed": 0}
        with mock.patch("auto_results.fetch_match_stats",
                        return_value={"full": None, "h1": None, "h2": None, "second_yellows": [0, 0]}):
            self.assertIsNone(ST._match_figures(m, c))
        self.assertNotIn(78, ST._match_cache)

    def test_extra_time_matches_fetch_the_halves(self):
        m = _feed_match(79, 1, 2, reason={"short": "AET"})
        with mock.patch("auto_results.fetch_match_stats",
                        return_value={"full": None, "h1": None, "h2": None,
                                      "second_yellows": [0, 0]}) as f:
            ST._match_figures(m, {"hits": 0, "fetched": 0, "failed": 0})
        self.assertTrue(f.call_args.kwargs["halves"])


class ShadowOnly(unittest.TestCase):
    def test_production_prompts_carry_no_stat_averages(self):
        for prompt in (M.SYSTEM_PROMPT, M.LEAGUE_SYSTEM_PROMPT):
            self.assertNotIn("TEAM STAT AVERAGES", prompt)

    def _run(self, stat_context, picks):
        text = SimpleNamespace(type="text", text=json.dumps({"picks": picks}))
        msg = SimpleNamespace(content=[text], stop_reason="end_turn",
                              usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        fx = {"Premier League": [{"match_id": 5, "home": "Arsenal", "away": "Leeds United",
                                  "home_id": 1, "away_id": 2}]}
        with mock.patch.object(M.claude.messages, "create", return_value=msg) as c, \
                mock.patch("usage_tracker.record_anthropic_usage"):
            out = OS.analyse_with_opus(fx, stat_context)
        return out, c.call_args.kwargs, fx

    def test_averages_reach_the_opus_payload_without_touching_the_pool(self):
        ctx = {5: {"home_stats_last10": {"matches_averaged": 9}, "away_stats_last10": "INSUFFICIENT DATA: x"}}
        _out, kw, fx = self._run(ctx, [])
        self.assertIn("TEAM STAT AVERAGES", kw["system"])
        self.assertIn('"home_stats_last10"', kw["messages"][0]["content"])
        self.assertIn("INSUFFICIENT DATA", kw["messages"][0]["content"])
        self.assertNotIn("home_stats_last10", fx["Premier League"][0])

    def test_stats_picks_are_tagged_estimated_odds_and_never_core(self):
        picks = [{"match": "Arsenal vs Leeds United", "league": "Premier League",
                  "bet_type": "Total Corners", "pick": "Over 9.5 Corners", "odds": 1.8},
                 {"match": "Arsenal vs Leeds United", "league": "Premier League",
                  "bet_type": "Draw No Bet", "pick": "Arsenal", "odds": 1.4}]
        out, _kw, _fx = self._run(None, picks)
        self.assertIn("Estimated odds", out[0]["market_tag"])
        self.assertEqual(out[0]["pick_tier"], "Extended")
        self.assertEqual(out[1]["market_tag"], OS.OPUS_MARKET_TAG)


class _FakeWs:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def get_all_values(self):
        return self.rows

    def get(self, rng):
        r = int(rng.split(":")[0][1:])
        return [self.rows[r - 1][2:4]]

    def update(self, values, range_name, value_input_option=None):
        self.updates.append((range_name, values))

    def batch_update(self, updates):
        self.updates.extend((u["range"], u["values"]) for u in updates)


def _row(bt, pk, result, pnl, tag=""):
    r = [""] * len(OT.OPUS_HEADERS)
    r[0], r[1], r[2], r[3] = "04-Oct-2026", "Arsenal vs Leeds United", bt, pk
    r[OT.OPUS_HEADERS.index("Result")] = result
    r[OT.OPUS_HEADERS.index("Profit/Loss")] = pnl
    r[OT.OPUS_HEADERS.index("Stake EUR (SIM)")] = "100"
    r[OT.OPUS_HEADERS.index("Market Tag")] = tag
    return r


class EstimatedOddsOnTheOpusTab(unittest.TestCase):
    def test_classification(self):
        self.assertTrue(OT.is_estimated_odds_pick("Team Cards", "Arsenal Under 1.5 Cards"))
        self.assertTrue(OT.is_estimated_odds_pick("Total Shots on Target", "Over 8.5 Shots on Target"))
        self.assertFalse(OT.is_estimated_odds_pick("Match Winner", "Cardiff Win"))
        self.assertFalse(OT.is_estimated_odds_pick("Over/Under", "Over 2.5 Goals"))

    def test_settling_a_stats_pick_writes_the_verdict_and_no_pnl(self):
        ws = _FakeWs([OT.OPUS_HEADERS, _row("Total Corners", "Over 9.5 Corners", "", "")])
        with mock.patch.object(OT, "_opus_ws", return_value=ws):
            self.assertTrue(OT.update_opus_row_result(2, "WIN", 0.8))
        self.assertEqual(ws.updates[-1], ("G2:H2", [["WIN", ""]]))

    def test_settling_a_priced_pick_still_writes_pnl(self):
        ws = _FakeWs([OT.OPUS_HEADERS, _row("Match Winner", "Arsenal Win", "", "")])
        with mock.patch.object(OT, "_opus_ws", return_value=ws):
            OT.update_opus_row_result(2, "WIN", 0.8)
        self.assertEqual(ws.updates[-1], ("G2:H2", [["WIN", 0.8]]))

    def test_running_total_and_bankroll_skip_stats_rows_and_repair_them(self):
        ws = _FakeWs([OT.OPUS_HEADERS,
                      _row("Match Winner", "Arsenal Win", "WIN", "1.0"),
                      _row("Total Corners", "Over 9.5 Corners", "WIN", "0.8", tag="Extended market"),
                      _row("Match Winner", "Arsenal Win", "LOSS", "-1.0")])
        with mock.patch.object(OT, "_opus_ws", return_value=ws):
            OT.recalculate_opus_running_totals()
        got = dict(ws.updates)
        self.assertEqual(got["I2"], [[1.0]])
        self.assertEqual(got["I3"], [[""]])        # stats row: no running total
        self.assertEqual(got["I4"], [[0.0]])       # 1.0 - 1.0, the 0.8 never counted
        self.assertEqual(got["J4"], [[1000.0]])
        self.assertEqual(got["H3"], [[""]])        # stale P&L blanked
        self.assertEqual(got["S3"], [["Extended market · Estimated odds"]])

    def test_hit_rate_table_reports_line_and_hit_rate_only(self):
        rows = [OT.OPUS_HEADERS,
                _row("Total Corners", "Over 9.5 Corners", "WIN", ""),
                _row("Total Corners", "Over 9.5 Corners", "LOSS", ""),
                _row("Total Corners", "Over 9.5 Corners", "WIN", ""),
                _row("Team Cards", "Arsenal Under 1.5 Cards", "", ""),
                _row("Match Winner", "Arsenal Win", "WIN", "1.0")]
        table = OT.stats_market_rows(rows)
        self.assertIn(["Cards", "One team", "Under", 1.5, 1, 0, 0, 0, 0, ""], table)
        self.assertIn(["Corners", "Match total", "Over", 9.5, 3, 3, 2, 1, 0, 66.7], table)
        self.assertEqual(table[-1][:4], ["All stats markets", "", "", ""])
        self.assertEqual(table[-1][4], 4)
        self.assertNotIn("P&L", " ".join(OT.STATS_SUMMARY_HEADERS))


if __name__ == "__main__":
    unittest.main()
