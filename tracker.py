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
                session   TEXT    NOT NULL DEFAULT 'morning'
            )
        """)
        # Migrate existing DBs that predate the session column
        try:
            conn.execute("ALTER TABLE picks ADD COLUMN session TEXT NOT NULL DEFAULT 'morning'")
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
            "INSERT INTO picks (date, match, league, bet_type, pick, odds, session) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pick_date, match, league, bet_type, pick, odds, session),
        )
        conn.commit()
    try:
        log_to_excel(match, league, bet_type, pick, odds, confidence, pick_date,
                     claude_prob=claude_prob, market_prob=market_prob)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Excel log failed: %s", exc)


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
