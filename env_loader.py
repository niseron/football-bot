"""
env_loader.py — .env loading with a self-healing UTF-8 BOM guard.

On 10 Jul 2026 the local .env got saved as "UTF-8 with BOM", which made
python-dotenv read the first line's key as '\\ufeffANTHROPIC_API_KEY' — that
one variable silently failed to load while every other line worked. All
entry points now load .env through load_env() below instead of calling
dotenv.load_dotenv() directly. VS Code keeps re-adding the BOM on save, so
since 19 Jul 2026 load_env() rewrites the file in place without the BOM
(binary rewrite — every other byte untouched) instead of just warning.
encoding='utf-8-sig' stays on the load as a second line of defence in case
the rewrite ever fails (e.g. file locked), so a BOM can never break a
variable either way.

On Railway no .env file exists (variables are injected straight into the
environment) — load_env() is a silent no-op there, exactly like
load_dotenv() was.
"""
from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).with_name(".env")
_UTF8_BOM = b"\xef\xbb\xbf"


def load_env() -> None:
    """Load the repo's .env into os.environ, fixing a UTF-8 BOM in place."""
    if not _ENV_PATH.exists():
        return
    try:
        raw = _ENV_PATH.read_bytes()
        if raw.startswith(_UTF8_BOM):
            _ENV_PATH.write_bytes(raw[len(_UTF8_BOM):])
            log.info(".env had a UTF-8 BOM — rewrote it as plain UTF-8.")
    except OSError:
        pass  # unreadable/locked .env → let load_dotenv surface/skip it as usual
    load_dotenv(_ENV_PATH, encoding="utf-8-sig")
