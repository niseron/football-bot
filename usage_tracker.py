"""
usage_tracker.py — API usage and cost tracking across every paid source.

Four sources, all verified programmatically readable on 4 Aug 2026 (no
dashboard-only figures, and nothing here is estimated):

  1. Anthropic   — `message.usage` on every messages.create() response.
                   Cost is computed from ANTHROPIC_PRICING below.
  2. The Odds API — `x-requests-used` / `x-requests-remaining` headers.
  3. RapidAPI football — `X-RateLimit-Requests-*` headers.
  4. RapidAPI tennis   — same headers, SEPARATE subscription on the same key.

Anthropic is the odd one out: its usage is per-response and ephemeral, and
there is no org-level usage/cost API to query after the fact. So this module
must RECORD each call as it happens. The other three report their own
cumulative totals, so they are read live at report time and never stored.

Storage is a Google Sheets tab, not SQLite: Railway's filesystem is ephemeral
and picks.db does not survive a redeploy.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

USAGE_SHEET = "API Usage"
USAGE_HEADERS = [
    "Date", "Timestamp UTC", "Job", "Model",
    "Input Tokens", "Output Tokens", "Cache Read", "Cache Write", "Cost USD",
]

# USD per 1M tokens. Source: Anthropic API reference, checked 4 Aug 2026 —
# not recalled from memory. Add a row when a new model is introduced; an
# unlisted model logs its tokens with a cost of 0.0 and a warning rather than
# guessing a price.
ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    #                    input,  output
    "claude-sonnet-4-6": (3.00, 15.00),
}
# Cache reads bill at ~0.1x input, 5-minute cache writes at ~1.25x input.
# The bot sends no cache_control today, so these are 0 — included so the
# figures stay correct if caching is ever switched on.
_CACHE_READ_MULTIPLIER = 0.10
_CACHE_WRITE_MULTIPLIER = 1.25

# The date this module shipped. Anthropic usage cannot be reconstructed for
# any call made before it, so the "month to date" figure is really
# "since tracking started" until the first full month elapses — the summary
# says so rather than presenting a number that looks complete and isn't.
TRACKING_START = date(2026, 8, 4)

# ── The Odds API budget ──────────────────────────────────────────────────────
# The free tier is 500 units/month. A unit is NOT a request: each call bills
# regions x markets, so the bot's "eu,uk" x "h2h,totals,spreads" call costs
# SIX units. Measured 4 Aug 2026: one call moved x-requests-used 294 -> 300.
ODDS_API_MONTHLY_LIMIT = 500
ODDS_API_UNITS_PER_CALL = 6

# Hard stop. Polling halts completely below this many remaining units, so the
# quota can never reach zero mid-month and take the daily picks run's odds
# enrichment down with it. Enrichment produces the value flags on live picks;
# closing-odds polling only feeds CLV analysis, so when the two compete for a
# nearly-empty quota, polling is what yields.
ODDS_API_RESERVE = 50
# Warn in the daily summary below this fraction of the monthly limit.
ODDS_API_WARN_FRACTION = 0.25

RAPIDAPI_HOSTS = {
    "football": "free-api-live-football-data.p.rapidapi.com",
    "tennis": os.environ.get("TENNIS_RAPIDAPI_HOST", "tennis-api-atp-wta-itf.p.rapidapi.com"),
}


# ── Anthropic: record each call ──────────────────────────────────────────────

def anthropic_cost(model: str, usage) -> float:
    """USD cost of one Claude call from its usage object. 0.0 if unpriced."""
    prices = ANTHROPIC_PRICING.get(model)
    if not prices:
        log.warning("usage_tracker: no price for model %r — logging tokens, cost 0.0", model)
        return 0.0
    in_rate, out_rate = prices
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return round(
        (usage.input_tokens * in_rate
         + read * in_rate * _CACHE_READ_MULTIPLIER
         + write * in_rate * _CACHE_WRITE_MULTIPLIER
         + usage.output_tokens * out_rate) / 1_000_000,
        6,
    )


def _usage_ws():
    """The 'API Usage' worksheet, created with headers if absent."""
    from excel_tracker import _get_spreadsheet
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(USAGE_SHEET)
    except Exception:
        ws = ss.add_worksheet(USAGE_SHEET, rows=2000, cols=len(USAGE_HEADERS))
        ws.update(values=[USAGE_HEADERS], range_name="A1", value_input_option="RAW")
        log.info("usage_tracker: created '%s' sheet", USAGE_SHEET)
        return ws


def record_anthropic_usage(job: str, model: str, usage) -> None:
    """
    Append one Claude call to the usage sheet. NEVER raises — usage tracking
    must not be able to break a picks run. A dropped row costs one line of
    reporting accuracy; a raised exception costs the day's picks.
    """
    try:
        now = datetime.now(timezone.utc)
        row = [
            now.strftime("%d-%b-%Y"),
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            job,
            model,
            usage.input_tokens,
            usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            anthropic_cost(model, usage),
        ]
        _usage_ws().append_row(row, value_input_option="RAW")
        log.info(
            "usage_tracker: %s used %d in / %d out tokens ($%.4f)",
            job, usage.input_tokens, usage.output_tokens, row[-1],
        )
    except Exception as exc:
        log.warning("usage_tracker: failed to record usage for %s (non-fatal): %s", job, exc)


def _read_usage_rows() -> list[list[str]]:
    try:
        return _usage_ws().get_all_values()[1:]
    except Exception as exc:
        log.warning("usage_tracker: could not read usage sheet: %s", exc)
        return []


def anthropic_totals() -> dict:
    """Today's and since-tracking-started Anthropic totals from the sheet."""
    today = date.today()
    out = {
        "today": {"calls": 0, "input": 0, "output": 0, "cost": 0.0},
        "month": {"calls": 0, "input": 0, "output": 0, "cost": 0.0},
        "by_job_today": {},
    }
    for r in _read_usage_rows():
        try:
            d = datetime.strptime(r[0], "%d-%b-%Y").date()
            inp, outp, cost = int(r[4] or 0), int(r[5] or 0), float(r[8] or 0)
        except (ValueError, IndexError):
            continue
        if d.year == today.year and d.month == today.month:
            b = out["month"]
            b["calls"] += 1; b["input"] += inp; b["output"] += outp; b["cost"] += cost
        if d == today:
            b = out["today"]
            b["calls"] += 1; b["input"] += inp; b["output"] += outp; b["cost"] += cost
            job = r[2] if len(r) > 2 else "?"
            j = out["by_job_today"].setdefault(job, {"calls": 0, "cost": 0.0})
            j["calls"] += 1; j["cost"] += cost
    return out


# ── The Odds API: live quota ─────────────────────────────────────────────────

_odds_quota_cache: tuple[datetime, dict] | None = None
_ODDS_QUOTA_TTL = timedelta(minutes=10)


def fetch_odds_quota(*, force: bool = False) -> dict | None:
    """
    Live Odds API quota from response headers. None if unavailable.

    Uses GET /v4/sports, which is FREE — verified 4 Aug 2026: the call did not
    move x-requests-used. So the hard stop can check the budget without
    spending any of it. Cached briefly so a 15-minute poller doesn't re-check
    on every cycle.
    """
    global _odds_quota_cache
    now = datetime.now(timezone.utc)
    if not force and _odds_quota_cache and now - _odds_quota_cache[0] < _ODDS_QUOTA_TTL:
        return _odds_quota_cache[1]

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return None
    try:
        resp = requests.get("https://api.the-odds-api.com/v4/sports/",
                            params={"apiKey": api_key}, timeout=10)
        resp.raise_for_status()
        used = int(resp.headers.get("x-requests-used", 0))
        remaining = int(resp.headers.get("x-requests-remaining", 0))
    except Exception as exc:
        log.warning("usage_tracker: Odds API quota check failed: %s", exc)
        return None

    quota = {
        "used": used,
        "remaining": remaining,
        "limit": used + remaining,
        "calls_left": remaining // ODDS_API_UNITS_PER_CALL,
    }
    _odds_quota_cache = (now, quota)
    return quota


def odds_budget_exhausted() -> bool:
    """
    True when Odds API polling must stop to protect the reserve.

    Fails OPEN (returns False) when the quota can't be read: a transient
    network failure on the free /sports endpoint should not silently disable
    closing-odds polling for the rest of the month. The daily cap still bounds
    the damage in that case.
    """
    quota = fetch_odds_quota()
    if quota is None:
        return False
    if quota["remaining"] < ODDS_API_RESERVE:
        log.warning(
            "usage_tracker: Odds API HARD STOP — %d units remaining is below the "
            "%d-unit reserve; polling halted so the daily picks enrichment keeps working",
            quota["remaining"], ODDS_API_RESERVE,
        )
        return True
    return False


# ── RapidAPI: live quota ─────────────────────────────────────────────────────

def fetch_rapidapi_quota(pipeline: str) -> dict | None:
    """
    Live RapidAPI quota for one pipeline's subscription. None if unavailable.

    Football and tennis are separate subscriptions on the same RAPIDAPI_KEY,
    so they have independent quotas and must be read (and reported) separately.
    There is no free endpoint here — the cheapest available call is made and
    its headers read, so this costs one request against that plan.
    """
    host = RAPIDAPI_HOSTS.get(pipeline)
    key = os.environ.get("RAPIDAPI_KEY")
    if not host or not key:
        return None
    paths = {
        "football": ("/football-get-matches-by-date",
                     {"date": datetime.now(timezone.utc).strftime("%Y%m%d")}),
        "tennis": (f"/tennis/v2/atp/fixtures/{datetime.now(timezone.utc):%Y-%m-%d}/", {}),
    }
    path, params = paths[pipeline]
    try:
        resp = requests.get(f"https://{host}{path}",
                            headers={"x-rapidapi-host": host, "x-rapidapi-key": key},
                            params=params, timeout=15)
        h = resp.headers
        limit = int(h.get("X-RateLimit-Requests-Limit", 0))
        remaining = int(h.get("X-RateLimit-Requests-Remaining", 0))
    except Exception as exc:
        log.warning("usage_tracker: RapidAPI %s quota check failed: %s", pipeline, exc)
        return None
    if not limit:
        return None
    reset_s = int(h.get("X-RateLimit-Requests-Reset", 0) or 0)
    return {
        "host": host,
        "limit": limit,
        "remaining": remaining,
        "used": limit - remaining,
        "reset_days": round(reset_s / 86400, 1) if reset_s else None,
    }


# ── Daily summary ────────────────────────────────────────────────────────────

def _bar(used: int, limit: int, width: int = 20) -> str:
    if not limit:
        return ""
    filled = min(width, round(width * used / limit))
    return "█" * filled + "░" * (width - filled)


def build_daily_summary() -> str:
    """Assemble the daily API usage + cost report. Plain text for Discord."""
    today = date.today()
    lines = [f"📊 **API USAGE — {today:%d %b %Y}**", ""]

    # Anthropic
    a = anthropic_totals()
    t, m = a["today"], a["month"]
    lines.append("**Anthropic (claude-sonnet-4-6)**")
    lines.append(f"  Today: {t['calls']} call(s) · {t['input']:,} in / {t['output']:,} out "
                 f"· **${t['cost']:.4f}**")
    for job, j in sorted(a["by_job_today"].items()):
        lines.append(f"     └ {job}: {j['calls']} call(s), ${j['cost']:.4f}")
    days_tracked = (today - TRACKING_START).days + 1
    lines.append(f"  {today:%B} so far: {m['calls']} call(s) · **${m['cost']:.4f}**")
    lines.append(f"  _Accruing since tracking started {TRACKING_START:%d %b %Y} "
                 f"({days_tracked}d) — not a full month-to-date. Anthropic exposes usage "
                 f"only per response, so calls before that date cannot be recovered._")
    lines.append("")

    # The Odds API
    lines.append("**The Odds API** (shared: football + tennis)")
    q = fetch_odds_quota(force=True)
    if q:
        pct_left = q["remaining"] / q["limit"] if q["limit"] else 0
        lines.append(f"  `{_bar(q['used'], q['limit'])}` {q['used']}/{q['limit']} units used")
        lines.append(f"  {q['remaining']} left ≈ **{q['calls_left']} calls** "
                     f"(1 call = {ODDS_API_UNITS_PER_CALL} units)")
        if q["remaining"] < ODDS_API_RESERVE:
            lines.append(f"  🛑 **HARD STOP ACTIVE** — below the {ODDS_API_RESERVE}-unit "
                         f"reserve. Closing-odds polling is halted; picks enrichment continues.")
        elif pct_left < ODDS_API_WARN_FRACTION:
            lines.append(f"  ⚠️ **LOW — {pct_left:.0%} remaining** "
                         f"(warning threshold {ODDS_API_WARN_FRACTION:.0%}). "
                         f"Hard stop trips at {ODDS_API_RESERVE} units.")
    else:
        lines.append("  _quota unavailable_")
    lines.append("")

    # RapidAPI, one block per subscription
    lines.append("**RapidAPI** (separate plans, same key)")
    for pipeline in ("football", "tennis"):
        r = fetch_rapidapi_quota(pipeline)
        if not r:
            lines.append(f"  {pipeline.title()}: _quota unavailable_")
            continue
        reset = f" · resets in {r['reset_days']}d" if r["reset_days"] else ""
        lines.append(f"  {pipeline.title()}: `{_bar(r['used'], r['limit'])}` "
                     f"{r['used']:,}/{r['limit']:,}{reset}")
    return "\n".join(lines)


def post_daily_summary() -> bool:
    """Build and send the daily summary to the 'usage' Discord channel."""
    from discord_bot import send_to_discord
    try:
        text = build_daily_summary()
    except Exception as exc:
        log.error("usage_tracker: failed to build daily summary: %s", exc)
        return False
    return send_to_discord("usage", message=text)


if __name__ == "__main__":
    from env_loader import load_env
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(build_daily_summary())
