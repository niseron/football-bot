"""Single entry point for Railway.

Football jobs: daily picks, live result checks, closing odds, weekly summary.
Tennis jobs:   daily tennis picks, tennis closing odds.

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

from auto_results import _format_result_notification, _telegram_send, run_auto_results
from closing_odds import run_closing_odds_check
from discord_bot import send_to_discord
from main import daily_picks_job
from tennis_auto_results import (
    _format_tennis_result_notification,
    run_tennis_auto_results,
)
from tennis_closing_odds import run_tennis_closing_odds_check
from tennis_main import daily_tennis_picks_job
from tracker import init_db
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


async def closing_odds_job() -> None:
    try:
        await asyncio.to_thread(run_closing_odds_check)
    except Exception as exc:
        log.error("Closing odds check failed (non-fatal): %s", exc)


async def tennis_closing_odds_job() -> None:
    try:
        await asyncio.to_thread(run_tennis_closing_odds_check)
    except Exception as exc:
        log.error("Tennis closing odds check failed (non-fatal): %s", exc)


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


async def main() -> None:
    init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_picks_job, "cron",
        hour=9, minute=0, timezone="Europe/Brussels",
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
        hour=9, minute=30, timezone="Europe/Brussels",
    )
    scheduler.add_job(
        tennis_closing_odds_job, "interval", minutes=15,
    )
    scheduler.add_job(
        tennis_live_results_check, "interval", minutes=30,
    )
    scheduler.start()

    log.info(
        "Scheduler running — football: morning picks 09:00, "
        "weekly summary Mon 09:05, live results every 30 min, "
        "closing odds every 15 min | tennis: picks 09:30, "
        "live results every 30 min, closing odds every 15 min (Europe/Brussels)"
    )

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
