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
from telegram import Bot

from env_loader import load_env
from tracker import log_pick, picks_exist_for_session
from excel_tracker import calculate_kelly_stake
from card_generator import generate_picks_card, generate_picks_card_ig
from discord_bot import build_pick_embed, send_to_discord

load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")
TELEGRAM_IG_CHANNEL_ID = os.environ.get("TELEGRAM_IG_CHANNEL_ID")

# Single-ID domestic leagues (fotmob-based IDs)
# Bundesliga/La Liga/Serie A/Ligue 1 IDs verified 19 Jul 2026 against the live
# API's 2026-27 opening matchdays (team rosters checked, not just ID reuse).
LEAGUES = {
    "Premier League": 47,
    "Jupiler Pro League": 900433,
    "Bundesliga": 54,
    "La Liga": 87,
    "Serie A": 55,
    "Ligue 1": 53,
}

# 2026 FIFA World Cup — group-stage leagueIds confirmed via live API scan Jun 11-28.
# One ID per group/batch; knockout IDs are unknown until draws happen post-group stage.
WC_2026_IDS: set[int] = {
    914609,                                    # opening batch  (Jun 11)
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

# Regex to identify youth-team suffixes  e.g. "U19", "U-21", "U 23"
_YOUTH_RE = re.compile(r"\bU[\s-]?1[5-9]\b|\bU[\s-]?2[0-3]\b|youth|junior", re.IGNORECASE)

RAPIDAPI_HOST = "free-api-live-football-data.p.rapidapi.com"

ODDS_API_HOST = "https://api.the-odds-api.com/v4"

# Maps our internal competition names to The Odds API's sport keys.
ODDS_API_SPORT_KEYS: dict[str, str] = {
    "Premier League": "soccer_epl",
    "Jupiler Pro League": "soccer_belgium_first_div",
    "FIFA World Cup 2026": "soccer_fifa_world_cup",
    "Bundesliga": "soccer_germany_bundesliga",
    "La Liga": "soccer_spain_la_liga",
    "Serie A": "soccer_italy_serie_a",
    "Ligue 1": "soccer_france_ligue_one",
}

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


def _is_wc_knockout(match: dict, domestic_ids: set[int]) -> bool:
    """
    Detect a WC knockout match on a previously-unknown leagueId.
    Fires when both teams are confirmed WC participants and the leagueId
    is not a known domestic/club competition.
    """
    lid = match.get("leagueId")
    if lid in WC_2026_IDS or lid in domestic_ids:
        return False  # already handled, or definitely not WC
    home = match["home"]["longName"]
    away = match["away"]["longName"]
    return _is_wc_participant(home) and _is_wc_participant(away)


def partition_fixtures(all_matches: list[dict]) -> dict[str, list[dict]]:
    """Split today's upcoming matches into per-league buckets."""
    upcoming = [m for m in all_matches if _is_upcoming(m)]

    result: dict[str, list[dict]] = {}

    # Domestic leagues — single leagueId each
    domestic_ids: set[int] = set(LEAGUES.values())
    for league_name, league_id in LEAGUES.items():
        found = [m for m in upcoming if m.get("leagueId") == league_id]
        if found:
            result[league_name] = [build_fixture_summary(m) for m in found]

    # World Cup — active until July 19 2026
    # Primary: known group-stage leagueIds
    # Fallback: any match where both teams are confirmed WC participants (catches knockout IDs)
    if date.today() <= WC_2026_END:
        wc = [
            m for m in upcoming
            if m.get("leagueId") in WC_2026_IDS or _is_wc_knockout(m, domestic_ids)
        ]
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

    return result


# ── Form & H2H enrichment ────────────────────────────────────────────────────

def _api_headers() -> dict:
    return {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": os.environ.get("RAPIDAPI_KEY", "")}


def _fetch_team_recent(team_id: int, n: int = 5) -> list[dict]:
    """Return last n finished matches for a team. Empty list on any error."""
    try:
        resp = requests.get(
            f"https://{RAPIDAPI_HOST}/football-get-team-matches",
            headers=_api_headers(),
            params={"teamId": team_id, "matchType": "previous", "limit": n},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("response", {}).get("matches", [])[:n]
    except Exception as exc:
        log.debug("fetch_team_recent(%s) skipped: %s", team_id, exc)
        return []


def _fetch_h2h(home_id: int, away_id: int, n: int = 5) -> list[dict]:
    """Return last n H2H meetings between two teams. Empty list on any error."""
    try:
        resp = requests.get(
            f"https://{RAPIDAPI_HOST}/football-get-h2h",
            headers=_api_headers(),
            params={"firstTeamId": home_id, "secondTeamId": away_id, "limit": n},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("response", {}).get("matches", [])[:n]
    except Exception as exc:
        log.debug("fetch_h2h(%s, %s) skipped: %s", home_id, away_id, exc)
        return []


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
    All network calls are individually try/except'd — failure leaves the
    fixture unchanged and the rest of the job continues normally.
    """
    team_cache: dict[int, list[dict]] = {}

    for league, fixtures in fixtures_by_league.items():
        for fixture in fixtures:
            home_id = fixture.get("home_id")
            away_id = fixture.get("away_id")
            if not home_id or not away_id:
                continue

            # Fetch (or reuse cached) recent matches for each team
            if home_id not in team_cache:
                time.sleep(1)
                team_cache[home_id] = _fetch_team_recent(home_id)
            home_matches = team_cache[home_id]

            if away_id not in team_cache:
                time.sleep(1)
                team_cache[away_id] = _fetch_team_recent(away_id)
            away_matches = team_cache[away_id]

            # Head-to-head
            time.sleep(1)
            h2h_matches = _fetch_h2h(home_id, away_id)

            fixture["home_form"]   = _form_string(home_matches, home_id)
            fixture["away_form"]   = _form_string(away_matches, away_id)
            fixture["home_recent"] = [_summarize_match(m, home_id) for m in home_matches]
            fixture["away_recent"] = [_summarize_match(m, away_id) for m in away_matches]
            fixture["h2h"]         = [_summarize_match(m) for m in h2h_matches]

            log.info(
                "Context enriched: %s vs %s | home=%s away=%s h2h=%d",
                fixture["home"], fixture["away"],
                fixture["home_form"] or "N/A",
                fixture["away_form"] or "N/A",
                len(h2h_matches),
            )


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


def _fetch_odds_events(sport_key: str | None) -> list[dict] | None:
    """
    Raw fetch of every event + bookmaker odds for one Odds API sport_key.
    None if the sport_key/API key is missing or the request fails. Split out
    from fetch_real_odds so callers that need odds for several matches in the
    same competition (e.g. the closing-odds job) can fetch once and filter
    client-side, instead of one request per match.
    """
    api_key = os.environ.get("ODDS_API_KEY")
    if not sport_key or not api_key:
        return None
    try:
        resp = requests.get(
            f"{ODDS_API_HOST}/sports/{sport_key}/odds",
            params={
                "apiKey": api_key,
                "regions": "eu,uk",
                "markets": "h2h,totals,spreads",
                "oddsFormat": "decimal",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.debug("_fetch_odds_events(%s) failed: %s", sport_key, exc)
        return None


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
    sport_key = ODDS_API_SPORT_KEYS.get(competition)
    events = _fetch_odds_events(sport_key)
    if events is None:
        return None

    event = next(
        (
            e for e in events
            if _team_match(e.get("home_team", ""), home_team)
            and _team_match(e.get("away_team", ""), away_team)
        ),
        None,
    )
    if event is None:
        return None

    return _parse_odds_event(event)


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
    """
    odds_cache: dict[tuple[str, str, str], dict | None] = {}

    for pick in picks:
        try:
            match = pick.get("match", "")
            if " vs " not in match:
                continue
            home, away = match.split(" vs ", 1)
            league = pick.get("league", "")

            cache_key = (home, away, league)
            if cache_key not in odds_cache:
                odds_cache[cache_key] = fetch_real_odds(home, away, league)
            real_odds = odds_cache[cache_key]
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

SYSTEM_PROMPT = """You are a professional football betting analyst with deep expertise in the Premier League,
Belgian Jupiler Pro League, Bundesliga, La Liga, Serie A, Ligue 1, and international tournament football
including the FIFA World Cup.
You receive upcoming fixtures for the next 48 hours and must identify the top 5 value bets across all competitions.

Each fixture may include the following enriched context — use it to sharpen your analysis:
- home_form / away_form: last 5 results for each team as W/D/L (oldest → newest). venue field: H=home, A=away.
- home_recent / away_recent: score details for those last 5 matches.
- h2h: last 5 head-to-head meetings between the two teams with scores.
- knockout: true — an elimination match (e.g. World Cup knockout rounds) that goes to extra time
  and penalties if level after 90 minutes.
When this data is present, weight recent form and H2H trends heavily in your reasoning.

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


def _strip_code_fences(text: str) -> str:
    # Claude sometimes prefaces the JSON with a sentence of prose before the
    # fence (e.g. "I'll analyze these fixtures...\n\n```json\n{...}\n```"),
    # so search for the fenced block anywhere in the text rather than
    # assuming it starts at position 0.
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _notify_picks_failed(reason: str) -> None:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID):
        log.error("Cannot send picks-failed Telegram alert — bot token/channel not configured")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "text": f"⚠️ Picks failed today — {reason}. Check logs.",
            },
            timeout=10,
        )
    except Exception as exc:
        log.error("Failed to send picks-failed Telegram alert: %s", exc)


def analyse_with_claude(fixtures_by_league: dict[str, list[dict]]) -> list[dict]:
    # Strip internal team/match IDs — not useful to Claude
    _STRIP = {"home_id", "away_id"}
    clean = {
        league: [{k: v for k, v in f.items() if k not in _STRIP} for f in fixtures]
        for league, fixtures in fixtures_by_league.items()
    }
    payload = json.dumps(clean, indent=2, default=str)
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Upcoming fixtures (next 48 hours):\n\n{payload}"}],
    )
    raw = message.content[0].text.strip()
    log.info("Claude raw response (%d chars):\n%s", len(raw), raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: Claude sometimes wraps the JSON in a ```json ... ``` fence
        try:
            data = json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError as exc:
            log.error(
                "Claude response is not valid JSON, even after stripping code fences. "
                "Full raw response:\n%s",
                raw,
            )
            _notify_picks_failed("Claude returned an unparseable response")
            raise ValueError(f"Could not parse Claude response as JSON: {exc}") from exc

    picks = data.get("picks", [])
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for pick in picks:
        key = (pick.get("match"), pick.get("bet_type"))
        if key not in seen:
            seen.add(key)
            deduped.append(pick)
    return deduped


# ── Telegram ──────────────────────────────────────────────────────────────────

def _escape_md(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def format_telegram_message(picks: list[dict], header: str = "Football Picks") -> str:
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    lines = [f"*{_escape_md(header)} — {_escape_md(today)}*\n"]
    for i, p in enumerate(picks, 1):
        kelly = p.get("kelly")
        if kelly is not None and float(kelly.get("stake") or 0) == 0:
            kelly_line = "  ⛔ No stake — negative edge\n"
        elif kelly is not None:
            note_suffix = f" — {_escape_md(kelly['note'])}" if kelly.get("note") else ""
            stake_str = f"{kelly['stake']:.2f}"
            kelly_line = (
                f"  💰 Suggested stake: €{_escape_md(stake_str)} \\(Kelly{note_suffix}\\)\n"
            )
        else:
            kelly_line = ""

        market_odds = p.get("market_odds")
        if market_odds is not None:
            value_tag = " 🔥 *VALUE*" if p.get("value") else ""
            odds_line = (
                f"  Odds: Claude `{_escape_md(str(p['odds']))}` "
                f"\\| Market `{_escape_md(str(market_odds))}`{value_tag} "
                f"\\| Confidence: {_escape_md(p['confidence'])}\n"
            )
        else:
            odds_line = (
                f"  Odds: `{_escape_md(str(p['odds']))}` \\| Confidence: {_escape_md(p['confidence'])}\n"
            )

        lines.append(
            f"*{i}\\. {_escape_md(p['match'])}* \\({_escape_md(p['league'])}\\)\n"
            f"  Bet: {_escape_md(p['bet_type'])} — *{_escape_md(p['pick'])}*\n"
            + odds_line
            + f"  _{_escape_md(p['reasoning'])}_\n"
            + kelly_line
        )
    lines.append("_Good luck\\! Bet responsibly\\._")
    return "\n".join(lines)


async def send_to_telegram(text: str):
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHANNEL_ID,
        text=text,
        parse_mode="MarkdownV2",
    )


# ── Discord (additive delivery — never affects the Telegram flow) ────────────

def _discord_pick_embed(p: dict) -> dict:
    """One pick as a Discord embed; the league renders as the author line."""
    return build_pick_embed(p, context=p.get("league", ""))


async def _send_photo(path, chat_id: str | None = None) -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    with open(path, "rb") as f:
        await bot.send_photo(chat_id=chat_id or TELEGRAM_CHANNEL_ID, photo=f)


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

    for pick in picks:
        try:
            claude_prob = pick.get("probability")
            log_pick(
                match=pick["match"],
                league=pick["league"],
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
                session="morning",
                claude_prob=float(claude_prob) if claude_prob is not None else None,
                market_prob=pick.get("market_prob"),
                kickoff_utc=kickoff_lookup.get(pick["match"], ""),
            )
        except Exception as exc:
            log.warning("Failed to log pick: %s", exc)

    message = format_telegram_message(picks, header="Football Picks")
    try:
        await send_to_telegram(message)
        log.info("Sent %d morning picks to Telegram", len(picks))
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)

    card = None
    try:
        card = generate_picks_card(picks, session="morning")
        await _send_photo(card)
        log.info("Picks card sent: %s", card.name)
    except Exception as exc:
        log.warning("Picks card failed (non-fatal): %s", exc)

    # Discord delivery — additive; send_to_discord never raises
    try:
        if card is not None:
            send_to_discord("picks-cards", image_path=card)
        for pick in picks:
            channel_key = DISCORD_LEAGUE_CHANNEL_KEYS.get(pick.get("league", ""))
            if channel_key:
                send_to_discord(channel_key, embed=_discord_pick_embed(pick))
    except Exception as exc:
        log.warning("Discord picks delivery failed (non-fatal): %s", exc)

    try:
        ig_card = generate_picks_card_ig(picks)
        log.info("Instagram picks card saved: %s", ig_card.name)
        if TELEGRAM_IG_CHANNEL_ID:
            await _send_photo(ig_card, chat_id=TELEGRAM_IG_CHANNEL_ID)
            log.info("Instagram picks card sent to TELEGRAM_IG_CHANNEL_ID")
        else:
            log.info("TELEGRAM_IG_CHANNEL_ID not set — skipping send")
        # Discord mirror — same 'picks-cards' channel as the regular card,
        # intentional (both card variants land in one place); send_to_discord
        # never raises
        send_to_discord("picks-cards", image_path=ig_card)
    except Exception as exc:
        log.warning("Instagram picks card failed (non-fatal): %s", exc)

    # Fable 5 shadow — side-by-side model comparison EXPERIMENT on the exact
    # same enriched fixture pool. Fully non-fatal: a Fable failure can never
    # affect the production Sonnet picks above. Lazy import avoids a circular
    # import (fable_shadow imports SYSTEM_PROMPT/claude back from this module).
    try:
        from fable_shadow import run_fable_shadow
        await asyncio.to_thread(run_fable_shadow, fixtures_by_league, kickoff_lookup)
    except Exception as exc:
        log.warning("Fable 5 shadow pipeline failed (non-fatal): %s", exc)


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
