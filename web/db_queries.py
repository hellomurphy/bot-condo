"""Query helpers for the web UI — build rows for results table and export.

Connects directly to the SQLite file instead of importing database.db,
because db.py pulls in config.py at module-load time and config.py calls
_require("DEEPSEEK_API_KEY") which crashes when the key is not set.
"""
import os
import sqlite3

_DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "condo.db"))


def _connect() -> sqlite3.Connection:
    db_path = os.getenv("DB_PATH", _DB_PATH)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_results_rows() -> list[dict]:
    """
    Join listings + posts + first image. Returns list of plain dicts.
    Returns [] if DB doesn't exist or has no data.
    """
    try:
        conn = _connect()
        rows = conn.execute("""
            SELECT
                l.id,
                l.condo_name,
                l.room_type,
                l.size_sqm,
                l.floor,
                l.rent,
                l.move_in_cost,
                l.location_tags,
                l.status,
                l.summary,
                l.created_at,
                p.post_url,
                p.source,
                p.scraped_at,
                p.raw_text,
                p.status AS post_status,
                (
                    SELECT pi.image_url
                    FROM post_images pi
                    WHERE pi.post_ref = l.post_ref
                    ORDER BY pi.id
                    LIMIT 1
                ) AS first_image_url
            FROM listings l
            JOIN posts p ON p.id = l.post_ref
            ORDER BY l.created_at DESC
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def delete_listing(listing_id: int) -> bool:
    """Delete one listing row. Returns True if deleted."""
    conn = _connect()
    try:
        conn.execute("BEGIN")
        cur = conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
