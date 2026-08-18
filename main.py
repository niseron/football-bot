import asyncio
import difflib
import json
import logging
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

import anthropic
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from env_loader import load_env
from tracker import log_picks_batch, picks_exist_for_session
from excel_tracker import PICK_TIER_CORE, PICK_TIER_EXTENDED, calculate_kelly_stake
from card_generator import generate_picks_card, generate_picks_card_ig
from discord_bot import build_pick_embed, send_to_discord

load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# httpx logs "METHOD <full URL> HTTP/1.1 200 OK" at INFO for every request its
# clients make. The Anthropic SDK is built on httpx and its URL is harmless
# (the key travels in a header), but python-telegram-bot put its bot TOKEN in
# the URL PATH, so five plaintext credentials went into the Railway logs before
# 18 Aug 2026. Telegram is gone now and the leak with it; this stays as
# defence-in-depth, because the next httpx-backed SDK added here should not be
# able to reintroduce it. RapidAPI and The Odds API are unaffected either way:
# both go through `requests`, which never logs URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Single-ID domestic leagues.
#
# Every id here is a *stable fotmob parent competition id*: the by-date feed tags
# these leagues with the parent id itself, so it survives a season rollover.
# Audited 8 Aug 2026 against live 2026-27 fixtures — each one resolves to itself
# through football-get-match-detail's parentLeagueId (47->47, 54->54, 87->87,
# 55->55, 53->53), so none of them can go stale the way Jupiler did.
#
# An id that does NOT resolve to itself is season-scoped and does not belong in
# this dict — it belongs in PARENT_RESOLVED_IDS below. Jupiler Pro League was
# exactly that case: pinned here as 900433 since the initial commit, matching
# nothing on any date checked, and producing zero picks in the bot's entire
# history (0 rows in the Sheet's Picks tab, verified 8 Aug 2026).
LEAGUES = {
    "Premier League": 47,
    "Bundesliga": 54,
    "La Liga": 87,
    "Serie A": 55,
    "Ligue 1": 53,
}

# 2026 FIFA World Cup — group-stage leagueIds confirmed via live API scan Jun 11-28.
# One ID per group/batch; knockout IDs are unknown until draws happen post-group stage.
#
# 914609 was in this set, seeded as the "opening batch (Jun 11)". It is NOT a
# World Cup id — football-get-match-detail resolves it to 'Friendlies', parent
# 114 (checked 4 Aug 2026). It pulled international friendlies into the World
# Cup bucket; Vietnam vs Myanmar on 18 Jul 2026 was logged as a World Cup pick
# because of it. Removed. See _is_wc_match() for the ordering fix that stops a
# bad id here from overriding participant validation ever again.
WC_2026_IDS: set[int] = {
    894790, 894791, 894792, 894793,            # groups batch   (Jun 12-14)
    894794, 894795, 894796, 894797,            # groups batch   (Jun 14-15)
    894798, 894799, 894800, 894801,            # groups batch   (Jun 17-28)
}
WC_2026_END = date(2026, 7, 19)              # final is July 19

# Exact longNames used by this API for all 48 WC 2026 participants.
# Used to detect knockout matches on new, previously-unknown leagueIds.
WC_2026_PARTICIPANTS: set[str] = {
    # Hosts
    "USA", "Canada", "Mexico",
    # CONMEBOL
    "Argentina", "Brazil", "Uruguay", "Colombia", "Ecuador",
    "Paraguay", "Bolivia", "Venezuela", "Chile", "Peru",
    # UEFA
    "England", "France", "Germany", "Spain", "Portugal",
    "Netherlands", "Belgium", "Italy", "Croatia", "Serbia",
    "Switzerland", "Austria", "Denmark", "Poland", "Turkiye",
    "Slovakia", "Scotland", "Wales", "Georgia", "Slovenia",
    "Hungary", "Czechia", "Romania", "Albania", "Ukraine",
    "Finland", "Norway", "Sweden", "Greece", "Iceland",
    "North Macedonia", "Kosovo", "Bosnia and Herzegovina",
    "Armenia", "Azerbaijan", "Bulgaria",
    # CAF
    "Morocco", "Senegal", "Egypt", "Nigeria", "Cameroon",
    "Ghana", "Mali", "Ivory Coast", "South Africa", "Cape Verde",
    "Tunisia", "Algeria", "DR Congo", "Angola", "Zimbabwe",
    "Zambia", "Tanzania", "Kenya", "Guinea", "Benin", "Comoros",
    "Mozambique", "Gambia",
    # CONCACAF
    "Costa Rica", "Panama", "Honduras", "Trinidad and Tobago",
    "Cuba", "Haiti", "Jamaica", "El Salvador", "Nicaragua",
    "Belize", "Curacao", "Guatemala", "Martinique", "Guadeloupe",
    # AFC
    "South Korea", "Japan", "Iran", "Saudi Arabia", "Qatar",
    "Australia", "Iraq", "Uzbekistan", "China", "India",
    "Thailand", "Vietnam", "Indonesia", "Oman", "Bahrain",
    "Jordan", "UAE", "Kyrgyzstan", "Tajikistan", "Syria",
    # OFC
    "New Zealand", "New Caledonia", "Fiji", "Tahiti",
    "Vanuatu", "Solomon Islands", "Papua New Guinea",
}

# Competitions whose feed leagueId is season-specific (and for UEFA, also
# stage-specific). The by-date feed carries no competition name, only a leagueId,
# and for these that id rotates — when UEFA qualifying gives way to the league
# phase, and for all of them every new season. Pinning it makes the competition
# silently vanish, so unfamiliar ids are resolved to their stable fotmob parent
# competition id and matched against these instead.
#
# This is why Champions League is NOT in LEAGUES: its feed id was 904988 in the
# 2025-26 league phase and is 937348 in 2026-27 qualifying. Jupiler Pro League
# joined it on 8 Aug 2026 for exactly the same reason — its 2026-27 feed id is
# 937988, while the 900433 pinned in LEAGUES since the initial commit matched
# nothing on any date checked.
#
# Parent ids confirmed against live fixtures via football-get-match-detail:
# UEFA 4 Aug 2026, Belgium 8 Aug 2026 (937988 -> parent 40 "Belgian Pro League").
PARENT_RESOLVED_IDS: dict[str, set[int]] = {
    "Jupiler Pro League": {
        40,      # Belgian Pro League (fotmob "First Division A")
    },
    "Champions League": {
        42,      # Champions League — league phase + knockout rounds
        10611,   # Champions League Qualification
    },
    "Europa League": {
        73,      # Europa League — league phase + knockout rounds
        10613,   # Europa League Qualification
    },
    "Conference League": {
        10216,   # Conference League — league phase + knockout rounds
        10615,   # Conference League Qualification
    },
}

# Feed leagueIds already known, per competition. Seeded with the ids live on
# 4 Aug 2026 (UEFA) and 8 Aug 2026 (Belgium) so the common case costs no lookups;
# the resolver adds rotated ids as it discovers them. A stale seed is not a
# failure mode — it costs one discovery sweep and is then replaced.
FEED_LEAGUE_IDS: dict[str, set[int]] = {
    "Jupiler Pro League": {937988},  # Belgian Pro League 2026-27
    "Champions League": {937348},    # Champions League Qualification 2026-27
    "Europa League": {937349},       # Europa League Qualification 2026-27
    "Conference League": {937351},   # Conference League Qualification 2026-27
}

# Competitions identifiable by a stable club roster, mapped to the parent
# leagueId whose team list defines it. Used ONLY to rank discovery candidates
# (see _discover_feed_ids) — never to decide membership, because the roster comes
# from the parent's last completed season and so misses promoted clubs: on
# 8 Aug 2026 parent 40's roster covered 13 of the 16 clubs in the 2026-27 Belgian
# slate (Kortrijk, Lommel and Beveren had just come up). Ranking tolerates that
# happily; deciding membership on it would drop a third of the fixtures.
#
# UEFA competitions are deliberately absent — their entrants turn over every
# season, and the fixture-count heuristic already surfaces them.
ROSTER_PARENTS: dict[str, int] = {
    "Jupiler Pro League": 40,
}

# Feed leagueId -> stable parent leagueId, cached for the process lifetime
# (main.py runs as a long-lived scheduler, so each id is resolved at most once
# per deploy). A cached None means "looked up, not a competition we track" and
# is never looked up again.
_parent_league_cache: dict[int, int | None] = {}

# Parent leagueId -> its roster of team ids, same process-lifetime caching.
_roster_cache: dict[int, set[int]] = {}

# Cap on match-detail lookups per run, so a day full of unfamiliar competitions
# can never blow out the RapidAPI budget.
MAX_PARENT_LOOKUPS_PER_RUN = 12

# Hard cap on picks per COMPETITION per run (15 Aug 2026). The prompt asks for a
# ranked list of at most this many, but that is only an instruction — it has been
# exceeded in practice (16 Jun 2026: 8 picks in one run; 14 Jun: 14). Enforced
# once, in _analyse_one_league(), so every downstream consumer — sheet, card,
# Discord embeds — sees the identical list.
#
# History: 5 globally, raised to 10 globally on 13 Aug 2026 when picks became
# ranked and tiered, and made PER-LEAGUE on 15 Aug 2026. A day with eight
# competitions in the 48h window can therefore produce 30+ picks where it used to
# produce 10. The cap is a ceiling and nothing more: the prompt forbids padding,
# a competition with nothing worth backing returns an empty list, and a short
# list is the expected outcome, never a fault to be corrected.
MAX_PICKS_PER_LEAGUE = 10

# Core stays GLOBAL and unchanged at 5 a day: the best 5 bets across the whole
# slate, whatever it costs the individual competitions. Core is the baseline
# series running unbroken since 30 Jun 2026 — it keeps the card, the running
# total, the Summary tab and every calibration/edge/CLV report to itself.
# Extended picks are logged, settled and posted to their league's
# Discord channel, but are excluded from all of the above (excel_tracker's
# _core_rows is the single filter enforcing that).
#
# This split is a REPORTING boundary, not a quality gate: an Extended pick is
# still a bet Claude judged worth making, just a lower-conviction one.
#
# Because the per-league calls each see only their own competition, the global
# ordering that used to fall out of one ranked list no longer exists — Core is
# selected explicitly by _select_core_picks() instead.
CORE_PICKS_PER_RUN = 5

# Regex to identify youth-team suffixes  e.g. "U19", "U-21", "U 23"
_YOUTH_RE = re.compile(r"\bU[\s-]?1[5-9]\b|\bU[\s-]?2[0-3]\b|youth|junior", re.IGNORECASE)

RAPIDAPI_HOST = "free-api-live-football-data.p.rapidapi.com"

ODDS_API_HOST = "https://api.the-odds-api.com/v4"

# Maps our internal competition names to The Odds API's sport keys. A value may
# be a tuple when one competition spans several keys — see _fetch_odds_events.
ODDS_API_SPORT_KEYS: dict[str, str | tuple[str, ...]] = {
    "Premier League": "soccer_epl",
    "Jupiler Pro League": "soccer_belgium_first_div",
    "FIFA World Cup 2026": "soccer_fifa_world_cup",
    "Bundesliga": "soccer_germany_bundesliga",
    "La Liga": "soccer_spain_la_liga",
    "Serie A": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
    # Champions League splits its season across two keys: the main one is
    # inactive until the league phase in September, while the qualification key
    # is live now. Tried in order, first non-empty wins — an out-of-season key
    # answers 200 with an empty list and is not billed, so this costs nothing.
    "Champions League": (
        "soccer_uefa_champs_league",
        "soccer_uefa_champs_league_qualification",
    ),
    # Europa League has the same gap as Conference League below: there is no
    # 'soccer_uefa_europa_league_qualification' key (the API answers 404
    # UNKNOWN_SPORT for it, checked 13 Aug 2026), and the league-phase key
    # itself returns an empty event list until September. So qualifying-round
    # picks are Claude-odds-only until the league phase makes this key live.
    "Europa League": "soccer_uefa_europa_league",
    # The Odds API has no separate key for Conference League qualifying, so
    # qualifying-round picks stay Claude-odds-only (fetch_real_odds returns
    # None and the caller falls back) until the league phase makes this live.
    "Conference League": "soccer_uefa_europa_conference_league",
}

# Seconds between individual pick embeds. Discord's per-channel ceiling is about
# one sustained message per second; a competition can now send 10 in a row.
DISCORD_PICK_SEND_DELAY = 1.0

# Maps competition names to Discord channel-mapping keys (discord_bot.py).
# A league missing here (or a key missing from DISCORD_CHANNELS_JSON) is
# simply not routed to Discord.
DISCORD_LEAGUE_CHANNEL_KEYS: dict[str, str] = {
    "Premier League": "premier-league",
    "Jupiler Pro League": "jupiler-pro-league",
    "FIFA World Cup 2026": "world-cup",
    "Bundesliga": "bundesliga",
    "La Liga": "la-liga",
    "Serie A": "serie-a",
    "Ligue 1": "ligue-1",
    "Champions League": "champions-league",
    "Europa League": "europa-league",
    "Conference League": "conference-league",
}

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── API helpers ───────────────────────────────────────────────────────────────

def _is_upcoming(match: dict) -> bool:
    s = match.get("status", {})
    return (
        not s.get("finished", False)
        and not s.get("started", False)
        and not s.get("cancelled", False)
    )


def fetch_upcoming_matches() -> list[dict]:
    """Fetch all matches in the next 48 hours (today + tomorrow UTC)."""
    headers = {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": os.environ.get("RAPIDAPI_KEY")}
    matches: list[dict] = []
    for offset in range(2):
        if offset > 0:
            time.sleep(2)
        dt = datetime.now(timezone.utc) + timedelta(days=offset)
        date_str = dt.strftime("%Y%m%d")
        url = f"https://{RAPIDAPI_HOST}/football-get-matches-by-date"
        resp = requests.get(url, headers=headers, params={"date": date_str}, timeout=15)
        resp.raise_for_status()
        day_matches = resp.json().get("response", {}).get("matches", [])
        log.info("API: fetched %d matches for %s", len(day_matches), dt.strftime("%Y-%m-%d"))
        matches.extend(day_matches)
    return matches


def build_fixture_summary(match: dict) -> dict:
    status = match.get("status", {})
    return {
        "match_id":    match["id"],
        "home":        match["home"]["longName"],
        "away":        match["away"]["longName"],
        "kickoff_utc": status.get("utcTime", match.get("time", "")),
        "home_id":     match["home"].get("id"),
        "away_id":     match["away"].get("id"),
    }


def _is_wc_participant(name: str) -> bool:
    """True if name is a senior WC 2026 national team (not a youth side)."""
    return name in WC_2026_PARTICIPANTS and not _YOUTH_RE.search(name)


def _is_wc_match(match: dict, club_ids: set[int]) -> bool:
    """
    True if a fixture belongs to the World Cup.

    BOTH teams must be confirmed senior WC participants, and that check runs
    for EVERY fixture — it is never skipped because the leagueId looked right.

    It used to be the other way round. The selection read

        leagueId in WC_2026_IDS or _is_wc_knockout(match, domestic_ids)

    and `_is_wc_knockout` returned False the moment it saw a known WC id, so a
    match on any id in WC_2026_IDS was accepted with no participant validation
    at all. One wrong id in that set therefore silently overrode a correct
    check — and did: 914609 was seeded as a WC id but is the international
    'Friendlies' id, which is how Vietnam vs Myanmar became a World Cup pick on
    18 Jul 2026 even though Myanmar is not a WC participant.

    The leagueId no longer decides membership. It survives only as a
    disqualifier (a club competition is never the World Cup) and, at the call
    site, as the group-vs-knockout signal.
    """
    if match.get("leagueId") in club_ids:
        return False
    home = match["home"]["longName"]
    away = match["away"]["longName"]
    return _is_wc_participant(home) and _is_wc_participant(away)


def _resolve_parent_league(match: dict) -> int | None:
    """
    Stable fotmob parent competition id for a feed match, or None on any error.
    The by-date fixtures feed carries only the season/stage-specific leagueId
    and no competition name, so the match-detail endpoint is the only way to
    tell (say) Conference League qualifying from Europa League qualifying.
    """
    try:
        resp = requests.get(
            f"https://{RAPIDAPI_HOST}/football-get-match-detail",
            headers=_api_headers(),
            params={"eventid": match["id"]},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("response", {}).get("detail", {}).get("parentLeagueId")
    except Exception as exc:
        log.debug("parent-league lookup failed for match %s: %s", match.get("id"), exc)
        return None


def _roster_team_ids(parent_id: int) -> set[int]:
    """
    Team ids that played in a parent competition's most recent full season.
    One API call per parent id, cached for the process lifetime. Returns an
    empty set on any error, which simply drops that competition back to
    fixture-count ranking — the sweep still works, it just ranks blindly.
    """
    if parent_id in _roster_cache:
        return _roster_cache[parent_id]

    teams: set[int] = set()
    try:
        resp = requests.get(
            f"https://{RAPIDAPI_HOST}/football-get-all-matches-by-league",
            headers=_api_headers(),
            params={"leagueid": parent_id},
            timeout=15,
        )
        resp.raise_for_status()
        for m in resp.json().get("response", {}).get("matches", []):
            for side in ("home", "away"):
                tid = (m.get(side) or {}).get("id")
                if tid is not None:
                    teams.add(int(tid))
        log.info("Roster for parent league %s: %d teams", parent_id, len(teams))
    except Exception as exc:
        log.debug("roster lookup failed for parent league %s: %s", parent_id, exc)

    _roster_cache[parent_id] = teams
    return teams


def _discover_feed_ids(
    upcoming: list[dict], known_ids: set[int], missing: list[str]
) -> dict[str, set[int]]:
    """
    Find feed leagueIds we don't know yet, by resolving each unfamiliar leagueId
    to its parent competition. One lookup per distinct leagueId, cached
    process-wide (hits and misses both) and capped per run.

    Candidate ORDER is what makes the cap survivable, and fixture count alone is
    not good enough. A UEFA round is one of the bigger blocks on its matchday, so
    largest-first finds it — but a domestic matchday is small. Measured on the
    live 8 Aug 2026 slate: 138 unfamiliar ids, and the 3-match Belgian block
    ranked #62 by size, far outside the 12-lookup cap. Ranking by size alone
    would have left Jupiler Pro League undiscoverable in practice, which is the
    whole point of putting it here.

    So blocks are ranked first by how much of their line-up matches a missing
    competition's known roster (ROSTER_PARENTS), then by fixture count. The
    Belgian block scores 5/6 on that measure and sorts to the front; the parent
    lookup that follows still has the final say, so a roster near-miss on a
    promoted club costs nothing and a false lead is simply rejected.

    ONE shared sweep serves every tracked competition, and it has to be:
    _parent_league_cache records each leagueId exactly once, so a second,
    per-competition sweep would skip every id the first had already resolved —
    including that competition's own ids, filed as cached misses.
    """
    by_league: dict[int, list[dict]] = {}
    for m in upcoming:
        lid = m.get("leagueId")
        if lid is None or lid in known_ids or lid in _parent_league_cache:
            continue
        by_league.setdefault(lid, []).append(m)

    if not by_league:
        return {}

    rosters = [
        teams
        for competition in missing
        if competition in ROSTER_PARENTS
        for teams in (_roster_team_ids(ROSTER_PARENTS[competition]),)
        if teams
    ]

    def _rank(item: tuple[int, list[dict]]) -> tuple[float, int]:
        """Best roster overlap first (as a fraction of the block's own teams),
        then most fixtures. Both negated — sorted() is ascending."""
        _lid, matches = item
        overlap = 0.0
        if rosters:
            team_ids = {
                tid
                for m in matches
                for tid in (m["home"].get("id"), m["away"].get("id"))
                if tid is not None
            }
            if team_ids:
                overlap = max(len(team_ids & r) / len(team_ids) for r in rosters)
        return (-overlap, -len(matches))

    candidates = sorted(by_league.items(), key=_rank)
    if len(candidates) > MAX_PARENT_LOOKUPS_PER_RUN:
        log.info(
            "Parent-league discovery: %d unfamiliar leagueIds, resolving the top %d "
            "(missing: %s)",
            len(candidates), MAX_PARENT_LOOKUPS_PER_RUN, ", ".join(missing),
        )

    found: dict[str, set[int]] = {}
    for lid, matches in candidates[:MAX_PARENT_LOOKUPS_PER_RUN]:
        time.sleep(1)
        parent = _resolve_parent_league(matches[0])
        _parent_league_cache[lid] = parent
        for competition, parent_ids in PARENT_RESOLVED_IDS.items():
            if parent in parent_ids:
                found.setdefault(competition, set()).add(lid)
                log.info(
                    "Discovered %s leagueId %s (parent %s, %d matches)",
                    competition, lid, parent, len(matches),
                )
                break
    return found


def partition_fixtures(all_matches: list[dict]) -> dict[str, list[dict]]:
    """Split today's upcoming matches into per-league buckets."""
    upcoming = [m for m in all_matches if _is_upcoming(m)]

    result: dict[str, list[dict]] = {}

    # Domestic leagues — single stable leagueId each
    domestic_ids: set[int] = set(LEAGUES.values())
    for league_name, league_id in LEAGUES.items():
        found = [m for m in upcoming if m.get("leagueId") == league_id]
        if found:
            result[league_name] = [build_fixture_summary(m) for m in found]

    # Every club-competition id we know, used below only as a World Cup
    # disqualifier — a club fixture is never a national-team tournament match.
    club_ids: set[int] = set(domestic_ids)
    for feed_ids in FEED_LEAGUE_IDS.values():
        club_ids |= feed_ids

    # World Cup — active until July 19 2026.
    # Membership is decided by _is_wc_match(), which requires BOTH teams to be
    # confirmed WC participants for every fixture. The leagueId is not an
    # alternative route in (that was the bug — see _is_wc_match); below it only
    # separates known group-stage ids from knockout ones.
    if date.today() <= WC_2026_END:
        wc = [m for m in upcoming if _is_wc_match(m, club_ids)]
        if wc:
            knockout_new = [m for m in wc if m.get("leagueId") not in WC_2026_IDS]
            if knockout_new:
                new_ids = {m["leagueId"] for m in knockout_new}
                log.info("Knockout detection found %d matches on new leagueId(s): %s",
                         len(knockout_new), new_ids)
            # WC_2026_IDS holds only group-stage leagueIds, so any other WC
            # leagueId is a knockout round — flag it so Claude scopes Match
            # Winner picks to 90 min vs full-time incl. ET/pens (SYSTEM_PROMPT).
            summaries = []
            for m in wc:
                f = build_fixture_summary(m)
                if m.get("leagueId") not in WC_2026_IDS:
                    f["knockout"] = True
                summaries.append(f)
            result["FIFA World Cup 2026"] = summaries

    # Competitions matched by stable parent id — the Belgian Pro League, plus the
    # UEFA club competitions' qualifying ties and league-phase/knockout games.
    resolved: dict[str, list[dict]] = {
        competition: [m for m in upcoming if m.get("leagueId") in feed_ids]
        for competition, feed_ids in FEED_LEAGUE_IDS.items()
    }
    missing = [c for c, matches in resolved.items() if not matches]
    if missing:
        # At least one competition has nothing under a known id: either it
        # simply isn't playing in this window, or its feed id rotated
        # (qualifying -> league phase, or a new season). Only then is the sweep
        # worth its lookups — and results are cached, so a rotated id costs one
        # discovery and never again.
        known_ids = domestic_ids | WC_2026_IDS
        for feed_ids in FEED_LEAGUE_IDS.values():
            known_ids |= feed_ids
        for competition, new_ids in _discover_feed_ids(upcoming, known_ids, missing).items():
            FEED_LEAGUE_IDS.setdefault(competition, set()).update(new_ids)
            resolved[competition] = [
                m for m in upcoming if m.get("leagueId") in FEED_LEAGUE_IDS[competition]
            ]
    for competition, matches in resolved.items():
        if matches:
            result[competition] = [build_fixture_summary(m) for m in matches]

    return result


# ── Form & H2H enrichment ────────────────────────────────────────────────────

def _api_headers() -> dict:
    return {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": os.environ.get("RAPIDAPI_KEY", "")}


# Matches per team fed to Claude as recent form, and how far back the by-date
# feed is walked to find them.
#
# 35 days is a CAP, not a fixed cost: the walk stops as soon as every team in
# the pool has FORM_MATCHES finished matches. Mid-season a club plays 5 matches
# in ~21 days. The cap matters at the edges of a season — measured on the
# 14 Aug 2026 pool, 35 days covered 5 matches for only 6 of 16 teams and 3+ for
# 11 of 16, because the 2026-27 season had just started and Jun-Jul was the
# World Cup break. Widening it to 42 recovered one more team, so the limit there
# is the calendar, not the window. A short form string is therefore normal and
# correct near a season start; SYSTEM_PROMPT tells Claude to read it as "up to 5".
FORM_LOOKBACK_DAYS = 35
FORM_MATCHES = 5

# 'YYYYMMDD' -> that date's matches, for STRICTLY PAST dates only. A past date's
# results never change, so entries live for the process lifetime; main.py runs as
# a long-lived scheduler, which is what makes the trailing window cost one sweep
# per deploy and exactly one new call on each following day.
#
# Deliberately NOT auto_results._matches_cache, which wraps the same endpoint:
# that one carries a 30-minute TTL because it settles in-progress fixtures.
# Same endpoint, opposite lifetime — sharing it would either re-fetch immutable
# history every half hour or make settlement read stale scores.
_day_feed_cache: dict[str, list[dict]] = {}

# match_id -> parsed H2H payload. fetch_upcoming_matches covers 48 hours, so a
# fixture is offered on two consecutive days; caching by event id means only its
# first appearance costs a call.
_h2h_cache: dict[int, dict] = {}


def _fetch_day_matches(day: date) -> list[dict]:
    """
    Every match on one PAST date, cached for the process lifetime.
    Empty list on any error — and a failure is NOT cached, so the next run
    retries it rather than pinning a hole in the form window.
    """
    key = day.strftime("%Y%m%d")
    if key in _day_feed_cache:
        return _day_feed_cache[key]
    try:
        resp = requests.get(
            f"https://{RAPIDAPI_HOST}/football-get-matches-by-date",
            headers=_api_headers(),
            params={"date": key},
            timeout=15,
        )
        resp.raise_for_status()
        matches = resp.json().get("response", {}).get("matches", [])
    except Exception as exc:
        log.warning("form: by-date fetch failed for %s — form will be thinner: %s", key, exc)
        return []
    _day_feed_cache[key] = matches
    return matches


def _build_form_index(team_ids: set[int], today: date) -> dict[int, list[dict]]:
    """
    Recent finished matches per team, NEWEST FIRST, by walking the by-date feed
    backwards from yesterday.

    One call per DATE serves every team in the pool, because the feed is global.
    That is the whole reason this is affordable: the per-team endpoint this
    replaced (football-get-team-matches) would have cost one call per team, and
    it never existed on this host anyway — it answered 404 from the day the
    enrichment shipped (29 Jun 2026) until it was replaced on 14 Aug 2026.
    """
    found: dict[int, list[dict]] = {tid: [] for tid in team_ids}
    days_used = 0
    for back in range(1, FORM_LOOKBACK_DAYS + 1):
        if all(len(v) >= FORM_MATCHES for v in found.values()):
            break
        days_used = back
        for m in _fetch_day_matches(today - timedelta(days=back)):
            if not (m.get("status") or {}).get("finished"):
                continue
            for side in ("home", "away"):
                tid = (m.get(side) or {}).get("id")
                if tid in found and len(found[tid]) < FORM_MATCHES:
                    found[tid].append(m)

    covered = sum(1 for v in found.values() if len(v) >= FORM_MATCHES)
    empty = [tid for tid, v in found.items() if not v]
    log.info(
        "form: walked %d day(s) for %d team(s) — %d with a full %d-match history, %d with none",
        days_used, len(team_ids), covered, FORM_MATCHES, len(empty),
    )
    if empty:
        log.warning("form: no recent matches found for team id(s): %s", sorted(empty))
    return found


def _summarize_h2h_match(match: dict) -> dict:
    """
    One head-to-head row.

    The H2H payload has its OWN shape, unlike every other endpoint here: team
    names live under home.name (not longName) and the score is a single
    status.scoreStr string ("2 - 1") rather than per-side score fields. So
    _summarize_match does not apply to these rows.
    """
    try:
        status = match.get("status") or {}
        return {
            "date":  (status.get("utcTime") or "")[:10],
            "match": f"{match['home']['name']} vs {match['away']['name']}",
            "score": (status.get("scoreStr") or "").replace(" ", ""),
        }
    except Exception:
        return {}


def _fetch_h2h(match_id: int, home_name: str, away_name: str) -> dict:
    """
    Head-to-head for one fixture, keyed on the fixture's own event id.

    Two traps, both live: the endpoint is football-get-head-to-head (NOT
    football-get-h2h, which 404s), and its payload sits under
    response.lineup — not response.matches like the rest of this API.

    Returns {} when a pairing has no history; a first-ever meeting answers 200
    with an empty lineup, which is a normal outcome and not a failure.
    """
    if match_id in _h2h_cache:
        return _h2h_cache[match_id]
    try:
        resp = requests.get(
            f"https://{RAPIDAPI_HOST}/football-get-head-to-head",
            headers=_api_headers(),
            params={"eventid": match_id},
            timeout=10,
        )
        resp.raise_for_status()
        lineup = (resp.json().get("response") or {}).get("lineup") or {}
    except Exception as exc:
        log.warning("h2h: fetch failed for %s vs %s (match %s): %s",
                    home_name, away_name, match_id, exc)
        return {}

    # The list carries future fixtures too (a 2027 league meeting showed up in
    # testing), so filter to played matches before taking the most recent five.
    finished = [m for m in lineup.get("matches", []) if (m.get("status") or {}).get("finished")]
    finished.sort(key=lambda m: (m.get("status") or {}).get("utcTime") or "", reverse=True)
    parsed: dict = {"meetings": [_summarize_h2h_match(m) for m in finished[:FORM_MATCHES]]}

    # summary is [home wins, draws, away wins] oriented on the QUERIED fixture.
    # Verified 14 Aug 2026 against Cercle Brugge vs St.Truiden, where [14, 6, 7]
    # reproduced Cercle's (the home side's) W14 D6 L7 across all 27 played
    # meetings exactly. Emitted with the team names spelled out so the
    # orientation cannot be misread downstream.
    summary = lineup.get("summary")
    if isinstance(summary, list) and len(summary) == 3:
        parsed["record"] = {
            f"{home_name} wins": summary[0],
            "draws": summary[1],
            f"{away_name} wins": summary[2],
        }

    _h2h_cache[match_id] = parsed
    return parsed


def _result_for_team(match: dict, team_id: int) -> str:
    try:
        h_id = match["home"].get("id")
        h_s  = int(match["home"].get("score") or 0)
        a_s  = int(match["away"].get("score") or 0)
        is_home = (h_id == team_id)
        gs, gc = (h_s, a_s) if is_home else (a_s, h_s)
        if gs > gc: return "W"
        if gs < gc: return "L"
        return "D"
    except Exception:
        return "?"


def _form_string(matches: list[dict], team_id: int) -> str:
    """Space-separated W/D/L string, oldest → newest."""
    return " ".join(_result_for_team(m, team_id) for m in matches)


def _summarize_match(match: dict, team_id: int | None = None) -> dict:
    try:
        h   = match["home"]["longName"]
        a   = match["away"]["longName"]
        h_s = match["home"].get("score", "?")
        a_s = match["away"].get("score", "?")
        s: dict = {"match": f"{h} vs {a}", "score": f"{h_s}-{a_s}"}
        if team_id is not None:
            s["venue"] = "H" if match["home"].get("id") == team_id else "A"
        return s
    except Exception:
        return {}


def enrich_with_context(fixtures_by_league: dict[str, list[dict]]) -> None:
    """
    Mutates each fixture in-place, adding recent form and H2H context.

    Form is built once for the whole pool by walking the by-date feed backwards
    (one call per date, shared by every team); H2H is one call per fixture.
    Every network call is individually guarded, so a failure thins that fixture
    and the run continues — but it is reported at WARNING, never swallowed.

    That last point is the reason this function was rewritten on 14 Aug 2026.
    Both endpoints it used to call had answered 404 since the enrichment shipped
    on 29 Jun, and the failures were logged at DEBUG while basicConfig sets
    INFO — so nothing was ever emitted, and the summary line below printed
    "home=N/A away=N/A h2h=0" in a healthy-looking INFO line for 46 days. Never
    log an enrichment failure below WARNING, and never let the summary line
    render a fixture that retrieved nothing as if it had succeeded.
    """
    fixtures = [f for fx in fixtures_by_league.values() for f in fx]
    team_ids = {
        tid
        for f in fixtures
        for tid in (f.get("home_id"), f.get("away_id"))
        if tid
    }
    if not team_ids:
        log.warning("Enrichment skipped — no team ids on any of %d fixture(s)", len(fixtures))
        return

    form_index = _build_form_index(team_ids, date.today())

    enriched = 0
    for fixture in fixtures:
        home_id = fixture.get("home_id")
        away_id = fixture.get("away_id")
        if not home_id or not away_id:
            log.warning("Context skipped: %s vs %s — missing team id(s)",
                        fixture.get("home"), fixture.get("away"))
            continue

        # _build_form_index returns newest-first; _form_string reads oldest → newest.
        home_matches = form_index.get(home_id, [])[::-1]
        away_matches = form_index.get(away_id, [])[::-1]

        h2h = _fetch_h2h(fixture["match_id"], fixture["home"], fixture["away"])
        meetings = h2h.get("meetings", [])

        fixture["home_form"]   = _form_string(home_matches, home_id)
        fixture["away_form"]   = _form_string(away_matches, away_id)
        fixture["home_recent"] = [_summarize_match(m, home_id) for m in home_matches]
        fixture["away_recent"] = [_summarize_match(m, away_id) for m in away_matches]
        fixture["h2h"]         = meetings
        if h2h.get("record"):
            fixture["h2h_record"] = h2h["record"]

        if home_matches or away_matches or meetings:
            enriched += 1
            log.info(
                "Context enriched: %s vs %s | home=%s (%d) away=%s (%d) h2h=%d",
                fixture["home"], fixture["away"],
                fixture["home_form"] or "-", len(home_matches),
                fixture["away_form"] or "-", len(away_matches),
                len(meetings),
            )
        else:
            log.warning(
                "Context EMPTY: %s vs %s — no form and no H2H retrieved; this "
                "fixture goes to Claude on team names alone",
                fixture["home"], fixture["away"],
            )

    if not fixtures:
        return
    if enriched == 0:
        log.error(
            "Enrichment produced NOTHING for all %d fixture(s) — form and H2H are "
            "both unavailable, so picks will be made on team names alone. Check "
            "that the RapidAPI endpoints still exist before trusting this run.",
            len(fixtures),
        )
    elif enriched < len(fixtures):
        log.warning("Enrichment covered %d of %d fixture(s)", enriched, len(fixtures))
    else:
        log.info("Enrichment covered all %d fixture(s)", len(fixtures))


# ── Real odds (The Odds API) ─────────────────────────────────────────────────

_TEAM_NOISE_RE = re.compile(r"\b(fc|cf|afc|sc|cd|ac|club)\b", re.IGNORECASE)


def _normalize_team(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = _TEAM_NOISE_RE.sub("", name.lower())
    name = re.sub(r"[^a-z0-9 ]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _team_match(a: str, b: str) -> bool:
    """Fuzzy-match team names across the two APIs' differing naming conventions."""
    na, nb = _normalize_team(a), _normalize_team(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.72


def _fetch_odds_events(sport_key: str | tuple[str, ...] | None) -> list[dict] | None:
    """
    Raw fetch of every event + bookmaker odds for one Odds API sport_key.
    None if the sport_key/API key is missing or the request fails. Split out
    from fetch_real_odds so callers that need odds for several matches in the
    same competition (e.g. the closing-odds job) can fetch once and filter
    client-side, instead of one request per match.

    A competition may map to several keys (Champions League splits its season
    across a qualifying key and a main key). They are tried in order and the
    first non-empty result wins. Measured 4 Aug 2026: an out-of-season key
    answers HTTP 200 with an empty list and is NOT billed against the quota,
    so the extra key costs nothing — only a key that actually returns fixtures
    is charged, and that ends the loop.
    """
    api_key = os.environ.get("ODDS_API_KEY")
    if not sport_key or not api_key:
        return None
    keys = (sport_key,) if isinstance(sport_key, str) else tuple(sport_key)
    reachable: list[dict] | None = None
    for key in keys:
        try:
            resp = requests.get(
                f"{ODDS_API_HOST}/sports/{key}/odds",
                params={
                    "apiKey": api_key,
                    # ONE region on purpose: cost is regions x markets, so "eu"
                    # halves the call to 3 units. Measured 6 Aug 2026 — dropping
                    # "uk" loses 3 of 12 books per event (betfair_ex_uk,
                    # betfair_sb_uk, boylesports, paddypower) and keeps the
                    # Betfair EU exchange, so the averaged consensus barely moves.
                    "regions": "eu",
                    "markets": "h2h,totals,spreads",
                    "oddsFormat": "decimal",
                },
                timeout=10,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            log.debug("_fetch_odds_events(%s) failed: %s", key, exc)
            continue
        if events:
            return events
        reachable = events   # in season but no fixtures listed — keep looking
    return reachable


def _parse_odds_event(event: dict) -> dict:
    """Average bookmaker odds for one Odds API event into h2h/totals/spreads."""
    h2h: dict[str, list[float]] = {}
    totals: dict[float, dict[str, list[float]]] = {}
    spreads: dict[float, dict[str, list[float]]] = {}

    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            key = market.get("key")
            for outcome in market.get("outcomes", []):
                name, price, point = outcome.get("name"), outcome.get("price"), outcome.get("point")
                if name is None or price is None:
                    continue
                if key == "h2h":
                    h2h.setdefault(name, []).append(price)
                elif key == "totals" and point is not None:
                    totals.setdefault(point, {}).setdefault(name, []).append(price)
                elif key == "spreads" and point is not None:
                    spreads.setdefault(point, {}).setdefault(name, []).append(price)

    def _avg(prices: list[float]) -> float:
        return round(sum(prices) / len(prices), 2)

    return {
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "h2h": {name: _avg(prices) for name, prices in h2h.items()},
        "totals": [
            {"point": pt, **{name: _avg(prices) for name, prices in outcomes.items()}}
            for pt, outcomes in totals.items()
        ],
        "spreads": [
            {"point": pt, **{name: _avg(prices) for name, prices in outcomes.items()}}
            for pt, outcomes in spreads.items()
        ],
    }


def fetch_real_odds(home_team: str, away_team: str, competition: str) -> dict | None:
    """
    Fetch real market odds (h2h, totals, spreads/Asian handicap) for a fixture
    from The Odds API. Returns None if the competition isn't mapped, the API
    call fails, or the fixture can't be found — callers must treat None as
    "no real odds available" and fall back to Claude's estimated odds only.
    """
    events = _fetch_odds_events(ODDS_API_SPORT_KEYS.get(competition))
    if events is None:
        return None
    event = _find_odds_event(events, home_team, away_team)
    return _parse_odds_event(event) if event else None


def _find_odds_event(events: list[dict], home_team: str, away_team: str) -> dict | None:
    """This fixture's event inside an already-fetched list. No network call."""
    return next(
        (
            e for e in events
            if _team_match(e.get("home_team", ""), home_team)
            and _team_match(e.get("away_team", ""), away_team)
        ),
        None,
    )


_OU_RE = re.compile(r"(over|under)\s*([\d.]+)", re.IGNORECASE)
_AH_RE = re.compile(r"^(.*?)\s*([+-]?\d+(?:\.\d+)?)$")


def _match_market_odds(pick: dict, real_odds: dict) -> float | None:
    """Match a Claude pick to the corresponding outcome in real_odds. None if not found."""
    bet_type = (pick.get("bet_type") or "").lower()
    selection = (pick.get("pick") or "").strip()

    try:
        if "winner" in bet_type or "1x2" in bet_type or "moneyline" in bet_type:
            h2h = real_odds.get("h2h", {})
            if selection.lower() == "draw":
                return h2h.get("Draw")
            team_part = re.sub(r"\s+win$", "", selection, flags=re.IGNORECASE).strip()
            for name, odds in h2h.items():
                if _team_match(name, team_part):
                    return odds
            return None

        if "over" in bet_type or "under" in bet_type or "goals" in bet_type:
            m = _OU_RE.search(selection) or _OU_RE.search(bet_type)
            if not m:
                return None
            side, line = m.group(1).capitalize(), float(m.group(2))
            for row in real_odds.get("totals", []):
                if abs(row.get("point", -1) - line) < 0.01:
                    return row.get(side)
            return None

        if "handicap" in bet_type:
            m = _AH_RE.match(selection)
            if not m:
                return None
            team_part, line = m.group(1).strip(), float(m.group(2))
            for row in real_odds.get("spreads", []):
                if abs(row.get("point", -999) - line) < 0.01:
                    for name, odds in row.items():
                        if name != "point" and _team_match(name, team_part):
                            return odds
            return None
    except Exception as exc:
        log.debug("_match_market_odds failed for pick %s: %s", pick.get("match"), exc)
        return None

    return None


def _implied_prob(odds: float) -> float:
    return 1.0 / odds if odds else 0.0


def enrich_picks_with_real_odds(picks: list[dict]) -> None:
    """
    Mutates each pick in-place with 'market_odds' and 'value' (bool) fields by
    comparing Claude's implied probability against real market odds from The
    Odds API. A pick is flagged as value only when Claude's implied
    probability exceeds the market's by at least 5 percentage points.
    Any failure (missing ODDS_API_KEY, API down, fixture/market not found)
    leaves that pick unchanged — existing behaviour continues silently.

    The cache is keyed by COMPETITION, not by fixture. /odds returns every event
    in a competition in one billed 3-unit request, so one fetch serves all of
    that competition's picks and the rest is matched client-side, exactly as
    closing_odds.py already does. Keyed per fixture (as it was until 15 Aug
    2026) the same league-wide response was bought once per pick: harmless at 5
    picks a day, but 10 picks in one competition meant 10 identical requests and
    30 units for data already in hand.
    """
    events_cache: dict[str, list[dict] | None] = {}

    for pick in picks:
        try:
            match = pick.get("match", "")
            if " vs " not in match:
                continue
            home, away = match.split(" vs ", 1)
            league = pick.get("league", "")

            if league not in events_cache:
                events_cache[league] = _fetch_odds_events(ODDS_API_SPORT_KEYS.get(league))
            events = events_cache[league]
            if not events:
                continue
            event = _find_odds_event(events, home, away)
            real_odds = _parse_odds_event(event) if event else None
            if not real_odds:
                continue

            market_odds = _match_market_odds(pick, real_odds)
            if market_odds is None:
                continue

            pick["market_odds"] = market_odds
            claude_prob = _implied_prob(float(pick["odds"]))
            market_prob = _implied_prob(market_odds)
            pick["market_prob"] = round(market_prob * 100, 1)
            pick["value"] = (claude_prob - market_prob) >= 0.05
        except Exception as exc:
            log.debug("enrich_picks_with_real_odds skipped a pick: %s", exc)
            continue


# ── Claude analysis ───────────────────────────────────────────────────────────

# ── System prompts ───────────────────────────────────────────────────────────
#
# One shared BODY, two HEADs. Everything that describes the data, the bet
# formats and the JSON contract is identical for both and lives in _PROMPT_BODY;
# only the paragraph that states the scope and the cap differs.
#
# SYSTEM_PROMPT (global head + body) is byte-identical to the single-call prompt
# used before 15 Aug 2026 and is now sent ONLY by the Opus 5 shadow, which keeps
# the old whole-slate shape on purpose — see opus_shadow.py. Production sends
# LEAGUE_SYSTEM_PROMPT, once per competition. Edit _PROMPT_BODY when a change
# should reach both; edit a head only when it genuinely applies to one scope.
_PROMPT_HEAD_GLOBAL = """You are a professional football betting analyst with deep expertise in the Premier League,
Belgian Jupiler Pro League, Bundesliga, La Liga, Serie A, Ligue 1, the UEFA Champions League, the UEFA
Europa League, the UEFA Conference League, and international tournament football including the FIFA World Cup.
You receive upcoming fixtures for the next 48 hours and must identify the best value bets across all
competitions, ranked from best to worst — UP TO 10, in strict order of conviction.

RANKING — read this carefully:
- Rank 1 is your single highest-conviction bet; rank 10 is the weakest bet you would still genuinely
  place. Order the "picks" array by that ranking, best first.
- Return ONLY bets you actually believe are worth placing. If you find 6 genuinely good bets, return 6.
  If you find 3, return 3. If you find none, return an empty array.
- NEVER pad the list to reach 10. A padded pick is worse than a missing one: every pick returned is
  staked and settled for real, so filler costs money. There is no penalty for a short list, and no
  reward for a long one — being asked for up to 10 is permission, not a quota.
- Do not reorder to spread picks across competitions or bet types. Conviction is the only ranking
  criterion; if your five best bets are all in one competition, rank them 1-5 anyway.
"""

_PROMPT_HEAD_LEAGUE = """You are a professional football betting analyst with deep expertise in the Premier League,
Belgian Jupiler Pro League, Bundesliga, La Liga, Serie A, Ligue 1, the UEFA Champions League, the UEFA
Europa League, the UEFA Conference League, and international tournament football including the FIFA World Cup.
You receive the next 48 hours of fixtures for ONE competition, named in the message, and must identify the
best value bets in that competition, ranked from best to worst — UP TO 10, in strict order of conviction.

RANKING — read this carefully:
- Rank 1 is your single highest-conviction bet in this competition; rank 10 is the weakest bet you would
  still genuinely place. Order the "picks" array by that ranking, best first.
- Return ONLY bets you actually believe are worth placing. If you find 3 genuinely good bets, return 3.
  If you find none, return an empty array — a competition with nothing worth backing today is a normal
  and expected answer, not a failure, and an empty list costs you nothing.
- NEVER pad the list to reach 10. A padded pick is worse than a missing one: every pick returned is
  staked and settled for real, so filler costs money. There is no penalty for a short list, and no
  reward for a long one — being asked for up to 10 is permission, not a quota. You are judged on the
  strike rate of what you return, never on how much of the allowance you used.
- You are seeing this competition on its own, and other competitions are being analysed separately.
  Judge every bet against the market price on its own merits — never against the other bets in this
  list, and never against how many picks a competition "ought" to produce. A 24-fixture qualifying
  round does not owe you more picks than a 4-fixture matchday.
- Do not reorder to spread picks across bet types or match days. Conviction is the only ranking criterion.
"""

_PROMPT_BODY = """
Champions League, Europa League and Conference League fixtures are European club ties. Qualifying rounds and the
knockout phase are played over two legs, so the h2h data for such a fixture often contains the first leg
of the very same tie — when it does, treat it as the single most informative data point you have, and
factor in that a team holding a comfortable aggregate lead may rotate or play conservatively. Ties also
pair clubs from very different league strengths, so weight domestic-league quality alongside form. This
is at its most extreme in Champions League qualifying, where a champion of a small association can draw
a side from a major league: the gap in squad depth is usually wider than recent domestic form suggests,
because those domestic results were earned against far weaker opposition. Unless a fixture is explicitly
marked "knockout": true, bet on the 90 minutes of THIS leg only — a two-legged tie's individual leg is a
normal 3-way match, not an elimination game.

Each fixture may include the following enriched context — use it to sharpen your analysis:
- home_form / away_form: UP TO 5 recent results per team as W/D/L (oldest → newest). venue field: H=home, A=away.
- home_recent / away_recent: score details for those matches.
- h2h: up to 5 most recent head-to-head meetings between the two teams, each with a date and score.
- h2h_record: the all-time W/D/L tally between the two clubs, with the counts labelled by team name.
- knockout: true — an elimination match (e.g. World Cup knockout rounds) that goes to extra time
  and penalties if level after 90 minutes.
When this data is present, weight recent form and H2H trends heavily in your reasoning.

A form string may hold FEWER than 5 results, or be absent entirely — most often near the start of a
season, when a club genuinely has not played five competitive matches yet. Read it for what it is: two
results are two results, not a five-match trend, and they deserve correspondingly less weight. A short
or missing form string is a reason to lower conviction, never a reason to invent form you were not given.

Your knowledge of player rosters, retirements, transfers, injuries, and international squad selections
may be outdated — squads (especially international ones) change up to matchday due to injuries, form,
and late call-ups, and a player you recall as a starter may have retired, moved clubs, or been dropped
entirely. Do NOT name specific players in your reasoning unless that player is explicitly mentioned in
the home_recent, away_recent, or h2h context provided for that fixture. Otherwise, reason at the team
level only — attacking depth, defensive organization, midfield control, squad experience, tournament
pedigree, and home advantage — rather than citing individual names from memory.

Since live odds are not provided, use your knowledge of typical market pricing to estimate realistic
decimal odds (e.g. a heavy favourite ~1.35, slight favourite ~1.75, toss-up ~2.00 each side).

The "probability" field is your honest estimate of how likely the pick is to win (0-100). It should
reflect your true belief, not simply 100/odds — a value bet is precisely one where your probability
is higher than the odds imply. Your stated probabilities are tracked and scored for calibration over
time, so be realistic: a pick you'd expect to win 6 times out of 10 is 60, not 75.

For each recommendation output valid JSON with this exact structure:
{
  "picks": [
    {
      "rank": <1 = highest conviction, ascending; array must be in this order and ranks must be unique>,
      "match": "<Home longName> vs <Away longName>",
      "league": "<league name>",
      "bet_type": "<e.g. Match Winner / Both Teams to Score / Over 2.5 Goals / Double Chance / Asian Handicap>",
      "pick": "<selection using actual team names — never 'Home Win' or 'Away Win'. E.g. 'Sweden Win', 'Ivory Coast or Draw', 'Yes', 'Over 2.5 Goals', 'Argentina -1.5'>",
      "odds": <estimated decimal odds as a number>,
      "probability": <your estimated true probability of this pick winning, as a number from 0 to 100>,
      "confidence": "<High / Medium / Low>",
      "reasoning": "<2-3 sentence rationale covering form, head-to-head, and value>"
    }
  ]
}

IMPORTANT — pick field naming rules:
- NEVER use "Home Win" or "Away Win" — always use the actual team name, e.g. "Sweden Win", "Morocco Win"
- NEVER use "Home or Draw" or "Away or Draw" — use e.g. "Ivory Coast or Draw", "Japan or Draw"
- For Over/Under, BTTS, and Asian Handicap keep the standard format: "Over 2.5 Goals", "Yes", "No", "Argentina -1.5"

IMPORTANT — Match Winner picks on knockout fixtures (those marked "knockout": true):
The 90-minute market and the tie-winner market are DIFFERENT bets with different odds, so you MUST
append the time scope to the pick text — never output a bare "<Team> Win" for a knockout fixture:
- "<Team> Win (90 min)" — regulation time only. If the match is level after 90 minutes this bet
  LOSES. A 3-way market (win/draw/lose), so odds are higher. "Draw (90 min)" is also valid here.
- "<Team> Win (Full-Time incl. ET/Pens)" — the team to advance, counting extra time and penalty
  shootouts. A 2-way market, so odds are lower.
Pick whichever market offers better value, and make sure your odds and probability refer to that
same market. Non-knockout fixtures cannot go to extra time — keep the plain format ("Sweden Win").

Return ONLY the JSON block, no other text."""

SYSTEM_PROMPT        = _PROMPT_HEAD_GLOBAL + _PROMPT_BODY
LEAGUE_SYSTEM_PROMPT = _PROMPT_HEAD_LEAGUE + _PROMPT_BODY

# Chooses the day's Core five out of the per-league winners. It re-ranks, it
# never re-analyses: the candidates are already bets each league call judged
# worth placing, so this call sees no fixtures, no form and no H2H — only the
# picks themselves — and answers with ids. Returning ids rather than picks is
# what makes it impossible for this step to reword a selection, move a price or
# invent a bet that no league call made.
CORE_SELECTION_PROMPT = """You are a professional football betting analyst assembling the day's headline card.

Below are the value bets already selected as the best available in each competition on today's slate,
grouped by competition and already ranked within it. Every one of them is a bet worth placing — none of
them needs vetting again, and you are not being asked to find anything new.

Your only job is to choose the {n} you would put in the headline book for the day, ranked 1 (best) to
{n}, judging conviction across the whole slate as if you had seen every fixture at once. A competition's
rank-1 bet is NOT automatically stronger than another competition's rank-2 — weigh the size of the edge
against the quoted price, the stated probability and the confidence. Do not spread the selection across
competitions for balance: if the five strongest bets on the slate are all in one competition, take all
five.

Do not invent, reword, re-price or re-rank anything else. Return ids only.

Return ONLY this JSON, no other text:
{{"core": [<id>, <id>, ...]}}   — exactly {n} ids, best first, no duplicates, all drawn from the list above."""


def _strip_code_fences(text: str) -> str:
    # Claude sometimes prefaces the JSON with a sentence of prose before the
    # fence (e.g. "I'll analyze these fixtures...\n\n```json\n{...}\n```"),
    # so search for the fenced block anywhere in the text rather than
    # assuming it starts at position 0.
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


# Model identifiers must never reach a subscriber-facing channel, and since
# 18 Aug 2026 the alert relays a raw upstream error — exactly the kind of string
# that carries one.
_MODEL_NAME_RE = re.compile(r"\bclaude[\w.\-]*|\b(?:opus|sonnet|haiku)[\w.\-]*", re.I)
_VENDOR_NAME_RE = re.compile(r"\banthropic\s*", re.I)
_DANGLING_SEP_RE = re.compile(r"([(\[])\s*[,;]\s*")

ALERT_DETAIL_MAX_CHARS = 300


def _scrub_model_names(text: str) -> str:
    """Strip the AI stack out of text bound for a subscriber-facing channel."""
    stripped = _VENDOR_NAME_RE.sub("", _MODEL_NAME_RE.sub("the model", text))
    # Dropping the vendor name can strand the separator that followed it:
    # "(Anthropic, claude-x)" would otherwise read "(, the model)".
    stripped = _DANGLING_SEP_RE.sub(r"\1", stripped)
    return " ".join(stripped.split())


def _notify_picks_failed(reason: str, detail: str = "") -> None:
    """
    Announce a total picks failure on Discord, the only delivery surface.

    Three consecutive whole-slate failures (16-18 Aug 2026, an exhausted API
    credit balance) went unseen for three days because this alert was
    Telegram-only behind a guard that returned early when Telegram was
    unconfigured. Telegram was removed entirely on 18 Aug 2026; the lesson that
    outlives it is that this function must never again fail quietly, so it
    checks delivery and logs an error when the send did not land.

    `detail` carries the underlying error so the alert says WHY, not just THAT.
    It is scrubbed of model AND vendor names and truncated: the channel is
    subscriber-facing, and an upstream error is arbitrary third-party text.
    """
    text = f"⚠️ Picks failed today — {reason}."
    if detail:
        clean = _scrub_model_names(detail)
        if len(clean) > ALERT_DETAIL_MAX_CHARS:
            clean = clean[:ALERT_DETAIL_MAX_CHARS] + "…"
        text += f"\nReason: {clean}"
    text += "\nCheck logs."

    # The football picks hub, mirroring how tennis alerts into its own picks
    # channel. This is the ONLY surface now, so an undelivered alert is a
    # silent outage — hence the explicit check on the return value rather than
    # trusting the fire-and-forget contract.
    if not send_to_discord("picks-cards", message=text):
        log.error("Could not deliver picks-failed alert to Discord ('picks-cards')")


def _parse_picks_response(raw: str, scope: str) -> list[dict]:
    """
    The picks array out of one model response. Raises ValueError if the text is
    not JSON even after the code-fence fallback — the caller decides whether
    that kills the run (all leagues failed) or just loses one competition.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: Claude sometimes wraps the JSON in a ```json ... ``` fence
        try:
            data = json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError as exc:
            log.error(
                "%s: response is not valid JSON, even after stripping code fences. "
                "Full raw response:\n%s",
                scope, raw,
            )
            raise ValueError(f"Could not parse response as JSON ({scope}): {exc}") from exc
    return data.get("picks", []) or []


def _analyse_one_league(
    league: str,
    fixtures: list[dict],
    *,
    cache_system_prompt: bool = False,
) -> list[dict]:
    """
    Up to MAX_PICKS_PER_LEAGUE conviction-ranked picks for ONE competition.

    Each competition gets its own call so it is judged on its own merits: a
    24-fixture Conference League qualifying round and a 4-fixture matchday are
    no longer competing for slots in a single global list of 10, which is what
    the per-league cap exists to fix.

    `cache_system_prompt` marks the ~1.8k-token system prompt as cacheable. It
    is identical across every league call in a run and the calls are seconds
    apart, so on a multi-league slate the first call writes the cache (billed at
    1.25x) and every later one reads it (0.1x). Off for a one-league run, where
    the write premium would be pure loss. usage_tracker prices both.
    """
    # Strip internal team/match IDs — not useful to Claude
    _STRIP = {"home_id", "away_id"}
    clean = [{k: v for k, v in f.items() if k not in _STRIP} for f in fixtures]
    payload = json.dumps(clean, indent=2, default=str)

    system: object = LEAGUE_SYSTEM_PROMPT
    if cache_system_prompt:
        system = [{
            "type": "text",
            "text": LEAGUE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]

    message = claude.messages.create(
        model="claude-sonnet-4-6",
        # 10 picks with 2-3 sentences of reasoning each ran to 1,999 output
        # tokens on 13 Aug 2026 — 97% of the old 2,048 ceiling, i.e. one verbose
        # run away from a truncated, unparseable response. max_tokens is a
        # ceiling, not a charge: unused headroom costs nothing.
        max_tokens=4096,
        temperature=0,
        system=system,
        messages=[{
            "role": "user",
            "content": (
                f"Competition: {league}\n\n"
                f"Upcoming {league} fixtures (next 48 hours):\n\n{payload}"
            ),
        }],
    )
    # Record token usage before anything can raise on the parse below —
    # the call is billed whether or not we manage to read the JSON. Every league
    # call keeps the same 'football-picks' job label so the daily usage summary
    # still reports one comparable line for the picks run.
    try:
        from usage_tracker import record_anthropic_usage
        record_anthropic_usage("football-picks", message.model, message.usage)
    except Exception as exc:
        log.debug("usage recording skipped: %s", exc)

    raw = message.content[0].text.strip()
    log.info("Claude raw response for %s (%d chars):\n%s", league, len(raw), raw)

    picks = _parse_picks_response(raw, league)

    seen: set[tuple] = set()
    deduped: list[dict] = []
    for pick in picks:
        key = (pick.get("match"), pick.get("bet_type"))
        if key not in seen:
            seen.add(key)
            deduped.append(pick)

    # Single enforcement point for MAX_PICKS_PER_LEAGUE — upstream of logging, so
    # a pick can never reach the sheet (and therefore P&L) without also reaching
    # delivery. Never silent: the dropped picks are logged in full.
    if len(deduped) > MAX_PICKS_PER_LEAGUE:
        dropped = deduped[MAX_PICKS_PER_LEAGUE:]
        log.warning(
            "%s returned %d picks, capping at %d — dropping %d: %s",
            league, len(deduped), MAX_PICKS_PER_LEAGUE, len(dropped),
            "; ".join(f"{p.get('match')} [{p.get('bet_type')} · {p.get('pick')}]"
                      for p in dropped),
        )
        deduped = deduped[:MAX_PICKS_PER_LEAGUE]

    # league_rank comes from array POSITION, never from the model's own "rank"
    # field: position is what dedup and the cap above already operate on, so a
    # returned number could disagree with where the pick actually sits (or
    # collide after a duplicate is removed). The model's ordering is respected;
    # its numbering is not load-bearing.
    #
    # The league is overwritten rather than read from the response. This call
    # was handed exactly one competition, so its name is known for certain here
    # — and it drives Discord channel routing, the Odds API sport key and the
    # sheet's League column, none of which should depend on the model echoing a
    # label back correctly.
    for i, pick in enumerate(deduped, 1):
        pick["league_rank"] = i
        pick["league"] = league

    log.info("%s: %d pick(s) returned", league, len(deduped))
    return deduped


def _pick_edge(pick: dict) -> float:
    """
    Stated probability minus the probability its own price implies. Used only to
    break ties in the deterministic Core ordering — never to select picks.
    """
    try:
        prob = float(pick.get("probability") or 0) / 100.0
        odds = float(pick.get("odds") or 0)
    except (TypeError, ValueError):
        return 0.0
    return prob - _implied_prob(odds)


def _deterministic_core_order(picks: list[dict]) -> list[dict]:
    """
    Fallback global ordering: every league's rank-1 pick first (best edge
    first), then every rank-2, and so on. Used when the Core selection call
    fails, and whenever there are so few candidates that asking is pointless.
    """
    return sorted(picks, key=lambda p: (p.get("league_rank", 99), -_pick_edge(p)))


def _render_core_candidates(picks: list[dict]) -> str:
    """The candidate list the Core selection call reads. Ids are list indices."""
    by_league: dict[str, list[tuple[int, dict]]] = {}
    for i, p in enumerate(picks):
        by_league.setdefault(p.get("league", "?"), []).append((i, p))

    blocks: list[str] = []
    for league, entries in by_league.items():
        lines = [f"## {league}"]
        for i, p in entries:
            lines.append(
                f"[id {i}] rank {p.get('league_rank', '?')} of {len(entries)} in this competition"
                f" — {p.get('match', '?')}\n"
                f"    {p.get('bet_type', '?')}: {p.get('pick', '?')} @ {p.get('odds', '?')}"
                f" | probability {p.get('probability', '?')}%"
                f" | confidence {p.get('confidence', 'N/A')}\n"
                f"    {p.get('reasoning', '') or '(no reasoning given)'}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _select_core_with_claude(picks: list[dict], n: int) -> list[dict] | None:
    """
    The n highest-conviction picks across every competition, best first, or None
    if the call fails. Returns the SAME dict objects that were passed in — the
    model answers with ids, so nothing it says can alter a pick.
    """
    payload = _render_core_candidates(picks)
    try:
        message = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            temperature=0,
            system=CORE_SELECTION_PROMPT.format(n=n),
            messages=[{"role": "user", "content": payload}],
        )
        try:
            from usage_tracker import record_anthropic_usage
            record_anthropic_usage("football-core-select", message.model, message.usage)
        except Exception as exc:
            log.debug("usage recording skipped: %s", exc)

        raw = message.content[0].text.strip()
        log.info("Core selection response (%d chars):\n%s", len(raw), raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = json.loads(_strip_code_fences(raw))

        chosen: list[dict] = []
        seen_ids: set[int] = set()
        for raw_id in data.get("core", []):
            idx = int(raw_id)
            if idx in seen_ids or not (0 <= idx < len(picks)):
                log.warning("Core selection returned an unusable id %r — ignored", raw_id)
                continue
            seen_ids.add(idx)
            chosen.append(picks[idx])
        if len(chosen) != n:
            # Short or malformed answers are recoverable: keep what came back in
            # the order it came back, and fill from the deterministic order.
            log.warning(
                "Core selection returned %d usable id(s), expected %d — filling "
                "the remainder deterministically", len(chosen), n,
            )
            for p in _deterministic_core_order(picks):
                if len(chosen) >= n:
                    break
                if p not in chosen:
                    chosen.append(p)
        return chosen[:n]
    except Exception as exc:
        log.warning(
            "Core selection call failed (%s) — falling back to the deterministic "
            "order. The run continues: this decides ordering only, never which "
            "bets exist.", exc,
        )
        try:
            from usage_tracker import alert_anthropic_failure
            alert_anthropic_failure("football-core-select", exc, "claude-sonnet-4-6")
        except Exception as inner:
            log.debug("failure alert skipped: %s", inner)
        return None


def _select_core_picks(picks: list[dict]) -> list[dict]:
    """
    Tag every pick Core or Extended and return them in delivery order: the Core
    five first (rank 1-5), then the Extended picks grouped by competition.

    Core is GLOBAL — the best CORE_PICKS_PER_RUN bets on the whole slate — while
    each league call only ever saw its own competition. So the global comparison
    that used to fall out of one ranked list is made explicitly here.

    Core is deliberately ordered first: excel_tracker's logging guard allows one
    open bet per fixture, so if a fixture somehow carries two picks, the Core one
    is the one that reaches the sheet.
    """
    if not picks:
        return []

    n_core = min(CORE_PICKS_PER_RUN, len(picks))
    if len(picks) <= CORE_PICKS_PER_RUN:
        # Every pick is Core anyway — the only open question is their order, and
        # that is not worth a model call.
        core = _deterministic_core_order(picks)
    else:
        core = _select_core_with_claude(picks, n_core) or _deterministic_core_order(picks)[:n_core]

    core_ids = {id(p) for p in core}
    for i, pick in enumerate(core, 1):
        pick["rank"] = i
        pick["pick_tier"] = PICK_TIER_CORE
    extended = [p for p in picks if id(p) not in core_ids]
    for pick in extended:
        # No global rank: an Extended pick's standing is its rank inside its own
        # competition, and pretending otherwise would invite a reader to compare
        # numbers that were never compared.
        pick["rank"] = None
        pick["pick_tier"] = PICK_TIER_EXTENDED

    return core + extended


def analyse_with_claude(fixtures_by_league: dict[str, list[dict]]) -> list[dict]:
    """
    One Claude call per competition (up to MAX_PICKS_PER_LEAGUE picks each), then
    one global selection call that names the Core five.

    A competition whose call fails costs that competition only — the rest of the
    slate is still delivered, and only a run where EVERY competition failed
    raises and alerts, which is the behaviour the single-call version had.
    """
    picks_by_league: dict[str, list[dict]] = {}
    failed: list[str] = []
    errors: list[str] = []
    # Cache the shared system prompt only when more than one league will use it.
    cache_prompt = len(fixtures_by_league) > 1

    for league, fixtures in fixtures_by_league.items():
        if not fixtures:
            continue
        try:
            picks_by_league[league] = _analyse_one_league(
                league, fixtures, cache_system_prompt=cache_prompt,
            )
        except Exception as exc:
            failed.append(league)
            errors.append(str(exc))
            log.error("Analysis failed for %s — that competition is skipped: %s", league, exc)
            # Alert the ops channel on an exhausted credit balance, and record
            # every failure for the daily summary's status line. Deduped per
            # day, so a slate where all ten competitions fail posts ONE alert.
            try:
                from usage_tracker import alert_anthropic_failure
                alert_anthropic_failure("football-picks", exc, "claude-sonnet-4-6")
            except Exception as inner:
                log.debug("failure alert skipped: %s", inner)

    if failed and not any(picks_by_league.values()):
        # Nothing survived: same outcome as a failed single call, so alert and
        # raise exactly as before. Reason text is relayed verbatim into the
        # alert — keep it free of model names. The first upstream error travels
        # with it: every competition failing almost always means ONE shared
        # cause (an exhausted credit balance, a bad key, an upstream outage),
        # and naming it is the difference between noticing in minutes and
        # noticing in days.
        _notify_picks_failed(
            "the analysis returned no usable picks",
            detail=errors[0] if errors else "",
        )
        raise ValueError(
            f"Analysis failed for every competition attempted ({', '.join(failed)})"
        )
    if failed:
        log.warning(
            "%d of %d competition(s) failed and were skipped: %s",
            len(failed), len(failed) + len(picks_by_league), ", ".join(failed),
        )

    # Dedupe ACROSS competitions on the same (match, bet_type) key the single
    # call used to apply to its whole output. Each league call already deduped
    # its own response, so this only fires if one fixture reached two buckets —
    # partition_fixtures is built not to do that, but the leagueId sets it
    # matches on are discovered at runtime, and the cost of being wrong here is
    # two stakes on one result.
    all_picks: list[dict] = []
    seen: set[tuple] = set()
    for league_picks in picks_by_league.values():
        for p in league_picks:
            key = (p.get("match"), p.get("bet_type"))
            if key in seen:
                log.warning(
                    "Dropping %s [%s] from %s — the same bet came back from another "
                    "competition, so one fixture reached two buckets",
                    p.get("match"), p.get("bet_type"), p.get("league"),
                )
                continue
            seen.add(key)
            all_picks.append(p)

    ordered = _select_core_picks(all_picks)

    n_core = sum(1 for p in ordered if p.get("pick_tier") == PICK_TIER_CORE)
    log.info(
        "Analysis returned %d pick(s) across %d competition(s): %d Core, %d Extended (%s)",
        len(ordered), len(picks_by_league), n_core, len(ordered) - n_core,
        ", ".join(f"{lg} {len(ps)}" for lg, ps in picks_by_league.items()) or "none",
    )
    # A short list — per league or overall — is the expected, correct outcome
    # when Claude finds fewer good bets; the prompt forbids padding. Logged at
    # INFO, never warned about: treating it as a fault is exactly what would
    # pressure the lists back toward filler picks.
    log.info(
        "Cap is %d per competition; no padding is applied to reach it",
        MAX_PICKS_PER_LEAGUE,
    )

    return ordered


# ── Discord (the delivery surface) ───────────────────────────────────────

def _discord_pick_embed(p: dict) -> dict:
    """
    One pick as a Discord embed; the league renders as the author line.

    Extended picks are labelled in that author line and carry their rank WITHIN
    THEIR OWN COMPETITION (since 15 Aug 2026 — each competition is analysed
    separately, so there is no global rank to show and "#3" means third-best in
    this league, not third-best on the slate). The label matters: an Extended
    pick is a real bet but sits outside the tracked 5-pick book, and an
    unlabelled embed would imply otherwise.
    """
    # Kelly stake, Core only. It lived ONLY in the Telegram digest until
    # 18 Aug 2026 — not in this embed, not on the card — so removing Telegram
    # without moving it here would have deleted the staking advice from every
    # surface at once. Core only because that is exactly who the digest showed
    # it for: Extended picks sit outside the tracked book, and a stake figure on
    # one would read as a claim on the bankroll it is deliberately excluded from.
    kelly = p.get("kelly")
    if kelly is not None and p.get("pick_tier", PICK_TIER_CORE) == PICK_TIER_CORE:
        stake = float(kelly.get("stake") or 0)
        if stake == 0:
            p = {**p, "stake_display": "⛔ No stake — negative edge"}
        else:
            note = f" — {kelly['note']}" if kelly.get("note") else ""
            p = {**p, "stake_display": f"€{stake:.2f} (Kelly{note})"}

    league = p.get("league", "")
    rank   = p.get("league_rank")
    if p.get("pick_tier") == PICK_TIER_EXTENDED:
        # "league rank 3", never a bare "#3": several picks a day now carry the
        # same number in different channels, and a bare one would read as a
        # position on the whole slate.
        context = f"{league} · EXTENDED · league rank {rank}" if rank else f"{league} · EXTENDED"
    else:
        context = league
    return build_pick_embed(p, context=context)


def _pick_log_entry(pick: dict, kickoff_lookup: dict[str, str]) -> dict:
    """One pick as a tracker.log_picks_batch entry."""
    claude_prob = pick.get("probability")
    return {
        "match": pick["match"],
        "league": pick["league"],
        "bet_type": pick["bet_type"],
        "pick": pick["pick"],
        "odds": float(pick["odds"]),
        "confidence": pick.get("confidence", "N/A"),
        "claude_prob": float(claude_prob) if claude_prob is not None else None,
        "market_prob": pick.get("market_prob"),
        "kickoff_utc": kickoff_lookup.get(pick["match"], ""),
        # The price the card shows and settlement pays out at.
        "market_odds": pick.get("market_odds"),
        "pick_tier": pick.get("pick_tier", PICK_TIER_CORE),
    }


# ── Main job ──────────────────────────────────────────────────────────────────

def _kickoff_lookup(fixtures_by_league: dict[str, list[dict]]) -> dict[str, str]:
    """
    Map '<Home> vs <Away>' -> kickoff_utc, so each Claude pick (which uses that
    exact match string per SYSTEM_PROMPT) can be tagged with its kickoff time
    for the closing-odds job. Purely additive metadata — never affects picks.
    """
    lookup: dict[str, str] = {}
    for fixtures in fixtures_by_league.values():
        for f in fixtures:
            lookup[f"{f['home']} vs {f['away']}"] = f.get("kickoff_utc", "")
    return lookup


async def daily_picks_job():
    log.info("Starting morning picks job")

    if picks_exist_for_session("morning"):
        log.info("Morning picks already logged for today — skipping")
        return

    try:
        all_matches = fetch_upcoming_matches()
        log.info("Fetched %d total matches for next 48 hours", len(all_matches))
    except Exception as exc:
        log.error("Failed to fetch today's matches: %s", exc)
        return

    fixtures_by_league = partition_fixtures(all_matches)

    if not fixtures_by_league:
        log.info("No upcoming fixtures today across tracked competitions — skipping analysis")
        return

    for league, fixtures in fixtures_by_league.items():
        log.info("  %s: %d upcoming fixtures", league, len(fixtures))

    try:
        enrich_with_context(fixtures_by_league)
    except Exception as exc:
        log.warning("Context enrichment failed — proceeding without form/H2H data: %s", exc)

    try:
        picks = analyse_with_claude(fixtures_by_league)
    except Exception as exc:
        log.error("Claude analysis failed: %s", exc)
        return

    try:
        kickoff_lookup = _kickoff_lookup(fixtures_by_league)
    except Exception as exc:
        log.warning("Kickoff lookup build failed (non-fatal): %s", exc)
        kickoff_lookup = {}

    try:
        enrich_picks_with_real_odds(picks)
    except Exception as exc:
        log.warning("Real odds enrichment failed — proceeding with Claude odds only: %s", exc)

    try:
        for pick in picks:
            pick["kelly"] = calculate_kelly_stake(
                pick["bet_type"], float(pick["odds"]), pick.get("confidence", "")
            )
    except Exception as exc:
        log.warning("Kelly stake calculation failed (picks will send without it): %s", exc)

    # BOTH tiers are logged and settled — the tier travels with the row so the
    # reporting layer can filter on it (excel_tracker._core_rows). Written as
    # ONE batch: `picks` is Core-first, and a 30-pick run logged one at a time
    # costs ~120 Sheets calls and 30 full-sheet repaints.
    try:
        written = log_picks_batch(
            [_pick_log_entry(p, kickoff_lookup) for p in picks], session="morning",
        )
        log.info("Logged %d of %d pick(s) to the sheet", written, len(picks))
    except Exception as exc:
        log.warning("Failed to log picks: %s", exc)

    # From here on the CARD sees Core only, so the daily post stays the same
    # 5-pick book it has been since 30 Jun 2026. Extended picks reach Discord's
    # league channels below, and the Sheet above.
    # Sorted by rank rather than trusting list order: the renderers number the
    # picks 1..5 by position, so conviction order has to be a guarantee here,
    # not something that happens to hold.
    core_picks     = sorted(
        [p for p in picks if p.get("pick_tier", PICK_TIER_CORE) == PICK_TIER_CORE],
        key=lambda p: p.get("rank") or 99,
    )
    extended_picks = [p for p in picks if p.get("pick_tier") == PICK_TIER_EXTENDED]

    # The Telegram text digest is gone with Telegram (18 Aug 2026) and is NOT
    # reproduced on Discord: it was a flat re-listing of the same Core picks the
    # per-pick embeds below already carry in richer form, and its one piece of
    # unique content — the Kelly stake — moved into the Core embed itself.
    card = None
    try:
        # Core only, passed explicitly: generate_picks_card also slices to 5
        # internally, but relying on that would make the card's contents an
        # accident of ordering rather than a stated choice.
        card = generate_picks_card(core_picks, session="morning")
        log.info("Picks card generated: %s", card.name)
    except Exception as exc:
        log.warning("Picks card failed (non-fatal): %s", exc)

    # Discord delivery — the only delivery; send_to_discord never raises.
    # Both tiers post to their league channel; the embed marks which is which.
    #
    # Paced deliberately. Discord allows roughly one sustained message per second
    # per channel, and since 15 Aug 2026 a single competition can send 10 embeds
    # back to back. send_to_discord retries a 429 exactly once, so an unpaced
    # burst would spend that retry immediately and then start dropping picks.
    try:
        if card is not None:
            send_to_discord("picks-cards", image_path=card)
        sent = 0
        for pick in picks:
            channel_key = DISCORD_LEAGUE_CHANNEL_KEYS.get(pick.get("league", ""))
            if not channel_key:
                continue
            if sent:
                await asyncio.sleep(DISCORD_PICK_SEND_DELAY)
            send_to_discord(channel_key, embed=_discord_pick_embed(pick))
            sent += 1
    except Exception as exc:
        log.warning("Discord picks delivery failed (non-fatal): %s", exc)

    log.info("Delivered %d Core and %d Extended pick(s)", len(core_picks), len(extended_picks))

    # Opus 5 shadow — side-by-side model comparison on the EXACT same enriched
    # fixture pool this run just used, so the model is the only variable (no
    # second RapidAPI fetch, no re-enrichment). Deliberately last, after every
    # production surface above has already been logged and delivered: a shadow
    # failure can then only ever cost the shadow. Inert unless the
    # 'opus-shadow' Discord key is configured. Lazy import avoids a circular
    # import — opus_shadow imports SYSTEM_PROMPT/claude back from this module.
    try:
        from opus_shadow import run_opus_shadow
        await asyncio.to_thread(run_opus_shadow, fixtures_by_league, kickoff_lookup)
    except Exception as exc:
        log.warning("Opus 5 shadow failed (non-fatal): %s", exc)
        try:
            from usage_tracker import alert_anthropic_failure
            from opus_shadow import OPUS_MODEL
            alert_anthropic_failure("opus-shadow", exc, OPUS_MODEL)
        except Exception as inner:
            log.debug("failure alert skipped: %s", inner)

    try:
        ig_card = generate_picks_card_ig(core_picks)
        log.info("Instagram picks card saved: %s", ig_card.name)
        # Same 'picks-cards' channel as the regular card, intentional (both card
        # variants land in one place). Until 18 Aug 2026 this card ALSO went to a
        # dedicated Telegram channel (TELEGRAM_IG_CHANNEL_ID) used for sourcing
        # Instagram posts; that destination is gone, and the card is now pulled
        # from Discord instead. It is still generated and saved to cards/ either
        # way, so nothing about the artefact itself changed.
        send_to_discord("picks-cards", image_path=ig_card)
    except Exception as exc:
        log.warning("Instagram picks card failed (non-fatal): %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_picks_job, "cron", hour=12, minute=0, timezone="Europe/Brussels")
    scheduler.start()
    log.info("Scheduler started — picks will post daily at 12:00 Europe/Brussels")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
