"""
opus_referees.py — referee cards table for the Opus shadow ONLY (4 Oct 2026).

For every finished match in the tracked competitions, the referee from
football-get-match-referee joined to that match's 90-minute card count — the
settlement rule exactly (yellow + red + one per second-yellow lineup event,
extra time excluded), read through opus_stats, so the same match yields the
same figure everywhere. Nothing in production reads this module; only
opus_shadow and run_all's nightly batch import it.

TWO WINDOWS (14 Oct 2026):
  * the current season, SEASON_START (1 Jul 2026) to yesterday — every tracked
    competition: the five domestic leagues, Jupiler, the three UEFA club
    competitions and the Nations League. Updated by every shadow run.
  * history, HISTORY_START (1 Jul 2025) to SEASON_START — the 2025-26 season of
    HISTORY_COMPETITIONS only (the five leagues + Jupiler). Filled ONLY by the
    nightly batch (run_all, 23:15 UTC), never by a shadow run, so a picks day
    can never spend quota on last season.

QUOTA GUARD: every fetch here goes through QuotaGuard, which keeps
QUOTA_FLOOR_FRACTION (30%) of the monthly football RapidAPI plan untouched for
daily picks and settlement. It reads the plan's own X-RateLimit headers and
fails CLOSED — no reading, no fetching. A batch that reaches the floor stops,
flushes what it has, and leaves the rest open for the next night.

DURABLE STORE: two Google Sheet tabs, because Railway's disk is wiped on every
deploy and re-walking the season costs thousands of RapidAPI calls.
  * 'Opus Referee Cards' — one row per match, including matches whose referee is
    blank or whose stats are incomplete (so they are never fetched again).
  * 'Opus Referee Meta'  — the past days already fully scanned, and the feed
    leagueIds resolved to a parent competition (rotating UEFA / Belgian ids).
    A scanned day before SEASON_START means scanned for HISTORY_COMPETITIONS.
Loaded once per process (two reads), appended in batches. A day is marked
scanned only when every tracked match on it got a definitive row; a fetch
failure or a guard stop leaves the day open for the next run.

MATCHING: a referee is the exact (name, country) pair the feed returns — no
folding, no fuzzy match. The feed gives no referee id, so before an average is
used, football-players-search is asked for that name: two or more 'referee'
suggestions with the same name and country code is a collision, and a
collision — or a search that failed — is 'referee unknown'. So is a blank
referee, a blank country, or fewer than REF_MIN_MATCHES complete matches.
"""
from __future__ import annotations

import logging
import math
import time
import unicodedata
from datetime import date, timedelta

log = logging.getLogger(__name__)

SEASON_START = date(2026, 7, 1)      # 2026-27: UEFA qualifying starts in July
HISTORY_START = date(2025, 7, 1)     # 2025-26, domestic leagues only (14 Oct 2026)
HISTORY_COMPETITIONS = ("Premier League", "Bundesliga", "La Liga", "Serie A",
                        "Ligue 1", "Jupiler Pro League")
REF_MIN_MATCHES = 5
REF_UNKNOWN = "referee unknown"

# Quota guard (14 Oct 2026). The floor is the user's rule: at least 30% of the
# monthly plan stays untouched for production. A match costs at most 5 calls
# (referee, all-stats, two halves after extra time, two lineups after a red),
# so a fetch is only started with that much room above the floor.
QUOTA_FLOOR_FRACTION = 0.30
QUOTA_MATCH_MARGIN = 5
QUOTA_READING_MAX_AGE_S = 120        # older header readings are re-read live (1 call)
NIGHTLY_CALL_CAP = 600               # one night's batch, on top of the floor

REF_SHEET = "Opus Referee Cards"
META_SHEET = "Opus Referee Meta"
REF_HEADERS = ["Match ID", "Date", "Competition", "Home", "Away", "Home ID", "Away ID",
               "Referee", "Country", "Country Code", "Cards Home", "Cards Away",
               "Cards Total", "Note"]
META_HEADERS = ["Kind", "Key", "Value"]

FLUSH_EVERY_DAYS = 5                 # backfill progress survives an interruption

_rows: dict[int, dict] = {}          # match_id -> row dict (REF_HEADERS keys)
_scanned_days: set[str] = set()      # 'YYYYMMDD'
_league_competition: dict[int, str] = {}   # feed leagueId -> competition ('' = not tracked)
_collision_cache: dict[tuple[str, str], bool | None] = {}
_loaded = False


# ── Quota guard ──────────────────────────────────────────────────────────────

class QuotaGuard:
    """
    Says whether one more fetch may run without taking the football RapidAPI
    plan under QUOTA_FLOOR_FRACTION of its monthly limit, and — with
    `call_cap` — without one batch spending more than that many calls.

    Reads remaining quota from the headers auto_results._rapid_get records on
    every response; a reading older than QUOTA_READING_MAX_AGE_S is replaced by
    a live one (usage_tracker.fetch_rapidapi_quota, 1 call). Calls made outside
    _rapid_get (the by-date feed, parent lookups) are counted via
    `spent_outside` until the next header reading includes them. No reading at
    all means False: the guard fails closed.
    """

    def __init__(self, call_cap: int | None = None):
        self.call_cap = call_cap
        self.limit: int | None = None
        self.remaining: int | None = None
        self.start: int | None = None
        self.reason = ""
        self._stamp: float | None = None
        self._untracked = 0

    def _read(self) -> bool:
        from auto_results import rapid_quota
        q = rapid_quota()
        if q and time.monotonic() - q[2] <= QUOTA_READING_MAX_AGE_S \
                and (self._stamp is None or q[2] > self._stamp):
            self.limit, self.remaining, self._stamp = q
            self._untracked = 0
        if self._stamp is None or time.monotonic() - self._stamp > QUOTA_READING_MAX_AGE_S:
            from usage_tracker import fetch_rapidapi_quota
            live = fetch_rapidapi_quota("football")
            if not live:
                return False
            self.limit, self.remaining = live["limit"], live["remaining"]
            self._stamp = time.monotonic()
            self._untracked = 0
        if self.start is None:
            self.start = self.remaining
        return True

    def spent_outside(self, n: int = 1) -> None:
        self._untracked += n

    def left(self) -> int | None:
        return None if self.remaining is None else self.remaining - self._untracked

    def floor(self) -> int | None:
        return None if self.limit is None else math.ceil(self.limit * QUOTA_FLOOR_FRACTION)

    def allows(self, need: int = QUOTA_MATCH_MARGIN) -> bool:
        if not self._read():
            self.reason = "quota unreadable"
            return False
        left, floor = self.left(), self.floor()
        if left - need < floor:
            self.reason = f"quota floor: {left} left, floor {floor}"
            return False
        if self.call_cap is not None and (self.start - left) + need > self.call_cap:
            self.reason = f"batch cap of {self.call_cap} calls reached"
            return False
        return True


# ── Sheets ───────────────────────────────────────────────────────────────────

def _ws(name: str, headers: list[str]):
    import gspread
    from excel_tracker import _get_spreadsheet
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(name, rows=200, cols=len(headers))
        ws.update(values=[headers], range_name="A1", value_input_option="RAW")
        return ws


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load(force: bool = False) -> None:
    """
    Read both tabs (once per process, or again with `force`). Raises on a
    Sheets failure, leaving what was loaded intact: both tabs are read before
    anything is replaced. `force` is how a batch picks up rows another writer
    (a local backfill run) appended after this process started — without it a
    long-lived Railway process would re-fetch days already in the Sheet.
    """
    global _loaded
    if _loaded and not force:
        return
    rows = _ws(REF_SHEET, REF_HEADERS).get_all_values()
    meta = _ws(META_SHEET, META_HEADERS).get_all_values()
    _rows.clear()
    _scanned_days.clear()
    _league_competition.clear()
    for r in rows[1:]:
        d = dict(zip(REF_HEADERS, r + [""] * len(REF_HEADERS)))
        mid = _int(d["Match ID"])
        if mid is not None:
            _rows[mid] = d
    for kind, key, value in ((r + ["", "", ""])[:3] for r in meta[1:]):
        if kind == "day":
            _scanned_days.add(key)
        elif kind == "league" and _int(key) is not None:
            _league_competition[int(key)] = value
    _loaded = True
    log.info("opus_referees: loaded %d match row(s), %d scanned day(s), %d resolved league id(s)",
             len(_rows), len(_scanned_days), len(_league_competition))


def _flush(new_rows: list[dict], new_meta: list[list]) -> None:
    from opus_stats import _save_cache
    if new_rows:
        _ws(REF_SHEET, REF_HEADERS).append_rows(
            [[r.get(h, "") for h in REF_HEADERS] for r in new_rows], value_input_option="RAW")
    if new_meta:
        _ws(META_SHEET, META_HEADERS).append_rows(new_meta, value_input_option="RAW")
    _save_cache()
    new_rows.clear()
    new_meta.clear()


# ── Feed ─────────────────────────────────────────────────────────────────────

def fetch_referee(match_id: int) -> dict | None:
    """{'name', 'country', 'code'} ('' when the feed has none), or None on a failed call."""
    from auto_results import _rapid_get
    raw = _rapid_get("football-get-match-referee", {"eventid": match_id})
    if not isinstance(raw, dict) or raw.get("status") != "success":
        return None
    ref = (raw.get("response") or {}).get("referee") or {}
    return {"name": (ref.get("text") or "").strip(),
            "country": (ref.get("country") or "").strip(),
            "code": (ref.get("countryCode") or "").strip()}


def _tracked_ids() -> dict[int, str]:
    """Feed leagueId -> competition, for every id known to belong to a tracked one."""
    from main import FEED_LEAGUE_IDS, LEAGUES
    ids = {lid: name for name, lid in LEAGUES.items()}
    for name, feed_ids in FEED_LEAGUE_IDS.items():
        ids.update({lid: name for lid in feed_ids})
    ids.update({lid: c for lid, c in _league_competition.items() if c})
    return ids


def _competition_for_parent(parent) -> str:
    from main import LEAGUES, PARENT_RESOLVED_IDS
    for name, lid in LEAGUES.items():
        if parent == lid:
            return name
    for name, parents in PARENT_RESOLVED_IDS.items():
        if parent in parents:
            return name
    return ""


def _usable(m: dict) -> bool:
    s = m.get("status") or {}
    return bool(s.get("finished")) and not s.get("cancelled") and not s.get("awarded")


def _window(history: bool, today: date) -> tuple[date, date]:
    return (HISTORY_START, min(SEASON_START, today)) if history else (SEASON_START, today)


def open_days(history: bool, today: date | None = None) -> int:
    """Days in the window not yet fully scanned (no network once loaded)."""
    start, end = _window(history, today or date.today())
    return sum((start + timedelta(days=i)).strftime("%Y%m%d") not in _scanned_days
               for i in range((end - start).days))


# ── Table update ─────────────────────────────────────────────────────────────

def update_table(today: date | None = None, *, history: bool = False,
                 guard: QuotaGuard | None = None) -> dict:
    """
    Add every not-yet-scanned past day's tracked matches in one window to the
    table (see the module docstring for the two windows). Returns counters;
    `stopped` names the guard's reason when it ended the walk early. Unfamiliar
    leagueIds are resolved to their parent competition only when their block
    shares a team with a stored competition (or is two UEFA nations) — one
    match-detail call per such id, persisted.
    """
    from main import LEAGUES, UEFA_NATIONS, _fetch_day_matches, _resolve_parent_league
    from opus_stats import _load_cache, _match_figures

    load()
    _load_cache()
    today = today or date.today()
    guard = guard or QuotaGuard()
    allowed = set(HISTORY_COMPETITIONS) if history else None
    # Which stored teams make an unfamiliar leagueId worth a lookup. In history
    # only competitions WITHOUT a fixed id need one (Jupiler: the big five are
    # in LEAGUES); keying on every stored club would resolve the domestic
    # leagues of every UEFA qualifier entrant, ~1 call each, for nothing.
    resolve_from = (allowed - set(LEAGUES)) if history else None
    teams: set[int] = set()
    for r in _rows.values():
        if resolve_from is None or r["Competition"] in resolve_from:
            teams.update(t for t in (_int(r["Home ID"]), _int(r["Away ID"])) if t)

    c = {"days": 0, "matches": 0, "failed": 0, "resolved": 0, "hits": 0, "fetched": 0,
         "stopped": ""}
    new_rows: list[dict] = []
    new_meta: list[list] = []
    since_flush = 0
    day, end = _window(history, today)
    while day < end and not c["stopped"]:
        key = day.strftime("%Y%m%d")
        day += timedelta(days=1)
        if key in _scanned_days:
            continue
        if not guard.allows():
            c["stopped"] = guard.reason
            break
        guard.spent_outside()             # the by-date feed bypasses _rapid_get
        matches = [m for m in _fetch_day_matches(day - timedelta(days=1)) if _usable(m)]
        if not matches:
            continue                      # a failed day fetch reads as empty — retry next run
        tracked = _tracked_ids()
        blocks: dict[int, list[dict]] = {}
        for m in matches:
            blocks.setdefault(m.get("leagueId"), []).append(m)
        for lid, ms in blocks.items():
            if lid in tracked or lid in _league_competition or lid is None:
                continue
            ids = {(m.get(s) or {}).get("id") for m in ms for s in ("home", "away")}
            nations = not history and all((m.get(s) or {}).get("longName") in UEFA_NATIONS
                                          for m in ms for s in ("home", "away"))
            if not (ids & teams or nations):
                continue
            if not guard.allows(need=1):
                c["stopped"] = guard.reason
                break
            time.sleep(1)
            guard.spent_outside()
            parent = _resolve_parent_league(ms[0])
            if parent is None:
                continue                  # lookup failed: not persisted, retried later
            comp = _competition_for_parent(parent)
            _league_competition[lid] = comp
            new_meta.append(["league", str(lid), comp])
            c["resolved"] += 1
            if comp:
                tracked[lid] = comp
                log.info("opus_referees: leagueId %s -> %s (parent %s)", lid, comp, parent)

        day_ok = not c["stopped"]
        for m in matches:
            if c["stopped"]:
                break
            comp = tracked.get(m.get("leagueId"))
            mid = m.get("id")
            if not comp or mid is None or (allowed is not None and comp not in allowed):
                continue
            h, a = (m.get("home") or {}), (m.get("away") or {})
            if resolve_from is None or comp in resolve_from:
                teams.update(t for t in (h.get("id"), a.get("id")) if t)
            if mid in _rows:
                continue
            if not guard.allows():
                c["stopped"] = guard.reason
                day_ok = False
                break
            ref = fetch_referee(mid)
            fig = _match_figures(m, c) if ref is not None else None
            if ref is None or fig is None:
                day_ok = False
                c["failed"] += 1
                continue
            complete = "incomplete" not in fig
            row = {"Match ID": str(mid), "Date": key, "Competition": comp,
                   "Home": h.get("longName", ""), "Away": a.get("longName", ""),
                   "Home ID": str(h.get("id") or ""), "Away ID": str(a.get("id") or ""),
                   "Referee": ref["name"], "Country": ref["country"], "Country Code": ref["code"],
                   "Cards Home": str(fig["cards"][0]) if complete else "",
                   "Cards Away": str(fig["cards"][1]) if complete else "",
                   "Cards Total": str(sum(fig["cards"])) if complete else "",
                   "Note": "" if complete else fig["incomplete"]}
            _rows[mid] = row
            new_rows.append(row)
            c["matches"] += 1
        if day_ok:
            _scanned_days.add(key)
            new_meta.append(["day", key, ""])
            c["days"] += 1
        since_flush += 1
        if since_flush >= FLUSH_EVERY_DAYS:
            _flush(new_rows, new_meta)
            since_flush = 0
    _flush(new_rows, new_meta)
    c["open_days"] = open_days(history, today)
    log.info("opus_referees: %s window — %d day(s) scanned, %d match row(s) added, %d failed, "
             "%d league id(s) resolved, %d day(s) still open%s; table now %d match(es)",
             "2025-26 history" if history else "current-season", c["days"], c["matches"],
             c["failed"], c["resolved"], c["open_days"],
             f"; STOPPED ({c['stopped']})" if c["stopped"] else "", len(_rows))
    return c


def nightly_backfill(call_cap: int = NIGHTLY_CALL_CAP, today: date | None = None) -> dict:
    """
    run_all's 23:15 UTC batch: the current season first, then 2025-26 history,
    under ONE guard capped at `call_cap` calls and the 30% floor. Costs no
    RapidAPI call at all once both windows are fully scanned.
    """
    load(force=True)                      # the Sheet, not this process's memory, is the truth
    guard = QuotaGuard(call_cap)
    season = update_table(today, guard=guard)
    history = update_table(today, history=True, guard=guard) if not season["stopped"] else None
    return {"season": season, "history": history, "quota_left": guard.left()}


# ── Pick time ────────────────────────────────────────────────────────────────

def referee_index(rows=None) -> dict[tuple[str, str], list[int]]:
    """(name, country) -> total 90-minute cards per complete match. Exact keys only."""
    idx: dict[tuple[str, str], list[int]] = {}
    for r in (rows if rows is not None else _rows.values()):
        name, country, total = r.get("Referee", ""), r.get("Country", ""), _int(r.get("Cards Total"))
        if name and country and total is not None:
            idx.setdefault((name, country), []).append(total)
    return idx


def _fold(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(ch)).casefold().strip()


def referee_collides(name: str, code: str) -> bool | None:
    """
    True when the player search lists 2+ referees with this name and country
    code (the feed carries no referee id, so that is a collision); None when
    the search could not be read. Cached per process.
    """
    key = (name, code)
    if key in _collision_cache:
        return _collision_cache[key]
    from auto_results import _rapid_get
    raw = _rapid_get("football-players-search", {"search": name})
    if not isinstance(raw, dict) or raw.get("status") != "success":
        return None                       # not cached: retried on the next run
    sugg = (raw.get("response") or {}).get("suggestions") or []
    same = [s for s in sugg if s.get("type") == "referee"
            and _fold(s.get("name") or "") == _fold(name) and (s.get("ccode") or "") == code]
    _collision_cache[key] = len(same) >= 2
    return _collision_cache[key]


def referee_summary(ref: dict | None, index: dict, collides) -> dict | str:
    """What Opus sees for one fixture: the referee's average, or REF_UNKNOWN."""
    if not ref or not ref.get("name") or not ref.get("country"):
        return REF_UNKNOWN
    totals = index.get((ref["name"], ref["country"]), [])
    if len(totals) < REF_MIN_MATCHES:
        return REF_UNKNOWN
    if collides(ref["name"], ref.get("code", "")) is not False:
        return REF_UNKNOWN
    return {"name": ref["name"], "country": ref["country"],
            "cards_per_match_90min": round(sum(totals) / len(totals), 2),
            "matches": len(totals)}


def build_referee_context(fixtures_by_league: dict[str, list[dict]]) -> dict:
    """
    {match_id: {'referee': ...}} for every fixture. Updates the current-season
    window first (never history). Under the quota floor a fixture's referee is
    not fetched and it reads 'referee unknown'.
    """
    guard = QuotaGuard()
    try:
        load(force=True)
        update_table(guard=guard)
    except Exception as exc:
        log.warning("opus_referees: table update failed — using what is loaded: %s", exc)
    index = referee_index()
    out, known, skipped = {}, 0, 0
    for f in (f for fx in fixtures_by_league.values() for f in fx):
        if guard.allows(need=2):          # the referee, plus one name search
            summary = referee_summary(fetch_referee(f.get("match_id")), index, referee_collides)
        else:
            summary, skipped = REF_UNKNOWN, skipped + 1
        known += isinstance(summary, dict)
        out[f.get("match_id")] = {"referee": summary}
    log.info("opus_referees: %d of %d fixture(s) have a known referee average%s", known, len(out),
             f"; {skipped} not fetched ({guard.reason})" if skipped else "")
    return out


def unknown_for_all(fixtures_by_league: dict[str, list[dict]]) -> dict:
    return {f.get("match_id"): {"referee": REF_UNKNOWN}
            for fx in fixtures_by_league.values() for f in fx}


def coverage_by_competition() -> dict[str, dict]:
    """
    Per competition: matches, matches with a named referee, complete cards,
    distinct referees, and the per-referee complete-match counts ('ref_cards').
    """
    out: dict[str, dict] = {}
    for r in _rows.values():
        g = out.setdefault(r["Competition"], {"matches": 0, "named": 0, "with_cards": 0,
                                               "refs": set(), "ref_cards": {}})
        g["matches"] += 1
        if r["Referee"]:
            g["named"] += 1
            g["refs"].add((r["Referee"], r["Country"]))
        if r["Cards Total"]:
            g["with_cards"] += 1
            if r["Referee"] and r["Country"]:
                k = (r["Referee"], r["Country"])
                g["ref_cards"][k] = g["ref_cards"].get(k, 0) + 1
    return out
