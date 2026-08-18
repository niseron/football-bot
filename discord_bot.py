"""
discord_bot.py — THE delivery layer for the football AND tennis pipelines.

Discord is the only delivery surface. Telegram was removed entirely on
18 Aug 2026 (tennis never used it); nothing in this repo posts anywhere else,
so a send that fails here is not "additive" or "a mirror" — it is the delivery
failing, and every caller should read it that way. Send-only, so it talks to
Discord's REST API directly via requests — no discord.py client, no gateway
connection, no event loop of its own.

Config (both must be set for any send to happen):
    DISCORD_BOT_TOKEN     — bot token from the Discord Developer Portal
    DISCORD_CHANNELS_JSON — single-line JSON dict mapping channel keys to
                            Discord channel IDs, e.g.
                            {"picks-cards": "111...", "premier-league": "222..."}

Channel keys used by the pipeline (any key may be omitted — it is skipped):
    picks-cards         daily picks PNG card, plus the IG-variant card
                        (both land here every run, intentional)  (main.py)
    results-cards       results PNG card                (auto_results.py)
    weekly-cards        weekly summary PNG card         (weekly_summary.py)
    premier-league      per-pick embed                  (main.py)
    jupiler-pro-league  per-pick embed                  (main.py)
    world-cup           per-pick embed                  (main.py)
    bundesliga          per-pick embed                  (main.py)
    la-liga             per-pick embed                  (main.py)
    serie-a             per-pick embed                  (main.py)
    ligue-1             per-pick embed                  (main.py)
    champions-league    per-pick embed                  (main.py)
    europa-league       per-pick embed                  (main.py)
    conference-league   per-pick embed                  (main.py)
    tennis-picks        TENNIS top-tier per-pick embed (both players inside
                        TENNIS_RANK_THRESHOLD), Discord-only  (tennis_main.py)
    tennis-picks-lower  TENNIS lower-tier per-pick embed (either player
                        outside the threshold or unranked)    (tennis_main.py)
    tennis-results      TENNIS settled result text               (run_all.py)
    usage               daily API usage / cost summary        (usage_tracker.py)

Long reports (the weekly summary, the monthly calibration report) go through
send_long_to_discord(), which splits on line boundaries. send_to_discord()
TRUNCATES at 2000 characters, which is right for a stray oversized pick embed
and wrong for a report — a silently half-posted performance report is exactly
the kind of quiet failure this pipeline keeps getting bitten by.

Individual pick messages are Discord EMBEDS built by build_pick_embed()
(title = match, colour by confidence, inline Bet Type / Odds / Confidence
fields, reasoning as description, 🔥 VALUE footer). Card and result sends
are unchanged plain text/images.

send_to_discord() NEVER raises: missing token/mapping/key, a bad image path,
or a Discord API failure all log a line and return False. That contract predates
Discord being the only surface and is kept deliberately — one unreachable
channel must not abort a run that still has other channels to deliver to. The
trade-off is that callers who need to KNOW whether delivery happened have to
check the return value; the picks-failed alert does exactly that.

Test all configured channels (sends a text + image to each):
    python discord_bot.py --test
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

from env_loader import load_env

load_env()

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
_MAX_CONTENT_LEN = 2000  # Discord message content hard limit

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()


def _parse_channel_map() -> dict[str, str]:
    raw = os.environ.get("DISCORD_CHANNELS_JSON", "").strip()
    if not raw:
        return {}
    try:
        mapping = json.loads(raw)
        return {str(k): str(v) for k, v in mapping.items() if v}
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        log.warning("DISCORD_CHANNELS_JSON is not valid JSON — Discord delivery disabled: %s", exc)
        return {}


DISCORD_CHANNELS: dict[str, str] = _parse_channel_map()

# Embed stripe colours by pick confidence: High green, Medium blue,
# Low orange. Colours only affect the embed's left-side stripe — Discord
# embeds have no per-field text colouring.
_EMBED_COLORS = {"high": 0x00C853, "medium": 0x2196F3, "low": 0xFF6F00}


def build_pick_embed(pick: dict, context: str | None = None) -> dict:
    """
    One pick as a Discord embed dict: title = match, colour by confidence,
    the selection as a full-width field, Bet Type / Odds / Confidence as
    inline fields, the full reasoning as description, and a 🔥 VALUE footer
    when the pick beat the market. `context` (e.g. tennis's
    'ATP | Wimbledon | Grass') renders as the small author line on top.
    """
    confidence = str(pick.get("confidence", "N/A"))
    # ONE odds figure, never two: the real market price when we matched one,
    # otherwise the estimate. Both values are still written to the sheet
    # ('Claude Prob %' / 'Market Prob %') — calibration and CLV need them; this
    # is display only. Nothing user-facing names the model.
    market_odds = pick.get("market_odds")
    odds_value = str(market_odds if market_odds is not None else pick.get("odds", "?"))

    embed: dict = {
        "title": str(pick.get("match", "?"))[:256],
        "color": _EMBED_COLORS.get(confidence.lower(), _EMBED_COLORS["low"]),
        "description": str(pick.get("reasoning", "") or ""),
        "fields": [
            {"name": "Pick", "value": f"**{pick.get('pick', '?')}**", "inline": False},
            {"name": "Bet Type", "value": str(pick.get("bet_type", "?")), "inline": True},
            {"name": "Odds", "value": odds_value, "inline": True},
            {"name": "Confidence", "value": confidence, "inline": True},
        ],
    }
    # Optional pre-formatted stake line (tennis sets 'stake_display' with its
    # SIM tag — simulated Kelly stakes; football embeds carry no stake field)
    if pick.get("stake_display"):
        embed["fields"].append(
            {"name": "Stake", "value": str(pick["stake_display"]), "inline": True}
        )
    if context:
        embed["author"] = {"name": context[:256]}
    if pick.get("value"):
        embed["footer"] = {"text": "🔥 VALUE"}
    return embed


def send_to_discord(
    channel_key: str,
    message: str | None = None,
    image_path=None,
    embed: dict | None = None,
) -> bool:
    """
    Post text, an image, and/or an embed (a dict matching Discord's embed
    JSON schema — see build_pick_embed) to the Discord channel mapped to
    channel_key. Returns True on success, False on any skip or failure.
    Never raises.
    """
    try:
        if not DISCORD_BOT_TOKEN or not DISCORD_CHANNELS:
            log.info("Discord not configured — skipping '%s'", channel_key)
            return False
        channel_id = DISCORD_CHANNELS.get(channel_key)
        if not channel_id:
            log.info("Discord channel key '%s' not mapped — skipping", channel_key)
            return False
        if message is None and image_path is None and embed is None:
            log.info("Discord send to '%s' skipped — nothing to send", channel_key)
            return False

        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
        payload: dict = {"content": (message or "")[:_MAX_CONTENT_LEN]}
        if embed is not None:
            payload["embeds"] = [embed]

        for attempt in (1, 2):
            if image_path is not None:
                with open(image_path, "rb") as f:
                    resp = requests.post(
                        url,
                        headers=headers,
                        data={"payload_json": json.dumps(payload)},
                        files={"files[0]": (Path(image_path).name, f, "image/png")},
                        timeout=30,
                    )
            else:
                resp = requests.post(url, headers=headers, json=payload, timeout=15)

            if resp.status_code == 429 and attempt == 1:
                # Rate limited — wait what Discord asks (capped) and retry once
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                time.sleep(min(retry_after, 10.0))
                continue

            resp.raise_for_status()
            log.info("Discord: sent to '%s'%s", channel_key, " (image)" if image_path else "")
            return True
        return False
    except Exception as exc:
        log.warning("Discord send to '%s' failed (non-fatal): %s", channel_key, exc)
        return False


def send_long_to_discord(channel_key: str, text: str) -> bool:
    """
    Post text of any length, split across as many messages as it takes.

    send_to_discord() truncates at _MAX_CONTENT_LEN. For a pick embed that is a
    sane guard; for the weekly summary or the monthly calibration report it
    would silently drop the tail, and a report that quietly loses its last
    section is worse than one that never posted. Splits on line boundaries so a
    table row or a stat line is never cut in half, and falls back to a hard
    character split only for a single line longer than the limit.

    Returns True only if EVERY chunk was delivered.
    """
    if not text:
        return False

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > _MAX_CONTENT_LEN:
            # Pathological single line — hard-split it.
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:_MAX_CONTENT_LEN])
            line = line[_MAX_CONTENT_LEN:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > _MAX_CONTENT_LEN:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    ok = True
    for i, chunk in enumerate(chunks):
        if i:
            # Same pacing rationale as the pick embeds: Discord allows roughly
            # one sustained message per second per channel and send_to_discord
            # spends its single 429 retry immediately.
            time.sleep(1)
        if not send_to_discord(channel_key, message=chunk):
            ok = False
    if len(chunks) > 1:
        log.info("Discord: '%s' report sent as %d message(s)", channel_key, len(chunks))
    return ok


# ── Channel test ──────────────────────────────────────────────────────────────

def _make_test_image() -> Path:
    """Small PNG used by --test so image posting is exercised end-to-end."""
    from PIL import Image, ImageDraw

    out_dir = Path(__file__).parent / "cards"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "discord_test.png"

    img = Image.new("RGB", (640, 360), "#0a0a0a")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 629, 349], outline="#39ff14", width=4)
    draw.text((40, 150), "Football Picks Bot — Discord test image", fill="#39ff14")
    img.save(path)
    return path


def test_all_channels() -> bool:
    """Send a test text + test image to every configured channel key. True if all succeed."""
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNELS:
        print("Discord not configured — set DISCORD_BOT_TOKEN and DISCORD_CHANNELS_JSON first.")
        return False

    image = _make_test_image()
    ok = failed = 0
    for key in DISCORD_CHANNELS:
        sent = send_to_discord(
            key,
            message=f"✅ Test from Football Picks Bot — channel key `{key}` is wired up correctly.",
            image_path=image,
        )
        status = "OK " if sent else "FAIL"
        print(f"  [{status}] {key} -> {DISCORD_CHANNELS[key]}")
        ok += sent
        failed += not sent
        time.sleep(1)  # stay clear of per-route rate limits

    print(f"\n{ok} succeeded, {failed} failed out of {len(DISCORD_CHANNELS)} configured channel(s).")
    return failed == 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--test" in sys.argv:
        raise SystemExit(0 if test_all_channels() else 1)
    print(__doc__)
