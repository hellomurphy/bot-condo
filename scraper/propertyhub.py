"""Scrape listing data from propertyhub.in.th project pages.

PropertyHub is a Next.js app — all listing data is embedded in a
<script id="__NEXT_DATA__"> JSON block in the initial HTML response.
No Playwright or HTML parsing needed; httpx alone is sufficient.
"""
import asyncio
import json
import re

PROVIDER_ID = "propertyhub"
PROVIDER_NAME = "PropertyHub"
URL_PATTERN = r"^https?://propertyhub\.in\.th/[^/]+/[^/]+"

import httpx

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "th-TH,th;q=0.9,en;q=0.8",
}
_IMAGE_CDN = "https://bcdn.propertyhub.in.th"
_REQUEST_TIMEOUT = 10.0
_MAX_PAGES = 2  # newest listings cluster on early pages; caps request count
_PAGE_DELAY = 0.8  # seconds between page requests

_ROOM_TYPE_MAP = {
    "STUDIO": "Studio",
    "ONE_BED_ROOM": "1 ห้องนอน",
    "TWO_BED_ROOM": "2 ห้องนอน",
    "THREE_BED_ROOM": "3 ห้องนอน",
    "FOUR_BED_ROOM": "4 ห้องนอน",
    "PENTHOUSE": "Penthouse",
}

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


async def scrape_project(
    project_url: str,
    min_price: int | None = None,
    max_price: int | None = None,
    min_size_sqm: float | None = None,
    min_floor: int | None = None,
) -> list[dict]:
    """Fetch all matching listings from a PropertyHub project URL.

    Raises RuntimeError on network failure, ValueError if the URL is not a
    valid PropertyHub project page.
    """
    async with httpx.AsyncClient(headers=_HEADERS, timeout=_REQUEST_TIMEOUT, follow_redirects=True) as client:
        page_props = await _fetch_page_props(client, project_url, page=1)
        listings_data = page_props.get("listings")
        if not listings_data:
            raise ValueError(f"No listings found at {project_url} — check the URL is a valid project page")

        raw_items = listings_data.get("listings") or listings_data.get("data") or []
        total_pages = (listings_data.get("pagination") or {}).get("totalPages", 1)
        pages_to_fetch = min(total_pages, _MAX_PAGES)

        for page in range(2, pages_to_fetch + 1):
            await asyncio.sleep(_PAGE_DELAY)
            extra = await _fetch_page_props(client, project_url, page=page)
            extra_block = extra.get("listings") or {}
            extra_items = extra_block.get("listings") or extra_block.get("data") or []
            raw_items.extend(extra_items)

        results = []
        for raw in raw_items:
            try:
                listing = _parse_listing(raw)
            except Exception:
                continue
            if _passes_filters(listing, min_price, max_price, min_size_sqm, min_floor):
                results.append(listing)
        return results


async def _fetch_page_props(client: httpx.AsyncClient, base_url: str, page: int) -> dict:
    url = base_url if page == 1 else f"{base_url}?page={page}"
    try:
        resp = await asyncio.wait_for(client.get(url), timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except asyncio.TimeoutError:
        raise RuntimeError(f"Timeout fetching {url}")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"HTTP {e.response.status_code} fetching {url}")
    except httpx.HTTPError as e:
        raise RuntimeError(f"Network error fetching {url}: {e}")

    m = _NEXT_DATA_RE.search(resp.text)
    if not m:
        raise ValueError(f"No __NEXT_DATA__ found in response from {url}")
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse __NEXT_DATA__ JSON from {url}: {e}")
    return data.get("props", {}).get("pageProps", {})


def _parse_listing(raw: dict) -> dict:
    listing_id = raw.get("id") or raw.get("slug") or ""

    listing_url = (
        f"https://propertyhub.in.th/listings/{listing_id}"
        if listing_id
        else ""
    )

    # Price
    price_obj = (raw.get("price") or {}).get("forRent") or {}
    monthly_obj = price_obj.get("monthly") or {}
    monthly_rent: int | None = None
    if monthly_obj.get("type") == "AMOUNT":
        try:
            monthly_rent = int(monthly_obj["price"])
        except (TypeError, ValueError, KeyError):
            pass

    # Room info
    room_info = raw.get("roomInformation") or {}
    size_sqm: float | None = None
    try:
        size_sqm = float(room_info["roomArea"])
    except (TypeError, ValueError, KeyError):
        pass

    floor_raw = room_info.get("onFloor")
    floor = str(floor_raw) if floor_raw is not None else None

    room_type_raw = room_info.get("roomType", "")
    room_type = _ROOM_TYPE_MAP.get(room_type_raw, room_type_raw)

    # Cover image
    cover_raw = raw.get("coverPicture") or ""
    cover_image_url = (_IMAGE_CDN + cover_raw) if cover_raw.startswith("/") else cover_raw or None

    # Title — try common fields
    title = raw.get("title") or raw.get("name") or raw.get("slug") or ""

    return {
        "listing_id": str(listing_id),
        "listing_url": listing_url,
        "title": title,
        "monthly_rent": monthly_rent,
        "size_sqm": size_sqm,
        "floor": floor,
        "room_type": room_type,
        "cover_image_url": cover_image_url,
        "refreshed_at": raw.get("refreshedAt") or raw.get("modifiedAt"),
    }


def _passes_filters(
    listing: dict,
    min_price: int | None,
    max_price: int | None,
    min_size_sqm: float | None,
    min_floor: int | None,
) -> bool:
    rent = listing.get("monthly_rent")
    if min_price is not None:
        if rent is None or rent < min_price:
            return False
    if max_price is not None:
        if rent is None or rent > max_price:
            return False

    size = listing.get("size_sqm")
    if min_size_sqm is not None:
        if size is None or size < min_size_sqm:
            return False

    if min_floor is not None:
        floor_str = listing.get("floor")
        try:
            floor_int = int(floor_str)
            if floor_int < min_floor:
                return False
        except (TypeError, ValueError):
            pass  # non-numeric floor (G, B1) — skip the filter

    return True
