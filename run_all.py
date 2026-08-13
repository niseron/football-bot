"""Single entry point for Railway.

Football jobs: daily picks, live result checks, closing odds, weekly summary.
Tennis jobs:   daily tennis picks, tennis live result checks.

Tennis has NO closing-odds job: The Odds API was switched off for tennis on
6 Aug 2026 (see tennis_main.TENNIS_ODDS_API_ENABLED) so all Odds API units go
to football. Football keeps its closing-odds poller unchanged.

The tennis jobs are a fully separate system (tennis_main / tennis_excel_tracker /
tennis_closing_odds / tennis_calibration): they share this process and scheduler
but no data paths, sheet tabs, calibration samples, or request budgets with the
football jobs.
"""
import asyncio
import logging

from env_loader import load_env

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from auto_results import (
    _format_pending_notification,
    _format_result_notification,
    _telegram_send,
    run_auto_results,
)
from closing_odds import run_closing_odds_check
from discord_bot import send_to_discord
from main import daily_picks_job
from tennis_auto_results import (
    _format_tennis_result_notification,
    run_tennis_auto_results,
)
from tennis_main import daily_tennis_picks_job
from tracker import init_db
from usage_tracker import post_daily_summary as post_usage_summary
from weekly_summary import post_weekly_summary

_notified: set[tuple] = set()
_tennis_notified: set[tuple] = set()


async def live_results_check() -> None:
    log.info("Running live results check...")
    try:
        stats, resolved = await asyncio.to_thread(run_auto_results, 2)
    except Exception as exc:
        log.error("Live results check failed: %s", exc)
        return
    for r in resolved:
        key = (r["match"], r["bet_type"], r["pick"])
        if key in _notified:
            continue
        msg = _format_result_notification(r)
        log.info("Sending result notification: %s | %s", r["match"], r["result"])
        await asyncio.to_thread(_telegram_send, msg)
        # Discord mirror — same trigger, same text; send_to_discord never raises
        await asyncio.to_thread(send_to_discord, "results-cards", msg)
        _notified.add(key)

    # Picks evaluate_pick() could not settle. Discord-only and deliberately
    # louder than a log line: before 12 Aug 2026 these were silent and a row
    # could age out of the lookback window unsettled. run_auto_results already
    # decides what to announce (first sighting, then again 24h after kickoff)
    # and de-duplicates, so everything here just gets sent.
    for p in stats.get("pending_alerts", []):
        log.warning("PENDING pick needs manual settlement (%s): %s | %s — %s",
                    p["stage"], p["match"], p["pick"], p["reason"])
        await asyncio.to_thread(
            send_to_discord, "results-cards", _format_pending_notification(p)
        )


async def closing_odds_job() -> None:
    try:
        await asyncio.to_thread(run_closing_odds_check)
    except Exception as exc:
        log.error("Closing odds check failed (non-fatal): %s", exc)


async def usage_summary_job() -> None:
    """Daily API usage + cost report to the 'usage' Discord channel."""
    try:
        await asyncio.to_thread(post_usage_summary)
    except Exception as exc:
        log.error("Usage summary failed (non-fatal): %s", exc)


async def tennis_live_results_check() -> None:
    """Tennis mirror of live_results_check — fully independent of the football job."""
    log.info("Running tennis live results check...")
    try:
        stats, resolved = await asyncio.to_thread(run_tennis_auto_results)
    except Exception as exc:
        log.error("Tennis live results check failed: %s", exc)
        return
    for r in resolved:
        key = (r["match"], r["bet_type"], r["pick"])
        if key in _tennis_notified:
            continue
        msg = _format_tennis_result_notification(r)
        log.info("Sending tennis result notification: %s | %s", r["match"], r["result"])
        # Tennis is Discord-ONLY (no Telegram, own channel — never the
        # football 'results-cards'); send_to_discord never raises
        await asyncio.to_thread(send_to_discord, "tennis-results", msg)
        _tennis_notified.add(key)


async def opus_shadow_results_check() -> None:
    """
    Settle Opus 5 shadow rows. SHEET ONLY — unlike the football and tennis
    result checks above, nothing is delivered anywhere: the shadow is a data
    experiment, so `resolved` is deliberately dropped rather than posted.

    Inert while the 'opus-shadow' channel key is unset (run_opus_auto_results
    returns immediately), so this job costs nothing when the experiment is off.
    """
    try:
        from opus_shadow import run_opus_auto_results
        stats, resolved = await asyncio.to_thread(run_opus_auto_results, 2)
        if stats:
            log.info(
                "Opus shadow settlement: checked=%s updated=%s pending=%s errors=%s",
                stats.get("checked"), stats.get("updated"),
                stats.get("pending"), stats.get("errors"),
            )
            # `resolved` is deliberately dropped — the shadow settles to the
            # sheet only. PENDING rows are NOT dropped: a silently stranded row
            # is how the shadow's data would quietly rot, so they are logged
            # loudly here. They stay out of Discord (no alert channel for the
            # experiment) but are visible in the Railway logs.
            for a in stats.get("pending_alerts") or []:
                log.warning(
                    "Opus shadow PENDING (%s) row %s: %s — %s [%s] score %s — %s",
                    a.get("stage"), a.get("sheet_row"), a.get("match"),
                    a.get("pick"), a.get("bet_type"), a.get("score"), a.get("reason"),
                )
    except Exception as exc:
        log.warning("Opus shadow results check failed (non-fatal): %s", exc)


async def main() -> None:
    init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_picks_job, "cron",
        hour=12, minute=0, timezone="Europe/Brussels",
    )
    scheduler.add_job(
        post_weekly_summary, "cron",
        day_of_week="mon", hour=9, minute=5, timezone="Europe/Brussels",
    )
    scheduler.add_job(
        live_results_check, "interval", minutes=30,
    )
    scheduler.add_job(
        closing_odds_job, "interval", minutes=15,
    )
    # Tennis system — independent jobs, never intermixed with the football ones
    scheduler.add_job(
        daily_tennis_picks_job, "cron",
        hour=12, minute=30, timezone="Europe/Brussels",
    )
    # No tennis closing-odds job — Odds API disabled for tennis 6 Aug 2026
    scheduler.add_job(
        tennis_live_results_check, "interval", minutes=30,
    )
    # Opus 5 shadow settlement — sheet-only, no delivery. Same 30-min cadence
    # as football's and registered back-to-back, so in practice the two fire in
    # the SAME scheduler pass and run concurrently. That is safe rather than
    # merely tolerable: run_auto_results' shared PENDING-alert state is
    # namespaced per alert_scope, so neither run can consume the other's alert
    # slot no matter which thread gets there first.
    scheduler.add_job(
        opus_shadow_results_check, "interval", minutes=30,
    )
    # Usage + cost report — 23:50 so it captures a full day of both pipelines
    scheduler.add_job(
        usage_summary_job, "cron",
        hour=23, minute=50, timezone="Europe/Brussels",
    )
    scheduler.start()

    log.info(
        "Scheduler running — football: daily picks 12:00, "
        "weekly summary Mon 09:05, live results every 30 min, "
        "closing odds every 15 min | tennis: picks 12:30, "
        "live results every 30 min, NO closing odds (Odds API off for "
        "tennis) (Europe/Brussels)"
    )

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
