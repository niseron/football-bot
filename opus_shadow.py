"""
opus_shadow.py — Claude Opus 5 shadow experiment for the football pipeline.

Runs the production Sonnet pipeline's OWN enriched fixture pool through
`claude-opus-5` so the model is the only variable. Same fixtures, same form and
H2H context, same SYSTEM_PROMPT, same 10-pick ranked format and Core/Extended
tiers, same never-pad rule — nothing is re-fetched from RapidAPI, so the
comparison is clean and the fixture-side API cost is exactly zero.

Design mirrors the Fable 5 shadow (12-18 Jul 2026, removed 19 Jul in 35846c9),
which is why the generic hooks it left behind — `run_auto_results`'s
pending_source/row_writer/finalizer and `calibration`'s `ws_getter` — are used
rather than a second evaluation engine.

ISOLATION (the experiment is worthless without it):
  * Picks land only in the 'Opus Shadow Picks' tab (`opus_tracker`).
  * calibration.py's three reports read `excel_tracker._picks_ws` by default and
    are never pointed here, so Opus rows cannot reach calibration, edge or CLV.
  * Nothing here writes the football Picks or Summary tabs, or any Core metric.
  * Every entry point is wrapped so a failure can only ever cost the shadow —
    the production Sonnet picks have already been logged and delivered by the
    time this runs.

COST (measured 13 Aug 2026 on the live 38-fixture slate, not estimated):
Opus 5 is $5 / input MTok and $25 / output MTok, and runs ADAPTIVE THINKING BY
DEFAULT with thinking billed as output — which is ~79% of the cost. Measured
per run: 6,010 in / 1,413 out = $0.065 at effort 'low', / 2,440 = $0.091 at
'medium', / 4,441 = $0.141 at 'high'. `high` is the API default and what this
runs at: throttling the shadow would understate Opus and make the comparison
misleading. That is ~$4.29/month.

Odds: Opus picks different fixtures than Sonnet, so it needs its own market
prices. `enrich_picks_with_real_odds` bills one /odds call (3 units) per unique
fixture, so the shadow adds up to 30 units/day — ~912/month against the
20,000/month tier (4.6%). No closing-odds polling is done for the shadow, so
its CLV column stays empty and football's CLV budget is untouched.

Run manually:  python opus_shadow.py --now   (analyse + deliver, live)
               python opus_shadow.py --dry-run
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

OPUS_MODEL = "claude-opus-5"

# Adaptive thinking is on by default on Opus 5, and max_tokens caps thinking
# PLUS response text together — production's 2048 would truncate mid-answer.
# Measured worst case at effort 'high' was 4,441 output tokens; 16000 leaves
# headroom without being an open cheque.
OPUS_MAX_TOKENS = 16000

# The API default. See the cost note above for why the shadow is not throttled.
OPUS_EFFORT = "high"

# Discord channel key. Also the master gate: while this key is absent from
# DISCORD_CHANNELS_JSON the entire experiment is inert — no model call (so no
# cost), no sheet writes, no Odds API usage. Turning it on or off is a pure
# config change, on Railway and locally.
OPUS_CHANNEL = "opus-shadow"


def opus_enabled() -> bool:
    """True only when the opus-shadow channel key is configured."""
    from discord_bot import DISCORD_CHANNELS
    return bool(DISCORD_CHANNELS.get(OPUS_CHANNEL))


# ── Analysis ─────────────────────────────────────────────────────────────────

def analyse_with_opus(fixtures_by_league: dict[str, list[dict]]) -> list[dict]:
    """
    The production payload and SYSTEM_PROMPT, model swapped to Opus 5.

    Rank and tier are assigned here from array position for the same reason
    main.analyse_with_claude does it: dedup and the cap operate on position, so
    a model-returned rank number could tier a pick differently from where it
    actually sits. Never pads — a short list is the correct outcome.
    """
    # Imported inside the function: main.py imports this module lazily from
    # daily_picks_job, so a module-level back-import would be circular.
    from main import (
        CORE_PICKS_PER_RUN,
        MAX_PICKS_PER_RUN,
        SYSTEM_PROMPT,
        _strip_code_fences,
        claude,
    )
    from excel_tracker import PICK_TIER_CORE, PICK_TIER_EXTENDED
    from usage_tracker import record_anthropic_usage

    _STRIP = {"home_id", "away_id"}
    clean = {
        league: [{k: v for k, v in f.items() if k not in _STRIP} for f in fixtures]
        for league, fixtures in fixtures_by_league.items()
    }
    payload = json.dumps(clean, indent=2, default=str)

    message = claude.messages.create(
        model=OPUS_MODEL,
        max_tokens=OPUS_MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"effort": OPUS_EFFORT},
        messages=[{
            "role": "user",
            "content": f"Upcoming fixtures (next 48 hours):\n\n{payload}",
        }],
    )

    try:
        record_anthropic_usage("opus-shadow", OPUS_MODEL, message.usage)
    except Exception as exc:
        log.warning("opus_shadow: usage recording failed (non-fatal): %s", exc)

    u = message.usage
    log.info(
        "OPUS 5 SHADOW: %d in + %d out tokens (effort=%s) = $%.4f this call",
        u.input_tokens, u.output_tokens, OPUS_EFFORT,
        (u.input_tokens * 5.0 + u.output_tokens * 25.0) / 1_000_000,
    )

    # Opus 5 returns thinking block(s) before the text block, so take the first
    # TEXT block rather than content[0] — the exact bug that broke the Fable
    # shadow on its first live run (fixed in 03b3ff0, 12 Jul 2026).
    raw = next(
        (b.text for b in message.content if getattr(b, "type", "") == "text"), ""
    ).strip()
    if not raw:
        raise ValueError(
            f"Opus 5 returned no text block (stop_reason={message.stop_reason})"
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_strip_code_fences(raw))

    picks = data.get("picks", [])
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for pick in picks:
        key = (pick.get("match"), pick.get("bet_type"))
        if key not in seen:
            seen.add(key)
            deduped.append(pick)

    if len(deduped) > MAX_PICKS_PER_RUN:
        log.warning(
            "opus_shadow: %d picks returned, capping at %d",
            len(deduped), MAX_PICKS_PER_RUN,
        )
        deduped = deduped[:MAX_PICKS_PER_RUN]

    for i, pick in enumerate(deduped, 1):
        pick["rank"] = i
        pick["pick_tier"] = PICK_TIER_CORE if i <= CORE_PICKS_PER_RUN else PICK_TIER_EXTENDED

    n_core = sum(1 for p in deduped if p["pick_tier"] == PICK_TIER_CORE)
    log.info("opus_shadow: %d pick(s) — %d Core, %d Extended",
             len(deduped), n_core, len(deduped) - n_core)
    return deduped


# ── Discord ──────────────────────────────────────────────────────────────────

def _opus_embed(pick: dict) -> dict:
    """
    One Opus pick as a Discord embed, labelled so it can never be mistaken for
    a production pick: the author line names the model and the tier, and the
    stake carries a SIM tag because no Opus pick is staked for real.
    """
    from discord_bot import build_pick_embed

    league = pick.get("league", "")
    tier = pick.get("pick_tier", "")
    rank = pick.get("rank")
    context = f"OPUS 5 SHADOW · {league} · {tier}"
    if rank:
        context += f" #{rank}"

    shown = dict(pick)
    kelly = pick.get("kelly") or {}
    if kelly.get("stake") is not None:
        note = f" ({kelly['note']})" if kelly.get("note") else ""
        shown["stake_display"] = f"EUR {float(kelly['stake']):.2f} SIM{note}"
    return build_pick_embed(shown, context=context)


# ── Entry point called from daily_picks_job ──────────────────────────────────

def run_opus_shadow(
    fixtures_by_league: dict[str, list[dict]],
    kickoff_lookup: dict[str, str],
    *,
    dry_run: bool = False,
) -> list[dict]:
    """
    Analyse the ALREADY-ENRICHED production fixture pool with Opus 5, log the
    picks to the shadow tab, and post them to the opus-shadow channel.

    Returns the picks (empty list when the experiment is off or produced none).
    Callers treat any exception as non-fatal — see the wrapper in main.py.
    """
    if not opus_enabled():
        log.info("opus_shadow: '%s' not configured — skipping (no cost)", OPUS_CHANNEL)
        return []
    if not fixtures_by_league or not any(fixtures_by_league.values()):
        log.info("opus_shadow: empty fixture pool — nothing to analyse (no cost)")
        return []

    from discord_bot import send_to_discord
    from main import enrich_picks_with_real_odds
    from opus_tracker import calculate_opus_kelly_stake, init_opus_sheet, log_opus_pick

    picks = analyse_with_opus(fixtures_by_league)
    if not picks:
        log.info("opus_shadow: Opus returned no picks — nothing to log")
        return []

    # Opus picks different fixtures than Sonnet, so it needs its own market
    # prices. Non-fatal: without them the picks stay Opus-odds-only, exactly as
    # Conference/Europa qualifying already are.
    try:
        enrich_picks_with_real_odds(picks)
    except Exception as exc:
        log.warning("opus_shadow: odds enrichment failed — Opus odds only: %s", exc)

    for pick in picks:
        try:
            pick["kelly"] = calculate_opus_kelly_stake(
                pick["bet_type"], float(pick["odds"]), pick.get("confidence", "")
            )
        except Exception as exc:
            log.warning("opus_shadow: SIM stake failed for %s: %s", pick.get("match"), exc)

    if dry_run:
        print("\n" + "=" * 74)
        print(f"OPUS 5 SHADOW (dry run) — {len(picks)} pick(s), effort={OPUS_EFFORT}")
        print("=" * 74)
        for p in picks:
            k = p.get("kelly") or {}
            print(f'  #{p.get("rank"):<2} [{p.get("pick_tier"):8}] {p["match"]}')
            print(f'      {p["bet_type"]}: {p["pick"]} @ {p["odds"]} '
                  f'({p.get("confidence")}, p={p.get("probability")}, '
                  f'stake=EUR {k.get("stake", "n/a")} SIM)')
        print("=" * 74)
        print("--dry-run: nothing logged, nothing sent.\n")
        return picks

    try:
        init_opus_sheet()
    except Exception as exc:
        log.error("opus_shadow: sheet init failed: %s", exc)

    for pick in picks:
        try:
            claude_prob = pick.get("probability")
            log_opus_pick(
                match=pick["match"],
                league=pick.get("league", ""),
                bet_type=pick["bet_type"],
                pick=pick["pick"],
                odds=float(pick["odds"]),
                confidence=pick.get("confidence", "N/A"),
                claude_prob=float(claude_prob) if claude_prob is not None else None,
                market_prob=pick.get("market_prob"),
                kickoff_utc=kickoff_lookup.get(pick["match"], ""),
                market_odds=pick.get("market_odds"),
                pick_tier=pick.get("pick_tier"),
                stake=(pick.get("kelly") or {}).get("stake"),
            )
        except Exception as exc:
            log.warning("opus_shadow: failed to log %s: %s", pick.get("match"), exc)

    # Text embeds, all leagues, both tiers. No cards — the shadow is a data
    # experiment, not a published product surface.
    sent = 0
    for pick in picks:
        if send_to_discord(OPUS_CHANNEL, embed=_opus_embed(pick)):
            sent += 1
    log.info("opus_shadow: posted %d/%d embed(s) to '%s'", sent, len(picks), OPUS_CHANNEL)
    return picks


# ── Settlement (reuses the production evaluation engine) ─────────────────────

def run_opus_auto_results(lookback_days: int = 2) -> tuple[dict, list[dict]]:
    """
    Settle Opus Shadow rows with the IDENTICAL evaluation logic as production —
    only the tab differs, via run_auto_results' three hooks. Deliberately no
    second engine: the 9-11 Jul 2026 tennis outage came from a bespoke
    settlement path against a results endpoint that could not return finished
    matches, and a copy-paste engine here would be the same class of risk.

    Results go to the SHEET ONLY — the returned 'resolved' list is intentionally
    not delivered anywhere, so nothing settles into a Discord channel.
    """
    if not opus_enabled():
        return {}, []

    from auto_results import run_auto_results
    from opus_tracker import (
        finalize_opus_sheet,
        get_pending_opus_rows,
        update_opus_row_result,
    )

    # alert_scope is load-bearing, not cosmetic: run_auto_results' PENDING-alert
    # dedup sets are module-level and shared with the football pipeline. Without
    # a distinct scope, an Opus PENDING on a fixture Sonnet also picked would
    # consume football's alert slot — and since this caller delivers no alerts,
    # that football row's alert would never be raised anywhere and it could
    # strand silently. Both pipelines analyse the same fixtures with the same
    # prompt, so the collision is expected rather than hypothetical.
    return run_auto_results(
        lookback_days,
        pending_source=get_pending_opus_rows,
        row_writer=update_opus_row_result,
        finalizer=finalize_opus_sheet,
        alert_scope="opus-shadow",
    )


if __name__ == "__main__":
    import sys

    from env_loader import load_env

    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if "--results" in sys.argv:
        stats, resolved = run_opus_auto_results(lookback_days=7)
        print(f"Opus shadow settlement: {stats}")
        for r in resolved:
            print(f"  {r.get('match')} — {r.get('result')} ({r.get('pnl')}u)")
        raise SystemExit(0)

    if "--now" in sys.argv or "--dry-run" in sys.argv:
        from main import (
            _kickoff_lookup,
            enrich_with_context,
            fetch_upcoming_matches,
            partition_fixtures,
        )

        fx = partition_fixtures(fetch_upcoming_matches())
        if not fx:
            print("No upcoming fixtures — nothing to analyse.")
            raise SystemExit(1)
        try:
            enrich_with_context(fx)
        except Exception as exc:
            log.warning("Context enrichment failed — proceeding without: %s", exc)
        run_opus_shadow(fx, _kickoff_lookup(fx), dry_run="--dry-run" in sys.argv)
        raise SystemExit(0)

    print(__doc__)
