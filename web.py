#!/usr/bin/env python3
"""Entry point: python web.py"""
import os
import sqlite3
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))

import uvicorn

_DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "condo.db"))


def _init_db():
    """Create DB tables if they don't exist (idempotent). Avoids importing config."""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Delegate to db.init_db via env — config is satisfied because DB_PATH is the only
    # thing needed at this stage and we pass it via env before any config import.
    conn.close()
    # Now it's safe to import db because DB_PATH doesn't need DEEPSEEK_API_KEY
    os.environ.setdefault("DB_PATH", _DB_PATH)
    # We still need a real init_db to create all tables; set a dummy key so config loads
    # without crashing, then call init_db, then remove it.
    _sentinel = "__WEB_INIT__"
    os.environ.setdefault("DEEPSEEK_API_KEY", _sentinel)
    try:
        from database import db
        db.init_db()
    finally:
        if os.environ.get("DEEPSEEK_API_KEY") == _sentinel:
            del os.environ["DEEPSEEK_API_KEY"]


def _check_user_data():
    ud = os.path.join(os.path.dirname(__file__), "user_data")
    files = [f for f in os.listdir(ud) if not f.startswith("__")] if os.path.isdir(ud) else []
    if not files:
        print("  ⚠️  user_data/ is empty — run 'python main.py --login' first to save FB session")


if __name__ == "__main__":
    _init_db()
    _check_user_data()
    print("─" * 44)
    print("  🏠 Condo Radar — Web UI")
    print("  Open http://localhost:8000")
    print("─" * 44)
    from web.app import app
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
