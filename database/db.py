import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with transaction() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at          TEXT NOT NULL,
                ended_at            TEXT,
                groups_scanned      INTEGER DEFAULT 0,
                posts_found         INTEGER DEFAULT 0,
                listings_extracted  INTEGER DEFAULT 0,
                must_call_count     INTEGER DEFAULT 0,
                status              TEXT
            );

            CREATE TABLE IF NOT EXISTS posts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id              INTEGER REFERENCES runs(id),
                source              TEXT NOT NULL DEFAULT 'facebook',
                post_id             TEXT,
                post_url            TEXT,
                group_url           TEXT,
                post_content_hash   TEXT,
                raw_text            TEXT,
                raw_text_redacted   TEXT,
                scraped_at          TEXT NOT NULL,
                last_seen_at        TEXT,
                status              TEXT DEFAULT 'new'
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_content_hash
                ON posts(post_content_hash) WHERE post_content_hash IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_post_id
                ON posts(post_id) WHERE post_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS post_images (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                post_ref        INTEGER NOT NULL REFERENCES posts(id),
                image_url       TEXT NOT NULL,
                base64_data     TEXT,
                image_type      TEXT,
                mapped_listing  INTEGER,
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS listings (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                post_ref            INTEGER NOT NULL REFERENCES posts(id),
                listing_type        TEXT,
                condo_name          TEXT,
                location_text       TEXT,
                station_name        TEXT,
                monthly_rent        REAL,
                size_sqm            REAL,
                room_type           TEXT,
                floor               TEXT,
                furnishing          TEXT,
                deposit_months      REAL,
                advance_months      REAL,
                move_in_cost        REAL,
                move_in_cost_stated REAL,
                other_fee_text      TEXT,
                contract_min_months INTEGER,
                available_date      TEXT,
                has_parking         INTEGER,
                has_washer          INTEGER,
                has_fridge          INTEGER,
                has_wifi            INTEGER,
                pet_allowed         INTEGER,
                agent_or_owner      TEXT,
                near_transit        INTEGER,
                confidence          REAL,
                risk_flags          TEXT,
                duplicate_flag      INTEGER DEFAULT 0,
                missing_fields      TEXT,
                questions_to_ask    TEXT,
                listing_fingerprint TEXT,
                created_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS post_comments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                post_ref    INTEGER NOT NULL REFERENCES posts(id),
                comment_text TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_post_comments_post_ref
                ON post_comments(post_ref);

            CREATE TABLE IF NOT EXISTS listing_scores (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id      INTEGER UNIQUE NOT NULL REFERENCES listings(id),
                hard_filter     TEXT,
                price_score     REAL,
                commute_score   REAL,
                condition_score REAL,
                terms_score     REAL,
                trust_score     REAL,
                base_total      REAL,
                vision_score    REAL,
                final_total     REAL,
                tier            TEXT,
                alerted_at      TEXT,
                scored_at       TEXT NOT NULL
            );
        """)


# --- Run helpers ---

def create_run() -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
            (_now(),)
        )
        return cur.lastrowid


def finish_run(run_id: int, stats: dict):
    with transaction() as conn:
        conn.execute("""
            UPDATE runs SET ended_at=?, groups_scanned=?, posts_found=?,
                listings_extracted=?, must_call_count=?, status='done'
            WHERE id=?
        """, (
            _now(),
            stats.get("groups_scanned", 0),
            stats.get("posts_found", 0),
            stats.get("listings_extracted", 0),
            stats.get("must_call_count", 0),
            run_id,
        ))


def fail_run(run_id: int):
    with transaction() as conn:
        conn.execute(
            "UPDATE runs SET ended_at=?, status='failed' WHERE id=?",
            (_now(), run_id)
        )


# --- Post helpers ---

def upsert_post(run_id: int, data: dict) -> tuple[int | None, bool, str | None]:
    """Returns (post_id, is_new, duplicate_reason)."""
    with transaction() as conn:
        # Check by post_id first
        if data.get("post_id"):
            row = conn.execute(
                "SELECT id FROM posts WHERE post_id=?", (data["post_id"],)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE posts SET last_seen_at=? WHERE id=?",
                    (_now(), row["id"])
                )
                return row["id"], False, "post_id"

        # Check by content hash
        if data.get("post_content_hash"):
            row = conn.execute(
                "SELECT id FROM posts WHERE post_content_hash=?",
                (data["post_content_hash"],)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE posts SET last_seen_at=? WHERE id=?",
                    (_now(), row["id"])
                )
                return row["id"], False, "content_hash"

        # Insert new
        cur = conn.execute("""
            INSERT INTO posts (run_id, source, post_id, post_url, group_url,
                post_content_hash, raw_text, raw_text_redacted, scraped_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            data.get("source", "facebook"),
            data.get("post_id"),
            data.get("post_url"),
            data.get("group_url"),
            data.get("post_content_hash"),
            data.get("raw_text"),
            data.get("raw_text_redacted"),
            _now(),
            data.get("status", "new"),
        ))
        return cur.lastrowid, True, None


def insert_post_comments(post_id: int, comments: list[str]):
    with transaction() as conn:
        conn.executemany(
            "INSERT INTO post_comments (post_ref, comment_text, created_at) VALUES (?, ?, ?)",
            [(post_id, c, _now()) for c in comments if c],
        )


def get_comments_for_post(post_id: int) -> list[str]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT comment_text FROM post_comments WHERE post_ref=? ORDER BY id",
            (post_id,)
        ).fetchall()
    return [r["comment_text"] for r in rows]


def update_post_status(post_id: int, status: str):
    with transaction() as conn:
        conn.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))


def insert_post_image(post_id: int, image_url: str):
    with transaction() as conn:
        conn.execute(
            "INSERT INTO post_images (post_ref, image_url, created_at) VALUES (?, ?, ?)",
            (post_id, image_url, _now())
        )


def update_image_base64(image_id: int, base64_data: str, image_type: str):
    with transaction() as conn:
        conn.execute(
            "UPDATE post_images SET base64_data=?, image_type=? WHERE id=?",
            (base64_data, image_type, image_id)
        )


def get_images_for_post(post_id: int) -> list[sqlite3.Row]:
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM post_images WHERE post_ref=?", (post_id,)
        ).fetchall()
    return rows


# --- Listing helpers ---

def insert_listing(post_id: int, data: dict) -> int:
    with transaction() as conn:
        cur = conn.execute("""
            INSERT INTO listings (
                post_ref, listing_type, condo_name, location_text, station_name,
                monthly_rent, size_sqm, room_type, floor, furnishing,
                deposit_months, advance_months, move_in_cost, move_in_cost_stated,
                other_fee_text, contract_min_months, available_date,
                has_parking, has_washer, has_fridge, has_wifi, pet_allowed,
                agent_or_owner, near_transit, confidence,
                risk_flags, duplicate_flag, missing_fields, questions_to_ask,
                listing_fingerprint, created_at
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """, (
            post_id,
            data.get("listing_type"),
            data.get("condo_name"),
            data.get("location_text"),
            data.get("station_name"),
            data.get("monthly_rent"),
            data.get("size_sqm"),
            data.get("room_type"),
            data.get("floor"),
            data.get("furnishing"),
            data.get("deposit_months"),
            data.get("advance_months"),
            data.get("move_in_cost"),
            data.get("move_in_cost_stated"),
            data.get("other_fee_text"),
            data.get("contract_min_months"),
            data.get("available_date"),
            data.get("has_parking"),
            data.get("has_washer"),
            data.get("has_fridge"),
            data.get("has_wifi"),
            data.get("pet_allowed"),
            data.get("agent_or_owner"),
            data.get("near_transit"),
            data.get("confidence"),
            data.get("risk_flags"),
            data.get("duplicate_flag", 0),
            data.get("missing_fields"),
            data.get("questions_to_ask"),
            data.get("listing_fingerprint"),
            _now(),
        ))
        return cur.lastrowid


def get_listing_by_fingerprint(fingerprint: str) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM listings WHERE listing_fingerprint=?", (fingerprint,)
    ).fetchone()
    conn.close()
    return row


# --- Score helpers ---

def insert_score(listing_id: int, data: dict):
    with transaction() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO listing_scores (
                listing_id, hard_filter, price_score, commute_score, condition_score,
                terms_score, trust_score, base_total, vision_score, final_total,
                tier, scored_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            listing_id,
            data.get("hard_filter"),
            data.get("price_score"),
            data.get("commute_score"),
            data.get("condition_score"),
            data.get("terms_score"),
            data.get("trust_score"),
            data.get("base_total"),
            data.get("vision_score"),
            data.get("final_total"),
            data.get("tier"),
            _now(),
        ))


def update_vision_score(listing_id: int, vision_score: float, final_total: float,
                        condition_score: float, tier: str):
    with transaction() as conn:
        conn.execute("""
            UPDATE listing_scores
            SET vision_score=?, final_total=?, condition_score=?, tier=?
            WHERE listing_id=?
        """, (vision_score, final_total, condition_score, tier, listing_id))


def set_alerted(score_id: int):
    with transaction() as conn:
        conn.execute(
            "UPDATE listing_scores SET alerted_at=? WHERE id=?",
            (_now(), score_id)
        )


# --- Query helpers for export ---

def get_listings_with_scores(tier_filter: str | None = None) -> list[sqlite3.Row]:
    conn = get_connection()
    base_q = """
        SELECT l.*, s.hard_filter, s.price_score, s.commute_score, s.condition_score,
               s.terms_score, s.trust_score, s.base_total, s.vision_score,
               s.final_total, s.tier, s.alerted_at, s.scored_at, s.id as score_id,
               p.post_url, p.group_url, p.scraped_at
        FROM listings l
        JOIN listing_scores s ON s.listing_id = l.id
        JOIN posts p ON p.id = l.post_ref
    """
    if tier_filter:
        rows = conn.execute(base_q + " WHERE s.tier=? ORDER BY s.final_total DESC",
                            (tier_filter,)).fetchall()
    else:
        rows = conn.execute(base_q + " WHERE s.tier != 'skip' ORDER BY s.final_total DESC").fetchall()
    conn.close()
    return rows


def get_unalerted_above_tier(min_tier_order: int) -> list[sqlite3.Row]:
    TIER_ORDER = ["skip", "maybe", "need_info", "shortlist", "must_call"]
    tiers = [t for i, t in enumerate(TIER_ORDER) if i >= min_tier_order]
    placeholders = ",".join("?" * len(tiers))
    conn = get_connection()
    rows = conn.execute(f"""
        SELECT l.*, s.final_total, s.tier, s.alerted_at, s.id as score_id
        FROM listings l
        JOIN listing_scores s ON s.listing_id = l.id
        WHERE s.tier IN ({placeholders}) AND s.alerted_at IS NULL
    """, tiers).fetchall()
    conn.close()
    return rows


def cleanup_old_posts(days: int):
    cutoff = f"-{int(days)} days"
    with transaction() as conn:
        conn.execute("DELETE FROM listing_scores WHERE listing_id IN (SELECT id FROM listings WHERE post_ref IN (SELECT id FROM posts WHERE scraped_at < datetime('now', ?)))", (cutoff,))
        conn.execute("DELETE FROM listings WHERE post_ref IN (SELECT id FROM posts WHERE scraped_at < datetime('now', ?))", (cutoff,))
        conn.execute("DELETE FROM post_images WHERE post_ref IN (SELECT id FROM posts WHERE scraped_at < datetime('now', ?))", (cutoff,))
        conn.execute("DELETE FROM posts WHERE scraped_at < datetime('now', ?)", (cutoff,))
