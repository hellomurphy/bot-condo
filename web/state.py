"""In-memory run state. Single source of truth for all scrape sessions."""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import TypedDict, Any


class RunEntry(TypedDict):
    queue: asyncio.Queue
    logs: list[str]
    process: Any  # asyncio.subprocess.Process | None
    status: str   # "running" | "done" | "failed" | "stopped"
    started_at: str


runs: dict[str, RunEntry] = {}


def new_run(run_id: str) -> RunEntry:
    entry: RunEntry = {
        "queue": asyncio.Queue(),
        "logs": [],
        "process": None,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    runs[run_id] = entry
    return entry


def cleanup_stale(max_age_hours: int = 1) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    stale = [
        rid for rid, r in runs.items()
        if r["status"] != "running"
        and datetime.fromisoformat(r["started_at"]) < cutoff
    ]
    for rid in stale:
        del runs[rid]
