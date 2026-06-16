"""Single entry point for Railway: daily picks, live result checks, weekly summary."""
import asyncio
import logging

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from auto_results import _format_result_notification, _telegram_send, run_auto_results
from main import daily_picks_job
from tracker import init_db
from weekly_summary import post_weekly_summary

_notified: set[tuple] = set()


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
        _notified.add(key)


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
    scheduler.start()

    log.info("Scheduler running — daily picks 09:00, weekly summary Mon 09:05, live results every 30 min (Europe/Brussels)")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
