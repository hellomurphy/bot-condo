"""Spawn main.py as subprocess, stream stdout into asyncio.Queue."""
import asyncio
import os
import sys

from web import state

MAX_CONCURRENT_RUNS = 1
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_run_active() -> bool:
    return any(r["status"] == "running" for r in state.runs.values())


async def launch(run_id: str, user_env: dict[str, str]) -> None:
    run = state.runs[run_id]
    queue = run["queue"]

    env = {**os.environ, **user_env, "PYTHONUNBUFFERED": "1"}

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u",
            "main.py",
            cwd=_project_root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        run["process"] = proc

        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            run["logs"].append(line)
            await queue.put(line)

        await proc.wait()
        run["status"] = "done" if proc.returncode == 0 else "failed"

    except Exception as exc:
        run["logs"].append(f"[runner error] {exc}")
        run["status"] = "failed"

    finally:
        await queue.put(None)  # sentinel — unblocks SSE
        state.cleanup_stale()


async def stop(run_id: str) -> None:
    run = state.runs.get(run_id)
    if not run:
        return
    proc = run.get("process")
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
    run["status"] = "stopped"
    await run["queue"].put(None)
