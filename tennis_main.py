"""
tennis_main.py — daily ATP/WTA tennis picks pipeline.

Fully separate from the football system (main.py): its own RapidAPI data
source, its own Claude system prompt, its own Google Sheets tab ('Tennis
Picks' via tennis_excel_tracker.py), and its own schedule slot (12:30
Europe/Brussels, 30 min after football's daily picks). It imports nothing
from main.py / excel_tracker.py / tracker.py and shares no calibration data
or sheet columns with football.

Delivery is Discord-ONLY — unlike football, which posts to Telegram and
mirrors to Discord, tennis never touches Telegram (user preference: Discord
is easier to view). Each pick's text is posted to the 'tennis-picks' Discord
channel key via discord_bot.py's send_to_discord.

Data source: "Tennis API - ATP WTA ITF" (MatchStat) on RapidAPI — same
RAPIDAPI_KEY as the football API, but the account must be subscribed to this
API separately. Host overridable via TENNIS_RAPIDAPI_HOST.

Run manually:
    python tennis_main.py --now     one-shot: fetch + analyse + post immediately
    python tennis_main.py           start the scheduler (12:30 Europe/Brussels)
"""
import asyncio
import difflib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from card_generator import generate_tennis_picks_card
from discord_bot import build_pick_embed, send_to_discord
from env_loader import load_env
from tennis_excel_tracker import (
    calculate_tennis_kelly_stake,
    log_tennis_pick,
    tennis_picks_exist_for_today,
)

load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

TENNIS_RAPIDAPI_HOST = os.environ.get(
    "TENNIS_RAPIDAPI_HOST", "tennis-api-atp-wta-itf.p.rapidapi.com"
)

ODDS_API_HOST = "https://api.the-odds-api.com/v4"

TOURS = ("atp", "wta")

# Keep API usage and the Claude payload bounded on busy days (Grand Slams can
# have 60+ singles matches per day across both tours).
MAX_FIXTURES_PER_TOUR = 25
MAX_ENRICHED_FIXTURES = 20

# Rank tier split — no fixtures are ever excluded by rank. Picks where BOTH
# players are ranked inside the top TENNIS_RANK_THRESHOLD go to the
# 'tennis-picks' Discord channel; everything else (either player outside the
# threshold, or unranked) goes to 'tennis-picks-lower'. The tier is also
# logged to the Sheet's 'Rank Tier' column so calibration/CLV can eventually
# be reported per tier.
TENNIS_RANK_THRESHOLD = int(os.environ.get("TENNIS_RANK_THRESHOLD", "150"))
TENNIS_TOP_TIER_LABEL = f"Top {TENNIS_RANK_THRESHOLD}"
TENNIS_LOWER_TIER_LABEL = "Lower Ranked"

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── RapidAPI helpers ──────────────────────────────────────────────────────────

def _tennis_headers() -> dict:
    return {
        "x-rapidapi-host": TENNIS_RAPIDAPI_HOST,
        "x-rapidapi-key": os.environ.get("RAPIDAPI_KEY", ""),
    }


def _tennis_get(path: str, params: dict | None = None) -> dict | list | None:
    """GET a tennis API path. None on any error — callers must handle it."""
    try:
        resp = requests.get(
            f"https://{TENNIS_RAPIDAPI_HOST}{path}",
            headers=_tennis_headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.debug("tennis API %s failed: %s", path, exc)
        return None


def _data_list(js: dict | list | None) -> list:
    """The tennis API wraps most list responses in {"data": [...]}; some are bare arrays."""
    if js is None:
        return []
    if isinstance(js, dict):
        return js.get("data", []) or []
    return js


def _tennis_get_paged(path: str, max_pages: int = 30) -> list:
    """
    All items from a paginated tennis endpoint. The fixtures-by-date endpoint
    returns {"data": [10 items], "hasNextPage": true} — a busy day runs to
    25+ pages (250+ fixtures incl. qualifying/juniors), so reading only
    page 1 silently drops >90% of the slate (the bug that hid most picks
    from the results checker until 11 Jul 2026). Follows pages until
    hasNextPage is falsy or max_pages. A failed page gets one retry after a
    short pause (transient RapidAPI throttling was observed truncating a
    whole day to zero mid-run); a second failure stops with a warning so a
    partial slate is still returned.
    """
    items: list = []
    page = 1
    while page <= max_pages:
        js = _tennis_get(path, params={"page": page})
        if js is None:
            time.sleep(2)
            js = _tennis_get(path, params={"page": page})
            if js is None:
                log.warning("tennis API page fetch failed twice — %s page %d "
                            "(returning %d items so far)", path, page, len(items))
                break
        items.extend(_data_list(js))
        if not (isinstance(js, dict) and js.get("hasNextPage")):
            break
        page += 1
        time.sleep(0.5)
    return items


def _is_singles(fixture: dict) -> bool:
    """Doubles fixtures carry paired names ('A. Krajicek/H. Patten') — skip them."""
    p1 = (fixture.get("player1") or {}).get("name", "")
    p2 = (fixture.get("player2") or {}).get("name", "")
    return bool(p1) and bool(p2) and "/" not in p1 and "/" not in p2


def _parse_start(fixture: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(fixture.get("date", "")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


# ── Player rankings (player/profile endpoint — fixtures carry no rank) ───────

_rank_cache: dict[tuple[str, int], int | None] = {}


def _fetch_player_rank(tour: str, player_id: int | None) -> int | None:
    """Current official tour ranking via the player profile endpoint.
    None when unknown/unranked. Cached per (tour, player) within a run."""
    if not player_id:
        return None
    key = (tour, player_id)
    if key not in _rank_cache:
        time.sleep(0.4)
        js = _tennis_get(f"/tennis/v2/{tour}/player/profile/{player_id}")
        info = js.get("data", js) if isinstance(js, dict) else None
        rank = info.get("currentRank") if isinstance(info, dict) else None
        try:
            rank = int(rank)
            _rank_cache[key] = rank if rank > 0 else None
        except (TypeError, ValueError):
            _rank_cache[key] = None
    return _rank_cache[key]


def _fixture_tier(r1: int | None, r2: int | None) -> str:
    """'Top {N}' when BOTH players are ranked inside TENNIS_RANK_THRESHOLD,
    else 'Lower Ranked' (an unranked/unknown player counts as lower)."""
    both_top = (
        r1 is not None and r1 <= TENNIS_RANK_THRESHOLD
        and r2 is not None and r2 <= TENNIS_RANK_THRESHOLD
    )
    return TENNIS_TOP_TIER_LABEL if both_top else TENNIS_LOWER_TIER_LABEL


def _tier_priority(tier: str) -> int:
    """Sort key for tournament importance — lower is better. Real tier strings
    from the API: 'Grand Slam', 'ATP 250', 'WTA 1000', 'Challenger 125',
    'Challenger 75', 'WTA 125', 'ITF Event', 'Future'."""
    t = (tier or "").strip().lower()
    if "grand slam" in t:
        return 0
    if "1000" in t or "masters" in t or "finals" in t:
        return 1
    if "500" in t:
        return 2
    if "250" in t:
        return 3
    if "125" in t:
        return 4
    if "challenger" in t:
        return 5
    if "itf" in t or "future" in t:
        return 7
    return 6  # unknown — above ITF, below anything recognised


def fetch_upcoming_tennis_matches() -> dict[str, list[dict]]:
    """
    Fetch upcoming ATP and WTA singles fixtures for the next 48 hours
    (today + tomorrow UTC). Returns {"ATP": [fixtures], "WTA": [fixtures]}.
    The full paginated slate is fetched, then capped tier-first (see below).
    """
    now = datetime.now(timezone.utc)
    result: dict[str, list[dict]] = {}

    for tour in TOURS:
        fixtures: list[dict] = []
        for offset in range(2):
            if offset > 0 or tour != TOURS[0]:
                time.sleep(1)
            date_str = (now + timedelta(days=offset)).strftime("%Y-%m-%d")
            # Paginated: the full day's slate, not just the first 10 fixtures
            day = _tennis_get_paged(f"/tennis/v2/{tour}/fixtures/{date_str}")
            log.info("Tennis API: fetched %d %s fixtures for %s", len(day), tour.upper(), date_str)
            fixtures.extend(day)

        upcoming = []
        for f in fixtures:
            if not _is_singles(f):
                continue
            if f.get("result") or f.get("live"):
                continue  # finished or in progress
            start = _parse_start(f)
            if start is None or start <= now:
                continue
            upcoming.append(f)

        # Tier-aware cap: the paginated slate is ~200+ fixtures/day, and a
        # plain soonest-first cap fills every slot with early-starting ITF
        # Futures matches while Grand Slam / tour-level fixtures drop out
        # (observed 11 Jul 2026: 20/25 slots went to M15/W15 Rancho Santa Fe
        # and Wimbledon vanished). Sort by tournament tier, then start time.
        for f in upcoming:
            info = _fetch_tournament_info(tour, f.get("tournamentId"))
            f["_tier_prio"] = _tier_priority(info.get("tier", ""))
        upcoming.sort(key=lambda f: (f["_tier_prio"], f.get("date", "")))
        if len(upcoming) > MAX_FIXTURES_PER_TOUR:
            log.info("Capping %s fixtures %d → %d (tier-first)",
                     tour.upper(), len(upcoming), MAX_FIXTURES_PER_TOUR)
            upcoming = upcoming[:MAX_FIXTURES_PER_TOUR]

        # Rankings live on the player profile endpoint, not in fixture data
        for f in upcoming:
            f["_p1_rank"] = _fetch_player_rank(tour, (f.get("player1") or {}).get("id"))
            f["_p2_rank"] = _fetch_player_rank(tour, (f.get("player2") or {}).get("id"))

        if upcoming:
            result[tour.upper()] = upcoming

    return result


# ── Tournament info (name / surface / tier) ──────────────────────────────────

_tournament_cache: dict[tuple[str, int], dict] = {}


def _fetch_tournament_info(tour: str, tournament_id: int | None) -> dict:
    """Return {"tournament", "surface", "tier"} for a tournamentId. Empty strings on failure."""
    empty = {"tournament": "", "surface": "", "tier": ""}
    if not tournament_id:
        return empty
    key = (tour, tournament_id)
    if key not in _tournament_cache:
        time.sleep(0.5)
        js = _tennis_get(f"/tennis/v2/{tour}/tournament/info/{tournament_id}")
        info = js.get("data", js) if isinstance(js, dict) else None
        if isinstance(info, dict):
            _tournament_cache[key] = {
                "tournament": info.get("name", "") or "",
                "surface":    ((info.get("court") or {}).get("name", "")) or "",
                "tier":       info.get("tier", "") or "",
            }
        else:
            _tournament_cache[key] = empty
    return _tournament_cache[key]


def build_tennis_fixture_summary(tour: str, fixture: dict) -> dict:
    p1 = fixture.get("player1") or {}
    p2 = fixture.get("player2") or {}
    summary = {
        "match_id":     fixture.get("id"),
        "player1":      p1.get("name", ""),
        "player2":      p2.get("name", ""),
        "start_utc":    fixture.get("date", ""),
        "player1_id":   p1.get("id"),
        "player2_id":   p2.get("id"),
        "player1_rank": fixture.get("_p1_rank"),
        "player2_rank": fixture.get("_p2_rank"),
    }
    summary.update(_fetch_tournament_info(tour, fixture.get("tournamentId")))
    return summary


# ── Player form & H2H enrichment ─────────────────────────────────────────────

def _fetch_player_recent(tour: str, player_id: int, n: int = 5) -> list[dict]:
    """Last n completed matches for a player (most recent first). Empty on error."""
    if not player_id:
        return []
    return _data_list(_tennis_get(f"/tennis/v2/{tour}/player/past-matches/{player_id}"))[:n]


def _fetch_tennis_h2h(tour: str, p1_id: int, p2_id: int, n: int = 5) -> list[dict]:
    """Last n completed H2H meetings. Empty on error."""
    if not p1_id or not p2_id:
        return []
    return _data_list(_tennis_get(f"/tennis/v2/{tour}/fixtures/h2h/{p1_id}/{p2_id}"))[:n]


def _summarize_tennis_match(match: dict, player_id: int | None = None) -> dict:
    """One completed match as compact context. In archive data player1 is the winner."""
    try:
        p1 = match.get("player1") or {}
        p2 = match.get("player2") or {}
        s: dict = {
            "match": f"{p1.get('name', '?')} def. {p2.get('name', '?')}",
            "score": match.get("result", "") or "?",
        }
        if player_id is not None:
            s["outcome"] = "W" if p1.get("id") == player_id else "L"
        return s
    except Exception:
        return {}


def _form_string(matches: list[dict], player_id: int) -> str:
    """Space-separated W/L string, newest → oldest (API returns most recent first)."""
    out = []
    for m in matches:
        p1 = m.get("player1") or {}
        out.append("W" if p1.get("id") == player_id else "L")
    return " ".join(out)


def enrich_tennis_context(fixtures_by_tour: dict[str, list[dict]]) -> None:
    """
    Mutates each fixture summary in-place with recent form and H2H context.
    All network calls are individually guarded — a failure leaves the fixture
    unchanged and the rest of the job continues. Enrichment stops after
    MAX_ENRICHED_FIXTURES fixtures to bound API usage.
    """
    player_cache: dict[tuple[str, int], list[dict]] = {}
    enriched = 0

    for tour_label, fixtures in fixtures_by_tour.items():
        tour = tour_label.lower()
        for fixture in fixtures:
            if enriched >= MAX_ENRICHED_FIXTURES:
                log.info("Enrichment cap (%d fixtures) reached — remaining fixtures sent bare",
                         MAX_ENRICHED_FIXTURES)
                return
            p1_id = fixture.get("player1_id")
            p2_id = fixture.get("player2_id")
            if not p1_id or not p2_id:
                continue

            try:
                if (tour, p1_id) not in player_cache:
                    time.sleep(0.6)
                    player_cache[(tour, p1_id)] = _fetch_player_recent(tour, p1_id)
                p1_matches = player_cache[(tour, p1_id)]

                if (tour, p2_id) not in player_cache:
                    time.sleep(0.6)
                    player_cache[(tour, p2_id)] = _fetch_player_recent(tour, p2_id)
                p2_matches = player_cache[(tour, p2_id)]

                time.sleep(0.6)
                h2h = _fetch_tennis_h2h(tour, p1_id, p2_id)

                fixture["player1_form"]   = _form_string(p1_matches, p1_id)
                fixture["player2_form"]   = _form_string(p2_matches, p2_id)
                fixture["player1_recent"] = [_summarize_tennis_match(m, p1_id) for m in p1_matches]
                fixture["player2_recent"] = [_summarize_tennis_match(m, p2_id) for m in p2_matches]
                fixture["h2h"]            = [_summarize_tennis_match(m) for m in h2h]

                enriched += 1
                log.info(
                    "Tennis context enriched: %s vs %s | p1=%s p2=%s h2h=%d",
                    fixture["player1"], fixture["player2"],
                    fixture["player1_form"] or "N/A",
                    fixture["player2_form"] or "N/A",
                    len(h2h),
                )
            except Exception as exc:
                log.debug("Tennis enrichment skipped for %s vs %s: %s",
                          fixture.get("player1"), fixture.get("player2"), exc)
                continue


# ── Real odds (The Odds API — dynamic tennis sport keys) ─────────────────────

# The Odds API lists each tennis tournament as its own sport key
# (e.g. tennis_atp_wimbledon, tennis_wta_us_open) that only exists while the
# tournament is in season, so keys are discovered at runtime instead of being
# hardcoded like the football league map.
MAX_TENNIS_ODDS_KEYS_PER_RUN = 6


def fetch_active_tennis_sport_keys() -> list[str]:
    """Active tennis sport keys from The Odds API. The /sports call is quota-free."""
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return []
    try:
        resp = requests.get(f"{ODDS_API_HOST}/sports", params={"apiKey": api_key}, timeout=10)
        resp.raise_for_status()
        return [
            s["key"] for s in resp.json()
            if s.get("group") == "Tennis" and s.get("active") and s.get("key")
        ]
    except Exception as exc:
        log.debug("fetch_active_tennis_sport_keys failed: %s", exc)
        return []


def fetch_tennis_odds_events(sport_key: str) -> list[dict] | None:
    """Raw events + bookmaker odds for one tennis sport key. None on failure."""
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
        log.debug("fetch_tennis_odds_events(%s) failed: %s", sport_key, exc)
        return None


def parse_tennis_odds_event(event: dict) -> dict:
    """Average bookmaker odds for one event into h2h / totals (games) / spreads (game handicap)."""
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


def _normalize_player(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9 .]", "", name.lower())
    return re.sub(r"\s+", " ", name).strip()


def player_match(a: str, b: str) -> bool:
    """
    Fuzzy-match player names across APIs that abbreviate differently
    ('C. Alcaraz' vs 'Carlos Alcaraz' vs 'Alcaraz Garfia Carlos').
    """
    na, nb = _normalize_player(a), _normalize_player(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.72:
        return True
    # Token containment handles reordered/extended names ('Alcaraz Garfia
    # Carlos' vs 'Carlos Alcaraz') and initials ('C. Alcaraz'): every token of
    # the shorter name must appear in the longer one; a single-letter token
    # matches any token sharing its first letter.
    ta = na.replace(".", " ").split()
    tb = nb.replace(".", " ").split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not short:
        return False

    def _tok_in(tok: str, toks: list[str]) -> bool:
        if len(tok) == 1:
            return any(t and t[0] == tok for t in toks)
        return tok in toks

    return all(_tok_in(t, long_) for t in short)


_TENNIS_OU_RE = re.compile(r"(over|under)\s*([\d.]+)", re.IGNORECASE)
_TENNIS_AH_RE = re.compile(r"^(.*?)\s*([+-]\d+(?:\.\d+)?)$")


def match_tennis_market_odds(pick: dict, real_odds: dict) -> float | None:
    """
    Match a Claude tennis pick to the corresponding market outcome.
    Set Betting has no market in The Odds API's basic markets → always None.
    """
    bet_type = (pick.get("bet_type") or "").lower()
    selection = (pick.get("pick") or "").strip()

    try:
        if "winner" in bet_type or "moneyline" in bet_type:
            player_part = re.sub(r"\s+(to\s+win|win)$", "", selection, flags=re.IGNORECASE).strip()
            for name, odds in real_odds.get("h2h", {}).items():
                if player_match(name, player_part):
                    return odds
            return None

        # Handicap must be checked before totals — 'Handicap (games)' also
        # contains the word 'games'.
        if "handicap" in bet_type or "spread" in bet_type:
            m = _TENNIS_AH_RE.match(selection)
            if not m:
                return None
            player_part, line = m.group(1).strip(), float(m.group(2))
            for row in real_odds.get("spreads", []):
                if abs(row.get("point", -999) - line) < 0.01:
                    for name, odds in row.items():
                        if name != "point" and player_match(name, player_part):
                            return odds
            return None

        if "total" in bet_type or "over" in bet_type or "under" in bet_type or "games" in bet_type:
            m = _TENNIS_OU_RE.search(selection) or _TENNIS_OU_RE.search(bet_type)
            if not m:
                return None
            side, line = m.group(1).capitalize(), float(m.group(2))
            for row in real_odds.get("totals", []):
                if abs(row.get("point", -1) - line) < 0.01:
                    return row.get(side)
            return None
    except Exception as exc:
        log.debug("match_tennis_market_odds failed for pick %s: %s", pick.get("match"), exc)
        return None

    return None


def _implied_prob(odds: float) -> float:
    return 1.0 / odds if odds else 0.0


def enrich_tennis_picks_with_real_odds(picks: list[dict]) -> None:
    """
    Mutates each pick in-place with 'market_odds', 'market_prob', and 'value'
    by comparing Claude's implied probability against real market odds. A pick
    is flagged as value only when Claude's implied probability exceeds the
    market's by at least 5 percentage points. Any failure leaves that pick
    unchanged.
    """
    if not picks:
        return
    sport_keys = fetch_active_tennis_sport_keys()[:MAX_TENNIS_ODDS_KEYS_PER_RUN]
    if not sport_keys:
        log.info("No active tennis sport keys on The Odds API — Claude odds only")
        return

    events: list[dict] = []
    for key in sport_keys:
        batch = fetch_tennis_odds_events(key)
        if batch:
            events.extend(batch)

    for pick in picks:
        try:
            match = pick.get("match", "")
            if " vs " not in match:
                continue
            p1, p2 = [s.strip() for s in match.split(" vs ", 1)]

            event = next(
                (
                    e for e in events
                    if (player_match(e.get("home_team", ""), p1) and player_match(e.get("away_team", ""), p2))
                    or (player_match(e.get("home_team", ""), p2) and player_match(e.get("away_team", ""), p1))
                ),
                None,
            )
            if event is None:
                continue

            market_odds = match_tennis_market_odds(pick, parse_tennis_odds_event(event))
            if market_odds is None:
                continue

            pick["market_odds"] = market_odds
            claude_prob = _implied_prob(float(pick["odds"]))
            market_prob = _implied_prob(market_odds)
            pick["market_prob"] = round(market_prob * 100, 1)
            pick["value"] = (claude_prob - market_prob) >= 0.05
        except Exception as exc:
            log.debug("enrich_tennis_picks_with_real_odds skipped a pick: %s", exc)
            continue


# ── Claude analysis ───────────────────────────────────────────────────────────

TENNIS_SYSTEM_PROMPT = """You are a professional tennis betting analyst with deep expertise in ATP and WTA
tour-level tennis. You receive upcoming singles fixtures for the next 48 hours and must identify the
top 5 value bets across both tours.

Each fixture may include the following enriched context — use it to sharpen your analysis:
- tournament / surface / tier: the event, its court surface (Hard, Clay, Grass) and level
  (Grand Slam, Masters, ATP 500/250, WTA 1000/500/250).
- player1_rank / player2_rank: the player's CURRENT official tour ranking (null when
  unranked/unknown). This is live data — trust it over any ranking you remember.
- player1_form / player2_form: last 5 results for each player as W/L (newest → oldest).
- player1_recent / player2_recent: score details for those matches ('X def. Y', set scores).
- h2h: previous head-to-head meetings between the two players with scores. In this archive
  data the first-listed player is always the WINNER of that meeting.
When this data is present, weight recent form, head-to-head record, and surface suitability
heavily in your reasoning. Surface is critical in tennis: clay-court specialists, grass-court
servers, and hard-court baseliners can produce very different results against the same opponent.

Your knowledge of player rankings, injuries, retirements, and coaching changes may be outdated —
tennis rosters and form shift week to week. Do NOT cite a player's current ranking, injury status,
or very recent results unless supported by the context provided. Reason from what the data shows:
form strings, H2H record, surface, and tournament level — plus durable traits you are confident of
(serve strength, preferred surface, big-match experience).

Supported bet types (use ONLY these):
- Match Winner — which player wins the match
- Total Games — over/under on total games in the match, e.g. 'Over 22.5 Games'
- Set Betting — exact set score, e.g. '2-0', '2-1' (best of 3) or '3-1' (Grand Slam men)
- Handicap (games) — game handicap, e.g. 'Alcaraz -4.5'

Since live odds are not provided, use your knowledge of typical tennis market pricing to estimate
realistic decimal odds (e.g. a heavy favourite ~1.20, moderate favourite ~1.55, toss-up ~1.90 each
side, set-betting favourites 2-0 ~2.10).

The "probability" field is your honest estimate of how likely the pick is to win (0-100). It should
reflect your true belief, not simply 100/odds — a value bet is precisely one where your probability
is higher than the odds imply. Your stated probabilities are tracked and scored for calibration over
time, so be realistic: a pick you'd expect to win 6 times out of 10 is 60, not 75.

For each recommendation output valid JSON with this exact structure:
{
  "picks": [
    {
      "match": "<Player 1 name> vs <Player 2 name>",
      "tour": "<ATP or WTA>",
      "tournament": "<tournament name>",
      "surface": "<Hard / Clay / Grass>",
      "bet_type": "<Match Winner / Total Games / Set Betting / Handicap (games)>",
      "pick": "<selection using actual player names — e.g. 'Sinner Win', 'Over 21.5 Games', 'Swiatek 2-0', 'Alcaraz -4.5'>",
      "odds": <estimated decimal odds as a number>,
      "probability": <your estimated true probability of this pick winning, as a number from 0 to 100>,
      "confidence": "<High / Medium / Low>",
      "reasoning": "<2-3 sentence rationale covering form, H2H, surface, and value>"
    }
  ]
}

IMPORTANT — pick field naming rules:
- The "match" field must use the exact player names as given in the fixture data.
- NEVER use 'Player 1 Win' or 'Favourite Win' — always the actual player name, e.g. 'Rybakina Win'
- Total Games format: 'Over 22.5 Games' / 'Under 20.5 Games'
- Set Betting format: '<Player> 2-0', '<Player> 2-1', '<Player> 3-0' etc.
- Handicap format: '<Player> -3.5' or '<Player> +4.5'

Return ONLY the JSON block, no other text."""


def _strip_code_fences(text: str) -> str:
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _notify_tennis_picks_failed(reason: str) -> None:
    # Tennis is Discord-only — the alert goes to the picks channel, never Telegram
    if not send_to_discord(
        "tennis-picks", message=f"⚠️ Tennis picks failed today — {reason}. Check logs."
    ):
        log.error("Could not deliver tennis picks-failed alert to Discord ('tennis-picks')")


def analyse_tennis_with_claude(fixtures_by_tour: dict[str, list[dict]]) -> list[dict]:
    # Strip internal IDs — not useful to Claude
    _STRIP = {"player1_id", "player2_id", "match_id"}
    clean = {
        tour: [{k: v for k, v in f.items() if k not in _STRIP} for f in fixtures]
        for tour, fixtures in fixtures_by_tour.items()
    }
    payload = json.dumps(clean, indent=2, default=str)
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        temperature=0,
        system=TENNIS_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Upcoming tennis fixtures (next 48 hours):\n\n{payload}"}],
    )
    raw = message.content[0].text.strip()
    log.info("Claude raw tennis response (%d chars):\n%s", len(raw), raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError as exc:
            log.error("Claude tennis response is not valid JSON. Full raw response:\n%s", raw)
            _notify_tennis_picks_failed("Claude returned an unparseable response")
            raise ValueError(f"Could not parse Claude tennis response as JSON: {exc}") from exc

    picks = data.get("picks", [])
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for pick in picks:
        key = (pick.get("match"), pick.get("bet_type"))
        if key not in seen:
            seen.add(key)
            deduped.append(pick)
    return deduped


# ── Discord (tennis is Discord-only — no Telegram) ────────────────────────────

def _discord_tennis_pick_embed(p: dict) -> dict:
    """One tennis pick as a Discord embed; tour/tournament/surface plus the
    players' rankings ('#54 vs #88') render as the author line."""
    context = " | ".join(str(p[k]) for k in ("tour", "tournament", "surface", "ranks") if p.get(k))
    return build_pick_embed(p, context=context)


def post_tennis_picks_to_discord(picks: list[dict]) -> int:
    """Post each pick as an embed to its rank-tier channel: 'tennis-picks'
    when both players rank inside TENNIS_RANK_THRESHOLD, 'tennis-picks-lower'
    otherwise. Every pick goes to exactly one channel. A dated header (text)
    precedes the picks in each channel that receives any. Returns how many
    pick embeds Discord accepted (send_to_discord never raises)."""
    today = datetime.now(timezone.utc).strftime("%d %b %Y")

    by_channel: dict[str, list[dict]] = {}
    for p in picks:
        key = (
            "tennis-picks"
            if p.get("rank_tier") == TENNIS_TOP_TIER_LABEL
            else "tennis-picks-lower"
        )
        by_channel.setdefault(key, []).append(p)
    log.info(
        "Tennis picks by rank tier: %s",
        ", ".join(f"{k}={len(v)}" for k, v in by_channel.items()),
    )

    sent = 0
    for key, channel_picks in by_channel.items():
        send_to_discord(key, message=f"🎾 **Tennis Picks — {today}**")
        for p in channel_picks:
            sent += send_to_discord(key, embed=_discord_tennis_pick_embed(p))
    return sent


# ── Main job ──────────────────────────────────────────────────────────────────

def _start_lookup(fixtures_by_tour: dict[str, list[dict]]) -> dict[str, str]:
    """Map '<Player 1> vs <Player 2>' -> start_utc for the tennis closing-odds job."""
    lookup: dict[str, str] = {}
    for fixtures in fixtures_by_tour.values():
        for f in fixtures:
            lookup[f"{f['player1']} vs {f['player2']}"] = f.get("start_utc", "")
    return lookup


def _rank_lookup(fixtures_by_tour: dict[str, list[dict]]) -> dict[str, str]:
    """Map '<Player 1> vs <Player 2>' -> '#54 vs #88' for the pick embeds.
    'NR' marks an unranked/unknown player; matches with no rank at all are omitted."""
    lookup: dict[str, str] = {}
    for fixtures in fixtures_by_tour.values():
        for f in fixtures:
            r1, r2 = f.get("player1_rank"), f.get("player2_rank")
            if r1 is None and r2 is None:
                continue
            fmt = lambda r: f"#{r}" if r is not None else "NR"
            lookup[f"{f['player1']} vs {f['player2']}"] = f"{fmt(r1)} vs {fmt(r2)}"
    return lookup


def _tier_lookup(fixtures_by_tour: dict[str, list[dict]]) -> dict[str, str]:
    """Map '<Player 1> vs <Player 2>' -> rank tier label for channel routing
    and the Sheet's 'Rank Tier' column."""
    lookup: dict[str, str] = {}
    for fixtures in fixtures_by_tour.values():
        for f in fixtures:
            lookup[f"{f['player1']} vs {f['player2']}"] = _fixture_tier(
                f.get("player1_rank"), f.get("player2_rank")
            )
    return lookup


def _ids_lookup(fixtures_by_tour: dict[str, list[dict]]) -> dict[str, str]:
    """Map '<Player 1> vs <Player 2>' -> 'tour:p1Id|p2Id' for the Sheet's
    'Player IDs' column. Auto-results uses it to settle each pick with a
    single past-matches call instead of re-scanning the day's full slate."""
    lookup: dict[str, str] = {}
    for tour_label, fixtures in fixtures_by_tour.items():
        tour = tour_label.lower()
        for f in fixtures:
            p1_id, p2_id = f.get("player1_id"), f.get("player2_id")
            if p1_id and p2_id:
                lookup[f"{f['player1']} vs {f['player2']}"] = f"{tour}:{p1_id}|{p2_id}"
    return lookup


async def daily_tennis_picks_job():
    log.info("Starting daily tennis picks job")

    if tennis_picks_exist_for_today():
        log.info("Tennis picks already logged for today — skipping")
        return

    try:
        raw_by_tour = fetch_upcoming_tennis_matches()
    except Exception as exc:
        log.error("Failed to fetch tennis fixtures: %s", exc)
        return

    if not raw_by_tour:
        log.info("No upcoming ATP/WTA fixtures in the next 48 hours — skipping analysis")
        return

    fixtures_by_tour: dict[str, list[dict]] = {}
    for tour_label, fixtures in raw_by_tour.items():
        tour = tour_label.lower()
        fixtures_by_tour[tour_label] = [build_tennis_fixture_summary(tour, f) for f in fixtures]
        log.info("  %s: %d upcoming fixtures", tour_label, len(fixtures))

    try:
        enrich_tennis_context(fixtures_by_tour)
    except Exception as exc:
        log.warning("Tennis context enrichment failed — proceeding without form/H2H: %s", exc)

    try:
        picks = analyse_tennis_with_claude(fixtures_by_tour)
    except Exception as exc:
        log.error("Claude tennis analysis failed: %s", exc)
        return

    if not picks:
        log.info("Claude returned no tennis picks today")
        return

    try:
        start_lookup = _start_lookup(fixtures_by_tour)
    except Exception as exc:
        log.warning("Tennis start-time lookup build failed (non-fatal): %s", exc)
        start_lookup = {}

    try:
        rank_lookup = _rank_lookup(fixtures_by_tour)
        tier_lookup = _tier_lookup(fixtures_by_tour)
        ids_lookup  = _ids_lookup(fixtures_by_tour)
        for pick in picks:
            match = pick.get("match", "")
            pick["ranks"] = rank_lookup.get(match, "")
            # Unmatched picks default to the lower tier — never dropped
            pick["rank_tier"]  = tier_lookup.get(match, TENNIS_LOWER_TIER_LABEL)
            pick["player_ids"] = ids_lookup.get(match, "")
    except Exception as exc:
        log.warning("Tennis rank/tier lookup build failed (non-fatal): %s", exc)

    try:
        enrich_tennis_picks_with_real_odds(picks)
    except Exception as exc:
        log.warning("Tennis real odds enrichment failed — Claude odds only: %s", exc)

    # SIMULATED Kelly staking — same half-Kelly/5%-cap logic as football, but
    # sized against the independent €100 TENNIS_REAL_BANKROLL and tagged SIM
    # everywhere (no real money on tennis until the pipeline is trusted)
    try:
        for pick in picks:
            kelly = calculate_tennis_kelly_stake(
                pick["bet_type"], float(pick["odds"]), pick.get("confidence", "")
            )
            pick["kelly"] = kelly
            stake = float(kelly.get("stake") or 0)
            pick["stake_display"] = (
                f"€{stake:.2f} · SIM" if stake > 0 else "€0 — negative edge · SIM"
            )
    except Exception as exc:
        log.warning("Tennis Kelly stake calculation failed (picks send without it): %s", exc)

    for pick in picks:
        try:
            claude_prob = pick.get("probability")
            kelly = pick.get("kelly") or {}
            log_tennis_pick(
                match=pick["match"],
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
                claude_prob=float(claude_prob) if claude_prob is not None else None,
                market_prob=pick.get("market_prob"),
                start_time_utc=start_lookup.get(pick["match"], ""),
                rank_tier=pick.get("rank_tier", TENNIS_LOWER_TIER_LABEL),
                stake_eur=kelly.get("stake"),
                player_ids=pick.get("player_ids") or None,
            )
        except Exception as exc:
            log.warning("Failed to log tennis pick: %s", exc)

    sent = await asyncio.to_thread(post_tennis_picks_to_discord, picks)
    if sent:
        log.info("Posted %d/%d tennis picks to Discord 'tennis-picks'", sent, len(picks))
    else:
        log.error(
            "No tennis picks reached Discord — check DISCORD_BOT_TOKEN / "
            "DISCORD_CHANNELS_JSON ('tennis-picks' key)"
        )

    # Branded PNG card with all of today's picks (both tiers) — additive to
    # the per-pick embeds above; send_to_discord never raises
    try:
        card = generate_tennis_picks_card(picks)
        log.info("Tennis picks card saved: %s", card.name)
        send_to_discord("tennis-picks", image_path=card)
    except Exception as exc:
        log.warning("Tennis picks card failed (non-fatal): %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_tennis_picks_job, "cron", hour=12, minute=30, timezone="Europe/Brussels")
    scheduler.start()
    log.info("Tennis scheduler started — picks will post daily at 12:30 Europe/Brussels")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    if "--now" in sys.argv:
        asyncio.run(daily_tennis_picks_job())
    else:
        asyncio.run(main())
