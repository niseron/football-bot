import sqlite3
from datetime import date
from pathlib import Path

from excel_tracker import log_to_excel

DB_PATH = Path(__file__).parent / "picks.db"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS picks (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT    NOT NULL,
                match     TEXT    NOT NULL,
                league    TEXT    NOT NULL,
                bet_type  TEXT    NOT NULL,
                pick      TEXT    NOT NULL,
                odds      REAL    NOT NULL,
                result    TEXT    DEFAULT 'PENDING',
                profit    REAL    DEFAULT NULL,
                session   TEXT    NOT NULL DEFAULT 'morning',
                pick_tier TEXT    NOT NULL DEFAULT 'Core'
            )
        """)
        # Migrate existing DBs that predate the session column
        try:
            conn.execute("ALTER TABLE picks ADD COLUMN session TEXT NOT NULL DEFAULT 'morning'")
        except Exception:
            pass
        # Migrate existing DBs that predate the pick_tier column (13 Aug 2026).
        # Defaulting to 'Core' matches the Sheet's blank-is-Core rule, so rows
        # written before tiers existed keep their original meaning.
        try:
            conn.execute("ALTER TABLE picks ADD COLUMN pick_tier TEXT NOT NULL DEFAULT 'Core'")
        except Exception:
            pass
        conn.commit()


def log_pick(
    match: str,
    league: str,
    bet_type: str,
    pick: str,
    odds: float,
    pick_date: str | None = None,
    confidence: str = "N/A",
    session: str = "morning",
    claude_prob: float | None = None,
    market_prob: float | None = None,
    kickoff_utc: str | None = None,
    market_odds: float | None = None,
    pick_tier: str = "Core",
):
    import logging as _logging
    _log = _logging.getLogger(__name__)
    init_db()
    pick_date = pick_date or date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT id FROM picks WHERE date = ? AND match = ? AND bet_type = ? AND pick = ? AND session = ?",
            (pick_date, match, bet_type, pick, session),
        ).fetchone()
        if existing:
            _log.info("Skipping duplicate pick (already in DB): %s — %s", match, pick)
            return
        conn.execute(
            "INSERT INTO picks (date, match, league, bet_type, pick, odds, session, pick_tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pick_date, match, league, bet_type, pick, odds, session, pick_tier),
        )
        conn.commit()
    try:
        log_to_excel(match, league, bet_type, pick, odds, confidence, pick_date,
                     claude_prob=claude_prob, market_prob=market_prob,
                     kickoff_utc=kickoff_utc, market_odds=market_odds,
                     pick_tier=pick_tier)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Excel log failed: %s", exc)


def log_picks_batch(entries: list[dict], session: str = "morning"):
    """
    Log a whole run's picks: one local SQLite insert each, then ONE Google
    Sheets batch for the lot.

    Returns excel_tracker.BatchLogResult — written / skipped / failed across
    BOTH guards. The local SQLite duplicate guard counts as 'skipped' (it is a
    deliberate drop, same as the sheet's fixture guard); anything the sheet
    could not write counts as 'failed' and the caller must alert on it.

    Same guards and same defaults as log_pick — the only difference is the
    number of sheet round-trips, which is what made this necessary once a run
    could produce 30+ picks instead of 10 (per-league cap, 15 Aug 2026).
    log_pick costs a full sheet read plus a full repaint PER PICK.

    Entries are written in list order, so pass Core first: excel_tracker's
    fixture guard then resolves any collision in Core's favour.
    """
    import logging as _logging
    from excel_tracker import BatchLogResult
    from excel_tracker import log_picks_batch as _sheet_batch

    _log = _logging.getLogger(__name__)
    init_db()
    today = date.today().isoformat()

    accepted: list[dict] = []
    db_skipped = 0
    # Same contract as excel_tracker's guard: a deliberate drop is identified,
    # not just counted, so the delivery layer can withhold it from Discord too.
    db_skipped_keys: set[tuple[str, str, str]] = set()
    with sqlite3.connect(DB_PATH) as conn:
        for e in entries:
            pick_date = e.get("pick_date") or today
            existing = conn.execute(
                "SELECT id FROM picks WHERE date = ? AND match = ? AND bet_type = ? "
                "AND pick = ? AND session = ?",
                (pick_date, e["match"], e["bet_type"], e["pick"], session),
            ).fetchone()
            if existing:
                _log.info("Skipping duplicate pick (already in DB): %s — %s",
                          e["match"], e["pick"])
                db_skipped += 1
                db_skipped_keys.add((e["match"], e["bet_type"], e["pick"]))
                continue
            conn.execute(
                "INSERT INTO picks (date, match, league, bet_type, pick, odds, session, pick_tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pick_date, e["match"], e.get("league", ""), e["bet_type"], e["pick"],
                 e["odds"], session, e.get("pick_tier", "Core")),
            )
            accepted.append({**e, "pick_date": pick_date})
        conn.commit()

    if not accepted:
        return BatchLogResult(0, db_skipped, 0, frozenset(db_skipped_keys))
    try:
        r = _sheet_batch(accepted)
        return BatchLogResult(
            r.written, r.skipped + db_skipped, r.failed,
            r.skipped_keys | frozenset(db_skipped_keys),
        )
    except Exception as exc:
        # _sheet_batch handles its own errors, so reaching here means something
        # unexpected — every accepted entry is lost and none of it was deliberate.
        _log.warning("Excel batch log failed: %s", exc)
        # `accepted` are failures, not deliberate drops — they stay publishable.
        return BatchLogResult(0, db_skipped, len(accepted), frozenset(db_skipped_keys))


def picks_exist_for_today() -> bool:
    init_db()
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM picks WHERE date = ?", (today,)
        ).fetchone()[0]
    return count > 0


def picks_exist_for_session(session: str) -> bool:
    """Return True if picks for today's date and the given session already exist."""
    init_db()
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM picks WHERE date = ? AND session = ?",
            (today, session),
        ).fetchone()[0]
    return count > 0


def update_result(pick_id: int, result: str, stake: float = 1.0):
    """Update a pick result. result must be 'WIN', 'LOSS', or 'VOID'."""
    with sqlite3.connect(DB_PATH) as conn:
        if result == "WIN":
            profit = (stake * odds) - stake if (odds := _get_odds(conn, pick_id)) else 0
        elif result == "LOSS":
            profit = -stake
        else:
            profit = 0.0
        conn.execute(
            "UPDATE picks SET result = ?, profit = ? WHERE id = ?",
            (result, profit, pick_id),
        )
        conn.commit()


def _get_odds(conn: sqlite3.Connection, pick_id: int) -> float | None:
    row = conn.execute("SELECT odds FROM picks WHERE id = ?", (pick_id,)).fetchone()
    return row[0] if row else None


def get_pending_picks() -> list[dict]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM picks WHERE result = 'PENDING'").fetchall()
        return [dict(r) for r in rows]


def get_all_picks() -> list[dict]:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM picks ORDER BY date DESC").fetchall()
        return [dict(r) for r in rows]


def summary() -> dict:
    init_db()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) FILTER (WHERE result != 'PENDING') AS settled,
                COUNT(*) FILTER (WHERE result = 'WIN')      AS wins,
                COUNT(*) FILTER (WHERE result = 'LOSS')     AS losses,
                ROUND(SUM(profit), 2)                       AS total_profit
            FROM picks
        """).fetchone()
        settled, wins, losses, total_profit = row
        win_rate = round(wins / settled * 100, 1) if settled else 0
        return {
            "settled": settled,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_profit": total_profit or 0.0,
        }


if __name__ == "__main__":
    init_db()
    print("Database initialised at", DB_PATH)
    print("Summary:", summary())
