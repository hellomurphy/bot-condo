"""FastAPI router for PropertyHub monitor feature."""
import asyncio
import re
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import database.db as db
from web import state
from web.ph_poller import _scrape_watch

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

_PH_URL_RE = re.compile(
    r"^https?://propertyhub\.in\.th/[^/]+/[^/]+",
    re.IGNORECASE,
)


def _normalize_url(raw: str) -> str:
    return unquote(raw.strip().rstrip("/"))


def _validate_ph_url(url: str) -> str | None:
    """Return error string if invalid, None if OK."""
    if not _PH_URL_RE.match(url):
        return "URL ต้องมาจาก propertyhub.in.th เท่านั้น (เช่น https://propertyhub.in.th/เช่าคอนโด/โครงการ-xxx)"
    return None


def _watch_summary(watch) -> dict:
    w = dict(watch)
    watch_id = w["id"]
    listings = db.get_ph_listings_for_watch(watch_id)
    last_ts = state.ph_poller["watch_last_scraped"].get(watch_id)
    w["listing_count"] = len(listings)
    w["new_count"] = sum(1 for l in listings if l["alerted_at"] is None and l["muted_at"] is None)
    w["last_scraped_ago"] = _seconds_ago(last_ts)
    w["is_scraping"] = watch_id in state.ph_poller["currently_scraping"]
    interval_sec = (w.get("interval_minutes") or 30) * 60
    if last_ts is not None:
        elapsed = time.monotonic() - last_ts
        w["next_in_seconds"] = max(0, int(interval_sec - elapsed))
    else:
        w["next_in_seconds"] = 0
    return w


def _seconds_ago(mono_ts: float | None) -> str | None:
    if mono_ts is None:
        return None
    secs = int(time.monotonic() - mono_ts)
    if secs < 60:
        return f"{secs} วินาทีที่แล้ว"
    if secs < 3600:
        return f"{secs // 60} นาทีที่แล้ว"
    return f"{secs // 3600} ชั่วโมงที่แล้ว"


def _listing_to_dict(row) -> dict:
    d = dict(row)
    return d


# ── Pages ──────────────────────────────────────────────────────────────────

@router.get("/propertyhub", response_class=HTMLResponse)
async def propertyhub_page(request: Request):
    watches = [_watch_summary(w) for w in db.get_ph_watches(active_only=False)]
    # Load listings for first watch (or all) for initial render
    all_listings: list[dict] = []
    for w in db.get_ph_watches(active_only=False):
        for row in db.get_ph_listings_for_watch(w["id"]):
            d = _listing_to_dict(row)
            d["watch_label"] = w["label"] or w["project_url"].split("/")[-1]
            all_listings.append(d)
    return templates.TemplateResponse(request, "propertyhub.html", {
        "watches": watches,
        "listings": all_listings,
    })


# ── Watch CRUD ─────────────────────────────────────────────────────────────

@router.post("/propertyhub/watches")
async def add_watch(request: Request):
    raw = await request.form()
    project_url = _normalize_url(raw.get("project_url", ""))
    err = _validate_ph_url(project_url)
    if err:
        return JSONResponse({"error": err}, status_code=422)

    try:
        min_price = int(raw["min_price"]) if raw.get("min_price") else None
        max_price = int(raw["max_price"]) if raw.get("max_price") else None
        min_size_sqm = float(raw["min_size_sqm"]) if raw.get("min_size_sqm") else None
        min_floor = int(raw["min_floor"]) if raw.get("min_floor") else None
        interval_minutes = int(raw.get("interval_minutes") or 30)
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": f"ค่าที่กรอกไม่ถูกต้อง: {e}"}, status_code=422)

    label = project_url.rstrip("/").split("/")[-1]
    try:
        watch_id = db.upsert_ph_watch({
            "project_url": project_url,
            "label": label,
            "min_price": min_price,
            "max_price": max_price,
            "min_size_sqm": min_size_sqm,
            "min_floor": min_floor,
            "interval_minutes": interval_minutes,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    watch = db.get_ph_watch(watch_id)
    return JSONResponse({"status": "added", "watch": _watch_summary(watch)})


@router.post("/propertyhub/watches/{watch_id}/delete")
async def delete_watch(watch_id: int):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    db.delete_ph_watch(watch_id)
    state.ph_poller["watch_last_scraped"].pop(watch_id, None)
    state.ph_poller["currently_scraping"].discard(watch_id)
    return JSONResponse({"status": "deleted", "watch_id": watch_id})


@router.post("/propertyhub/watches/{watch_id}/edit")
async def edit_watch(watch_id: int, request: Request):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    raw = await request.form()
    try:
        data = {
            "label": raw.get("label") or watch["label"],
            "min_price": int(raw["min_price"]) if raw.get("min_price") else None,
            "max_price": int(raw["max_price"]) if raw.get("max_price") else None,
            "min_size_sqm": float(raw["min_size_sqm"]) if raw.get("min_size_sqm") else None,
            "min_floor": int(raw["min_floor"]) if raw.get("min_floor") else None,
            "interval_minutes": int(raw.get("interval_minutes") or 30),
        }
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": f"ค่าที่กรอกไม่ถูกต้อง: {e}"}, status_code=422)
    db.update_ph_watch(watch_id, data)
    updated = db.get_ph_watch(watch_id)
    return JSONResponse({"status": "updated", "watch": _watch_summary(updated)})


@router.post("/propertyhub/watches/{watch_id}/scrape")
async def trigger_scrape(watch_id: int):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    if watch_id in state.ph_poller["currently_scraping"]:
        return JSONResponse({"status": "already_running"})
    # Reset last-scraped so the poller also picks it up; fire immediately via task
    state.ph_poller["watch_last_scraped"].pop(watch_id, None)
    asyncio.create_task(_scrape_watch(watch))
    return JSONResponse({"status": "started", "watch_id": watch_id})


# ── Listings ───────────────────────────────────────────────────────────────

@router.get("/propertyhub/watches/{watch_id}/listings")
async def get_watch_listings(watch_id: int):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    rows = db.get_ph_listings_for_watch(watch_id)
    return JSONResponse([_listing_to_dict(r) for r in rows])


@router.post("/propertyhub/listings/{listing_id}/mute")
async def mute_listing(listing_id: int):
    row = db.get_ph_listing(listing_id)
    if not row:
        return JSONResponse({"error": "ไม่พบ listing"}, status_code=404)
    db.set_ph_listing_muted(listing_id)
    return JSONResponse({"status": "muted", "listing_id": listing_id})


@router.post("/propertyhub/listings/{listing_id}/unmute")
async def unmute_listing(listing_id: int):
    row = db.get_ph_listing(listing_id)
    if not row:
        return JSONResponse({"error": "ไม่พบ listing"}, status_code=404)
    db.clear_ph_listing_muted(listing_id)
    return JSONResponse({"status": "unmuted", "listing_id": listing_id})


# ── Status ─────────────────────────────────────────────────────────────────

@router.get("/propertyhub/status")
async def ph_status():
    watches = [_watch_summary(w) for w in db.get_ph_watches(active_only=False)]
    return JSONResponse({
        "poller_running": state.ph_poller["task"] is not None and not state.ph_poller["task"].done(),
        "watches": watches,
    })
