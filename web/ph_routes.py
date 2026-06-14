"""FastAPI router for Providers monitor feature."""
import asyncio
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import database.db as db
from scraper.registry import PROVIDERS
from web import state
from web.ph_poller import _scrape_watch

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


def _normalize_url(raw: str) -> str:
    return unquote(raw.strip().rstrip("/"))


def _validate_provider_url(provider_id: str, url: str) -> str | None:
    """Return error string if invalid, None if OK."""
    import re
    pattern = PROVIDERS[provider_id].URL_PATTERN
    if not re.match(pattern, url, re.IGNORECASE):
        name = PROVIDERS[provider_id].PROVIDER_NAME
        return f"URL ไม่ถูกต้องสำหรับ {name}"
    return None


def _watch_summary(watch) -> dict:
    w = dict(watch)
    watch_id = w["id"]
    listings = db.get_ph_listings_for_watch(watch_id)
    last_ts = state.ph_poller["watch_last_scraped"].get(watch_id)
    w["listing_count"] = len(listings)
    w["new_count"] = sum(1 for l in listings if l["alerted_at"] is None and not l["is_read"])
    w["last_scraped_ago"] = _seconds_ago(last_ts)
    w["is_scraping"] = watch_id in state.ph_poller["currently_scraping"]
    interval_sec = (w.get("interval_minutes") or 30) * 60
    if last_ts is not None:
        elapsed = time.monotonic() - last_ts
        w["next_in_seconds"] = max(0, int(interval_sec - elapsed))
    else:
        w["next_in_seconds"] = 0
    provider = PROVIDERS.get(w.get("provider_id", "propertyhub"))
    w["provider_name"] = provider.PROVIDER_NAME if provider else w.get("provider_id", "")
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

@router.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    watches = [_watch_summary(w) for w in db.get_ph_watches(active_only=False)]
    all_listings: list[dict] = []
    for w in db.get_ph_watches(active_only=False):
        for row in db.get_ph_listings_for_watch(w["id"]):
            d = _listing_to_dict(row)
            d["watch_label"] = w["label"] or w["project_url"].split("/")[-1]
            all_listings.append(d)
    return templates.TemplateResponse(request, "providers.html", {
        "watches": watches,
        "listings": all_listings,
        "providers": PROVIDERS,
    })


# ── Watch CRUD ─────────────────────────────────────────────────────────────

@router.post("/providers/watches")
async def add_watch(request: Request):
    raw = await request.form()
    provider_id = raw.get("provider_id", "propertyhub")
    if provider_id not in PROVIDERS:
        return JSONResponse({"error": f"ไม่พบ Provider: {provider_id}"}, status_code=400)

    project_url = _normalize_url(raw.get("project_url", ""))
    err = _validate_provider_url(provider_id, project_url)
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
            "provider_id": provider_id,
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


@router.post("/providers/watches/{watch_id}/delete")
async def delete_watch(watch_id: int):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    db.delete_ph_watch(watch_id)
    state.ph_poller["watch_last_scraped"].pop(watch_id, None)
    state.ph_poller["currently_scraping"].discard(watch_id)
    return JSONResponse({"status": "deleted", "watch_id": watch_id})


@router.post("/providers/watches/{watch_id}/edit")
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


@router.post("/providers/watches/{watch_id}/scrape")
async def trigger_scrape(watch_id: int):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    if watch_id in state.ph_poller["currently_scraping"]:
        return JSONResponse({"status": "already_running"})
    state.ph_poller["watch_last_scraped"].pop(watch_id, None)
    asyncio.create_task(_scrape_watch(watch))
    return JSONResponse({"status": "started", "watch_id": watch_id})


# ── Listings ───────────────────────────────────────────────────────────────

@router.get("/providers/watches/{watch_id}/listings")
async def get_watch_listings(watch_id: int):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    rows = db.get_ph_listings_for_watch(watch_id)
    return JSONResponse([_listing_to_dict(r) for r in rows])


@router.post("/providers/watches/{watch_id}/listings/delete-all")
async def delete_all_listings(watch_id: int):
    watch = db.get_ph_watch(watch_id)
    if not watch:
        return JSONResponse({"error": "ไม่พบ watch"}, status_code=404)
    db.delete_ph_listings_for_watch(watch_id)
    return JSONResponse({"status": "deleted", "watch_id": watch_id})


@router.post("/providers/listings/{listing_id}/delete")
async def delete_listing(listing_id: int):
    row = db.get_ph_listing(listing_id)
    if not row:
        return JSONResponse({"error": "ไม่พบ listing"}, status_code=404)
    db.delete_ph_listing(listing_id)
    return JSONResponse({"status": "deleted", "listing_id": listing_id})


@router.post("/providers/listings/{listing_id}/mark-read")
async def mark_listing_read(listing_id: int):
    row = db.get_ph_listing(listing_id)
    if not row:
        return JSONResponse({"error": "ไม่พบ listing"}, status_code=404)
    db.set_ph_listing_read(listing_id)
    return JSONResponse({"status": "read", "listing_id": listing_id})


@router.post("/providers/listings/{listing_id}/mark-unread")
async def mark_listing_unread(listing_id: int):
    row = db.get_ph_listing(listing_id)
    if not row:
        return JSONResponse({"error": "ไม่พบ listing"}, status_code=404)
    db.clear_ph_listing_read(listing_id)
    return JSONResponse({"status": "unread", "listing_id": listing_id})


# ── Status ─────────────────────────────────────────────────────────────────

@router.get("/providers/status")
async def providers_status():
    watches = [_watch_summary(w) for w in db.get_ph_watches(active_only=False)]
    return JSONResponse({
        "poller_running": state.ph_poller["task"] is not None and not state.ph_poller["task"].done(),
        "watches": watches,
    })
