"""
fable_shadow.py — Fable 5 shadow pipeline (football only).

A side-by-side MODEL COMPARISON EXPERIMENT, not production:
  - Runs immediately after the Sonnet 4.6 picks each day, on the exact same
    enriched fixture pool (form, H2H, real odds) — no separate fixture fetch.
  - Same SYSTEM_PROMPT, model swapped to claude-fable-5.
  - Picks go to the 'Fable Picks' sheet tab and the 'fable-picks' Discord
    channel key (fail-silent until the channel ID is configured).
  - Settlement and closing odds reuse the production football logic through
    the source/writer hooks on run_auto_results / run_closing_odds_check.
  - Calibration (Brier / buckets / CLV) is fully separate: fable_calibration.py.

Cost control: Fable 5 is ~$10/$50 per million tokens (5-10x Sonnet 4.6), so
every call logs its exact token usage and dollar cost. Football only — the
tennis pipeline does NOT get a shadow.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

FABLE_MODEL = "claude-fable-5"
_INPUT_COST_PER_MTOK  = 10.0   # USD per 1M input tokens
_OUTPUT_COST_PER_MTOK = 50.0   # USD per 1M output tokens


def analyse_with_fable(fixtures_by_league: dict[str, list[dict]]) -> list[dict]:
    """Same payload and SYSTEM_PROMPT as analyse_with_claude, model swapped to
    Fable 5, with per-call token usage and dollar cost logged."""
    # Imported here, not at module top: main.py lazily imports this module
    # inside daily_picks_job, so a top-level back-import would be circular.
    from main import SYSTEM_PROMPT, _strip_code_fences, claude

    _STRIP = {"home_id", "away_id"}
    clean = {
        league: [{k: v for k, v in f.items() if k not in _STRIP} for f in fixtures]
        for league, fixtures in fixtures_by_league.items()
    }
    payload = json.dumps(clean, indent=2, default=str)
    message = claude.messages.create(
        model=FABLE_MODEL,
        max_tokens=2048,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Upcoming fixtures (next 48 hours):\n\n{payload}"}],
    )

    usage = message.usage
    cost = (usage.input_tokens / 1e6 * _INPUT_COST_PER_MTOK
            + usage.output_tokens / 1e6 * _OUTPUT_COST_PER_MTOK)
    log.info(
        "FABLE 5 COST: %d input + %d output tokens = $%.4f this call "
        "(rates $%.0f/$%.0f per MTok)",
        usage.input_tokens, usage.output_tokens, cost,
        _INPUT_COST_PER_MTOK, _OUTPUT_COST_PER_MTOK,
    )

    raw = message.content[0].text.strip()
    log.info("Fable 5 raw response (%d chars):\n%s", len(raw), raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError as exc:
            # Experiment is non-fatal by design: log loudly, no alert spam
            log.error("Fable 5 response is not valid JSON:\n%s", raw)
            raise ValueError(f"Could not parse Fable 5 response as JSON: {exc}") from exc

    picks = data.get("picks", [])
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for pick in picks:
        key = (pick.get("match"), pick.get("bet_type"))
        if key not in seen:
            seen.add(key)
            deduped.append(pick)
    return deduped


def run_fable_shadow(
    fixtures_by_league: dict[str, list[dict]],
    kickoff_lookup: dict[str, str],
) -> None:
    """Generate, enrich, log, and post Fable 5's picks. Called by
    daily_picks_job right after the Sonnet flow, on the same fixtures.
    Raises nothing fatal upward beyond what the caller's guard catches."""
    from main import enrich_picks_with_real_odds
    from discord_bot import build_pick_embed, send_to_discord
    from fable_tracker import log_fable_pick

    picks = analyse_with_fable(fixtures_by_league)
    if not picks:
        log.info("Fable 5 returned no picks today")
        return
    log.info("Fable 5 returned %d pick(s)", len(picks))

    try:
        enrich_picks_with_real_odds(picks)
    except Exception as exc:
        log.warning("Fable odds enrichment failed — Fable odds only: %s", exc)

    for pick in picks:
        try:
            claude_prob = pick.get("probability")
            log_fable_pick(
                match=pick["match"],
                league=pick.get("league", ""),
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
                claude_prob=float(claude_prob) if claude_prob is not None else None,
                market_prob=pick.get("market_prob"),
                kickoff_utc=kickoff_lookup.get(pick["match"], ""),
            )
        except Exception as exc:
            log.warning("Failed to log Fable pick: %s", exc)

    # Discord — fail-silent until the 'fable-picks' channel ID is configured
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    send_to_discord("fable-picks", message=f"🧪 **Fable 5 Shadow Picks — {today}** (experiment — not the production picks)")
    for pick in picks:
        context = f"Fable 5 experiment | {pick.get('league', '')}"
        send_to_discord("fable-picks", embed=build_pick_embed(pick, context=context))


# ── Settlement + closing odds (reuse production logic via hooks) ──────────────

def run_fable_auto_results(lookback_days: int = 2) -> tuple[dict, list[dict]]:
    """Settle Fable Picks rows with the exact same evaluation logic as the
    production football pipeline — only the tab differs. No notifications,
    no bankroll finalize (the experiment tracks units P&L only)."""
    from auto_results import run_auto_results
    from fable_tracker import get_pending_fable_rows, update_fable_row_result

    return run_auto_results(
        lookback_days,
        pending_source=get_pending_fable_rows,
        row_writer=update_fable_row_result,
        finalizer=lambda: None,
    )


def run_fable_closing_odds_check() -> None:
    """Closing-odds poll for Fable rows — same window/batching/daily request
    cap as production (the Odds API budget is shared deliberately)."""
    from closing_odds import run_closing_odds_check
    from fable_tracker import get_unsettled_fable_with_kickoff, update_fable_closing_odds

    run_closing_odds_check(
        picks_source=get_unsettled_fable_with_kickoff,
        odds_writer=update_fable_closing_odds,
    )
