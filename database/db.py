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
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                post_ref      INTEGER NOT NULL REFERENCES posts(id),
                condo_name    TEXT,
                room_type     TEXT,
                size_sqm      REAL,
                floor         TEXT,
                rent          REAL,
                move_in_cost  REAL,
                location_tags TEXT,
                status        TEXT,
                summary       TEXT,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS post_comments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                post_ref    INTEGER NOT NULL REFERENCES posts(id),
                comment_text TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_post_comments_post_ref
                ON post_comments(post_ref);

            CREATE TABLE IF NOT EXISTS ph_watches (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                project_url      TEXT NOT NULL UNIQUE,
                label            TEXT,
                provider_id      TEXT NOT NULL DEFAULT 'propertyhub',
                min_price        INTEGER,
                max_price        INTEGER,
                min_size_sqm     REAL,
                min_floor        INTEGER,
                interval_minutes INTEGER NOT NULL DEFAULT 30,
                active           INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ph_listings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                watch_id        INTEGER NOT NULL REFERENCES ph_watches(id) ON DELETE CASCADE,
                listing_id      TEXT NOT NULL,
                listing_url     TEXT NOT NULL,
                title           TEXT,
                monthly_rent    INTEGER,
                size_sqm        REAL,
                floor           TEXT,
                room_type       TEXT,
                cover_image_url TEXT,
                refreshed_at    TEXT,
                first_seen_at   TEXT NOT NULL,
                last_seen_at    TEXT NOT NULL,
                alerted_at      TEXT,
                is_read         INTEGER NOT NULL DEFAULT 0,
                UNIQUE(watch_id, listing_id)
            );

            CREATE INDEX IF NOT EXISTS idx_ph_listings_watch_id ON ph_listings(watch_id);
        """)
        # Migrate existing DB
        for stmt in [
            "ALTER TABLE ph_listings ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE ph_listings ADD COLUMN prev_monthly_rent INTEGER",
            "ALTER TABLE ph_watches ADD COLUMN provider_id TEXT NOT NULL DEFAULT 'propertyhub'",
            "ALTER TABLE listings ADD COLUMN rent REAL",
            "ALTER TABLE listings ADD COLUMN location_tags TEXT",
            "ALTER TABLE listings ADD COLUMN status TEXT",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass


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
                listings_extracted=?, status='done'
            WHERE id=?
        """, (
            _now(),
            stats.get("groups_scanned", 0),
            stats.get("posts_found", 0),
            stats.get("listings_extracted", 0),
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
                post_ref, condo_name, room_type, size_sqm, floor,
                rent, move_in_cost, location_tags, status, summary, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            post_id,
            data.get("condo_name"),
            data.get("room_type"),
            data.get("size_sqm"),
            data.get("floor"),
            data.get("rent"),
            data.get("move_in_cost"),
            data.get("location_tags"),
            data.get("status"),
            data.get("summary"),
            _now(),
        ))
        return cur.lastrowid


# --- Query helpers for export ---

def get_listings() -> list[sqlite3.Row]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT l.*, p.post_url, p.source, p.scraped_at
        FROM listings l
        JOIN posts p ON p.id = l.post_ref
        ORDER BY l.created_at DESC
    """).fetchall()
    conn.close()
    return rows


def cleanup_old_posts(days: int):
    cutoff = f"-{int(days)} days"
    with transaction() as conn:
        conn.execute("DELETE FROM listings WHERE post_ref IN (SELECT id FROM posts WHERE scraped_at < datetime('now', ?))", (cutoff,))
        conn.execute("DELETE FROM post_images WHERE post_ref IN (SELECT id FROM posts WHERE scraped_at < datetime('now', ?))", (cutoff,))
        conn.execute("DELETE FROM posts WHERE scraped_at < datetime('now', ?)", (cutoff,))


# --- PropertyHub watch helpers ---

def upsert_ph_watch(data: dict) -> int:
    with transaction() as conn:
        conn.execute("""
            INSERT INTO ph_watches (project_url, label, provider_id, min_price, max_price,
                min_size_sqm, min_floor, interval_minutes, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(project_url) DO UPDATE SET
                label=excluded.label,
                provider_id=excluded.provider_id,
                min_price=excluded.min_price,
                max_price=excluded.max_price,
                min_size_sqm=excluded.min_size_sqm,
                min_floor=excluded.min_floor,
                interval_minutes=excluded.interval_minutes,
                active=1
        """, (
            data["project_url"],
            data.get("label"),
            data.get("provider_id", "propertyhub"),
            data.get("min_price"),
            data.get("max_price"),
            data.get("min_size_sqm"),
            data.get("min_floor"),
            data.get("interval_minutes", 30),
            _now(),
        ))
        row = conn.execute(
            "SELECT id FROM ph_watches WHERE project_url=?", (data["project_url"],)
        ).fetchone()
        return row["id"]


def get_ph_watches(active_only: bool = True) -> list[sqlite3.Row]:
    conn = get_connection()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM ph_watches WHERE active=1 ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ph_watches ORDER BY id").fetchall()
    conn.close()
    return rows


def get_ph_watch(watch_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM ph_watches WHERE id=?", (watch_id,)).fetchone()
    conn.close()
    return row


def delete_ph_watch(watch_id: int):
    with transaction() as conn:
        conn.execute("DELETE FROM ph_watches WHERE id=?", (watch_id,))


def update_ph_watch(watch_id: int, data: dict):
    with transaction() as conn:
        conn.execute("""
            UPDATE ph_watches SET
                min_price=?, max_price=?, min_size_sqm=?, min_floor=?,
                interval_minutes=?, label=?
            WHERE id=?
        """, (
            data.get("min_price"),
            data.get("max_price"),
            data.get("min_size_sqm"),
            data.get("min_floor"),
            data.get("interval_minutes", 30),
            data.get("label"),
            watch_id,
        ))


# --- PropertyHub listing helpers ---

def upsert_ph_listing(watch_id: int, data: dict) -> tuple[int, bool, bool, bool, int | None]:
    """Returns (ph_listing_id, is_new, is_price_drop, is_muted, prev_rent).
    Preserves alerted_at/is_read unless price dropped (resets both to alert again).
    prev_rent is the old price before the drop, or None if not a price drop.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT id, monthly_rent, prev_monthly_rent FROM ph_listings "
            "WHERE watch_id=? AND listing_id=?",
            (watch_id, data["listing_id"])
        ).fetchone()
        if row:
            old_rent = row["monthly_rent"]
            new_rent = data.get("monthly_rent")
            is_price_drop = (
                old_rent is not None
                and new_rent is not None
                and new_rent < old_rent
            )
            # Only overwrite prev_monthly_rent when price drops; keep old value on price rise
            prev_rent_to_save = old_rent if is_price_drop else row["prev_monthly_rent"]
            is_muted = False

            conn.execute("""
                UPDATE ph_listings SET
                    listing_url=?, title=?, monthly_rent=?, prev_monthly_rent=?,
                    size_sqm=?, floor=?, room_type=?, cover_image_url=?, refreshed_at=?,
                    alerted_at = CASE WHEN ? THEN NULL ELSE alerted_at END,
                    is_read    = CASE WHEN ? THEN 0    ELSE is_read    END,
                    last_seen_at=?
                WHERE id=?
            """, (
                data.get("listing_url"),
                data.get("title"),
                new_rent,
                prev_rent_to_save,
                data.get("size_sqm"),
                data.get("floor"),
                data.get("room_type"),
                data.get("cover_image_url"),
                data.get("refreshed_at"),
                is_price_drop,
                is_price_drop,
                _now(),
                row["id"],
            ))
            return row["id"], False, is_price_drop, is_muted, (old_rent if is_price_drop else None)
        else:
            cur = conn.execute("""
                INSERT INTO ph_listings (
                    watch_id, listing_id, listing_url, title, monthly_rent,
                    size_sqm, floor, room_type, cover_image_url, refreshed_at,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                watch_id,
                data["listing_id"],
                data.get("listing_url"),
                data.get("title"),
                data.get("monthly_rent"),
                data.get("size_sqm"),
                data.get("floor"),
                data.get("room_type"),
                data.get("cover_image_url"),
                data.get("refreshed_at"),
                _now(),
                _now(),
            ))
            return cur.lastrowid, True, False, False, None


def get_ph_listings_for_watch(watch_id: int) -> list[sqlite3.Row]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM ph_listings WHERE watch_id=?
        ORDER BY first_seen_at DESC
    """, (watch_id,)).fetchall()
    conn.close()
    return rows


def get_ph_listing(listing_id: int) -> sqlite3.Row | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM ph_listings WHERE id=?", (listing_id,)).fetchone()
    conn.close()
    return row


def set_ph_listing_alerted(listing_id: int):
    with transaction() as conn:
        conn.execute(
            "UPDATE ph_listings SET alerted_at=? WHERE id=?",
            (_now(), listing_id)
        )


def set_ph_listing_read(listing_id: int):
    with transaction() as conn:
        conn.execute(
            "UPDATE ph_listings SET is_read=1 WHERE id=?",
            (listing_id,)
        )


def clear_ph_listing_read(listing_id: int):
    with transaction() as conn:
        conn.execute(
            "UPDATE ph_listings SET is_read=0 WHERE id=?",
            (listing_id,)
        )


def delete_ph_listing(listing_id: int):
    with transaction() as conn:
        conn.execute("DELETE FROM ph_listings WHERE id=?", (listing_id,))


def delete_ph_listings_for_watch(watch_id: int):
    with transaction() as conn:
        conn.execute("DELETE FROM ph_listings WHERE watch_id=?", (watch_id,))
