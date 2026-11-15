"""
Regression suite for the 10-16 Nov 2026 Odds API outage. Pure: no network, no
Sheets, no Discord.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

What it cost: the subscription lapsed and the key came back 401
DEACTIVATED_KEY. For five days every pick posted and settled at the model's
ESTIMATED odds, with no closing odds. `_fetch_odds_events` swallowed each 401
at DEBUG, the daily summary printed a neutral "quota unavailable", and the
only WARNING line leaked the key itself: `requests` embeds the full URL,
`?apiKey=...` included, in its exception messages, so 34 plaintext copies
reached the Railway logs.

Rules pinned here:
- an account-level error_code alerts 'usage' once a day, and again the next day;
- a self-healing failure never alerts;
- no log line, and no alert, ever carries the key or an `apiKey=` parameter;
- the summary names a rejected key instead of printing "quota unavailable";
- nothing Odds-related is written to the Anthropic API Failures tab.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402
import usage_tracker as ut  # noqa: E402

SECRET = "0123456789abcdef0123456789abcdef"   # stand-in key, 32 chars like the real one
SPORTS_URL = f"https://api.the-odds-api.com/v4/sports/?apiKey={SECRET}"
ODDS_URL = (f"https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            f"?apiKey={SECRET}&regions=eu&markets=h2h%2Ctotals%2Cspreads")

# Verbatim body The Odds API returned on 15-16 Nov 2026.
DEACTIVATED = {
    "message": "API key is deactivated. This could be due to cancelation or a failed payment",
    "error_code": "DEACTIVATED_KEY",
    "details_url": "https://the-odds-api.com/liveapi/guides/v4/api-error-codes.html#deactivated-key",
}


def _resp(status: int, body, url: str = SPORTS_URL, headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    r.url = url
    r.headers.update(headers or {})
    return r


def _messages(cm) -> list[str]:
    return [rec.getMessage() for rec in cm.records]


def _odds_block(summary: str) -> str:
    """The Odds API section of the daily summary (RapidAPI has its own 'unavailable')."""
    return summary.split("**The Odds API**", 1)[1].split("**RapidAPI**", 1)[0]


class _Base(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.dict(os.environ, {"ODDS_API_KEY": SECRET}),
            mock.patch.object(ut, "_alerted_failures", set()),
            mock.patch.object(ut, "_odds_quota_cache", None),
            mock.patch.object(ut, "_odds_quota_failure", None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.send = mock.patch("discord_bot.send_to_discord", return_value=True).start()
        # Pure suite: any HTTP call a test did not stub explicitly fails loudly.
        mock.patch.object(requests, "get",
                          side_effect=AssertionError("unexpected network call")).start()
        self.addCleanup(mock.patch.stopall)

    def assertKeyFree(self, texts):
        for t in texts:
            self.assertNotIn(SECRET, t)
            self.assertNotIn("apiKey=", t)


class AccountLevelErrorsAlert(_Base):
    def test_a_deactivated_key_alerts_usage_once_a_day(self):
        r = _resp(401, DEACTIVATED)
        with self.assertLogs(level="DEBUG") as cm:
            first = ut.note_odds_api_failure("closing-odds", resp=r)
            second = ut.note_odds_api_failure("football-picks", resp=r)
        self.assertTrue(first)
        self.assertFalse(second)
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[0], "usage")
        text = self.send.call_args.kwargs["message"]
        self.assertIn("REJECTED THE KEY", text)
        self.assertIn("`DEACTIVATED_KEY`", text)
        self.assertIn("`closing-odds`", text)
        self.assertIn("ODDS_API_KEY", text)
        self.assertKeyFree([text] + _messages(cm))

    def test_a_still_broken_key_alerts_again_the_next_day(self):
        class Day1(date):
            @classmethod
            def today(cls):
                return date(2026, 11, 11)

        class Day2(date):
            @classmethod
            def today(cls):
                return date(2026, 11, 12)

        r = _resp(401, DEACTIVATED)
        with mock.patch.object(ut, "date", Day1), self.assertLogs(level="WARNING"):
            self.assertTrue(ut.note_odds_api_failure("closing-odds", resp=r))
        with mock.patch.object(ut, "date", Day2), self.assertLogs(level="WARNING"):
            self.assertTrue(ut.note_odds_api_failure("closing-odds", resp=r))
        self.assertEqual(self.send.call_count, 2)

    def test_every_key_code_alerts(self):
        for code in ("MISSING_KEY", "INVALID_KEY", "DEACTIVATED_KEY"):
            with self.subTest(code=code), mock.patch.object(ut, "_alerted_failures", set()), \
                    self.assertLogs(level="WARNING"):
                self.assertTrue(ut.note_odds_api_failure(
                    "odds-quota-check", resp=_resp(401, {"message": "x", "error_code": code})))

    def test_out_of_credits_alerts_with_its_own_heading(self):
        r = _resp(401, {"message": "Usage quota has been reached", "error_code": "OUT_OF_USAGE_CREDITS"})
        with self.assertLogs(level="WARNING"):
            self.assertTrue(ut.note_odds_api_failure("football-picks", resp=r))
        text = self.send.call_args.kwargs["message"]
        self.assertIn("OUT OF USAGE CREDITS", text)
        self.assertNotIn("REJECTED THE KEY", text)

    def test_a_401_with_an_unreadable_body_still_alerts(self):
        with self.assertLogs(level="WARNING"):
            self.assertTrue(ut.note_odds_api_failure("closing-odds", resp=_resp(401, b"<html>")))
        self.assertIn("`HTTP 401`", self.send.call_args.kwargs["message"])

    def test_an_undelivered_alert_reports_false(self):
        self.send.return_value = False
        with self.assertLogs(level="ERROR"):
            self.assertFalse(ut.note_odds_api_failure("closing-odds", resp=_resp(401, DEACTIVATED)))


class SelfHealingFailuresDoNotAlert(_Base):
    def test_rate_limit_server_error_bad_request_and_timeouts_stay_quiet(self):
        cases = {
            "429 EXCEEDED_FREQ_LIMIT": dict(resp=_resp(429, {"message": "slow down",
                                                              "error_code": "EXCEEDED_FREQ_LIMIT"})),
            "500 no body": dict(resp=_resp(500, b"")),
            "422 INVALID_MARKET": dict(resp=_resp(422, {"message": "x", "error_code": "INVALID_MARKET"})),
            "timeout": dict(exc=requests.Timeout(f"Read timed out. url: {SPORTS_URL}")),
            "connection": dict(exc=requests.ConnectionError(
                f"HTTPSConnectionPool(host='api.the-odds-api.com', port=443): Max retries "
                f"exceeded with url: /v4/sports/?apiKey={SECRET}")),
        }
        for name, kw in cases.items():
            with self.subTest(name), self.assertLogs(level="DEBUG") as cm:
                self.assertFalse(ut.note_odds_api_failure("closing-odds", **kw))
            self.assertKeyFree(_messages(cm))
        self.send.assert_not_called()


class FetchOddsEvents(_Base):
    def test_a_dead_key_returns_none_alerts_once_and_logs_no_key(self):
        with mock.patch.object(M.requests, "get", return_value=_resp(401, DEACTIVATED, ODDS_URL)), \
                self.assertLogs(level="DEBUG") as cm:
            self.assertIsNone(M._fetch_odds_events("soccer_epl", job="closing-odds"))
            self.assertIsNone(M._fetch_odds_events("soccer_epl", job="closing-odds"))
        self.send.assert_called_once()
        self.assertIn("`closing-odds`", self.send.call_args.kwargs["message"])
        msgs = _messages(cm)
        self.assertTrue(any("HTTP 401 DEACTIVATED_KEY" in m for m in msgs))
        self.assertTrue(all(r.levelno >= logging.WARNING for r in cm.records
                            if "Odds API call failed" in r.getMessage()))
        self.assertKeyFree(msgs)

    def test_a_network_error_is_logged_by_type_only(self):
        boom = requests.ConnectionError(
            f"HTTPSConnectionPool(host='api.the-odds-api.com', port=443): Max retries "
            f"exceeded with url: /v4/sports/soccer_epl/odds?apiKey={SECRET}&regions=eu")
        with mock.patch.object(M.requests, "get", side_effect=boom), \
                self.assertLogs(level="DEBUG") as cm:
            self.assertIsNone(M._fetch_odds_events("soccer_epl"))
        msgs = _messages(cm)
        self.assertTrue(any("ConnectionError" in m for m in msgs))
        self.assertKeyFree(msgs)
        self.send.assert_not_called()

    def test_the_picks_run_names_its_own_job(self):
        pick = {"match": "Arsenal vs Chelsea", "league": "Premier League",
                "bet_type": "Match Winner", "pick": "Arsenal Win", "odds": "1.9"}
        with mock.patch.object(M.requests, "get", return_value=_resp(401, DEACTIVATED, ODDS_URL)), \
                self.assertLogs(level="WARNING"):
            M.enrich_picks_with_real_odds([pick])
        self.assertIn("`football-picks`", self.send.call_args.kwargs["message"])
        self.assertNotIn("market_odds", pick)          # falls back to the estimate, as before

    def test_a_healthy_fetch_logs_and_alerts_nothing(self):
        events = [{"home_team": "Arsenal", "away_team": "Chelsea", "bookmakers": []}]
        with mock.patch.object(M.requests, "get", return_value=_resp(200, events, ODDS_URL)), \
                self.assertNoLogs(level="WARNING"):
            self.assertEqual(M._fetch_odds_events("soccer_epl"), events)
        self.send.assert_not_called()

    def test_urllib3_cannot_log_request_lines(self):
        # urllib3 logs "GET /v4/sports/?apiKey=... HTTP/1.1" at DEBUG.
        self.assertGreaterEqual(logging.getLogger("urllib3").getEffectiveLevel(), logging.WARNING)


class DailySummary(_Base):
    def setUp(self):
        super().setUp()
        for name, value in {
            "anthropic_status": {"last_success": None, "last_failure": None, "healthy": True},
            "anthropic_totals": {"today": {"calls": 0, "input": 0, "output": 0, "cost": 0.0},
                                 "month": {"calls": 0, "input": 0, "output": 0, "cost": 0.0},
                                 "by_job_today": {}, "models_today": set()},
            "fetch_rapidapi_quota": None,
        }.items():
            mock.patch.object(ut, name, return_value=value).start()

    def test_a_rejected_key_is_named_not_reported_as_unavailable(self):
        with mock.patch.object(ut.requests, "get", return_value=_resp(401, DEACTIVATED)), \
                self.assertLogs(level="DEBUG") as cm:
            text = ut.build_daily_summary()
        odds = _odds_block(text)
        self.assertIn("KEY REJECTED", odds)
        self.assertIn("`DEACTIVATED_KEY`", odds)
        self.assertNotIn("_quota unavailable_", odds)
        self.send.assert_called_once()                 # the summary's own check alerts too
        self.assertKeyFree([text, self.send.call_args.kwargs["message"]] + _messages(cm))

    def test_a_network_blip_keeps_the_neutral_line(self):
        with mock.patch.object(ut.requests, "get", side_effect=requests.Timeout(SPORTS_URL)), \
                self.assertLogs(level="WARNING") as cm:
            text = ut.build_daily_summary()
        self.assertIn("_quota unavailable_ (Timeout)", _odds_block(text))
        self.assertNotIn("🛑", text)
        self.send.assert_not_called()
        self.assertKeyFree([text] + _messages(cm))

    def test_a_working_key_clears_the_failure(self):
        ok = _resp(200, [], headers={"x-requests-used": "0", "x-requests-remaining": "20000"})
        with mock.patch.object(ut.requests, "get", side_effect=[_resp(401, DEACTIVATED), ok]), \
                self.assertLogs(level="WARNING"):
            ut.fetch_odds_quota(force=True)
            self.assertEqual(ut._odds_quota_failure["code"], "DEACTIVATED_KEY")
            text = ut.build_daily_summary()
        self.assertIsNone(ut._odds_quota_failure)
        self.assertIn("0/20000 units used", _odds_block(text))
        self.assertNotIn("KEY REJECTED", text)


class NeverTheAnthropicFailuresTab(_Base):
    def test_odds_failures_are_not_recorded_as_anthropic_failures(self):
        # anthropic_status() reads every API Failures row as an Anthropic failure.
        with mock.patch.object(ut, "record_anthropic_failure") as rec, \
                mock.patch.object(ut, "_failures_ws") as tab, \
                self.assertLogs(level="WARNING"):
            ut.note_odds_api_failure("closing-odds", resp=_resp(401, DEACTIVATED))
        rec.assert_not_called()
        tab.assert_not_called()


if __name__ == "__main__":
    unittest.main()
