import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import anthropic
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot

from tracker import log_pick, picks_exist_for_session
from excel_tracker import calculate_kelly_stake, get_overall_win_rate
from card_generator import generate_picks_card

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID")

# Single-ID domestic leagues (fotmob-based IDs)
LEAGUES = {
    "Premier League": 47,
    "Jupiler Pro League": 900433,
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

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── API helpers ───────────────────────────────────────────────────────────────

def _kickoff_hour_utc(match: dict) -> int | None:
    """Return the kickoff hour (UTC, 0-23) or None if unparseable."""
    s = match.get("status", {})
    ko_str = s.get("utcTime", match.get("time", ""))
    if not ko_str:
        return None
    try:
        ko = datetime.fromisoformat(ko_str.replace("Z", "+00:00"))
        return ko.hour
    except (ValueError, TypeError):
        return None


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
        "match_id": match["id"],
        "home": match["home"]["longName"],
        "away": match["away"]["longName"],
        "kickoff_utc": status.get("utcTime", match.get("time", "")),
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
            result["FIFA World Cup 2026"] = [build_fixture_summary(m) for m in wc]

    return result


# ── Claude analysis ───────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional football betting analyst with deep expertise in the Premier League,
Belgian Jupiler Pro League, and international tournament football including the FIFA World Cup.
You receive upcoming fixtures for the next 48 hours and must identify the top 5 value bets across all competitions.

Since live odds are not provided, use your knowledge of typical market pricing to estimate realistic
decimal odds (e.g. a heavy favourite ~1.35, slight favourite ~1.75, toss-up ~2.00 each side).

For each recommendation output valid JSON with this exact structure:
{
  "picks": [
    {
      "match": "<Home longName> vs <Away longName>",
      "league": "<league name>",
      "bet_type": "<e.g. Match Winner / Both Teams to Score / Over 2.5 Goals / Double Chance / Asian Handicap>",
      "pick": "<selection using actual team names — never 'Home Win' or 'Away Win'. E.g. 'Sweden Win', 'Ivory Coast or Draw', 'Yes', 'Over 2.5 Goals', 'Argentina -1.5'>",
      "odds": <estimated decimal odds as a number>,
      "confidence": "<High / Medium / Low>",
      "reasoning": "<2-3 sentence rationale covering form, head-to-head, and value>"
    }
  ]
}

IMPORTANT — pick field naming rules:
- NEVER use "Home Win" or "Away Win" — always use the actual team name, e.g. "Sweden Win", "Morocco Win"
- NEVER use "Home or Draw" or "Away or Draw" — use e.g. "Ivory Coast or Draw", "Japan or Draw"
- For Over/Under, BTTS, and Asian Handicap keep the standard format: "Over 2.5 Goals", "Yes", "No", "Argentina -1.5"

Return ONLY the JSON block, no other text."""


def analyse_with_claude(fixtures_by_league: dict[str, list[dict]]) -> list[dict]:
    payload = json.dumps(fixtures_by_league, indent=2, default=str)
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Upcoming fixtures (next 48 hours):\n\n{payload}"}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())
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
        if kelly is not None:
            note_suffix = f" — {_escape_md(kelly['note'])}" if kelly.get("note") else ""
            kelly_line = (
                f"  💰 Suggested stake: €{_escape_md(f'{kelly[\"stake\"]:.2f}')} \\(Kelly{note_suffix}\\)\n"
            )
        else:
            kelly_line = ""
        lines.append(
            f"*{i}\\. {_escape_md(p['match'])}* \\({_escape_md(p['league'])}\\)\n"
            f"  Bet: {_escape_md(p['bet_type'])} — *{_escape_md(p['pick'])}*\n"
            f"  Odds: `{_escape_md(str(p['odds']))}` \\| Confidence: {_escape_md(p['confidence'])}\n"
            f"  _{_escape_md(p['reasoning'])}_\n"
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


async def _send_photo(path) -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    with open(path, "rb") as f:
        await bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=f)


# ── Main job ──────────────────────────────────────────────────────────────────

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
        picks = analyse_with_claude(fixtures_by_league)
    except Exception as exc:
        log.error("Claude analysis failed: %s", exc)
        return

    try:
        for pick in picks:
            pick["kelly"] = calculate_kelly_stake(
                pick["bet_type"], float(pick["odds"]), pick.get("confidence", "")
            )
    except Exception as exc:
        log.warning("Kelly stake calculation failed (picks will send without it): %s", exc)

    for pick in picks:
        try:
            log_pick(
                match=pick["match"],
                league=pick["league"],
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
                session="morning",
            )
        except Exception as exc:
            log.warning("Failed to log pick: %s", exc)

    message = format_telegram_message(picks, header="Football Picks")
    try:
        await send_to_telegram(message)
        log.info("Sent %d morning picks to Telegram", len(picks))
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)

    try:
        wr   = get_overall_win_rate()
        card = generate_picks_card(picks, overall_win_rate=wr, session="morning")
        await _send_photo(card)
        log.info("Picks card sent: %s", card.name)
    except Exception as exc:
        log.warning("Picks card failed (non-fatal): %s", exc)


async def evening_picks_job():
    log.info("Starting evening picks job")

    if picks_exist_for_session("evening"):
        log.info("Evening picks already logged for today — skipping")
        return

    try:
        all_matches = fetch_upcoming_matches()
        log.info("Fetched %d total matches for next 48 hours", len(all_matches))
    except Exception as exc:
        log.error("Failed to fetch matches for evening run: %s", exc)
        return

    # Only fixtures kicking off at 18:00 UTC or later
    evening_matches = [m for m in all_matches if (_kickoff_hour_utc(m) or 0) >= 18]
    log.info("Evening filter: %d matches with kickoff >= 18:00 UTC", len(evening_matches))

    fixtures_by_league = partition_fixtures(evening_matches)

    if not fixtures_by_league:
        log.info("No evening fixtures across tracked competitions — skipping analysis")
        return

    for league, fixtures in fixtures_by_league.items():
        log.info("  %s: %d evening fixtures", league, len(fixtures))

    try:
        picks = analyse_with_claude(fixtures_by_league)
    except Exception as exc:
        log.error("Claude analysis failed (evening): %s", exc)
        return

    try:
        for pick in picks:
            pick["kelly"] = calculate_kelly_stake(
                pick["bet_type"], float(pick["odds"]), pick.get("confidence", "")
            )
    except Exception as exc:
        log.warning("Kelly stake calculation failed (picks will send without it): %s", exc)

    for pick in picks:
        try:
            log_pick(
                match=pick["match"],
                league=pick["league"],
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
                session="evening",
            )
        except Exception as exc:
            log.warning("Failed to log evening pick: %s", exc)

    message = format_telegram_message(picks, header="Evening Picks")
    try:
        await send_to_telegram(message)
        log.info("Sent %d evening picks to Telegram", len(picks))
    except Exception as exc:
        log.error("Telegram send failed (evening): %s", exc)

    try:
        wr   = get_overall_win_rate()
        card = generate_picks_card(picks, overall_win_rate=wr, session="evening")
        await _send_photo(card)
        log.info("Evening picks card sent: %s", card.name)
    except Exception as exc:
        log.warning("Evening picks card failed (non-fatal): %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_picks_job, "cron", hour=9, minute=0, timezone="Europe/Brussels")
    scheduler.start()
    log.info("Scheduler started — picks will post daily at 09:00 Europe/Brussels")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
