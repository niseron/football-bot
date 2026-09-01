"""
Standalone weekly summary scheduler.
Posts a performance summary to Discord every Monday at 09:05 Europe/Brussels.
On the first Monday of each month, also posts a probability calibration report.

Both go to the 'weekly-cards' channel. Until 18 Aug 2026 both were TELEGRAM-ONLY
and only the card IMAGE reached Discord, so removing Telegram would have taken
the weekly text and the entire monthly calibration report off every surface at
once. They are ported here, not dropped.

The messages are still built in Telegram's MarkdownV2 (that is what the
builders and _esc() emit) and converted at the send boundary by
_to_discord_markdown(). Keeping the builders as they are means the escaping
rules stay in ONE place; the converter is the only thing that knows about
Discord's dialect.

Run alongside main.py:
    python weekly_summary.py
"""
import asyncio
import logging
import re
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from discord_bot import send_long_to_discord, send_to_discord
from env_loader import load_env
from excel_tracker import get_bet_type_breakdown, get_weekly_data

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# MarkdownV2 -> Discord. Telegram bold is *one* asterisk, Discord's is two, and
# MarkdownV2 backslash-escapes a long list of punctuation that Discord renders
# literally — left in place it would show every "\." and "\-" on screen.
# Italics (_x_), inline code and code fences mean the same thing in both, so
# they pass through untouched.
_MD2_BOLD_RE   = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
# Any backslash-escaped PUNCTUATION character, not an enumerated set: the
# builders hand-write escapes beyond what _esc() emits (the calibration report
# contains a literal "\\<"), and each character missed would show on screen as
# a stray backslash. MarkdownV2 only ever escapes punctuation, so this is exact.
_MD2_ESCAPE_RE = re.compile(r"\\([^\w\s])")


def _to_discord_markdown(text: str) -> str:
    """Render a MarkdownV2 message the way Discord expects it."""
    # Bold first: after unescaping, a literal "\*" would look like a delimiter.
    return _MD2_ESCAPE_RE.sub(r"\1", _MD2_BOLD_RE.sub(r"**\1**", text))


def _esc(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _extended_section(ext: dict | None) -> list[str]:
    """
    The Extended-tier block, as MarkdownV2 lines. Empty when there is nothing to
    report, so a Core-only week reads exactly as it always has.

    Reported BESIDE Core, never merged into it: Extended sits outside the staked
    book (no Kelly stake, no bankroll, no running total, and excluded from the
    calibration / edge / CLV series), so a reader who added the two P/L figures
    together would be adding a real book to a paper one. The caveat line says so
    on every send rather than relying on the reader remembering.

    Same arithmetic as the Core block — both come from
    excel_tracker._weekly_tier_stats — so the two win rates are comparable.
    """
    if not ext:
        return []
    total = ext.get("total_picks", 0) or 0
    if not total:
        return []
    pnl  = ext.get("pnl_week", 0.0) or 0.0
    sign = "+" if pnl >= 0 else ""
    return [
        "",
        "*\U0001f9e9 EXTENDED \\- tracked separately*",
        "_Outside the staked book: no stake, no bankroll, and never folded into "
        "the Core figures above\\._",
        f"\U0001f3af *Picks this week:* {total}",
        f"✅ Wins: *{ext.get('wins', 0)}*  ❌ Losses: *{ext.get('losses', 0)}*  "
        f"⏳ Pending: *{ext.get('pending', 0)}*",
        f"\U0001f4c8 Win rate: *{_esc(str(ext.get('win_rate', 0.0)))}%*",
        f"\U0001f4b0 P/L this week: *{_esc(sign + f'{pnl:.2f}')} units*",
    ]


def build_weekly_message(data: dict) -> str:
    if not data:
        return (
            "*ℹ️ Weekly Summary*\n\n"
            "_No picks logged yet\\. The bot will start posting picks once fixtures are available\\._"
        )

    total   = data["total_picks"]
    wins    = data["wins"]
    losses  = data["losses"]
    pending = data["pending"]
    rate    = data["win_rate"]
    pnl_w   = data["pnl_week"]
    rt      = data["running_total"]
    best    = data["best_pick"]
    w_start = data["week_start"]
    w_end   = data["week_end"]

    pnl_sign = "+" if pnl_w >= 0 else ""
    rt_sign  = "+" if rt    >= 0 else ""

    lines = [
        f"*\U0001f4ca Weekly Performance Summary*",
        f"_{_esc(w_start)} \\- {_esc(w_end)}_\n",
    ]

    if total == 0:
        # No CORE picks this week. Extended is still reported if it has any —
        # the two tiers are independent, so an empty Core week does not mean an
        # empty week.
        lines.append("_No Core picks sent this week yet\\._")
        pending_all = data.get("pending_picks", [])
        if pending_all:
            lines.append(f"\n*Pending picks from earlier:*")
            for p in pending_all[:5]:
                lines.append(
                    f"  • {_esc(p['match'])} \\| "
                    f"{_esc(p['bet_type'])} → {_esc(str(p['pick']))} "
                    f"@ {_esc(str(p['odds']))}"
                )
        lines += _extended_section(data.get("extended"))
        return "\n".join(lines)

    lines += [
        "*⭐ CORE \\- the tracked book*",
        f"\U0001f3af *Picks this week:* {total}",
        f"✅ Wins: *{wins}*  ❌ Losses: *{losses}*  ⏳ Pending: *{pending}*",
        f"\U0001f4c8 Win rate: *{_esc(str(rate))}%*",
        f"\U0001f4b0 P/L this week: *{_esc(pnl_sign + f'{pnl_w:.2f}')} units*",
    ]

    if best:
        best_pnl  = best.get("pnl", 0) or 0
        best_odds = best.get("odds", 0) or 0
        lines.append(
            f"\U0001f3c6 Best pick: _{_esc(str(best['match']))} "
            f"— {_esc(str(best['pick']))} @ {_esc(str(best_odds))} "
            f"\\({_esc('+' + f'{best_pnl:.2f}')} units\\)_"
        )
    elif pending > 0:
        lines.append(f"⏳ *{pending} pick\\(s\\) still pending result*")

    lines.append(f"\U0001f4c9 Running total P/L: *{_esc(rt_sign + f'{rt:.2f}')} units*")

    lines += _extended_section(data.get("extended"))

    breakdown = get_bet_type_breakdown()
    if breakdown:
        lines.append("\n*\U0001f4cb Bet Type Breakdown \\(Core, all\\-time settled\\)*")
        lines.append("`{:<22} {:>4}  {:>5}  {:>5}  {:>6}  {:>7}`".format(
            "Bet Type", "W", "L", "WR%", "Picks", "P/L"
        ))
        for b in breakdown:
            pnl_str = ("+" if b["pnl"] >= 0 else "") + f"{b['pnl']:.2f}"
            wr_str  = f"{b['win_rate']:.1f}%"
            name    = b["bet_type"][:22]
            lines.append("`{:<22} {:>4}  {:>5}  {:>5}  {:>6}  {:>7}`".format(
                name, b["wins"], b["losses"], wr_str, b["total"], pnl_str
            ))

    lines.append("\n_Bet responsibly\\. Past performance does not guarantee future results\\._")

    return "\n".join(lines)


def build_calibration_message() -> str | None:
    """
    Monthly calibration report message (MarkdownV2). None when there is no
    probability data yet or the report can't be built — caller skips posting.
    """
    from calibration import MIN_MEANINGFUL_SAMPLE, calibration_report, clv_report, edge_report

    cal = calibration_report()
    if not cal or cal["sample_size"] == 0:
        return None

    lines = [
        "*\U0001f4d0 Monthly Calibration Report*",
        "_How well our stated probabilities match reality_\n",
        "`{:<9} {:>5}  {:>7}  {:>7}`".format("Range", "Picks", "Stated", "Actual"),
    ]
    for b in cal["buckets"]:
        if not b["picks"]:
            continue
        lines.append("`{:<9} {:>5}  {:>6}%  {:>6}%`".format(
            b["range"], b["picks"], b["avg_stated"], b["actual_win_rate"]
        ))

    if cal["brier_score"] is not None:
        lines.append(f"\n\U0001f3af Brier score: *{_esc(str(cal['brier_score']))}* \\(lower is better; 0\\.25 \\= coin flip\\)")

    edge = edge_report()
    if edge and edge["sample_size"]:
        def _fmt_edge(v):
            return _esc(f"{v:+.1f}pp") if v is not None else "n/a"
        pos, neg = edge["positive_edge"], edge["negative_edge"]
        lines.append(
            f"\n*Edge analysis* \\({edge['sample_size']} picks with market odds\\)\n"
            f"  Avg edge on winners: *{_fmt_edge(edge['avg_edge_winners'])}*\n"
            f"  Avg edge on losers: *{_fmt_edge(edge['avg_edge_losers'])}*"
        )
        if pos["roi"] is not None:
            pos_roi = _esc(f"{pos['roi']:+.1f}%")
            lines.append(f"  ROI when our prob \\> market: *{pos_roi}* \\({pos['picks']} picks\\)")
        if neg["roi"] is not None:
            neg_roi = _esc(f"{neg['roi']:+.1f}%")
            lines.append(f"  ROI when our prob \\<\\= market: *{neg_roi}* \\({neg['picks']} picks\\)")

    n = cal["sample_size"]
    lines.append(f"\n\U0001f4e6 Sample size: *{n}* settled picks with probability data")
    if not cal["meaningful"]:
        lines.append(
            f"⚠️ _Results are not statistically meaningful below {MIN_MEANINGFUL_SAMPLE} settled picks\\. "
            f"Treat these numbers as directional only\\._"
        )

    clv = clv_report()
    if clv and clv["sample_size"]:
        avg_clv_str = f"{clv['avg_clv']:+.1f}%"
        pos_pct_str = f"{clv['pct_positive']:.1f}%"
        lines.append(
            f"\n*\U0001f4c9 Closing Line Value \\(CLV\\)*\n"
            f"_How much the price moved after the pick was made — the true edge signal_\n"
            f"  Avg CLV: *{_esc(avg_clv_str)}*\n"
            f"  Picks with positive CLV: *{_esc(pos_pct_str)}*"
        )
        pos_clv, neg_clv = clv["positive_clv_roi"], clv["negative_clv_roi"]
        if pos_clv["roi"] is not None:
            pos_clv_roi_str = f"{pos_clv['roi']:+.1f}%"
            lines.append(f"  ROI on positive\\-CLV picks: *{_esc(pos_clv_roi_str)}* \\({pos_clv['picks']} picks\\)")
        if neg_clv["roi"] is not None:
            neg_clv_roi_str = f"{neg_clv['roi']:+.1f}%"
            lines.append(f"  ROI on negative\\-CLV picks: *{_esc(neg_clv_roi_str)}* \\({neg_clv['picks']} picks\\)")

        n_clv = clv["sample_size"]
        lines.append(f"  Sample size: *{n_clv}* settled picks with closing odds")
        if not clv["meaningful"]:
            lines.append(
                f"⚠️ _CLV results are not statistically meaningful below {MIN_MEANINGFUL_SAMPLE} settled picks\\. "
                f"Treat these numbers as directional only\\._"
            )

    return "\n".join(lines)


def _is_first_monday_of_month() -> bool:
    today = date.today()
    return today.weekday() == 0 and today.day <= 7


async def post_weekly_summary():
    log.info("Building weekly summary...")
    data    = get_weekly_data()
    message = build_weekly_message(data)

    if not await asyncio.to_thread(
        send_long_to_discord, "weekly-cards", _to_discord_markdown(message)
    ):
        log.error("Weekly summary text was not delivered to Discord ('weekly-cards')")

    try:
        from card_generator import generate_weekly_card
        card_path = generate_weekly_card(data)
        await asyncio.to_thread(send_to_discord, "weekly-cards", None, card_path)
        log.info("Weekly card sent: %s", card_path.name)
    except Exception as exc:
        log.warning("Weekly card failed (non-fatal): %s", exc)

    # First Monday of the month: append the probability calibration report
    try:
        if _is_first_monday_of_month():
            cal_message = build_calibration_message()
            if cal_message:
                if not await asyncio.to_thread(
                    send_long_to_discord,
                    "weekly-cards",
                    _to_discord_markdown(cal_message),
                ):
                    log.error("Monthly calibration report was not delivered to Discord")
                else:
                    log.info("Monthly calibration report posted")
            else:
                log.info("Monthly calibration skipped — no probability data yet")
    except Exception as exc:
        log.warning("Monthly calibration report failed (non-fatal): %s", exc)

    log.info("Weekly summary posted to Discord")


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        post_weekly_summary,
        "cron",
        day_of_week="mon",
        hour=9,
        minute=5,
        timezone="Europe/Brussels",
    )
    scheduler.start()
    log.info("Weekly summary scheduler started — posts every Monday at 09:05 Europe/Brussels")

    # Uncomment to test immediately on startup:
    # await post_weekly_summary()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
