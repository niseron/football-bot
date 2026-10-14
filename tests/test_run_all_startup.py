"""
run_all startup must never depend on the Opus referee tabs (14 Oct 2026).

Loading 'Opus Referee Cards' / 'Opus Referee Meta' at startup is a convenience
(a broken tab shows in the deploy log). If it raises — a Sheets error, a
missing tab, a malformed row — or hangs (gspread sets no request timeout), the
scheduler and every production job must still start. Pure — no network, no
Sheets, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gspread  # noqa: E402

import run_all  # noqa: E402

ALL_JOBS = {
    "daily_picks_job", "post_weekly_summary", "live_results_check", "closing_odds_job",
    "daily_tennis_picks_job", "tennis_live_results_check", "opus_shadow_results_check",
    "usage_summary_job", "referee_backfill_job",
}


class _FakeScheduler:
    def __init__(self):
        self.jobs: list[str] = []
        self.started = False

    def add_job(self, func, *args, **kwargs):
        self.jobs.append(func.__name__)

    def start(self):
        self.started = True

    def shutdown(self):
        pass


def _run_main(on_first_tick=None) -> _FakeScheduler:
    """Run run_all.main() until its first idle tick, then stop it like SIGTERM would."""
    sched = _FakeScheduler()

    def tick(_seconds):
        if on_first_tick:
            on_first_tick(sched)
        raise SystemExit                          # main's own shutdown branch

    with mock.patch.object(run_all, "AsyncIOScheduler", return_value=sched), \
            mock.patch.object(run_all, "init_db"), \
            mock.patch.object(run_all.asyncio, "sleep", side_effect=tick):
        asyncio.run(run_all.main())
    return sched


class LoadFailureIsSwallowed(unittest.TestCase):
    def test_every_kind_of_load_failure_is_caught(self):
        failures = (
            RuntimeError("APIError: [429] Quota exceeded for quota metric 'Read requests'"),
            gspread.exceptions.WorksheetNotFound("Opus Referee Meta"),
            ValueError("malformed row"),
            KeyError("Match ID"),
        )
        for exc in failures:
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch("opus_shadow.opus_enabled", return_value=True), \
                    mock.patch("opus_referees.load", side_effect=exc) as load:
                run_all._load_referee_table()          # must not raise
                load.assert_called_once()

    def test_the_shadow_switch_itself_failing_is_caught(self):
        with mock.patch("opus_shadow.opus_enabled", side_effect=RuntimeError("bad channels json")):
            run_all._load_referee_table()


class SchedulerStartsRegardless(unittest.TestCase):
    def test_every_job_starts_when_the_load_raises(self):
        called = threading.Event()

        def failing_load(*_a, **_k):
            called.set()
            raise RuntimeError("Sheets 500")

        with mock.patch("opus_shadow.opus_enabled", return_value=True), \
                mock.patch("opus_referees.load", side_effect=failing_load):
            # wait inside the patches: the load runs on its own thread, and must
            # never reach the real Sheet after they are lifted
            sched = _run_main(lambda _s: called.wait(2))
        self.assertTrue(called.is_set())
        self.assertTrue(sched.started)
        self.assertEqual(set(sched.jobs), ALL_JOBS)

    def test_every_job_starts_while_the_load_is_hung(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        seen = {}

        def hung_load():
            entered.set()
            release.wait(3)                       # a Sheets read with no timeout
            finished.set()

        def at_first_tick(sched):
            entered.wait(2)
            # Awaiting the load before the scheduler (the 14 Oct first cut) would
            # only reach this tick after the load gave up: finished, so it fails.
            seen.update(started=sched.started, jobs=set(sched.jobs),
                        load_running=entered.is_set() and not finished.is_set())

        with mock.patch.object(run_all, "_load_referee_table", side_effect=hung_load):
            _run_main(at_first_tick)
        self.assertTrue(seen["load_running"])     # the load really was stuck...
        self.assertTrue(seen["started"])          # ...and the scheduler ran anyway
        self.assertEqual(seen["jobs"], ALL_JOBS)


if __name__ == "__main__":
    unittest.main()
