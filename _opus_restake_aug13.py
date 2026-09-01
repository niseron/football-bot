"""
_opus_restake_aug13.py — one-off: re-stake the existing 'Opus Shadow Picks' rows
to the new SIM sizing (€1000 start, flat €100 per bet, 13 Aug 2026).

The code change alone only affects rows written from now on: every row already on
the tab carries its own 'Stake EUR (SIM)' figure (€2 under the old half-Kelly
fallback), and recalculate_opus_running_totals rebuilds the bankroll column from
those per-row stakes. Without this backfill the tab would mix €2 and €100 rows in
one bankroll curve.

Touches ONLY the Opus Shadow Picks tab — no football or tennis tab is opened.

    python _opus_restake_aug13.py --dry-run   # print what would change
    python _opus_restake_aug13.py             # write
"""
from __future__ import annotations

import sys

# This file is a completed one-off whose body runs at MODULE level, and that
# body rewrites the Opus Shadow 'Stake EUR (SIM)' column and rebuilds the
# bankroll curve from it. Importing the module would therefore silently redo a
# backfill that is already done. Fail loudly instead — `python -c "import
# _opus_restake_aug13"` must never be a way to overwrite production data.
# (A sibling script, _run_now.py, did exactly this on 1 Sep 2026: an import
# smoke-test fired a full live picks run and posted to subscriber channels.)
if __name__ != "__main__":
    raise RuntimeError(
        "_opus_restake_aug13.py is a one-off script and must not be imported — "
        "importing it rewrites the Opus Shadow stake column. Run it directly: "
        "python _opus_restake_aug13.py [--dry-run]"
    )

from env_loader import load_env

load_env()

from opus_tracker import (  # noqa: E402  (must follow load_env)
    OPUS_FLAT_STAKE,
    OPUS_HEADERS,
    OPUS_STARTING_BANKROLL,
    _opus_ws,
    recalculate_opus_running_totals,
)

dry_run = "--dry-run" in sys.argv

ws = _opus_ws()
rows = ws.get_all_values()
if len(rows) < 2:
    print("Opus Shadow Picks: no data rows — nothing to re-stake.")
    raise SystemExit(0)

header = rows[0]
stake_i = header.index("Stake EUR (SIM)")
stake_col = chr(ord("A") + stake_i)          # single letter: 18 columns, well inside A-Z
print(f"Stake column: {stake_col} (index {stake_i}) of {len(header)} headers")
if header != OPUS_HEADERS:
    print(f"WARNING: header differs from OPUS_HEADERS\n  sheet: {header}\n  code:  {OPUS_HEADERS}")

data_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) if r and r[0]]
print(f"{len(data_rows)} data row(s):")
for i, r in data_rows:
    old = r[stake_i] if len(r) > stake_i else ""
    result = r[6] if len(r) > 6 else ""
    print(f"  row {i:<3} {r[0]:>11}  {r[1][:38]:<38} stake {old or '(blank)':>7} "
          f"-> {OPUS_FLAT_STAKE:.2f}   [{result or 'PENDING'}]")

if dry_run:
    print(f"\n--dry-run: nothing written. Bankroll would rebuild from "
          f"EUR {OPUS_STARTING_BANKROLL:.2f}.")
    raise SystemExit(0)

first, last = data_rows[0][0], data_rows[-1][0]
ws.update(
    values=[[OPUS_FLAT_STAKE] for _ in data_rows],
    range_name=f"{stake_col}{first}:{stake_col}{last}",
    value_input_option="USER_ENTERED",
)
print(f"\nWrote {OPUS_FLAT_STAKE:.2f} to {stake_col}{first}:{stake_col}{last}.")

recalculate_opus_running_totals()
print(f"Running totals + bankroll rebuilt from EUR {OPUS_STARTING_BANKROLL:.2f}.")

for r in _opus_ws().get_all_values()[1:]:
    if r and r[0]:
        print(f"  {r[0]:>11}  {r[1][:34]:<34} {r[6] or 'PENDING':<8} "
              f"P&L {r[7] or '-':>6}  run {r[8] or '-':>7}  bank {r[9] or '-':>8}  "
              f"stake {r[stake_i] or '-'}")
