"""
Regression suite for the 3 Nov 2026 feed change: `home.longName` /
`away.longName` vanished from every match of `football-get-matches-by-date`.
Pure — no network, no Sheets, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

What it cost: from 13:45 UTC on 3 Nov every settlement poll (football and the
Opus shadow) died on `KeyError: 'longName'`, and the 4 Nov 12:00 picks run died
in build_fixture_summary before analysis — no Core, no Extended, no Opus shadow,
and no alert on any channel. Two more reads degraded SILENTLY rather than
crashing: form rows came back {} and the Nations League discovery ranking lost
its name roster.

The fixtures below are the verbatim new shape (Athletic Club vs Real Sociedad,
1 Nov 2026, as served on 4 Nov) plus a match with no name field at all. The
rules pinned: names fall back longName -> name -> shortName; a match with none
is skipped and logged, never fatal to the run; and a crashed job is alerted.
"""
from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auto_results as ar  # noqa: E402
import feed_names as fn  # noqa: E402
import main as M  # noqa: E402
import run_all  # noqa: E402
import usage_tracker as ut  # noqa: E402

NL = "Nations League"

# Verbatim from the live feed on 4 Nov 2026 (date=20261102): NO longName.
NEW_SHAPE_FINISHED = {
    "id": 5868111, "leagueId": 87, "time": "01.11.2026 21:00",
    "home": {"id": 8315, "score": 2, "name": "Athletic Club"},
    "away": {"id": 8560, "score": 1, "name": "Real Sociedad"},
    "eliminatedTeamId": None, "statusId": 6, "tournamentStage": "11",
    "status": {
        "utcTime": "2026-11-01T20:00:00.000Z",
        "halfs": {"firstHalfStarted": "01.11.2026 21:05:26",
                  "secondHalfStarted": "01.11.2026 22:10:42"},
        "periodLength": 45, "finished": True, "started": True, "cancelled": False,
        "awarded": False, "scoreStr": "2 - 1",
        "reason": {"short": "FT", "shortKey": "fulltime_short",
                   "long": "Full-Time", "longKey": "finished"},
    },
    "timeTS": 1793563200000,
}

# Same feed, a side that carries a shortName as well (MLS, 1 Nov 2026).
NEW_SHAPE_WITH_SHORT = {
    "home": {"id": 130394, "score": 1, "name": "Seattle Sounders FC",
             "shortName": "Seattle Sounders"},
}


def _upcoming(mid: int, league_id: int, home: dict, away: dict) -> dict:
    return {
        "id": mid, "leagueId": league_id, "home": home, "away": away,
        "status": {"utcTime": "2026-11-04T20:00:00.000Z",
                   "finished": False, "started": False, "cancelled": False},
    }


def _nameless(mid: int, league_id: int = 47, finished: bool = False) -> dict:
    """A match whose sides carry an id and a score but no name field at all."""
    m = _upcoming(mid, league_id, {"id": mid * 10, "score": 0}, {"id": mid * 10 + 1, "score": 0})
    m["status"]["finished"] = m["status"]["started"] = finished
    return m


class TeamNameFallback(unittest.TestCase):

    def test_the_fixture_really_is_the_new_shape(self):
        # Negative control: if this fixture ever regains longName, every test
        # below stops pinning the 3 Nov failure.
        for side in ("home", "away"):
            self.assertNotIn("longName", NEW_SHAPE_FINISHED[side])
        with self.assertRaises(KeyError):
            NEW_SHAPE_FINISHED["home"]["longName"]

    def test_name_is_used_when_longname_is_gone(self):
        self.assertEqual(fn.team_name(NEW_SHAPE_FINISHED["home"]), "Athletic Club")
        self.assertEqual(fn.match_names(NEW_SHAPE_FINISHED), ("Athletic Club", "Real Sociedad"))

    def test_name_beats_shortname(self):
        # Measured 4 Nov: the new `name` equals the old longName on 118 of 118
        # sides; shortName is a different, shorter string.
        self.assertEqual(fn.team_name(NEW_SHAPE_WITH_SHORT["home"]), "Seattle Sounders FC")

    def test_longname_still_wins_if_the_provider_restores_it(self):
        self.assertEqual(
            fn.team_name({"longName": "Manchester City", "name": "Man City"}), "Manchester City")

    def test_shortname_is_the_last_resort(self):
        self.assertEqual(fn.team_name({"id": 1, "shortName": "Seattle Sounders"}), "Seattle Sounders")

    def test_no_usable_name_is_none(self):
        for side in (None, {}, {"id": 1}, {"longName": "", "name": "   "}, {"name": 42}, "x"):
            self.assertIsNone(fn.team_name(side), side)
        self.assertIsNone(fn.match_names(_nameless(1)))
        half = copy.deepcopy(NEW_SHAPE_FINISHED)
        del half["away"]["name"]
        self.assertIsNone(fn.match_names(half))


class PicksRunOnTheNewShape(unittest.TestCase):
    """main.py: the path that killed the 4 Nov slate, plus the two silent ones."""

    def test_build_fixture_summary_reads_name(self):
        f = M.build_fixture_summary(NEW_SHAPE_FINISHED)
        self.assertEqual((f["home"], f["away"]), ("Athletic Club", "Real Sociedad"))
        self.assertEqual((f["home_id"], f["away_id"]), (8315, 8560))

    def test_one_nameless_match_is_skipped_not_fatal(self):
        feed = [
            _upcoming(1, 47, {"id": 10, "name": "Arsenal"}, {"id": 11, "name": "Chelsea"}),
            _nameless(2, 47),
            _upcoming(3, 47, {"id": 30, "longName": "Liverpool"}, {"id": 31, "name": "Everton"}),
        ]
        with mock.patch.object(M, "_discover_feed_ids", return_value={}), \
             self.assertLogs("feed_names", level="WARNING") as logs:
            buckets = M.partition_fixtures(feed)
        self.assertEqual(
            [(f["home"], f["away"]) for f in buckets["Premier League"]],
            [("Arsenal", "Chelsea"), ("Liverpool", "Everton")])
        self.assertTrue(any("skipping match 2" in line for line in logs.output), logs.output)

    def test_a_wholly_nameless_feed_is_an_error_not_a_quiet_empty_day(self):
        with mock.patch.object(M, "_discover_feed_ids", return_value={}), \
             self.assertLogs("feed_names", level="ERROR") as logs:
            buckets = M.partition_fixtures([_nameless(5), _nameless(6)])
        self.assertEqual(buckets, {})
        self.assertTrue(any("ALL 2 match(es)" in line for line in logs.output), logs.output)

    def test_form_rows_are_not_silently_emptied(self):
        # _summarize_match's bare except turned the KeyError into {} for EVERY
        # form row — the run would have shipped with no form and no log line.
        row = M._summarize_match(NEW_SHAPE_FINISHED, team_id=8315)
        self.assertEqual(row, {"match": "Athletic Club vs Real Sociedad", "score": "2-1", "venue": "H"})
        self.assertEqual(M._summarize_match(_nameless(7)), {})

    def test_nations_league_discovery_still_ranks_by_name_on_the_new_shape(self):
        # .get("longName") went None on 3 Nov, scoring every national block 0
        # and sinking the Nations League below the lookup cap with no error.
        M._parent_league_cache.clear()
        nl_id, feed, mid = 999001, [], 1
        for block in range(30):
            for _ in range(8 + block):
                feed.append(_upcoming(mid, 800000 + block,
                                      {"id": mid * 10, "name": f"Club {mid}"},
                                      {"id": mid * 10 + 1, "name": f"Club {mid}b"}))
                mid += 1
        for home, away in (("Netherlands", "Germany"), ("Serbia", "Greece")):
            feed.append(_upcoming(mid, nl_id, {"id": mid * 10, "name": home},
                                  {"id": mid * 10 + 1, "name": away}))
            mid += 1
        resolve = lambda m: 9806 if m["leagueId"] == nl_id else 12345
        with mock.patch.object(M, "_resolve_parent_league", side_effect=resolve), \
             mock.patch.object(M, "_roster_team_ids", return_value=set()), \
             mock.patch.object(M.time, "sleep"):
            found = M._discover_feed_ids(feed, set(), [NL])
        M._parent_league_cache.clear()
        self.assertEqual(found, {NL: {nl_id}})


class SettlementOnTheNewShape(unittest.TestCase):
    """auto_results.py: the path that failed every poll from 3 Nov 13:45 UTC."""

    ROW = {
        "sheet_row": 900, "date": date(2026, 11, 1),
        "match": "Athletic Club vs Real Sociedad", "bet_type": "Match Winner",
        "pick": "Athletic Club Win", "odds": 1.9, "est_odds": 1.9, "market_odds": None,
    }

    def _run(self, buckets, scope):
        writes: list[tuple] = []
        with mock.patch.object(ar, "init_excel"), \
             mock.patch.object(ar, "_fetch_matches_cached", side_effect=lambda d: buckets.get(d, [])):
            stats, resolved = ar.run_auto_results(
                7,
                pending_source=lambda _days: [dict(self.ROW)],
                row_writer=lambda row, res, pnl: writes.append((row, res, pnl)),
                finalizer=lambda: None,
                alert_scope=scope,
            )
        return stats, resolved, writes

    def test_find_api_match_skips_a_nameless_candidate(self):
        pool = [_nameless(1, finished=True), NEW_SHAPE_FINISHED]
        self.assertIs(ar._find_api_match(pool, "Athletic Club", "Real Sociedad"), NEW_SHAPE_FINISHED)
        self.assertIs(
            ar._find_api_match(pool, "Real Sociedad", "Athletic Club", reversed_sides=True),
            NEW_SHAPE_FINISHED)

    def test_a_pick_settles_off_a_feed_without_longname(self):
        buckets = {date(2026, 11, 1): [_nameless(2, finished=True)],
                   date(2026, 11, 2): [NEW_SHAPE_FINISHED]}
        stats, resolved, writes = self._run(buckets, "test-feed-names-settles")
        self.assertEqual(writes, [(900, "WIN", ar.pnl_for_result("WIN", 1.9))])
        self.assertEqual(stats["errors"], 0)
        self.assertEqual((resolved[0]["home_name"], resolved[0]["away_name"]),
                         ("Athletic Club", "Real Sociedad"))

    def test_a_wholly_nameless_feed_leaves_the_row_pending_without_raising(self):
        buckets = {date(2026, 11, 1): [_nameless(3, finished=True), _nameless(4, finished=True)]}
        stats, resolved, writes = self._run(buckets, "test-feed-names-empty")
        self.assertEqual(stats["no_match"], 1)
        self.assertEqual(writes, [])


class FailuresAreAlerted(unittest.TestCase):
    """Neither failure posted anything on 3-4 Nov; now both reach 'usage'."""

    def test_a_crashed_picks_run_alerts_ops_and_subscribers(self):
        with mock.patch.object(M, "_run_daily_picks", side_effect=KeyError("longName")), \
             mock.patch.object(ut, "alert_job_failure") as ops, \
             mock.patch.object(M, "_notify_picks_failed") as subs, \
             self.assertLogs(M.log, level="ERROR"):
            asyncio.run(M.daily_picks_job())
        self.assertEqual(ops.call_args.args[0], "football-picks")
        self.assertIsInstance(ops.call_args.args[1], KeyError)
        subs.assert_called_once()
        self.assertIn("longName", subs.call_args.kwargs["detail"])

    def test_a_crash_after_delivery_began_does_not_claim_picks_failed(self):
        async def crash_mid_delivery(progress):
            progress["delivering"] = True
            raise RuntimeError("boom")
        with mock.patch.object(M, "_run_daily_picks", side_effect=crash_mid_delivery), \
             mock.patch.object(ut, "alert_job_failure") as ops, \
             mock.patch.object(M, "_notify_picks_failed") as subs, \
             self.assertLogs(M.log, level="ERROR"):
            asyncio.run(M.daily_picks_job())
        ops.assert_called_once()
        subs.assert_not_called()

    def test_a_failed_results_poll_alerts_ops(self):
        with mock.patch.object(run_all, "run_auto_results", side_effect=KeyError("longName")), \
             mock.patch.object(ut, "alert_job_failure") as ops:
            asyncio.run(run_all.live_results_check())
        self.assertEqual(ops.call_args.args[0], "football-results")

    def test_job_alert_is_deduped_per_day_per_job(self):
        with mock.patch.object(ut, "_alerted_failures", set()), \
             mock.patch("discord_bot.send_to_discord", return_value=True) as send:
            self.assertTrue(ut.alert_job_failure("football-results", KeyError("longName")))
            self.assertFalse(ut.alert_job_failure("football-results", KeyError("longName")))
            self.assertTrue(ut.alert_job_failure("football-picks", KeyError("longName")))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[0], "usage")
        self.assertIn("KeyError: 'longName'", send.call_args_list[0].kwargs["message"])

    # 5 Nov 2026: the three jobs that still failed silently. On 3 Nov the Opus
    # results check was the FIRST job to hit the missing longName (13:45 UTC)
    # and said so only in a WARNING line.

    def test_a_failed_opus_results_poll_alerts_ops_and_stays_non_fatal(self):
        import opus_shadow
        with mock.patch.object(opus_shadow, "run_opus_auto_results",
                               side_effect=KeyError("longName")), \
             mock.patch.object(ut, "alert_job_failure") as ops, \
             self.assertLogs(run_all.log, level="WARNING"):
            asyncio.run(run_all.opus_shadow_results_check())   # must not raise
        self.assertEqual(ops.call_args.args[0], "opus-shadow-results")
        self.assertIsInstance(ops.call_args.args[1], KeyError)

    def test_a_failed_tennis_results_poll_alerts_ops(self):
        with mock.patch.object(run_all, "run_tennis_auto_results",
                               side_effect=RuntimeError("feed down")), \
             mock.patch.object(ut, "alert_job_failure") as ops, \
             self.assertLogs(run_all.log, level="ERROR"):
            asyncio.run(run_all.tennis_live_results_check())
        self.assertEqual(ops.call_args.args[0], "tennis-results")

    def test_a_crashed_opus_picks_run_alerts_ops_only_and_never_raises(self):
        import opus_shadow
        boom = RuntimeError("opus down")
        with mock.patch.object(opus_shadow, "run_opus_shadow", side_effect=boom), \
             mock.patch.object(ut, "alert_job_failure") as ops, \
             mock.patch.object(ut, "alert_anthropic_failure") as credit, \
             mock.patch.object(M, "_notify_picks_failed") as subs, \
             self.assertLogs(M.log, level="WARNING"):
            asyncio.run(M._run_opus_shadow({"Champions League": []}, {}))
        ops.assert_called_once_with("opus-shadow-picks", boom)
        credit.assert_called_once()          # the credit-balance check is unchanged
        subs.assert_not_called()             # production picks were already delivered

    def test_a_healthy_opus_picks_run_alerts_nothing(self):
        import opus_shadow
        with mock.patch.object(opus_shadow, "run_opus_shadow", return_value=[]), \
             mock.patch.object(ut, "alert_job_failure") as ops:
            asyncio.run(M._run_opus_shadow({}, {}))
        ops.assert_not_called()


if __name__ == "__main__":
    unittest.main()
