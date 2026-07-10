"""
env_loader.py — .env loading with a UTF-8 BOM guard.

On 10 Jul 2026 the local .env got saved as "UTF-8 with BOM", which made
python-dotenv read the first line's key as '\\ufeffANTHROPIC_API_KEY' — that
one variable silently failed to load while every other line worked. All
entry points now load .env through load_env() below instead of calling
dotenv.load_dotenv() directly: encoding='utf-8-sig' strips a leading BOM
when present and is a byte-for-byte no-op otherwise, so a BOM can never
break a variable again. A warning is still logged when one is found —
re-save the file as plain UTF-8 (no BOM) to make it go away.

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
    """Load the repo's .env into os.environ, tolerating a UTF-8 BOM."""
    if not _ENV_PATH.exists():
        return
    try:
        if _ENV_PATH.read_bytes()[:3] == _UTF8_BOM:
            log.warning(
                ".env is saved as 'UTF-8 with BOM' — all variables still "
                "loaded correctly (utf-8-sig), but re-save the file as plain "
                "UTF-8 without BOM so this stops recurring."
            )
    except OSError:
        pass  # unreadable .env → let load_dotenv surface/skip it as usual
    load_dotenv(_ENV_PATH, encoding="utf-8-sig")
