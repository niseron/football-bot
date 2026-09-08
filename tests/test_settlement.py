"""
Regression suite for football settlement: evaluate_pick, fixture matching and
the PENDING alert reason. Pure — no network, no Sheets, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

Added 8 Sep 2026 after row 357 (FC Porto vs Manchester City, Match Winner /
Manchester City Win) alerted as "no settlement rule matched bet type 'Match
Winner'". The rule was fine: the row had been matched to that afternoon's UEFA
Youth League game, FC Porto U19 vs Manchester City U19, because fixture matching
was plain substring containment. The cases below pin both halves — the most
common bet type in the book settles through the whole loop, and a youth,
reserve or women's fixture can never stand in for the senior one again.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_results as ar  # noqa: E402


# ── Fixtures as the feed lists them ──────────────────────────────────────────

def _fx(fid, home, away, *, hs=None, as_=None, finished=False, utc="2026-09-08T19:00:00.000Z",
        reason_short="FT", halfs=None, aggregate=None):
    status = {
        "utcTime": utc,
        "finished": finished,
        "started": finished,
        "cancelled": False,
        "halfs": halfs if halfs is not None else {"firstHalfStarted": "x", "secondHalfStarted": "y"},
    }
    if finished:
        status["scoreStr"] = f"{hs} - {as_}"
        status["reason"] = {"short": reason_short, "long": "Full-Time"}
    if aggregate:
        status["aggregatedStr"] = aggregate
    return {
        "id": fid,
        "home": {"id": fid * 10 + 1, "name": home, "longName": home, "score": hs},
        "away": {"id": fid * 10 + 2, "name": away, "longName": away, "score": as_},
        "status": status,
    }


# The feed on 8 Sep 2026, as fetched at 14:30 UTC: the youth game sat in the
# 8 Sep bucket and had finished 3-1; the senior game sat in the 9 Sep bucket.
PORTO_CITY_U19 = _fx(1000021084, "FC Porto U19", "Manchester City U19",
                     hs=3, as_=1, finished=True, utc="2026-09-08T11:00:00.000Z")
PORTO_CITY     = _fx(6106286, "FC Porto", "Manchester City",
                     utc="2026-09-08T19:00:00.000Z")


class MatchWinnerSettlement(unittest.TestCase):
    """A plain Match Winner pick — the most common bet type in the book."""

    def test_row_357_shape_away_team_name_pick(self):
        ev = lambda h, a: ar.evaluate_pick(
            "Match Winner", "Manchester City Win", "FC Porto", "Manchester City", h, a)
        self.assertEqual(ev(0, 2), "WIN")
        self.assertEqual(ev(3, 1), "LOSS")
        self.assertEqual(ev(1, 1), "LOSS")

    def test_home_team_name_pick(self):
        ev = lambda h, a: ar.evaluate_pick(
            "Match Winner", "Real Madrid Win", "Real Madrid", "Inter", h, a)
        self.assertEqual(ev(2, 0), "WIN")
        self.assertEqual(ev(0, 1), "LOSS")
        self.assertEqual(ev(2, 2), "LOSS")

    def test_generic_side_labels(self):
        for pick in ("Home Win", "Home", "1"):
            self.assertEqual(ar.evaluate_pick("Match Winner", pick, "FC Porto", "Manchester City", 1, 0), "WIN", pick)
            self.assertEqual(ar.evaluate_pick("Match Winner", pick, "FC Porto", "Manchester City", 0, 1), "LOSS", pick)
        for pick in ("Away Win", "Away", "2"):
            self.assertEqual(ar.evaluate_pick("Match Winner", pick, "FC Porto", "Manchester City", 0, 1), "WIN", pick)
            self.assertEqual(ar.evaluate_pick("Match Winner", pick, "FC Porto", "Manchester City", 1, 0), "LOSS", pick)

    def test_bet_type_aliases(self):
        for bt in ("Match Winner", "1X2", "Match Result", "Moneyline", "match winner"):
            self.assertEqual(ar.evaluate_pick(bt, "Inter Win", "Real Madrid", "Inter", 0, 1), "WIN", bt)

    def test_draw_pick(self):
        self.assertEqual(ar.evaluate_pick("Match Winner", "Draw", "Juventus", "Milan", 1, 1), "WIN")
        self.assertEqual(ar.evaluate_pick("Match Winner", "Draw", "Juventus", "Milan", 2, 1), "LOSS")
        # 'X' is a draw even when the letter sits inside a club name.
        self.assertEqual(ar.evaluate_pick("Match Winner", "X", "Ajax", "PSV", 0, 0), "WIN")
        self.assertEqual(ar.evaluate_pick("Match Winner", "X", "Ajax", "PSV", 1, 0), "LOSS")

    def test_case_and_time_scope_suffix(self):
        self.assertEqual(ar.evaluate_pick(
            "MATCH WINNER", "manchester city win (90 min)", "FC Porto", "Manchester City", 0, 1), "WIN")
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Manchester City Win (Full-Time incl. ET/Pens)",
            "FC Porto", "Manchester City", 1, 2), "WIN")

    def test_bare_draw_bet_type_still_folds_into_match_winner(self):
        # Row 340 (6 Sep 2026): bet type 'Draw' / pick 'Draw'.
        self.assertEqual(ar.evaluate_pick("Draw", "Draw", "Fiorentina", "Torino", 1, 1), "WIN")
        self.assertEqual(ar.evaluate_pick("Draw", "Draw", "Fiorentina", "Torino", 1, 2), "LOSS")
        # A side named under a Draw bet type is contradictory: never a guess.
        self.assertEqual(ar.evaluate_pick("Draw", "Torino Win", "Fiorentina", "Torino", 1, 2), "PENDING")
        # And the Draw normalisation must never touch a real Match Winner label.
        self.assertFalse(ar._is_draw_bet_type("match winner"))
        self.assertFalse(ar._is_draw_bet_type("draw no bet"))

    def test_pick_naming_neither_side_is_pending_never_a_guess(self):
        # Row 357 against the youth fixture it was wrongly matched to.
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Manchester City Win", "FC Porto U19", "Manchester City U19", 3, 1),
            "PENDING")
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Manchester City Win", "Arsenal", "Chelsea", 0, 2), "PENDING")

    def test_pick_matching_both_sides_is_pending(self):
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "City", "Manchester City", "Bristol City", 2, 0), "PENDING")

    def test_extra_time_single_match_settles_off_level_90_minute_margin(self):
        # A single match only reaches extra time from a level score, so the
        # 90-minute result is a draw whatever the published score says.
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Manchester City Win", "FC Porto", "Manchester City", 1, 2,
            extra_time=True), "LOSS")
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Draw", "FC Porto", "Manchester City", 1, 2, extra_time=True), "WIN")
        # Full-time scope pays on the published score instead.
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Manchester City Win (Full-Time incl. ET/Pens)",
            "FC Porto", "Manchester City", 1, 2, extra_time=True), "WIN")

    def test_penalties_with_full_time_scope_is_pending(self):
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Manchester City Win (Full-Time incl. ET/Pens)",
            "FC Porto", "Manchester City", 1, 1, extra_time=True, penalties=True), "PENDING")

    def test_two_legged_extra_time_uses_the_derived_margin(self):
        # LASK 5-1 Celtic after extra time, aggregate 5-4: LASK led by 3 at 90'.
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "Celtic Win", "LASK", "Celtic", 5, 1,
            extra_time=True, two_legged=True, aggregate="5 - 4"), "LOSS")
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "LASK Win", "LASK", "Celtic", 5, 1,
            extra_time=True, two_legged=True, aggregate="5 - 4"), "WIN")
        # An aggregate that cannot be trusted leaves the pick PENDING.
        self.assertEqual(ar.evaluate_pick(
            "Match Winner", "LASK Win", "LASK", "Celtic", 5, 1,
            extra_time=True, two_legged=True, aggregate="2 - 1"), "PENDING")


class FixtureMatching(unittest.TestCase):
    """_find_api_match must pick the senior fixture, not another squad's game."""

    def test_senior_fixture_beats_youth_fixture_listed_first(self):
        pool = [PORTO_CITY_U19, PORTO_CITY]  # 8 Sep bucket first, then 9 Sep
        self.assertIs(ar._find_api_match(pool, "FC Porto", "Manchester City"), PORTO_CITY)

    def test_youth_fixture_alone_is_no_match(self):
        self.assertIsNone(ar._find_api_match([PORTO_CITY_U19], "FC Porto", "Manchester City"))

    def test_other_squad_markers_are_rejected(self):
        cases = [
            ("Bayern München", "Bayern München II"),
            ("Real Madrid", "Real Madrid Castilla"),
            ("Manchester City", "Manchester City Women"),
            ("Arsenal", "Arsenal W"),
            ("Ajax", "Jong Ajax"),
            ("Barcelona", "Barcelona U-19"),
            ("Inter", "Inter Under 21"),
            ("FC Porto", "FC Porto B"),
            ("Chelsea", "Chelsea Ladies"),
            ("Liverpool", "Liverpool Reserves"),
        ]
        for query, api_name in cases:
            self.assertEqual(
                ar._side_score(ar._normalise_team(query), ar._normalise_team(api_name)), 0,
                f"{query!r} must not match {api_name!r}")

    def test_prefix_and_suffix_containment_still_matches(self):
        cases = [
            ("Porto", "FC Porto"),
            ("Milan", "AC Milan"),
            ("Schalke", "Schalke 04"),
            ("Atletico", "Atletico Madrid"),
            ("Hearts", "Heart of Midlothian Hearts"),
        ]
        for query, api_name in cases:
            self.assertEqual(
                ar._side_score(ar._normalise_team(query), ar._normalise_team(api_name)),
                ar._SIDE_CONTAIN, f"{query!r} should still match {api_name!r}")

    def test_diacritics_and_transliteration_still_bridge(self):
        pool = [_fx(1, "Lillestrøm", "Nordsjælland")]
        self.assertIs(ar._find_api_match(pool, "Lillestrom", "Nordsjaelland"), pool[0])

    def test_exact_fixture_beats_containment_fixture(self):
        exact   = _fx(2, "Inter", "Orlando City")
        contain = _fx(3, "Inter Miami", "Orlando City")
        self.assertIs(ar._find_api_match([contain, exact], "Inter", "Orlando City"), exact)

    def test_ambiguous_containment_refuses_to_choose(self):
        pool = [_fx(4, "Atletico Madrid", "Sevilla"), _fx(5, "Atletico Mineiro", "Sevilla")]
        with self.assertLogs(ar.log, level="WARNING"):
            self.assertIsNone(ar._find_api_match(pool, "Atletico", "Sevilla"))

    def test_same_fixture_in_both_buckets_is_not_ambiguous(self):
        pool = [PORTO_CITY, dict(PORTO_CITY)]  # same id twice
        self.assertIs(ar._find_api_match(pool, "FC Porto", "Manchester City"), PORTO_CITY)

    def test_reversed_orientation_is_only_found_when_asked(self):
        pool = [_fx(6, "NEC Nijmegen", "Bodø/Glimt")]
        self.assertIsNone(ar._find_api_match(pool, "Bodø/Glimt", "NEC Nijmegen"))
        self.assertIs(ar._find_api_match(pool, "Bodø/Glimt", "NEC Nijmegen", reversed_sides=True),
                      pool[0])

    def test_youth_fixture_is_not_accepted_reversed_either(self):
        self.assertIsNone(ar._find_api_match(
            [PORTO_CITY_U19], "Manchester City", "FC Porto", reversed_sides=True))


class PendingReason(unittest.TestCase):

    def test_names_the_fixture_when_the_pick_fits_neither_side(self):
        reason = ar._pending_reason(
            "Match Winner", "Manchester City Win",
            home_name="FC Porto U19", away_name="Manchester City U19",
            extra_time=False, penalties=False, two_legged=False, scope_ft=False)
        self.assertIn("FC Porto U19 vs Manchester City U19", reason)
        self.assertNotIn("no settlement rule", reason)

    def test_generic_reason_only_for_a_genuinely_unknown_bet_type(self):
        reason = ar._pending_reason(
            "Correct Score", "2-1", home_name="A", away_name="B",
            extra_time=False, penalties=False, two_legged=False, scope_ft=False)
        self.assertIn("no settlement rule matched bet type 'Correct Score'", reason)

    def test_alert_text_shows_the_matched_fixture(self):
        text = ar._format_pending_notification({
            "stage": "initial", "sheet_row": 357,
            "match": "FC Porto vs Manchester City", "bet_type": "Match Winner",
            "pick": "Manchester City Win", "score": "3-1", "status_note": "",
            "fixture": "FC Porto U19 vs Manchester City U19", "reason": "r",
            "hours_since_kickoff": 2.0,
        })
        self.assertIn("Fixture: FC Porto U19 vs Manchester City U19", text)


class SettlementLoop(unittest.TestCase):
    """run_auto_results end to end on row 357, with the feed stubbed."""

    ROW_357 = {
        "sheet_row": 357, "date": date(2026, 9, 8),
        "match": "FC Porto vs Manchester City", "bet_type": "Match Winner",
        "pick": "Manchester City Win", "odds": 1.68, "est_odds": 1.8, "market_odds": 1.68,
    }

    def _run(self, buckets: dict[date, list[dict]], scope: str):
        writes: list[tuple] = []
        with mock.patch.object(ar, "init_excel"), \
             mock.patch.object(ar, "_fetch_matches_cached", side_effect=lambda d: buckets.get(d, [])):
            stats, resolved = ar.run_auto_results(
                7,
                pending_source=lambda _days: [dict(self.ROW_357)],
                row_writer=lambda row, res, pnl: writes.append((row, res, pnl)),
                finalizer=lambda: None,
                alert_scope=scope,
            )
        return stats, resolved, writes

    def test_before_the_senior_game_the_row_waits_instead_of_alerting(self):
        buckets = {date(2026, 9, 8): [PORTO_CITY_U19], date(2026, 9, 9): [PORTO_CITY]}
        stats, resolved, writes = self._run(buckets, "test-357-waiting")
        self.assertEqual(stats["not_finished"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(stats["pending_alerts"], [])
        self.assertEqual(writes, [])

    def test_after_the_senior_game_the_row_settles(self):
        senior = _fx(6106286, "FC Porto", "Manchester City", hs=0, as_=2, finished=True)
        buckets = {date(2026, 9, 8): [PORTO_CITY_U19], date(2026, 9, 9): [senior]}
        stats, resolved, writes = self._run(buckets, "test-357-settled")
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(writes, [(357, "WIN", ar.pnl_for_result("WIN", 1.68))])
        self.assertEqual(resolved[0]["home_name"], "FC Porto")
        self.assertEqual(resolved[0]["away_name"], "Manchester City")
        self.assertEqual((resolved[0]["home_score"], resolved[0]["away_score"]), (0, 2))

    def test_home_loss_settles_too(self):
        senior = _fx(6106286, "FC Porto", "Manchester City", hs=2, as_=1, finished=True)
        stats, resolved, writes = self._run({date(2026, 9, 9): [senior]}, "test-357-loss")
        self.assertEqual(writes, [(357, "LOSS", -1.0)])

    def test_only_the_youth_game_in_the_feed_means_not_found_not_pending(self):
        stats, resolved, writes = self._run({date(2026, 9, 8): [PORTO_CITY_U19]}, "test-357-youth-only")
        self.assertEqual(stats["no_match"], 1)
        self.assertEqual(stats["pending"], 0)
        self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
