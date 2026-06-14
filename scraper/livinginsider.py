"""Scrape listing data from livinginsider.com project pages.

LivingInsider is a traditional server-rendered PHP site — listing data is
embedded directly in HTML cards. BeautifulSoup parses the cards; no browser
or JS execution needed.
"""
import asyncio
import re

import httpx
from bs4 import BeautifulSoup

PROVIDER_ID = "livinginsider"
PROVIDER_NAME = "LivingInsider"
URL_PATTERN = r"^https?://www\.livinginsider\.com/living_project/\d+/\d+/.+/all/\d+/.+\.html$"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.livinginsider.com/",
    "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
}
_REQUEST_TIMEOUT = 15.0
_MAX_PAGES = 3
_PAGE_DELAY = 1.0

_PAGE_RE = re.compile(r"/all/(\d+)/")


async def scrape_project(
    project_url: str,
    min_price: int | None = None,
    max_price: int | None = None,
    min_size_sqm: float | None = None,
    min_floor: int | None = None,
) -> list[dict]:
    """Fetch all matching listings from a LivingInsider project URL."""
    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=_REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        html = await _fetch_html(client, project_url)
        soup = BeautifulSoup(html, "lxml")
        total_pages = _detect_total_pages(soup)
        pages_to_fetch = min(total_pages, _MAX_PAGES)

        raw_cards = soup.select("div.istock-list")

        for page in range(2, pages_to_fetch + 1):
            await asyncio.sleep(_PAGE_DELAY)
            page_html = await _fetch_html(client, _page_url(project_url, page))
            page_soup = BeautifulSoup(page_html, "lxml")
            raw_cards.extend(page_soup.select("div.istock-list"))

        results = []
        for card in raw_cards:
            try:
                listing = _parse_card(card)
            except Exception:
                continue
            if listing and _passes_filters(listing, min_price, max_price, min_size_sqm, min_floor):
                results.append(listing)
        return results


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await asyncio.wait_for(client.get(url), timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
    except asyncio.TimeoutError:
        raise RuntimeError(f"Timeout fetching {url}")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"HTTP {e.response.status_code} fetching {url}")
    except httpx.HTTPError as e:
        raise RuntimeError(f"Network error fetching {url}: {e}")
    return resp.text


def _detect_total_pages(soup: BeautifulSoup) -> int:
    pagination = soup.select_one("ul.pagination")
    if not pagination:
        return 1
    nums = [
        int(m.group(1))
        for a in pagination.find_all("a", href=True)
        for m in [_PAGE_RE.search(a["href"])]
        if m
    ]
    return max(nums) if nums else 1


def _page_url(base_url: str, page: int) -> str:
    return _PAGE_RE.sub(f"/all/{page}/", base_url)


def _parse_card(card) -> dict | None:
    # Listing ID from the div id attribute: "list2925911" → "2925911"
    card_id = card.get("id", "")
    listing_id = card_id.removeprefix("list") if card_id.startswith("list") else ""
    if not listing_id:
        # fallback: data-web-id on the favorite icon
        fav = card.select_one("img.listing-favorites")
        listing_id = fav["data-web-id"] if fav and fav.get("data-web-id") else ""

    # Listing URL — first detail link inside the card
    first_link = card.select_one('a[href*="/detail/"]')
    listing_url = first_link["href"] if first_link else ""

    # Title
    title_tag = card.select_one(".text-title-card")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # Cover image — first card-cover-img; prefer data-src (lazy load), fall back to src
    img_tag = card.select_one("img.card-cover-img")
    cover_image_url = None
    if img_tag:
        cover_image_url = img_tag.get("data-src") or img_tag.get("src") or None

    # Area (sqm)
    area_tag = card.select_one(".card-prop-area-row .card-prop-value")
    size_sqm: float | None = None
    if area_tag:
        area_text = area_tag.get_text(strip=True).replace("ตร.ม.", "").strip()
        try:
            size_sqm = float(area_text)
        except (ValueError, TypeError):
            pass

    # Floor and room info — items inside .card-prop-room-row
    prop_items = card.select(".card-prop-room-row .card-prop-item .card-prop-value")
    floor: str | None = None
    room_type: str = ""
    if len(prop_items) >= 1:
        raw_floor = prop_items[0].get_text(strip=True)
        floor = raw_floor.replace("ชั้นที่", "").strip() or None
    if len(prop_items) >= 2:
        room_type = prop_items[1].get_text(strip=True)

    # Rent price — only parse if price text contains /ด. (monthly marker).
    # Sale-only listings show "฿7.5M" with no /ด. → treat as no rent price.
    price_tag = card.select_one(".listing_cost .text_price")
    monthly_rent: int | None = None
    if price_tag:
        price_text = price_tag.get_text()
        if "/ด." in price_text:
            monthly_rent = _parse_price(price_text)

    # Last updated date
    date_tag = card.select_one(".istock-lastdate")
    refreshed_at = _parse_thai_date(date_tag.get_text(strip=True)) if date_tag else None

    return {
        "listing_id": str(listing_id),
        "listing_url": listing_url,
        "title": title,
        "monthly_rent": monthly_rent,
        "size_sqm": size_sqm,
        "floor": floor,
        "room_type": room_type,
        "cover_image_url": cover_image_url,
        "refreshed_at": refreshed_at,
    }


def _parse_price(text: str) -> int | None:
    m = re.search(r"[\d,]+", text)
    return int(m.group(0).replace(",", "")) if m else None


def _parse_thai_date(raw: str) -> str | None:
    m = re.search(r"(\d{1,2})/(\d{2})/(\d{4})", raw)
    if not m:
        return None
    day, month, year_be = m.group(1), m.group(2), int(m.group(3))
    return f"{year_be - 543}-{month}-{day.zfill(2)}"


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
            pass  # range like "21-50" or None — skip filter

    return True
