"""
Opus shadow ONLY (4 Oct 2026): the referee cards table and what Opus is told
about a fixture's referee. Pure — no network, no Sheets, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402
import opus_referees as R  # noqa: E402
import opus_shadow as OS  # noqa: E402

TURPIN = {"name": "Clement Turpin", "country": "France", "code": "FRA"}


def _rows(name, country, totals):
    return [{"Referee": name, "Country": country, "Cards Total": str(t)} for t in totals]


def _never(_n, _c):
    return False


class RefereeSummary(unittest.TestCase):
    def test_five_matches_gives_an_average_and_count(self):
        idx = R.referee_index(_rows("Clement Turpin", "France", [3, 4, 5, 4, 6]))
        self.assertEqual(R.referee_summary(TURPIN, idx, _never),
                         {"name": "Clement Turpin", "country": "France",
                          "cards_per_match_90min": 4.4, "matches": 5})

    def test_under_five_matches_is_unknown(self):
        idx = R.referee_index(_rows("Clement Turpin", "France", [3, 4, 5, 4]))
        self.assertEqual(R.referee_summary(TURPIN, idx, _never), R.REF_UNKNOWN)

    def test_blank_referee_or_country_is_unknown(self):
        idx = R.referee_index(_rows("Clement Turpin", "France", [3] * 9))
        for ref in (None, {"name": "", "country": "", "code": ""},
                    {"name": "Clement Turpin", "country": "", "code": ""}):
            self.assertEqual(R.referee_summary(ref, idx, _never), R.REF_UNKNOWN)

    def test_matching_is_exact_name_and_country(self):
        idx = R.referee_index(_rows("Clément Turpin", "France", [3] * 9)
                              + _rows("Clement Turpin", "Belgium", [3] * 9))
        self.assertEqual(R.referee_summary(TURPIN, idx, _never), R.REF_UNKNOWN)

    def test_a_collision_or_an_unreadable_search_is_unknown(self):
        idx = R.referee_index(_rows("Clement Turpin", "France", [3] * 9))
        self.assertEqual(R.referee_summary(TURPIN, idx, lambda n, c: True), R.REF_UNKNOWN)
        self.assertEqual(R.referee_summary(TURPIN, idx, lambda n, c: None), R.REF_UNKNOWN)

    def test_incomplete_matches_do_not_count(self):
        rows = _rows("Clement Turpin", "France", [3, 4, 5, 4]) + [
            {"Referee": "Clement Turpin", "Country": "France", "Cards Total": ""}]
        self.assertEqual(R.referee_summary(TURPIN, R.referee_index(rows), _never), R.REF_UNKNOWN)


class Collision(unittest.TestCase):
    def setUp(self):
        R._collision_cache.clear()

    def _search(self, suggestions):
        return {"status": "success", "response": {"suggestions": suggestions}}

    def test_two_referees_with_the_same_name_and_country_collide(self):
        s = [{"type": "referee", "name": "Michael Oliver", "ccode": "ENG"},
             {"type": "referee", "name": "Michael Oliver", "ccode": "ENG"},
             {"type": "player", "name": "Michael Oliver"}]
        with mock.patch("auto_results._rapid_get", return_value=self._search(s)):
            self.assertTrue(R.referee_collides("Michael Oliver", "ENG"))

    def test_one_referee_plus_a_player_namesake_does_not(self):
        s = [{"type": "referee", "name": "Michael Oliver", "ccode": "ENG"},
             {"type": "player", "name": "Michael Oliver"}]
        with mock.patch("auto_results._rapid_get", return_value=self._search(s)):
            self.assertFalse(R.referee_collides("Michael Oliver", "ENG"))

    def test_failed_search_is_none_and_not_cached(self):
        with mock.patch("auto_results._rapid_get", return_value=None):
            self.assertIsNone(R.referee_collides("X Y", "ENG"))
        self.assertNotIn(("X Y", "ENG"), R._collision_cache)


def _m(mid, lid, h, a, day="2026-09-20"):
    return {"id": mid, "leagueId": lid,
            "home": {"id": h, "longName": f"T{h}"}, "away": {"id": a, "longName": f"T{a}"},
            "status": {"finished": True, "utcTime": f"{day}T14:00:00.000Z"}}


class _OpenGuard:
    """Always allows, never touches the network."""
    reason = ""

    def __init__(self, call_cap=None):
        pass

    def allows(self, need=R.QUOTA_MATCH_MARGIN):
        return True

    def spent_outside(self, n=1):
        pass

    def left(self):
        return None


class _CountdownGuard(_OpenGuard):
    """Allows `n` fetch checks, then stops as the 30% floor would."""
    def __init__(self, n):
        self.n = n

    def allows(self, need=R.QUOTA_MATCH_MARGIN):
        self.n -= 1
        if self.n < 0:
            self.reason = "quota floor: test"
            return False
        return True


class UpdateTable(unittest.TestCase):
    def setUp(self):
        R._rows.clear(); R._scanned_days.clear(); R._league_competition.clear()
        R._loaded = True
        self.flushed = []
        self.patches = [
            mock.patch.object(R, "SEASON_START", date(2026, 9, 20)),
            mock.patch.object(R, "HISTORY_START", date(2026, 9, 18)),
            mock.patch.object(R, "QuotaGuard", _OpenGuard),
            mock.patch.object(R, "load"),                 # never the live Sheet
            mock.patch.object(R, "_flush", side_effect=lambda r, m: (self.flushed.extend(r), r.clear(), m.clear())),
            mock.patch("opus_stats._load_cache"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        R._loaded = False

    def _run(self, feed, refs, figs, parent=None, **kw):
        with mock.patch("main._fetch_day_matches", side_effect=lambda d: feed.get(d, [])), \
                mock.patch.object(R, "fetch_referee", side_effect=lambda mid: refs.get(mid)), \
                mock.patch("opus_stats._match_figures", side_effect=lambda m, c: figs.get(m["id"])), \
                mock.patch("main._resolve_parent_league", return_value=parent), \
                mock.patch("time.sleep"):
            return R.update_table(today=date(2026, 9, 21), **kw)

    # ── 2025-26 history window (14 Oct 2026) ─────────────────────────────────

    def test_history_stores_only_the_six_domestic_leagues(self):
        R._league_competition[555] = "Champions League"
        feed = {date(2026, 9, 18): [_m(1, 47, 10, 11, "2026-09-18"), _m(2, 555, 12, 13, "2026-09-18")]}
        figs = {i: {"cards": [1, 2]} for i in (1, 2)}
        c = self._run(feed, {1: TURPIN, 2: TURPIN}, figs, history=True)
        self.assertEqual(c["matches"], 1)
        self.assertEqual(R._rows[1]["Competition"], "Premier League")
        self.assertNotIn(2, R._rows)                  # UEFA is current-season only
        self.assertIn("20260918", R._scanned_days)

    def test_history_window_ends_where_the_season_starts(self):
        feed = {date(2026, 9, 20): [_m(1, 47, 10, 11)]}
        c = self._run(feed, {1: TURPIN}, {1: {"cards": [1, 1]}}, history=True)
        self.assertNotIn(1, R._rows)                  # 20 Sep is current season
        self.assertNotIn("20260920", R._scanned_days)
        self.assertEqual(c["open_days"], 2)           # 18-19 Sep: an empty feed stays open

    def test_history_looks_up_only_blocks_sharing_a_jupiler_club(self):
        R._rows[8] = {"Home ID": "10", "Away ID": "11", "Competition": "Premier League",
                      "Referee": "", "Country": "", "Cards Total": ""}
        R._rows[9] = {"Home ID": "20", "Away ID": "21", "Competition": "Jupiler Pro League",
                      "Referee": "", "Country": "", "Cards Total": ""}
        feed = {date(2026, 9, 18): [_m(1, 888, 10, 60, "2026-09-18"),    # a PL club's cup tie
                                    _m(2, 900433, 20, 61, "2026-09-18")]}  # last season's Jupiler id
        with mock.patch("main._resolve_parent_league", return_value=40) as res, \
                mock.patch("main._fetch_day_matches", side_effect=lambda d: feed.get(d, [])), \
                mock.patch.object(R, "fetch_referee", return_value=TURPIN), \
                mock.patch("opus_stats._match_figures", return_value={"cards": [2, 2]}), \
                mock.patch("time.sleep"):
            R.update_table(today=date(2026, 9, 21), history=True)
        self.assertEqual([c.args[0]["id"] for c in res.call_args_list], [2])
        self.assertEqual(R._rows[2]["Competition"], "Jupiler Pro League")

    def test_the_shadow_window_never_walks_history(self):
        feed = {date(2026, 9, 18): [_m(1, 47, 10, 11, "2026-09-18")]}
        self._run(feed, {1: TURPIN}, {1: {"cards": [1, 1]}})
        self.assertNotIn(1, R._rows)

    def test_a_guard_stop_saves_what_it_has_and_leaves_the_day_open(self):
        feed = {date(2026, 9, 20): [_m(1, 47, 10, 11), _m(2, 47, 12, 13)]}
        figs = {i: {"cards": [1, 1]} for i in (1, 2)}
        c = self._run(feed, {1: TURPIN, 2: TURPIN}, figs, guard=_CountdownGuard(2))
        self.assertEqual(c["stopped"], "quota floor: test")
        self.assertEqual([r["Match ID"] for r in self.flushed], ["1"])   # flushed, not lost
        self.assertNotIn(2, R._rows)
        self.assertNotIn("20260920", R._scanned_days)                   # retried next night

    def test_a_closed_guard_fetches_nothing(self):
        feed = {date(2026, 9, 20): [_m(1, 47, 10, 11)]}
        with mock.patch("main._fetch_day_matches", side_effect=lambda d: feed.get(d, [])) as f:
            c = R.update_table(today=date(2026, 9, 21), guard=_CountdownGuard(0))
        f.assert_not_called()
        self.assertTrue(c["stopped"])

    def test_nightly_batch_costs_nothing_once_both_windows_are_scanned(self):
        R._scanned_days.update({"20260918", "20260919", "20260920"})

        class _NoReads(_OpenGuard):
            def allows(self, need=R.QUOTA_MATCH_MARGIN):
                raise AssertionError("a fully scanned table must not even read the quota")

        with mock.patch("main._fetch_day_matches") as f, \
                mock.patch.object(R, "QuotaGuard", _NoReads):
            out = R.nightly_backfill(today=date(2026, 9, 21))
        f.assert_not_called()
        self.assertEqual((out["season"]["open_days"], out["history"]["open_days"]), (0, 0))

    def test_the_shadow_skips_fixture_lookups_under_the_floor(self):
        with mock.patch.object(R, "QuotaGuard", lambda cap=None: _CountdownGuard(0)), \
                mock.patch.object(R, "update_table"), \
                mock.patch.object(R, "fetch_referee") as f:
            out = R.build_referee_context({"Premier League": [{"match_id": 7}]})
        f.assert_not_called()
        self.assertEqual(out, {7: {"referee": R.REF_UNKNOWN}})


    def test_tracked_match_is_stored_with_ninety_minute_cards(self):
        feed = {date(2026, 9, 20): [_m(1, 47, 10, 11), _m(2, 999999, 50, 51)]}
        c = self._run(feed, {1: TURPIN}, {1: {"corners": [1, 1], "cards": [2, 3], "shots_on_target": [1, 1]}})
        self.assertEqual(c["matches"], 1)
        row = R._rows[1]
        self.assertEqual((row["Competition"], row["Referee"], row["Country"], row["Cards Total"]),
                         ("Premier League", "Clement Turpin", "France", "5"))
        self.assertNotIn(2, R._rows)               # untracked league, no shared team: ignored
        self.assertIn("20260920", R._scanned_days)

    def test_a_failed_fetch_leaves_the_day_open(self):
        feed = {date(2026, 9, 20): [_m(1, 47, 10, 11)]}
        self._run(feed, {1: None}, {})
        self.assertNotIn(1, R._rows)
        self.assertNotIn("20260920", R._scanned_days)

    def test_incomplete_stats_are_stored_without_cards(self):
        feed = {date(2026, 9, 20): [_m(1, 47, 10, 11)]}
        self._run(feed, {1: TURPIN}, {1: {"incomplete": "no corners"}})
        self.assertEqual(R._rows[1]["Cards Total"], "")
        self.assertEqual(R._rows[1]["Note"], "no corners")

    def test_a_rotated_uefa_id_sharing_a_team_is_resolved_and_tracked(self):
        R._rows[9] = {"Home ID": "10", "Away ID": "12", "Referee": "", "Country": "", "Cards Total": ""}
        feed = {date(2026, 9, 20): [_m(3, 777777, 10, 60)]}
        self._run(feed, {3: TURPIN}, {3: {"cards": [1, 1]}}, parent=42)
        self.assertEqual(R._rows[3]["Competition"], "Champions League")
        self.assertEqual(R._league_competition[777777], "Champions League")

    def test_scanned_days_and_stored_matches_are_never_fetched_again(self):
        R._scanned_days.add("20260920")
        with mock.patch("main._fetch_day_matches") as f:
            self._run({}, {}, {})
        f.assert_not_called()


class Guard(unittest.TestCase):
    """The 30% floor, read off the plan's own headers. No network."""

    def _guard(self, remaining, limit=20000, cap=None, live=None):
        g = R.QuotaGuard(cap)
        q = (limit, remaining, R.time.monotonic())
        self.patches = [mock.patch("auto_results.rapid_quota", return_value=q),
                        mock.patch("usage_tracker.fetch_rapidapi_quota", return_value=live)]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        return g

    def test_stops_before_a_match_could_cross_thirty_percent(self):
        self.assertTrue(self._guard(6005).allows())              # 6005 - 5 = 6000, the floor
        self.assertFalse(self._guard(6004).allows())
        g = self._guard(6004)
        g.allows()
        self.assertIn("quota floor", g.reason)

    def test_calls_outside_the_recorded_helper_count_against_the_floor(self):
        g = self._guard(6010)
        self.assertTrue(g.allows())
        g.spent_outside(6)
        self.assertFalse(g.allows())

    def test_batch_cap_bounds_one_night(self):
        g = self._guard(15000, cap=600)
        self.assertTrue(g.allows())
        g.spent_outside(596)
        self.assertFalse(g.allows())
        self.assertIn("batch cap", g.reason)

    def test_no_reading_at_all_fails_closed(self):
        g = R.QuotaGuard()
        with mock.patch("auto_results.rapid_quota", return_value=None), \
                mock.patch("usage_tracker.fetch_rapidapi_quota", return_value=None):
            self.assertFalse(g.allows())
        self.assertEqual(g.reason, "quota unreadable")

    def test_a_stale_header_reading_is_re_read_live(self):
        g = R.QuotaGuard()
        stale = (20000, 15000, R.time.monotonic() - R.QUOTA_READING_MAX_AGE_S - 1)
        with mock.patch("auto_results.rapid_quota", return_value=stale), \
                mock.patch("usage_tracker.fetch_rapidapi_quota",
                           return_value={"limit": 20000, "remaining": 6001}) as live:
            self.assertFalse(g.allows())
        live.assert_called_once_with("football")


class Load(unittest.TestCase):
    def tearDown(self):
        R._rows.clear(); R._scanned_days.clear(); R._league_competition.clear()
        R._loaded = False

    def test_both_tabs_read_back_including_short_rows(self):
        ref = [R.REF_HEADERS, ["5", "20260920", "Premier League", "A", "B", "1", "2",
                               "Michael Oliver", "England", "ENG", "1", "2", "3", ""]]
        meta = [R.META_HEADERS, ["day", "20260920"], ["league", "937348", "Champions League"],
                ["league", "1234", ""]]
        fake = {R.REF_SHEET: ref, R.META_SHEET: meta}
        R._loaded = False
        with mock.patch.object(R, "_ws", side_effect=lambda name, h: mock.Mock(
                get_all_values=mock.Mock(return_value=fake[name]))):
            R.load()
        self.assertEqual(R._rows[5]["Cards Total"], "3")
        self.assertIn("20260920", R._scanned_days)
        self.assertEqual(R._league_competition, {937348: "Champions League", 1234: ""})

    def _sheet(self, fake):
        return mock.patch.object(R, "_ws", side_effect=lambda name, h: mock.Mock(
            get_all_values=mock.Mock(return_value=fake[name])))

    def test_force_picks_up_another_writers_rows(self):
        R._rows[1] = {"Match ID": "1"}; R._scanned_days.add("20250801"); R._loaded = True
        fake = {R.REF_SHEET: [R.REF_HEADERS, ["2"] + [""] * 13],
                R.META_SHEET: [R.META_HEADERS, ["day", "20250802"]]}
        with self._sheet(fake):
            R.load()                                   # already loaded: no read
            self.assertIn(1, R._rows)
            R.load(force=True)
        self.assertEqual(set(R._rows), {2})
        self.assertEqual(R._scanned_days, {"20250802"})

    def test_malformed_rows_are_skipped_never_raised(self):
        ref = [R.REF_HEADERS,
               [],                                                   # empty row
               ["abc", "20250801", "Premier League"],                # non-numeric id, short
               ["7", "20250801", "Premier League"] + ["x"] * 30,     # far too many columns
               ["", "", ""],
               ["8"]]
        meta = [R.META_HEADERS, [], ["day"], ["league", "not-an-id", "X"],
                ["???", "20250801", ""], ["league", "42"]]
        with self._sheet({R.REF_SHEET: ref, R.META_SHEET: meta}):
            R.load(force=True)
        self.assertEqual(set(R._rows), {7, 8})
        self.assertEqual(R._rows[8]["Cards Total"], "")
        self.assertEqual(R._league_competition, {42: ""})
        self.assertEqual(R._scanned_days, {""})                      # a bare 'day' row: inert key
        self.assertEqual(R.referee_index(), {})                      # nothing usable reaches Opus

    def test_a_failed_reload_keeps_what_was_loaded(self):
        R._rows[1] = {"Match ID": "1"}; R._loaded = True
        with mock.patch.object(R, "_ws", side_effect=RuntimeError("429")):
            with self.assertRaises(RuntimeError):
                R.load(force=True)
        self.assertIn(1, R._rows)


class ShadowOnly(unittest.TestCase):
    def test_production_prompts_carry_no_referee_block(self):
        for prompt in (M.SYSTEM_PROMPT, M.LEAGUE_SYSTEM_PROMPT):
            self.assertNotIn("REFEREE:", prompt)
        self.assertIn("REFEREE:", OS._OPUS_REFEREE_PROMPT)


if __name__ == "__main__":
    unittest.main()
