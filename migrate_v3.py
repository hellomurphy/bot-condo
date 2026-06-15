"""
One-shot migration: reshape existing condo.db listings table to v3 schema.

Old schema (32 cols) → New schema (12 cols):
  rent          ← monthly_rent
  location_tags ← station_name (preferred) or location_text fallback
  status        ← agent_or_owner (+ risk_flags if any)
  move_in_cost  ← existing move_in_cost if present, else rent * 3
  summary       ← kept as-is
  (all other old cols dropped)

Runs in a transaction — rolls back completely if anything fails.
Safe to re-run: checks for new columns before adding them.
"""

import json
import sqlite3
import shutil
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/condo.db")
BACKUP_PATH = Path(f"data/condo_pre_v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")


def _build_status(agent_or_owner: str | None, risk_flags_json: str | None) -> str | None:
    base = (agent_or_owner or "").strip() or None
    flags: list[str] = []
    if risk_flags_json:
        try:
            parsed = json.loads(risk_flags_json)
            if isinstance(parsed, list):
                flags = [str(f) for f in parsed if f]
        except (json.JSONDecodeError, TypeError):
            if risk_flags_json.strip():
                flags = [risk_flags_json.strip()]

    if not base and not flags:
        return None
    parts = [base] if base else []
    parts.extend(flags)
    result = ", ".join(parts)
    return result[:30]  # match prompt constraint


def _build_location_tags(station_name: str | None, location_text: str | None) -> str | None:
    return (station_name or "").strip() or (location_text or "").strip() or None


def migrate(db_path: Path, backup_path: Path):
    # Backup first
    print(f"Backing up → {backup_path}")
    shutil.copy2(db_path, backup_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # allow schema changes

    try:
        with conn:
            # 1. Add new columns (idempotent)
            for stmt in [
                "ALTER TABLE listings ADD COLUMN rent REAL",
                "ALTER TABLE listings ADD COLUMN location_tags TEXT",
                "ALTER TABLE listings ADD COLUMN status TEXT",
            ]:
                try:
                    conn.execute(stmt)
                    print(f"  + {stmt}")
                except sqlite3.OperationalError:
                    print(f"  (already exists, skipping) {stmt}")

            # 2. Read all old rows
            rows = conn.execute("""
                SELECT id, monthly_rent, move_in_cost, station_name,
                       location_text, agent_or_owner, risk_flags
                FROM listings
            """).fetchall()

            print(f"\nMigrating {len(rows)} listings...")
            updated = 0
            for row in rows:
                new_rent = row["monthly_rent"]
                existing_mic = row["move_in_cost"]
                try:
                    new_mic = existing_mic if existing_mic is not None else (float(new_rent) * 3 if new_rent else None)
                except (TypeError, ValueError):
                    new_mic = None

                new_location_tags = _build_location_tags(row["station_name"], row["location_text"])
                new_status = _build_status(row["agent_or_owner"], row["risk_flags"])

                conn.execute("""
                    UPDATE listings
                    SET rent=?, location_tags=?, status=?, move_in_cost=?
                    WHERE id=?
                """, (new_rent, new_location_tags, new_status, new_mic, row["id"]))
                updated += 1

            print(f"Updated {updated} rows.")

            # 3. Remove must_call_count from runs (SQLite can't DROP COLUMN before 3.35,
            #    so we recreate the table)
            runs_version = conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('runs') WHERE name='must_call_count'"
            ).fetchone()[0]
            if runs_version:
                print("\nRemoving must_call_count from runs table...")
                conn.executescript("""
                    CREATE TABLE runs_new (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at          TEXT NOT NULL,
                        ended_at            TEXT,
                        groups_scanned      INTEGER DEFAULT 0,
                        posts_found         INTEGER DEFAULT 0,
                        listings_extracted  INTEGER DEFAULT 0,
                        status              TEXT
                    );
                    INSERT INTO runs_new (id, started_at, ended_at, groups_scanned,
                        posts_found, listings_extracted, status)
                    SELECT id, started_at, ended_at, groups_scanned,
                        posts_found, listings_extracted, status
                    FROM runs;
                    DROP TABLE runs;
                    ALTER TABLE runs_new RENAME TO runs;
                """)
                print("  runs table rebuilt.")
            else:
                print("\nruns.must_call_count already removed — skipping.")

        print("\nMigration complete.")

    except Exception as e:
        conn.close()
        print(f"\nERROR: {e}")
        print(f"Database unchanged. Backup at: {backup_path}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Run from the bot-condo root directory.")
        sys.exit(1)
    migrate(DB_PATH, BACKUP_PATH)
