"""Background asyncio task that polls PropertyHub watches on their intervals."""
import asyncio
import time

import database.db as db
from alerts.notify import notify_ph_listing
from scraper.propertyhub import scrape_project
from web import state

_LOOP_SLEEP = 60  # seconds between poll-loop wakeups


async def poll_loop():
    """Run forever while the FastAPI app is alive. Errors are logged, never raised."""
    while True:
        try:
            await _run_all_watches()
        except Exception as e:
            print(f"[ph_poller] loop error: {e}")
        await asyncio.sleep(_LOOP_SLEEP)


async def _run_all_watches():
    try:
        watches = db.get_ph_watches(active_only=True)
    except Exception as e:
        print(f"[ph_poller] failed to load watches: {e}")
        return

    for watch in watches:
        watch_id = watch["id"]

        if watch_id in state.ph_poller["currently_scraping"]:
            continue

        last = state.ph_poller["watch_last_scraped"].get(watch_id)
        interval_sec = (watch["interval_minutes"] or 30) * 60
        if last is not None and (time.monotonic() - last) < interval_sec:
            continue

        asyncio.create_task(_scrape_watch(watch))


async def _scrape_watch(watch):
    watch_id = watch["id"]
    state.ph_poller["currently_scraping"].add(watch_id)
    try:
        listings = await scrape_project(
            project_url=watch["project_url"],
            min_price=watch["min_price"],
            max_price=watch["max_price"],
            min_size_sqm=watch["min_size_sqm"],
            min_floor=watch["min_floor"],
        )
        for lst in listings:
            try:
                ph_id, is_new = db.upsert_ph_listing(watch_id, lst)
                if is_new:
                    notify_ph_listing(
                        title=lst.get("title"),
                        rent=lst.get("monthly_rent"),
                        size=lst.get("size_sqm"),
                        floor=lst.get("floor"),
                    )
                    db.set_ph_listing_alerted(ph_id)
            except Exception as e:
                print(f"[ph_poller] watch {watch_id} listing upsert error: {e}")

        state.ph_poller["watch_last_scraped"][watch_id] = time.monotonic()
        print(f"[ph_poller] watch {watch_id} scraped {len(listings)} matching listings")
    except Exception as e:
        print(f"[ph_poller] watch {watch_id} scrape failed: {e}")
    finally:
        state.ph_poller["currently_scraping"].discard(watch_id)
