"""
Regression suite for how a competition is registered and, above all, for the
discovery ranking that decides whether a SMALL competition is ever found at all.
Pure — no Sheets, no network, no sleeping.

Run from football-bot/:

    python -m unittest discover -s tests -t . -v

The ranking is the part worth pinning. `_discover_feed_ids` may spend only
MAX_PARENT_LOOKUPS_PER_RUN parent lookups per run, so a competition that sorts
below that cap is invisible no matter how correctly everything else is wired.
Ranking by fixture count alone hid Jupiler Pro League (62nd of 138 on the live
8 Aug 2026 slate) and would hide the Nations League far more thoroughly: a group
plays two matches on a matchday, and those blocks ranked 90th to 112th of 114 on
the live 3-4 Oct 2026 window. ROSTER_PARENTS cannot rescue it either, because
football-get-all-matches-by-league answers 0 matches for the Nations League
parents — hence NATION_ROSTERS and the name-overlap arm of the ranking.

So these tests assert the ordering directly, and each one carries its negative
control: with the roster removed the same input must FAIL to find the block.
A test that passes both with and without the mechanism pins nothing.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402

NL = "Nations League"


def _match(mid: int, league_id: int, home: str, away: str,
           home_id: int = 0, away_id: int = 0) -> dict:
    return {
        "id": mid,
        "leagueId": league_id,
        "home": {"id": home_id or mid * 10, "longName": home},
        "away": {"id": away_id or mid * 10 + 1, "longName": away},
        "status": {"utcTime": "2026-09-24T18:45:00.000Z",
                   "finished": False, "started": False, "cancelled": False},
    }


class Registration(unittest.TestCase):
    """Nations League sits on the parent-resolution path, not in LEAGUES."""

    def test_not_pinned_as_a_stable_single_id_league(self):
        # Every feed id resolves to a tier parent rather than to itself
        # (920743 -> 9806, checked live 22 Sep 2026), which is exactly the
        # test that sent Jupiler out of LEAGUES.
        self.assertNotIn(NL, M.LEAGUES)
        self.assertIn(NL, M.PARENT_RESOLVED_IDS)

    def test_all_four_tier_parents_are_tracked(self):
        # One parent per tier and no competition id above them, so missing one
        # silently drops a whole tier of the competition.
        self.assertEqual(M.PARENT_RESOLVED_IDS[NL], {9806, 9807, 9808, 9809})

    def test_every_group_id_is_seeded(self):
        # 14 groups: A1-A4, B1-B4, C1-C4, D1-D2.
        self.assertEqual(len(M.FEED_LEAGUE_IDS[NL]), 14)
        self.assertEqual(M.FEED_LEAGUE_IDS[NL], set(range(920741, 920755)))

    def test_routed_to_discord_and_priced_by_the_odds_api(self):
        self.assertEqual(M.DISCORD_LEAGUE_CHANNEL_KEYS[NL], "nations-league")
        self.assertEqual(M.ODDS_API_SPORT_KEYS[NL], "soccer_uefa_nations_league")

    def test_named_in_both_prompt_heads(self):
        # The per-competition call is what production sends; the global head
        # goes to the Opus shadow, which analyses the same fixture pool and so
        # must know the competition exists too.
        for prompt in (M.LEAGUE_SYSTEM_PROMPT, M.SYSTEM_PROMPT):
            self.assertIn("UEFA Nations League", prompt)

    def test_prompt_warns_that_national_team_form_is_absent(self):
        # Measured 22 Sep 2026: 0 of 52 teams on the 24-26 Sep matchday had a
        # single match inside FORM_LOOKBACK_DAYS. Claude must read that as the
        # international calendar, not as a signal about the teams.
        self.assertIn("EMPTY", M.LEAGUE_SYSTEM_PROMPT)
        self.assertIn("ten matches a year", M.LEAGUE_SYSTEM_PROMPT)


class PartitionFromTheSeed(unittest.TestCase):
    """A seeded feed id buckets without spending a single parent lookup."""

    def test_seeded_ids_bucket_directly(self):
        feed = [
            _match(1, 920744, "Netherlands", "Germany"),
            _match(2, 920754, "Andorra", "Malta"),
            _match(3, 47, "Arsenal", "Chelsea"),
        ]
        with mock.patch.object(M, "_discover_feed_ids") as discover:
            discover.return_value = {}
            buckets = M.partition_fixtures(feed)
        self.assertEqual(
            sorted(f["home"] for f in buckets[NL]), ["Andorra", "Netherlands"])
        self.assertEqual([f["home"] for f in buckets["Premier League"]], ["Arsenal"])

    def test_a_nations_league_id_can_never_be_read_as_a_world_cup_match(self):
        # The feed ids join the World Cup disqualifier set. Both sides of a
        # Nations League tie are senior national teams, so without that the
        # participant check alone would happily claim the fixture.
        self.assertTrue(M.WC_2026_PARTICIPANTS & M.UEFA_NATIONS)
        club_ids = set(M.LEAGUES.values())
        for ids in M.FEED_LEAGUE_IDS.values():
            club_ids |= ids
        self.assertFalse(
            M._is_wc_match(_match(1, 920744, "Netherlands", "Germany"), club_ids))


class DiscoveryRanking(unittest.TestCase):
    """The cap is the whole problem: a block below it is never looked up."""

    def setUp(self):
        M._parent_league_cache.clear()
        M._roster_cache.clear()
        # No sleeping and no network: the sweep paces itself at 1s a candidate.
        for target in ("time.sleep", "_roster_team_ids"):
            patcher = mock.patch.object(
                M, target.split(".")[-1],
                **({"return_value": set()} if "roster" in target else {}))
            if target == "time.sleep":
                patcher = mock.patch.object(M.time, "sleep")
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _slate(nl_id: int = 999001):
        """One tiny national-team block buried under 30 much larger club blocks."""
        feed, mid = [], 1
        for block in range(30):
            for _ in range(8 + block):      # 8..37 matches each
                feed.append(_match(mid, 800000 + block, f"Club {mid}", f"Club {mid}b"))
                mid += 1
        for home, away in (("Netherlands", "Germany"), ("Serbia", "Greece")):
            feed.append(_match(mid, nl_id, home, away))
            mid += 1
        return feed, nl_id

    def _sweep(self, feed, nl_id, missing):
        def resolve(match):
            return 9806 if match["leagueId"] == nl_id else 12345
        with mock.patch.object(M, "_resolve_parent_league", side_effect=resolve):
            return M._discover_feed_ids(feed, set(), missing)

    def test_a_two_match_national_block_outranks_thirty_larger_club_blocks(self):
        feed, nl_id = self._slate()
        self.assertEqual(self._sweep(feed, nl_id, [NL]), {NL: {nl_id}})

    def test_negative_control_without_the_nation_roster_it_is_missed(self):
        # Same slate, same cap, ranking by fixture count alone — the block is
        # 31st of 31 and the 12-lookup budget never reaches it. This is the
        # test that fails if NATION_ROSTERS is ever quietly dropped.
        feed, nl_id = self._slate()
        with mock.patch.dict(M.NATION_ROSTERS, clear=True):
            self.assertEqual(self._sweep(feed, nl_id, [NL]), {})

    def test_a_competition_not_missing_contributes_no_roster(self):
        # Rosters come from the MISSING list, so a competition already found
        # stops steering the sweep — the reason all 14 group ids want finding
        # in one sweep rather than a few per day.
        feed, nl_id = self._slate()
        self.assertEqual(self._sweep(feed, nl_id, ["Champions League"]), {})

    def test_womens_sides_do_not_score_against_the_mens_roster(self):
        # The same feed carries women's national blocks, spelled "Germany (W)".
        # They must not consume lookups meant for the men's competition.
        feed, nl_id = self._slate()
        feed = [m for m in feed if m["leagueId"] != nl_id]
        for home, away in (("Germany (W)", "Netherlands (W)"),
                           ("Serbia (W)", "Greece (W)")):
            feed.append(_match(99000 + len(feed), 999002, home, away))
        self.assertEqual(self._sweep(feed, nl_id, [NL]), {})

    def test_a_partial_roster_still_ranks_the_block_in(self):
        # Overlap is a fraction, so an unlisted entrant — a reinstated
        # federation, a renamed side — degrades the score instead of zeroing it.
        feed, nl_id = self._slate()
        feed = [m for m in feed
                if m["leagueId"] != nl_id or m["home"]["longName"] != "Serbia"]
        feed.append(_match(99999, nl_id, "Elbonia", "Greece"))
        self.assertEqual(self._sweep(feed, nl_id, [NL]), {NL: {nl_id}})


class OddsApiTeamAliases(unittest.TestCase):
    """The alias table is whole-name and exact, so it cannot widen a match."""

    def test_czechia_bridges_to_czech_republic(self):
        # Measured 22 Sep 2026: the one NAME failure in 26 live Nations League
        # fixtures. Without this the fixture falls back to estimated odds.
        self.assertTrue(M._team_match("Czechia", "Czech Republic"))
        self.assertTrue(M._team_match("Czech Republic", "Czechia"))

    def test_names_that_already_matched_still_match(self):
        for a, b in (("Turkiye", "Turkey"),
                     ("Ireland", "Republic of Ireland"),
                     ("Bosnia and Herzegovina", "Bosnia & Herzegovina")):
            self.assertTrue(M._team_match(a, b), f"{a} vs {b}")

    def test_the_23_aug_2026_mispricing_stays_fixed(self):
        # An alias must never reopen the collapse that priced a 1.26 favourite
        # at 8.98. _team_match stays permissive on purpose — it only decides
        # which candidates are worth scoring — so the guarantee to pin is that
        # the SCORER still separates them by more than TEAM_MATCH_MARGIN and
        # that _best_team_match therefore still picks the right club.
        self.assertEqual(
            M._best_team_match("Club Brugge", ["Cercle Brugge KSV", "Club Brugge"]),
            "Club Brugge")
        self.assertGreater(
            M._team_similarity("Club Brugge", "Club Brugge")
            - M._team_similarity("Cercle Brugge KSV", "Club Brugge"),
            M.TEAM_MATCH_MARGIN)

    def test_an_alias_rewrites_one_whole_name_and_nothing_else(self):
        self.assertEqual(M._normalize_team("Czechia"), "czech republic")
        # A name that merely CONTAINS an alias key is left alone.
        self.assertEqual(M._normalize_team("Czechia United"), "czechia united")
        self.assertFalse(M._team_match("Chechnya", "Czech Republic"))


if __name__ == "__main__":
    unittest.main()
