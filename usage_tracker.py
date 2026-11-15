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
import re
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

USAGE_SHEET = "API Usage"
USAGE_HEADERS = [
    "Date", "Timestamp UTC", "Job", "Model",
    "Input Tokens", "Output Tokens", "Cache Read", "Cache Write", "Cost USD",
]

# Failures live in their OWN tab, deliberately (18 Aug 2026). The usage sheet is
# the cost ledger and every reader of it counts rows as calls and sums column I
# — a failure row there would inflate the call count and dilute the per-job
# figures with events that consumed nothing. Keeping them apart means the cost
# math needs no "is this row real" filter, and the failures tab doubles as the
# audit trail for the status line in the daily summary.
FAILURE_SHEET = "API Failures"
FAILURE_HEADERS = ["Date", "Timestamp UTC", "Job", "Model", "Error"]

# USD per 1M tokens. Source: Anthropic API reference, checked 4 Aug 2026 —
# not recalled from memory. Add a row when a new model is introduced; an
# unlisted model logs its tokens with a cost of 0.0 and a warning rather than
# guessing a price.
ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    #                    input,  output
    "claude-sonnet-4-6": (3.00, 15.00),
    # Added 13 Aug 2026 for the Opus 5 shadow experiment. Rates confirmed
    # against the live models reference that day, not recalled: Claude Opus 5
    # is $5 / input MTok and $25 / output MTok.
    #
    # Worth knowing when reading the Opus rows: Opus 5 runs ADAPTIVE THINKING
    # BY DEFAULT and thinking tokens bill as OUTPUT. Measured on the 13 Aug
    # slate, output (and so ~79% of the cost) scales with the effort level —
    # 1,413 tokens at 'low', 2,440 at 'medium', 4,441 at 'high'. An Opus row
    # whose output dwarfs the Sonnet row on the same fixtures is that, not a
    # bug.
    "claude-opus-5": (5.00, 25.00),
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
# Paid tier from 6 Aug 2026: 20,000 units/month ($30/mo). A unit is NOT a
# request: each call bills regions x markets. The bot now sends ONE region
# ("eu") x "h2h,totals,spreads" = THREE units. Measured 6 Aug 2026: one such
# call moved x-requests-used 432 -> 435, exactly half the previous "eu,uk"
# cost of six. Dropping the uk region loses 3 of 12 bookmakers per event
# (betfair_ex_uk, betfair_sb_uk, boylesports, paddypower) and KEEPS the
# Betfair EU exchange, so the consensus average barely moves.
ODDS_API_MONTHLY_LIMIT = 20_000
ODDS_API_UNITS_PER_CALL = 3

# Hard stop. Polling halts completely below this many remaining units, so the
# quota can never reach zero mid-month and take the daily picks run's odds
# enrichment down with it. Enrichment produces the value flags on live picks;
# closing-odds polling only feeds CLV analysis, so when the two compete for a
# nearly-empty quota, polling is what yields.
#
# Derived from the limit the API actually REPORTS, never a hardcoded absolute:
# the reserve has to track whichever tier is really in force. A plan change —
# or a key that has not picked the new plan up yet — then needs no code edit,
# and both the 500-unit and 20,000-unit cases get a sane stop instead of one
# of them being wildly wrong in whichever direction.
ODDS_API_RESERVE_FRACTION = 0.10
ODDS_API_RESERVE_FLOOR = 50
# Warn in the daily summary below this fraction of the monthly limit.
ODDS_API_WARN_FRACTION = 0.25


def odds_api_reserve(limit: int | None = None) -> int:
    """Hard-stop threshold, in units, for the tier actually in force."""
    limit = limit or ODDS_API_MONTHLY_LIMIT
    return max(ODDS_API_RESERVE_FLOOR, int(limit * ODDS_API_RESERVE_FRACTION))

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


def _failures_ws(create: bool = True):
    """
    The 'API Failures' worksheet. `create=False` returns None when the tab does
    not exist yet, so a read never provokes a write — the daily summary asks for
    this on a schedule and should not create a tab just to find it empty.
    """
    from excel_tracker import _get_spreadsheet
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(FAILURE_SHEET)
    except Exception:
        if not create:
            return None
        ws = ss.add_worksheet(FAILURE_SHEET, rows=1000, cols=len(FAILURE_HEADERS))
        ws.update(values=[FAILURE_HEADERS], range_name="A1", value_input_option="RAW")
        log.info("usage_tracker: created '%s' sheet", FAILURE_SHEET)
        return ws


def _read_failure_rows() -> list[list[str]]:
    try:
        ws = _failures_ws(create=False)
        return ws.get_all_values()[1:] if ws else []
    except Exception as exc:
        log.warning("usage_tracker: could not read failures sheet: %s", exc)
        return []


# The API returns no distinct error code for an exhausted balance — it is a
# generic 400 `invalid_request_error`, and `.type` is the same string a
# malformed request returns. The human-readable message is the only signal that
# separates "you owe money" from "your payload is wrong", so this matches on it.
# Deliberately narrow: a false positive would tell the user to top up an account
# that is already funded and send them looking in the wrong place.
_CREDIT_BALANCE_MARKER = "credit balance is too low"


def is_credit_balance_error(exc: Exception) -> bool:
    """True when an Anthropic call failed because the account is out of credit."""
    return _CREDIT_BALANCE_MARKER in str(exc).lower()


def record_anthropic_failure(job: str, model: str, exc: Exception) -> None:
    """
    Append one FAILED Claude call to the failures sheet. Never raises, for the
    same reason record_anthropic_usage doesn't: tracking a failure must not
    become a second failure.
    """
    try:
        now = datetime.now(timezone.utc)
        _failures_ws().append_row(
            [
                now.strftime("%d-%b-%Y"),
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                job,
                model or "",
                " ".join(str(exc).split())[:500],
            ],
            value_input_option="RAW",
        )
    except Exception as inner:
        log.warning("usage_tracker: failed to record failure for %s (non-fatal): %s", job, inner)


# One alert per (day, kind) per process. A picks run makes one call PER
# COMPETITION, so an exhausted balance fails every one of them — without this
# guard a ten-competition slate would post ten identical alerts and train the
# reader to ignore the channel. Keyed by date so a still-broken account alerts
# again tomorrow rather than going quiet after the first day.
_alerted_failures: set[tuple[str, str]] = set()


def alert_anthropic_failure(job: str, exc: Exception, model: str = "") -> bool:
    """
    Record an Anthropic failure and, for an exhausted credit balance, alert the
    'usage' Discord channel immediately.

    Returns True if an alert was actually sent. Only credit-balance failures
    alert: they are account-wide and nothing recovers until someone tops up, so
    the sooner a human sees it the better. Other failures (a 429, a 529, a
    transient network error) are recorded for the status line but not alerted —
    they are usually self-healing, and the picks-failed alert already covers the
    case where they killed a whole slate.

    'usage' is the ops channel, not a subscriber-facing one, so the raw error
    goes out unscrubbed here. The subscriber-facing picks-failed alert scrubs
    model and vendor names (main._scrub_model_names); this one deliberately does
    not — the person reading it needs the verbatim message.
    """
    from discord_bot import send_to_discord

    record_anthropic_failure(job, model, exc)

    if not is_credit_balance_error(exc):
        return False

    key = (date.today().isoformat(), "credit-balance")
    if key in _alerted_failures:
        log.info("usage_tracker: credit-balance alert already sent today — not repeating")
        return False
    _alerted_failures.add(key)

    now = datetime.now(timezone.utc)
    detail = " ".join(str(exc).split())
    text = (
        "🛑 **ANTHROPIC API FAILURE — OUT OF CREDIT**\n"
        f"Job: `{job}`" + (f" (`{model}`)" if model else "") + "\n"
        f"Time: {now:%d %b %Y %H:%M} UTC\n\n"
        f"```{detail[:1200]}```\n"
        "Every Anthropic call — football picks, tennis picks and the Opus shadow — "
        "fails until credit is added. Repeat alerts are suppressed for the rest of today."
    )
    if not send_to_discord("usage", message=text):
        log.error("usage_tracker: could not deliver credit-balance alert to Discord ('usage')")
        return False
    return True


def alert_sheet_write_failure(
    job: str,
    *,
    attempted: int,
    written: int,
    skipped: int,
    failed: int,
) -> bool:
    """
    Alert the 'usage' channel when a pick batch did not fully reach the Sheet.

    Called whenever `failed` is non-zero — a PARTIAL loss alerts exactly as
    loudly as a total one. That distinction is the whole reason this exists:
    between 20 and 29 Aug 2026 six slates were written to Discord and never to
    the Sheet, and the only trace was `log.info("Logged 0 of 29 pick(s)")` in
    the Railway logs. Nothing was delivered anywhere, nobody looked, and 128
    picks were lost before a manual audit found them.

    `skipped` is reported but never triggers the alert on its own: the duplicate
    guard dropping a pick is correct behaviour and happens most days.

    NOT deduped, unlike alert_anthropic_failure. That guard exists because one
    exhausted balance fans out across ten per-competition calls in a single run;
    here there is exactly one batch call per run, so every alert is a distinct
    real loss and suppressing the second would hide it.

    'usage' is the OPS channel — the text is deliberately unscrubbed and names
    the sheet, the job and the counts, because the reader is the operator.
    """
    from discord_bot import send_to_discord

    now = datetime.now(timezone.utc)
    text = (
        "🛑 **PICKS DID NOT REACH THE SHEET**\n"
        f"Job: `{job}`\n"
        f"Time: {now:%d %b %Y %H:%M} UTC\n\n"
        f"Picks generated: **{attempted}**\n"
        f"Written to the Sheet: **{written}**\n"
        f"Skipped by the duplicate guard (expected): **{skipped}**\n"
        f"**LOST: {failed}**\n\n"
        "These picks were delivered to Discord but are absent from the Picks tab, "
        "so they will never settle and never reach the Summary, calibration or CLV. "
        "Check the Railway logs for `Sheets read failed` / `Sheets batch write failed` "
        "around this timestamp. A 429 means the Sheets per-minute quota was exhausted."
    )
    if not send_to_discord("usage", message=text):
        log.error(
            "usage_tracker: could not deliver sheet-write-failure alert to Discord "
            "('usage') — %d pick(s) lost by %s and NOTHING announced it", failed, job,
        )
        return False
    return True


def alert_job_failure(job: str, exc: Exception | str) -> bool:
    """
    Alert the ops 'usage' channel that a production job crashed or could not run.

    Added 4 Nov 2026. Until then 'usage' only heard about an exhausted Anthropic
    balance, a Sheet write gap or a data-integrity breach — so when the feed
    dropped `longName` on 3 Nov, every settlement poll failed for ~21 hours and
    the 4 Nov picks run died before analysis, and NOTHING was posted anywhere:
    the only trace was an ERROR line in the Railway logs.

    Deduped per (day, job): live_results_check runs every 30 minutes and a
    broken feed fails all of them, and an alert channel that cries wolf gets
    muted. Unscrubbed, like every 'usage' alert — the reader is the operator.
    """
    from discord_bot import send_to_discord

    key = (date.today().isoformat(), f"job:{job}")
    if key in _alerted_failures:
        log.info("usage_tracker: job-failure alert for %s already sent today", job)
        return False
    _alerted_failures.add(key)

    now = datetime.now(timezone.utc)
    detail = " ".join(
        (f"{type(exc).__name__}: {exc}" if isinstance(exc, Exception) else str(exc)).split()
    )
    text = (
        "🛑 **PRODUCTION JOB FAILED**\n"
        f"Job: `{job}`\n"
        f"Time: {now:%d %b %Y %H:%M} UTC\n\n"
        f"```{detail[:1200]}```\n"
        "Check the Railway logs around this timestamp for the traceback. "
        "Repeat alerts for this job are suppressed for the rest of today."
    )
    if not send_to_discord("usage", message=text):
        log.error("usage_tracker: could not deliver job-failure alert for %s to Discord "
                  "('usage') — %s", job, detail)
        return False
    return True


def alert_data_integrity(kind: str, detail: str, rows: list[str] | None = None) -> bool:
    """
    Alert the ops 'usage' channel that a number in the sheet cannot be right.

    For invariant breaches, not failures: nothing crashed, the data is simply
    impossible. Two feed it — a settled row whose P&L does not follow from its
    result and price, and a matched market price that disagrees wildly with the
    model estimate for the same pick (the 23 Aug 2026 Club Brugge mispricing,
    where a 1.26 favourite was priced and paid at the underdog's 8.98).

    Unscrubbed, like every 'usage' alert: the reader is the operator and needs
    the exact figures. Deduped per (day, kind) so one bad slate cannot spam the
    channel — an alert channel that cries wolf gets muted, and then the next one
    is invisible.
    """
    from discord_bot import send_to_discord

    key = (date.today().isoformat(), f"integrity:{kind}")
    if key in _alerted_failures:
        log.warning("usage_tracker: integrity alert '%s' already sent today: %s", kind, detail)
        return False
    _alerted_failures.add(key)

    now = datetime.now(timezone.utc)
    body = [
        "🧮 **DATA INTEGRITY CHECK FAILED**\n",
        f"Check: `{kind}`\n",
        f"Time: {now:%d %b %Y %H:%M} UTC\n\n",
        f"{detail}\n",
    ]
    if rows:
        body.append("\nAffected:\n```\n" + "\n".join(rows[:15]) + "\n```\n")
    body.append("\nThese figures are wrong in the Sheet and in anything already "
                "published from it. Repeat alerts for this check are suppressed "
                "for the rest of today.")
    text = "".join(body)
    if not send_to_discord("usage", message=text):
        log.error("usage_tracker: could not deliver integrity alert '%s' to Discord "
                  "('usage') — %s", kind, detail)
        return False
    return True


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
        # Distinct models seen today — the daily summary's heading is built from
        # this rather than a hardcoded name, so the Opus 5 shadow's spend is not
        # reported under Sonnet's label (13 Aug 2026).
        "models_today": set(),
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
            model = r[3] if len(r) > 3 else ""
            if model:
                out["models_today"].add(model)
    return out


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def anthropic_status() -> dict:
    """
    Health of the Anthropic API from this bot's point of view: the most recent
    successful call and the most recent failure, each with its job and time.

    This exists because a total outage is otherwise INVISIBLE in the daily
    summary — an account with no credit simply produces no usage rows, which
    reads identically to a quiet day with no fixtures. Comparing the two
    timestamps is what separates "nothing to do" from "nothing works": the
    16-18 Aug 2026 outage produced three consecutive silent zero-cost days.
    """
    last_ok: tuple[datetime, str, str] | None = None
    for r in _read_usage_rows():
        if len(r) < 4:
            continue
        ts = _parse_ts(r[1])
        if ts and (last_ok is None or ts > last_ok[0]):
            last_ok = (ts, r[2], r[3])

    last_fail: tuple[datetime, str, str] | None = None
    for r in _read_failure_rows():
        if len(r) < 5:
            continue
        ts = _parse_ts(r[1])
        if ts and (last_fail is None or ts > last_fail[0]):
            last_fail = (ts, r[2], r[4])

    healthy = True
    if last_fail and (last_ok is None or last_fail[0] > last_ok[0]):
        healthy = False

    return {"last_success": last_ok, "last_failure": last_fail, "healthy": healthy}


# ── The Odds API: failures (16 Nov 2026) ─────────────────────────────────────
# From ~10 Nov to 16 Nov 2026 the key was DEACTIVATED after the subscription
# lapsed. For five days every pick posted and settled at the model's ESTIMATED
# odds, with no closing odds, and nothing said so. main._fetch_odds_events
# swallowed each 401 at DEBUG, and the daily summary printed a neutral "quota
# unavailable", the same text a network blip produces. The one WARNING line
# that did fire leaked the key: `requests` puts the full URL, `?apiKey=...`
# included, into every HTTPError and ConnectionError message, so 34 plaintext
# copies reached the Railway logs.
#
# Two rules follow. A failed Odds API call is logged ONLY as status +
# error_code, or the exception TYPE, never str(exc) and never resp.url. And an
# account-level error alerts 'usage', at most once a day. "Account-level" is
# read from the body's error_code, never from the HTTP status alone: these four
# do not recover until a human acts. A 429 EXCEEDED_FREQ_LIMIT, a 5xx, a timeout
# or an empty list self-heals, so it is logged and not alerted. Odds API
# failures are deliberately NOT written to the API Failures tab:
# anthropic_status() reads every row there as an Anthropic failure.
ODDS_API_KEY_ERROR_CODES = frozenset({"MISSING_KEY", "INVALID_KEY", "DEACTIVATED_KEY"})
ODDS_API_CREDIT_ERROR_CODES = frozenset({"OUT_OF_USAGE_CREDITS"})
ODDS_API_ALERT_CODES = ODDS_API_KEY_ERROR_CODES | ODDS_API_CREDIT_ERROR_CODES

_ODDS_KEY_PARAM_RE = re.compile(r"(apiKey=)[^&\s'\"]+", re.IGNORECASE)


def redact_odds_key(text: str) -> str:
    """`text` with any apiKey query value, and the configured key itself, removed."""
    text = _ODDS_KEY_PARAM_RE.sub(r"\1<redacted>", str(text))
    key = os.environ.get("ODDS_API_KEY")
    if key:
        text = text.replace(key, "<redacted>")
    return text


def odds_api_error_code(resp) -> str | None:
    """The `error_code` of an Odds API error body, or None when there is none."""
    try:
        body = resp.json()
    except Exception:
        return None
    code = body.get("error_code") if isinstance(body, dict) else None
    return str(code) if code else None


def describe_odds_api_failure(resp=None, exc=None) -> str:
    """
    One loggable line for a failed Odds API call. It is built ONLY from the HTTP
    status, the body's error_code and the exception TYPE, because every other
    field (resp.url, str(exc)) can carry the key.
    """
    if resp is not None:
        code = odds_api_error_code(resp)
        return f"HTTP {resp.status_code}" + (f" {code}" if code else "")
    if exc is not None:
        return type(exc).__name__
    return "unknown failure"


def _odds_alert_code(resp) -> str | None:
    """The code to alert on, or None when this failure must not alert."""
    if resp is None:
        return None                       # network error/timeout: self-heals
    code = odds_api_error_code(resp)
    if code in ODDS_API_ALERT_CODES:
        return code
    # Fallback: a 401 whose body could not be read is still an auth failure.
    if code is None and resp.status_code == 401:
        return "HTTP 401"
    return None


def note_odds_api_failure(job: str, *, resp=None, exc=None) -> bool:
    """
    Log one failed Odds API call without the URL or key. For a key or credit
    error, also alert the 'usage' channel (once per day).

    Returns True only when an alert was actually sent. Never raises: it runs
    inside the odds fetch, and failure handling must not become a second
    failure.
    """
    try:
        log.warning("Odds API call failed (%s): %s", job, describe_odds_api_failure(resp, exc))
        code = _odds_alert_code(resp)
        if code is None:
            return False
        return _alert_odds_api_failure(job, code, resp)
    except Exception as inner:
        log.error("usage_tracker: Odds API failure handling failed for %s: %s",
                  job, type(inner).__name__)
        return False


def _alert_odds_api_failure(job: str, code: str, resp) -> bool:
    """Post the key/credit alert to 'usage'. Deduped per day like every ops alert."""
    from discord_bot import send_to_discord

    dedup = (date.today().isoformat(), "odds-api-key")
    if dedup in _alerted_failures:
        log.info("usage_tracker: Odds API key alert already sent today — not repeating")
        return False
    _alerted_failures.add(dedup)

    try:
        body = resp.json()
        api_message = body.get("message", "") if isinstance(body, dict) else ""
    except Exception:
        api_message = ""
    api_message = " ".join(redact_odds_key(api_message).split())[:300]

    credits = code in ODDS_API_CREDIT_ERROR_CODES
    heading = ("🛑 **ODDS API OUT OF USAGE CREDITS**" if credits
               else "🛑 **ODDS API REJECTED THE KEY**")
    action = ("Raise the plan or wait for the monthly reset." if credits
              else "Set a working `ODDS_API_KEY` on Railway (and in local `.env`), "
                   "then check it with the free `/v4/sports` call.")
    now = datetime.now(timezone.utc)
    text = (
        f"{heading}\n"
        f"Error: `{code}` (HTTP {resp.status_code})\n"
        f"Job: `{job}`\n"
        f"Time: {now:%d %b %Y %H:%M} UTC\n\n"
        + (f"```{api_message}```\n" if api_message else "")
        + "Until this is fixed, every pick posts and settles at the model's ESTIMATED "
          "odds: no market price, no value flag, and no closing odds, so no CLV. "
          "Nothing else fails, so nothing else will say so.\n"
        + action + " Repeat alerts are suppressed for the rest of today."
    )
    if not send_to_discord("usage", message=text):
        log.error("usage_tracker: could not deliver the Odds API %s alert to Discord ('usage')", code)
        return False
    return True


# ── The Odds API: live quota ─────────────────────────────────────────────────

_odds_quota_cache: tuple[datetime, dict] | None = None
_ODDS_QUOTA_TTL = timedelta(minutes=10)
# Why the most recent quota check failed — {"code", "status", "desc"} — or None
# after a successful one. Read by build_daily_summary to tell a rejected key
# apart from a network blip; both used to print the same "quota unavailable".
_odds_quota_failure: dict | None = None


def fetch_odds_quota(*, force: bool = False) -> dict | None:
    """
    Live Odds API quota from response headers. None if unavailable.

    Uses GET /v4/sports, which is FREE — verified 4 Aug 2026: the call did not
    move x-requests-used. So the hard stop can check the budget without
    spending any of it. Cached briefly so a 15-minute poller doesn't re-check
    on every cycle.

    A failure goes through note_odds_api_failure: logged without the URL, and
    alerted to 'usage' (once a day) when the key or the credits are the cause.
    """
    global _odds_quota_cache, _odds_quota_failure
    now = datetime.now(timezone.utc)
    if not force and _odds_quota_cache and now - _odds_quota_cache[0] < _ODDS_QUOTA_TTL:
        return _odds_quota_cache[1]

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return None
    try:
        resp = requests.get("https://api.the-odds-api.com/v4/sports/",
                            params={"apiKey": api_key}, timeout=10)
    except Exception as exc:
        # Never log str(exc): a requests ConnectionError/Timeout message embeds
        # the full URL, apiKey included. note_odds_api_failure logs the type.
        _odds_quota_failure = {"code": None, "status": None,
                               "desc": describe_odds_api_failure(exc=exc)}
        note_odds_api_failure("odds-quota-check", exc=exc)
        return None
    if not resp.ok:
        _odds_quota_failure = {"code": odds_api_error_code(resp), "status": resp.status_code,
                               "desc": describe_odds_api_failure(resp=resp)}
        note_odds_api_failure("odds-quota-check", resp=resp)
        return None
    try:
        used = int(resp.headers.get("x-requests-used", 0))
        remaining = int(resp.headers.get("x-requests-remaining", 0))
    except (TypeError, ValueError) as exc:
        _odds_quota_failure = {"code": None, "status": resp.status_code,
                               "desc": describe_odds_api_failure(exc=exc)}
        log.warning("usage_tracker: Odds API quota headers unreadable: %s", type(exc).__name__)
        return None
    _odds_quota_failure = None

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
    reserve = odds_api_reserve(quota["limit"])
    if quota["remaining"] < reserve:
        log.warning(
            "usage_tracker: Odds API HARD STOP — %d units remaining is below the "
            "%d-unit reserve (%.0f%% of the %d-unit tier this key reports); polling "
            "halted so the daily picks enrichment keeps working",
            quota["remaining"], reserve, ODDS_API_RESERVE_FRACTION * 100, quota["limit"],
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

def _ago(delta: timedelta) -> str:
    """Compact human age of a timestamp, e.g. '3d 2h', '14m'."""
    secs = int(max(delta.total_seconds(), 0))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _bar(used: int, limit: int, width: int = 20) -> str:
    if not limit:
        return ""
    filled = min(width, round(width * used / limit))
    return "█" * filled + "░" * (width - filled)


def build_daily_summary() -> str:
    """Assemble the daily API usage + cost report. Plain text for Discord."""
    today = date.today()
    now = datetime.now(timezone.utc)
    lines = [f"📊 **API USAGE — {today:%d %b %Y}**", ""]

    # Anthropic health FIRST, above the spend figures. A dead account shows up
    # in the numbers below only as an absence, which is indistinguishable from a
    # quiet day — this line is what makes a silent outage legible without
    # depending on the immediate alert having been seen.
    st = anthropic_status()
    ok, fail = st["last_success"], st["last_failure"]
    if st["healthy"]:
        if ok:
            lines.append(f"**Status** ✅ last Anthropic call OK — `{ok[1]}`, "
                         f"{ok[0]:%d %b %H:%M} UTC ({_ago(now - ok[0])} ago)")
        else:
            lines.append("**Status** — no Anthropic calls recorded yet")
    else:
        lines.append(f"**Status** 🛑 **LAST ANTHROPIC CALL FAILED** — `{fail[1]}`, "
                     f"{fail[0]:%d %b %H:%M} UTC ({_ago(now - fail[0])} ago)")
        lines.append(f"     └ {fail[2][:200]}")
        if ok:
            lines.append(f"     └ last success: {ok[0]:%d %b %H:%M} UTC "
                         f"({_ago(now - ok[0])} ago)")
        else:
            lines.append("     └ no successful call on record")
    lines.append("")

    # Anthropic
    a = anthropic_totals()
    t, m = a["today"], a["month"]
    # Model list is derived, not hardcoded: since 13 Aug 2026 the football and
    # tennis jobs run Sonnet while the Opus 5 shadow runs claude-opus-5 at 1.67x
    # the rate, so a fixed 'claude-sonnet-4-6' heading would have mislabelled
    # the shadow's spend as Sonnet's.
    models_today = sorted(a.get("models_today") or [])
    heading = ", ".join(models_today) if models_today else "no calls today"
    lines.append(f"**Anthropic ({heading})**")
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
    # Football-only since 6 Aug 2026 — tennis makes no Odds API calls
    # (tennis_main.TENNIS_ODDS_API_ENABLED = False).
    lines.append("**The Odds API** (football only — tennis disabled 6 Aug 2026)")
    q = fetch_odds_quota(force=True)
    if q:
        pct_left = q["remaining"] / q["limit"] if q["limit"] else 0
        lines.append(f"  `{_bar(q['used'], q['limit'])}` {q['used']}/{q['limit']} units used")
        lines.append(f"  {q['remaining']} left ≈ **{q['calls_left']} calls** "
                     f"(1 call = {ODDS_API_UNITS_PER_CALL} units)")
        reserve = odds_api_reserve(q["limit"])
        if q["remaining"] < reserve:
            lines.append(f"  🛑 **HARD STOP ACTIVE** — below the {reserve:,}-unit "
                         f"reserve. Closing-odds polling is halted; picks enrichment continues.")
        elif pct_left < ODDS_API_WARN_FRACTION:
            lines.append(f"  ⚠️ **LOW — {pct_left:.0%} remaining** "
                         f"(warning threshold {ODDS_API_WARN_FRACTION:.0%}). "
                         f"Hard stop trips at {reserve:,} units.")
        if q["limit"] < ODDS_API_MONTHLY_LIMIT:
            lines.append(f"  ❗ This key reports a **{q['limit']:,}-unit** tier, but the bot is "
                         f"configured for {ODDS_API_MONTHLY_LIMIT:,}. The paid plan has not "
                         f"reached this key yet — daily caps are sized for the larger tier.")
    else:
        # A rejected key must not read like a network blip. Both printed the
        # same neutral line through the whole 10-16 Nov 2026 outage.
        fail = _odds_quota_failure or {}
        code = fail.get("code")
        if code in ODDS_API_ALERT_CODES or (code is None and fail.get("status") == 401):
            label = code or "HTTP 401"
            what = ("OUT OF USAGE CREDITS" if code in ODDS_API_CREDIT_ERROR_CODES
                    else "KEY REJECTED")
            lines.append(f"  🛑 **{what} — `{label}`**. Picks post and settle at ESTIMATED odds; "
                         f"no closing odds are collected. Fix `ODDS_API_KEY` / the plan.")
        else:
            lines.append("  _quota unavailable_" + (f" ({fail['desc']})" if fail.get("desc") else ""))
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
