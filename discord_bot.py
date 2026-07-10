"""
discord_bot.py — Discord delivery layer for the football AND tennis pipelines.

For football this is purely additive: it mirrors the same picks/results/weekly
content that already goes to Telegram. For TENNIS it is the ONLY delivery
channel — tennis never posts to Telegram (user preference: Discord is easier
to view). Send-only, so it talks to Discord's REST API directly via requests —
no discord.py client, no gateway connection, no event loop of its own.

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
    tennis-picks        TENNIS top-tier per-pick embed (both players inside
                        TENNIS_RANK_THRESHOLD), Discord-only  (tennis_main.py)
    tennis-picks-lower  TENNIS lower-tier per-pick embed (either player
                        outside the threshold or unranked)    (tennis_main.py)
    tennis-results      TENNIS settled result text, Discord-only (run_all.py)

Individual pick messages are Discord EMBEDS built by build_pick_embed()
(title = match, colour by confidence, inline Bet Type / Odds / Confidence
fields, reasoning as description, 🔥 VALUE footer). Card and result sends
are unchanged plain text/images.

send_to_discord() NEVER raises: missing token/mapping/key, a bad image path,
or a Discord API failure all log a line and return False, so the existing
Telegram flow can never be broken from here.

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
    market_odds = pick.get("market_odds")
    if market_odds is not None:
        odds_value = f"Claude {pick.get('odds', '?')} | Market {market_odds}"
    else:
        odds_value = str(pick.get("odds", "?"))

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
