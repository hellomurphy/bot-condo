"""Query helpers for the web UI — build rows for results table and export.

Connects directly to the SQLite file instead of importing database.db,
because db.py pulls in config.py at module-load time and config.py calls
_require("DEEPSEEK_API_KEY") which crashes when the key is not set.
"""
import json
import os
import sqlite3

_DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "condo.db"))

FB_RESTRICTED_SIGNALS = [
    "ไม่สามารถดูเนื้อหานี้ได้ในขณะนี้",
    "เหตุการณ์นี้มักจะเกิดขึ้นเนื่องจากเจ้าของแชร์เนื้อหา",
    "เนื้อหาถูกลบไปแล้ว",
    "this content isn't available right now",
    "this content is not available right now",
    "this content may have been removed",
]


def _connect() -> sqlite3.Connection:
    db_path = os.getenv("DB_PATH", _DB_PATH)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _json_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return [value]
        return parsed if isinstance(parsed, list) else []
    return []


def _is_restricted_display(row: dict) -> bool:
    risk_flags = [str(flag).lower() for flag in _json_list(row.get("risk_flags"))]
    if "restricted_content" in risk_flags:
        return True

    raw_text = (row.get("raw_text") or "").lower()
    if any(signal in raw_text for signal in FB_RESTRICTED_SIGNALS):
        return True

    if row.get("tier") != "need_info":
        return False

    # Conservative legacy heuristic for rows extracted before the restricted guard existed.
    lacks_core_details = not any([
        row.get("condo_name"),
        row.get("location_text"),
        row.get("station_name"),
        row.get("monthly_rent"),
        row.get("size_sqm"),
        row.get("first_image_url"),
    ])
    return lacks_core_details


def _decorate_result_row(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    data["risk_flags_list"] = _json_list(data.get("risk_flags"))
    data["is_restricted_display"] = _is_restricted_display(data)
    return data


def get_results_rows() -> list[dict]:
    """
    Join listings + listing_scores + first post_image per listing.
    Returns list of plain dicts (JSON-serialisable).
    Returns [] if DB doesn't exist or has no data.
    """
    try:
        conn = _connect()
        rows = conn.execute("""
            SELECT
                l.id,
                l.condo_name,
                l.location_text,
                l.monthly_rent,
                l.move_in_cost,
                l.size_sqm,
                l.room_type,
                l.floor,
                l.furnishing,
                l.has_washer,
                l.has_parking,
                l.contract_min_months,
                l.available_date,
                l.agent_or_owner,
                l.duplicate_flag,
                l.risk_flags,
                l.questions_to_ask,
                s.final_total,
                s.vision_score,
                s.tier,
                s.price_score,
                s.commute_score,
                p.raw_text,
                p.status AS post_status,
                p.post_url,
                (
                    SELECT pi.image_url
                    FROM post_images pi
                    WHERE pi.post_ref = l.post_ref
                    ORDER BY pi.id
                    LIMIT 1
                ) AS first_image_url
            FROM listings l
            JOIN listing_scores s ON s.listing_id = l.id
            JOIN posts p ON p.id = l.post_ref
            WHERE s.tier != 'skip'
            ORDER BY s.final_total DESC
        """).fetchall()
        conn.close()
        return [_decorate_result_row(r) for r in rows]
    except Exception:
        return []


def delete_listing(listing_id: int) -> bool:
    """Delete one listing and its score row. Returns True if a listing was deleted."""
    conn = _connect()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM listing_scores WHERE listing_id=?", (listing_id,))
        cur = conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
