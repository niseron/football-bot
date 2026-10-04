"""
opus_stats.py — pre-match team stat averages for the Opus shadow ONLY (4 Oct 2026).

For every fixture in the pool, each side's per-match averages over its last
STATS_MATCHES finished matches (all competitions, home and away): corners,
cards and shots on target, each FOR and AGAINST. Nothing here is read by the
production Sonnet path — main.py is untouched and only opus_shadow imports this.

90 MINUTES ONLY, the same figure a stats pick settles on:
  * A match where extra time was played (auto_results.extra_time_played) uses
    first-half + second-half stats, which exclude extra time; no half split
    means the match has no 90-minute figure and does not count.
  * Cards use the settlement rule: yellow + red + one per second-yellow lineup
    event (the feed books a second yellow as one red and zero yellows), so a
    second-yellow dismissal counts 2. A red card with unreadable lineups, or a
    second yellow when a dismissal came in extra time, leaves the match out.
  * Every figure is read through auto_results.fetch_match_stats — the same
    endpoints and parser settlement uses, so the two cannot drift apart.

A match counts only when all three stats are complete. A team with fewer than
STATS_MIN_FULL such matches gets a sentence saying so instead of an average —
a 3-match average is noise presented as a number.

COST (measured 19 Sep 2026 on a 52-fixture / 104-team slate at 5 matches per
team: 316 stats calls cold, 1 new call over the following four days thanks to
the match-id cache). At 10 matches per team the cold figure roughly doubles,
plus up to STATS_LOOKBACK_DAYS by-date calls shared with main's form cache and
two lineup calls per match with a red card. Calls are paced 2 s apart by
auto_results._rapid_get, so a cold run takes ~20-30 min — it runs after every
production surface has delivered. The cache is keyed by match id (a past
match's 90-minute stats never change), held in memory and mirrored to
STATS_CACHE_PATH so a process restart within a deploy does not pay again.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATS_MATCHES = 10          # matches per team averaged
STATS_MIN_FULL = 5          # below this many complete matches: no average, say so
# How far back the by-date feed is walked to find STATS_MATCHES per team. Stops
# early once every team has them; national teams play rarely and will often
# end the walk short — they then get the insufficient-data sentence.
STATS_LOOKBACK_DAYS = 120

STATS_CACHE_PATH = Path(__file__).with_name("opus_stats_cache.json")

KINDS = ("corners", "cards", "shots_on_target")
_FEED_KEYS = ("corners", "yellow_cards", "red_cards", "ShotsOnTarget")

# match_id -> {"corners": [h, a], "cards": [h, a], "shots_on_target": [h, a]}
# for a complete match, or {"incomplete": reason}. A failed fetch is never
# cached, so the next run retries it.
_match_cache: dict[int, dict] = {}
_cache_loaded = False


# ── Cache ────────────────────────────────────────────────────────────────────

def _load_cache() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        raw = json.loads(STATS_CACHE_PATH.read_text(encoding="utf-8"))
        _match_cache.update({int(k): v for k, v in raw.items()})
        log.info("opus_stats: loaded %d cached match(es)", len(raw))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("opus_stats: cache file unreadable, starting empty: %s", exc)


def _save_cache() -> None:
    try:
        STATS_CACHE_PATH.write_text(json.dumps(_match_cache), encoding="utf-8")
    except Exception as exc:
        log.warning("opus_stats: cache file not written (non-fatal): %s", exc)


# ── One match's 90-minute figures ────────────────────────────────────────────

def ninety_minute_figures(match_stats: dict, extra_time: bool) -> dict:
    """
    fetch_match_stats' payload -> {"corners": [h, a], "cards": [h, a],
    "shots_on_target": [h, a]}, or {"incomplete": reason}. Mirrors
    auto_results._stat_market_verdict's reading exactly.
    """
    full = match_stats.get("full") or {}
    if extra_time:
        h1, h2 = match_stats.get("h1") or {}, match_stats.get("h2") or {}
        missing = [k for k in _FEED_KEYS if k not in h1 or k not in h2]
        if missing:
            return {"incomplete": "extra time without a half split for " + ", ".join(missing)}
        vals = {k: [h1[k][0] + h2[k][0], h1[k][1] + h2[k][1]] for k in _FEED_KEYS}
    else:
        missing = [k for k in _FEED_KEYS if k not in full]
        if missing:
            return {"incomplete": "no " + ", ".join(missing)}
        vals = {k: full[k] for k in _FEED_KEYS}

    reds, full_reds = vals["red_cards"], full.get("red_cards")
    second = match_stats.get("second_yellows")
    if (sum(reds) > 0 or sum(full_reds or [0]) > 0) and second is None:
        return {"incomplete": "red card with unreadable lineups"}
    second = second or [0, 0]
    if extra_time and full_reds != reds and sum(second) > 0:
        return {"incomplete": "dismissal in extra time with a second yellow"}

    return {
        "corners": list(vals["corners"]),
        "cards": [vals["yellow_cards"][i] + reds[i] + second[i] for i in (0, 1)],
        "shots_on_target": list(vals["ShotsOnTarget"]),
    }


def _stats_final(status: dict, extra_time: bool) -> bool:
    """Same settle delay settlement uses: stats keep changing after full time."""
    from auto_results import STATS_SETTLE_AFTER_KICKOFF_MIN, STATS_SETTLE_EXTRA_TIME_MIN
    wait = STATS_SETTLE_AFTER_KICKOFF_MIN + (STATS_SETTLE_EXTRA_TIME_MIN if extra_time else 0)
    try:
        ko = datetime.fromisoformat((status.get("utcTime") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) >= ko + timedelta(minutes=wait)


def _match_figures(match: dict, counters: dict) -> dict | None:
    """Cached 90-minute figures for one finished feed match; None = fetch failed."""
    mid = match.get("id")
    if mid in _match_cache:
        counters["hits"] += 1
        return _match_cache[mid]
    from auto_results import extra_time_played, fetch_match_stats

    status = match.get("status") or {}
    et = extra_time_played(status)
    if not _stats_final(status, et):
        return None
    raw = fetch_match_stats(mid, halves=et, dismissals=True)
    counters["fetched"] += 1
    if raw.get("full") is None or (et and (raw.get("h1") is None or raw.get("h2") is None)):
        counters["failed"] += 1
        return None
    figures = ninety_minute_figures(raw, et)
    _match_cache[mid] = figures
    return figures


# ── Per-team averages ────────────────────────────────────────────────────────

def _usable(match: dict) -> bool:
    status = match.get("status") or {}
    return bool(status.get("finished")) and not status.get("cancelled") and not status.get("awarded")


def _recent_matches(team_ids: set[int], today: date) -> dict[int, list[dict]]:
    """Last STATS_MATCHES finished matches per team, newest first, via main's day cache."""
    from main import _fetch_day_matches

    found: dict[int, list[dict]] = {tid: [] for tid in team_ids}
    seen: dict[int, set] = {tid: set() for tid in team_ids}
    for back in range(1, STATS_LOOKBACK_DAYS + 1):
        if all(len(v) >= STATS_MATCHES for v in found.values()):
            break
        for m in _fetch_day_matches(today - timedelta(days=back)):
            if not _usable(m):
                continue
            for side in ("home", "away"):
                tid = (m.get(side) or {}).get("id")
                if tid in found and len(found[tid]) < STATS_MATCHES and m.get("id") not in seen[tid]:
                    found[tid].append(m)
                    seen[tid].add(m.get("id"))
    return found


def team_summary(team_id: int, matches: list[dict], figures: dict[int, dict | None]):
    """
    The value Opus sees for one side: a dict of averages, or a sentence when
    fewer than STATS_MIN_FULL of the team's last matches have complete stats.
    """
    complete = []
    for m in matches:
        fig = figures.get(m.get("id"))
        if not fig or "incomplete" in fig:
            continue
        side = 0 if (m.get("home") or {}).get("id") == team_id else 1
        complete.append((fig, side))

    n, found = len(complete), len(matches)
    if n < STATS_MIN_FULL:
        return (f"INSUFFICIENT DATA: only {n} of this team's last {found} match(es) found "
                f"have full 90-minute corners/cards/shots-on-target stats (minimum "
                f"{STATS_MIN_FULL}) — no average given")
    avg: dict[str, float] = {}
    for kind in KINDS:
        avg[f"{kind}_for"] = round(sum(f[kind][s] for f, s in complete) / n, 1)
        avg[f"{kind}_against"] = round(sum(f[kind][1 - s] for f, s in complete) / n, 1)
    return {"matches_averaged": n, "of_last": found, "per_match_90min": avg}


def build_stat_averages(fixtures_by_league: dict[str, list[dict]],
                        today: date | None = None) -> dict:
    """
    {match_id: {"home_stats_last10": ..., "away_stats_last10": ...}} for every
    fixture in the pool. Never raises for a single team or match — a failure
    there becomes that side's insufficient-data sentence.
    """
    _load_cache()
    fixtures = [f for fx in fixtures_by_league.values() for f in fx]
    team_ids = {t for f in fixtures for t in (f.get("home_id"), f.get("away_id")) if t}
    recent = _recent_matches(team_ids, today or date.today())

    counters = {"hits": 0, "fetched": 0, "failed": 0}
    figures: dict[int, dict | None] = {}
    for matches in recent.values():
        for m in matches:
            mid = m.get("id")
            if mid is not None and mid not in figures:
                figures[mid] = _match_figures(m, counters)
    _save_cache()

    out: dict = {}
    sufficient = 0
    for f in fixtures:
        entry = {}
        for side in ("home", "away"):
            tid = f.get(f"{side}_id")
            if not tid:
                entry[f"{side}_stats_last10"] = "UNAVAILABLE: no team id for this side"
                continue
            summary = team_summary(tid, recent.get(tid, []), figures)
            sufficient += isinstance(summary, dict)
            entry[f"{side}_stats_last10"] = summary
        out[f.get("match_id")] = entry
    log.info(
        "opus_stats: %d fixture(s), %d team(s); %d match stat(s) fetched (%d failed), "
        "%d from cache; %d of %d side(s) have an average",
        len(fixtures), len(team_ids), counters["fetched"], counters["failed"],
        counters["hits"], sufficient, 2 * len(fixtures),
    )
    return out


def unavailable_for_all(fixtures_by_league: dict[str, list[dict]], reason: str) -> dict:
    """Every fixture marked unavailable — used when the whole build failed."""
    msg = f"UNAVAILABLE: {reason}"
    return {f.get("match_id"): {"home_stats_last10": msg, "away_stats_last10": msg}
            for fx in fixtures_by_league.values() for f in fx}
