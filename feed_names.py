"""
Team names from the RapidAPI football feed — read defensively, in ONE place.

On 3 Nov 2026, between 13:06 and 13:45 UTC, the by-date feed
(`football-get-matches-by-date`) stopped sending `home.longName` /
`away.longName` on EVERY match, past and future dates alike. The full name the
bot had always used moved to `name`, and an optional `shortName` appeared:

    before:  "home": {"id": 8315, "name": ..., "longName": "Athletic Club", "score": 2}
    after:   "home": {"id": 8315, "name": "Athletic Club", "score": 2}
             "home": {"id": 130394, "name": "Seattle Sounders FC", "shortName": "Seattle Sounders", ...}

Measured 4 Nov 2026 against the 'Opus Referee Cards' tab, which stored the old
longName per match id: 118 of 118 sides across 29-31 Dec 2025 and
31 Oct-2 Nov 2026 carry the SAME string under the new `name`. So every
exact-name table (UEFA_NATIONS, WC_2026_PARTICIPANTS, _TEAM_ALIASES) and every
match string already in the Picks tab keeps lining up.

Seven direct `["longName"]` reads crashed or silently degraded: the 4 Nov
picks run died in `build_fixture_summary` before any analysis, every
settlement poller failed from 13:45 UTC on 3 Nov, form rows went empty, and the
Nations League discovery ranking lost its name roster.

Order is `longName` (if the provider ever restores it), then `name`, then
`shortName`. A side carrying none of them is unusable: the CALLER skips that one
match and logs it — one nameless match must never stop a whole run.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

NAME_FIELDS = ("longName", "name", "shortName")

# Match ids already reported as nameless, so a poller running every 30 minutes
# over a 300-match pool logs each bad match once per process, not per pick per
# cycle.
_reported: set = set()


def team_name(side) -> str | None:
    """The first non-empty name field of one side (`match["home"]` / `["away"]`), else None."""
    if not isinstance(side, dict):
        return None
    for field in NAME_FIELDS:
        value = side.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def match_names(match: dict) -> tuple[str, str] | None:
    """(home, away) for a feed match, or None when either side has no usable name."""
    if not isinstance(match, dict):
        return None
    home = team_name(match.get("home"))
    away = team_name(match.get("away"))
    if home is None or away is None:
        return None
    return home, away


def report_nameless(match: dict, context: str) -> None:
    """Log — once per match id per process — that a match was skipped for having no team name."""
    mid = match.get("id") if isinstance(match, dict) else None
    key = (context, mid)
    if key in _reported:
        return
    _reported.add(key)
    sides = {
        s: sorted((match.get(s) or {}).keys()) if isinstance(match, dict) else None
        for s in ("home", "away")
    }
    log.warning(
        "%s: skipping match %s — no team name in %s (side keys: home=%s away=%s)",
        context, mid, "/".join(NAME_FIELDS), sides["home"], sides["away"],
    )


def named_matches(matches: list[dict], context: str) -> list[dict]:
    """
    `matches` minus any match with a nameless side, each skipped one logged.

    When EVERY match is dropped the feed has changed shape again, and an empty
    list would otherwise read downstream as "no fixtures today" — so that case
    is logged at ERROR, never just WARNING.
    """
    kept = []
    for m in matches:
        if match_names(m) is None:
            report_nameless(m, context)
        else:
            kept.append(m)
    dropped = len(matches) - len(kept)
    if dropped and not kept:
        log.error(
            "%s: ALL %d match(es) lack a team name in %s — the feed shape has "
            "changed; nothing can be analysed or settled until feed_names.py is updated",
            context, dropped, "/".join(NAME_FIELDS),
        )
    elif dropped:
        log.warning("%s: skipped %d of %d match(es) with no team name",
                    context, dropped, len(matches))
    return kept
