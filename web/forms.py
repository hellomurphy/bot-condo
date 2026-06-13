"""Parse & validate web form data → env dict for subprocess.

API keys (DEEPSEEK_API_KEY, OPENAI_API_KEY) come from the server's own env,
not from the form — the UI does not expose key fields.
"""
import re
from typing import Any


def parse_form(data: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Returns (env_dict, errors). env keys match config.py env-var names."""
    errors: list[str] = []
    env: dict[str, str] = {}

    # FB Group URLs — list of inputs หรือ textarea (multiline)
    raw_urls = data.get("FB_GROUP_URLS") or ""
    if isinstance(raw_urls, list):
        urls = [u.strip() for u in raw_urls if u.strip()]
    else:
        urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    if not urls:
        errors.append("At least one FB Group URL is required")
    else:
        env["FB_GROUP_URLS"] = ",".join(urls)

    # Budget
    for key, default in [("TARGET_BUDGET", "12000"), ("MAX_BUDGET", "15000")]:
        val = (data.get(key) or default).strip()
        if not re.match(r"^\d+(\.\d+)?$", val):
            errors.append(f"{key} must be a number")
        else:
            env[key] = val

    # MAX_MOVE_IN_COST — optional; omit from env if blank
    raw_move_in = (data.get("MAX_MOVE_IN_COST") or "").strip()
    if raw_move_in:
        if not re.match(r"^\d+(\.\d+)?$", raw_move_in):
            errors.append("MAX_MOVE_IN_COST must be a number")
        else:
            env["MAX_MOVE_IN_COST"] = raw_move_in

    # Size / features
    sqm = (data.get("MIN_SIZE_SQM") or "24").strip()
    if not re.match(r"^\d+(\.\d+)?$", sqm):
        errors.append("Min sqm must be a number")
    else:
        env["MIN_SIZE_SQM"] = sqm

    env["MUST_HAVE_WASHER"] = "true" if data.get("MUST_HAVE_WASHER") in ("true", "on", "1", True) else "false"
    env["NEED_PARKING"] = "true" if data.get("NEED_PARKING") in ("true", "on", "1", True) else "false"

    # Scraping limits
    for key, default in [("MAX_SCROLL_ROUNDS", "8"), ("MAX_POSTS_PER_RUN", "150")]:
        val = (data.get(key) or default).strip()
        if not re.match(r"^\d+$", val):
            errors.append(f"{key} must be an integer")
        else:
            env[key] = val

    env["HEADLESS"] = "true"

    # Echo the effective form values into runner logs for easier debugging.
    env["WEB_DEBUG_FORM_VALUES"] = json_debug_snapshot(env)

    return env, errors


def json_debug_snapshot(env: dict[str, str]) -> str:
    """Compact debug string for confirming the web form values that reach the runner."""
    keys = [
        "TARGET_BUDGET",
        "MAX_BUDGET",
        "MAX_MOVE_IN_COST",
        "MIN_SIZE_SQM",
        "MUST_HAVE_WASHER",
        "NEED_PARKING",
        "MAX_SCROLL_ROUNDS",
        "MAX_POSTS_PER_RUN",
    ]
    parts = []
    for key in keys:
        if key in env:
            parts.append(f"{key}={env[key]}")
    return ";".join(parts)
