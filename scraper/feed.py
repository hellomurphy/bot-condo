"""Facebook group feed scraper — scroll, extract text + image URLs, pre-filter."""
import asyncio
import base64
import binascii
import hashlib
import random
import re
import unicodedata
from urllib.parse import urlparse, parse_qs

from playwright.async_api import BrowserContext, Page

import config
from scraper.browser import (
    SessionExpiredError,
    describe_session_status,
    is_session_valid,
)

# คำที่บ่งบอกว่าเจ้าของ/นายหน้า "ประกาศให้เช่า" (supply side)
RENT_KEYWORDS = [
    "ให้เช่า", "ปล่อยเช่า", "ปล่อยห้อง", "หาผู้เช่า", "ให้เช่าด่วน",
    "ห้องว่าง", "ว่างต้นเดือน", "ว่างสิ้นเดือน", "ว่างแล้ว",
    "ปล่อยต่อ", "หาคนรับช่วง", "สัญญาเช่า", "ราคาเช่า", "ค่าเช่า",
    "มีห้องว่าง", "ห้องพร้อมอยู่", "รับฝากเช่า", "พร้อมอยู่", "เข้าอยู่",
]
# "เช่า" อยู่แยกเพราะเป็นคำกลาง ทั้ง seeker และ landlord ใช้ได้
WEAK_RENT_KEYWORDS = ["เช่า"]

SALE_KEYWORDS = [
    "ขาย", "ขายดาวน์", "รับฝากขาย", "ผ่อนตรง", "โครงการใหม่", "presale",
    "investment", "yield", "ขายขาด", "โอนกรรมสิทธิ์", "ราคาขาย",
]
# คำขายที่ปนกับเช่าได้ (weak)
WEAK_SALE_KEYWORDS = ["ดาวน์", "โอน", "กู้", "ผ่อน"]

# คำที่บ่งบอกว่าผู้เช่า "ประกาศหาห้อง" (demand side)
SEEKER_KEYWORDS = [
    "หาห้อง", "หาคอนโด", "หาที่พัก", "มองหาห้อง", "มองหาคอนโด",
    "ต้องการเช่า", "ต้องการห้อง", "ต้องการหาห้อง",
    "รบกวนแนะนำ", "ช่วยแนะนำ", "ขอแนะนำ", "แนะนำด้วย", "แนะนำห้อง",
    "งบไม่เกิน", "ไม่เกินเดือนละ", "ราคาไม่เกิน",
    "ใครมีห้องว่าง", "มีใครมีห้อง", "ฝากหาห้อง",
    "กำลังหาห้อง", "กำลังหาคอนโด", "อยากได้ห้อง",
]
# คำ seeker ที่อ่อนกว่า (ต้องมีคู่กับ seeker อื่น)
WEAK_SEEKER_KEYWORDS = ["งบประมาณ", "มองหา", "เพิ่งย้าย", "กำลังหา"]

# first-person seeker — signal แรง ถ้ามีอันนี้แทบ confirm ได้เลย
SEEKER_FIRST_PERSON = [
    "ผมกำลังหา", "หนูกำลังหา", "เรากำลังหา", "ดิฉันกำลังหา",
    "ผมต้องการ", "หนูต้องการ", "ดิฉันต้องการ",
    "ผมมองหา", "หนูมองหา", "ผมหาคอนโด", "หนูหาคอนโด",
    "ผมหาห้อง", "หนูหาห้อง", "อยากหาห้อง", "อยากได้ห้อง",
    "ขอฝากหา", "รบกวนช่วยหา",
]

# signal เชิงโครงสร้างที่บ่งบอก listing จริง (supply)
LISTING_STRUCTURE_PATTERNS = [
    re.compile(r'\d+\s*ห้องนอน'),           # "2 ห้องนอน"
    re.compile(r'\d+\s*(?:sqm|ตรม|ตารางเมตร)'),  # "28 sqm"
    re.compile(r'ชั้น\s*\d+'),              # "ชั้น 15"
    re.compile(r'(?:มัดจำ|ประกัน|deposit)\s*\d+'), # "มัดจำ 2 เดือน"
    re.compile(r'\d+\s*เดือน(?:ล่วงหน้า|แรก|มัดจำ)'), # "2 เดือนล่วงหน้า"
    re.compile(r'(?:line|โทร|tel|contact)\s*[:@]?\s*\S+', re.IGNORECASE),  # ติดต่อ
]

PRICE_PATTERN = re.compile(r'\d[\d,]*\s*(?:บาท|฿|baht)', re.IGNORECASE)
MONTHLY_PRICE_PATTERN = re.compile(r'\d[\d,]*\s*(?:บาท|฿)?\s*(?:/เดือน|ต่อเดือน|/month|บาท/เดือน)')
BUDGET_PATTERN = re.compile(r'งบ\s*\d[\d,]*|ไม่เกิน\s*\d[\d,]*|ราคาไม่เกิน\s*\d[\d,]*|งบประมาณ\s*\d[\d,]*')

# Fast-pass: จับตัวเลขราคาค่าเช่ารายเดือน ไม่ต้องมี unit ก็ได้
# รองรับ: 8500, 8,500, 8.5k, 8.5K, 12K
_BUDGET_PRICE_RE = re.compile(
    r'(?<!\d)'
    r'(\d+(?:\.\d+)?[kK]|\d+(?:,\d{3})*(?:\.\d+)?)'
    r'(?!\d)'
)


def _has_budget_price(text: str, min_price: float, max_price: float) -> bool:
    """True ถ้าเจอตัวเลขในช่วง [min_price, max_price] — ใช้เป็น fast-pass ส่ง DeepSeek"""
    for m in _BUDGET_PRICE_RE.finditer(text):
        raw = m.group(1)
        try:
            value = float(raw[:-1]) * 1000 if raw[-1].lower() == 'k' else float(raw.replace(',', ''))
            if min_price <= value <= max_price:
                return True
        except ValueError:
            continue
    return False


# Supply signals ในเมนต์ — คำที่คน "มีห้องให้เช่า" พิมพ์เท่านั้น
# ต้องเจาะจง ไม่ใช้คำกลางอย่าง "บาท" หรือ "หา"
COMMENT_SUPPLY_SIGNALS = [
    # บอกว่ามีห้อง
    "มีค่ะ", "มีครับ", "มีนะคะ", "มีนะครับ", "มีห้องว่าง", "ห้องว่าง",
    "ว่างค่ะ", "ว่างครับ", "ว่างอยู่", "ว่างแล้ว",
    "ให้เช่า", "ปล่อยเช่า", "ปล่อยห้อง", "ปล่อยต่อ", "รับช่วงต่อ",
    "พร้อมอยู่", "เข้าอยู่ได้", "เข้าได้เลย",
    # ชวนติดต่อ
    "inbox", "inboxได้", "inbox มา", "ทักมาได้", "ทักได้เลย",
    "โทรได้", "ติดต่อได้", "ส่งรูป", "มีรูป", "ดูห้องได้",
    "line:", "line :", "line id", "@line",
    # รายละเอียด listing
    "ชั้น", "sqm", "ตรม", "ตารางเมตร",
    "เฟอร์", "เฟอร์ครบ", "เฟอร์บางส่วน",
    "แอร์", "เครื่องซักผ้า", "ตู้เย็น",
    "มัดจำ", "ประกัน", "deposit",
    "บาท/เดือน", "บาท/เดือน", "/เดือน", "ต่อเดือน",
]

# Blacklist — โพสประเภทนี้ตัดทิ้งก่อนทุก logic รวมถึง fast-pass
# เพราะถึงจะมีตัวเลขราคาในงบก็ไม่ใช่ห้องเช่ารายเดือนตรงๆ
BLACKLIST_KEYWORDS = [
    # หารูมเมท
    "หารูมเมท", "หาเพื่อนร่วมห้อง", "หาคนมาอยู่ด้วย", "รูมเมท",
    # หอพัก
    "หอพัก",
    # นายหน้ารับฝาก / co-agent
    "รับฝากขาย", "รับฝากเช่า", "โคเอเจ้นท์", "co-agent", "co agent",
    "ยินดีรับโคเอเจ้นท์", "ยินดีโคเอเจ้นท์",
    # รายวัน / รายสัปดาห์
    "รายวัน", "รายสัปดาห์", "/วัน", "ต่อวัน", "ต่อคืน", "รายคืน",
    # ขาย / โครงการ / Presale
    "ขายดาวน์", "โครงการใหม่", "presale", "pre-sale", "pre sale",
    "ราคาขาย", "ขายขาด",
]

BLACKLIST_PATTERNS = [
    # ครอบ typo / สะกดแปรผันของ "อพาร์ทเมนท์", "อพาร์ทเม้นท์", "อพาร์ตเมนต์"
    re.compile(r"อพาร[์]?(?:ท|ต)(?:เม[น็้]?นท[์]?|เมนต[์]?|ตเมนต[์]?)", re.IGNORECASE),
    re.compile(r"(?<!\S)หอ(?!\S)"),
    re.compile(r"แมนช(?:ัน|ั่น|ั่ยน|ั่นท์|ั่นต์|ชั่น)", re.IGNORECASE),
    re.compile(r"\bapartments?\b", re.IGNORECASE),
    re.compile(r"\bmansion\b", re.IGNORECASE),
]

HOUSE_RENT_KEYWORDS = [
    "บ้านให้เช่า", "บ้านปล่อยเช่า", "ปล่อยเช่าบ้าน", "ให้เช่าบ้าน", "บ้านเช่า",
    "house for rent", "home for rent",
]

HOUSE_TYPE_KEYWORDS = [
    "บ้านเดี่ยว", "บ้านแฝด", "ทาวน์โฮม", "ทาวน์เฮ้าส์", "ทาวน์เฮาส์",
    "โฮมออฟฟิศ", "single house", "townhome", "townhouse", "home office",
]

CONDO_ALLOWLIST_KEYWORDS = [
    "คอนโด", "condo", "condominium", "คอนโดมิเนียม",
]

FB_RESTRICTED_SIGNALS = [
    "ไม่สามารถดูเนื้อหานี้ได้ในขณะนี้",
    "เหตุการณ์นี้มักจะเกิดขึ้นเนื่องจากเจ้าของแชร์เนื้อหา",
    "เนื้อหาถูกลบไปแล้ว",
    "this content isn't available right now",
    "this content is not available right now",
    "this content may have been removed",
]

# Parasite signals — seeker คนอื่นมาเกาะหาด้วย → drop ทันที
# ถึงแม้จะมีตัวเลขงบประมาณก็ตาม
COMMENT_PARASITE_SIGNALS = [
    "หาด้วย", "ตามด้วย", "ขอด้วย", "เอาด้วย",
    "เหมือนกันเลย", "เหมือนกันค่ะ", "เหมือนกันครับ",
    "ขอด้วยคน", "หาด้วยคน", "เอาด้วยคน",
    "แบบเดียวกัน", "แบบนี้ด้วย",
    "ตามหาเหมือนกัน", "กำลังหาเหมือนกัน",
    "ขอแชร์ด้วย", "ขอติดตามด้วย",
]

POST_SELECTORS = [
    '[role="article"]',
    'div[data-pagelet*="FeedUnit"]',
    'div[data-ad-preview="message"]',
]
TEXT_SELECTORS = [
    '[data-ad-preview="message"]',
    'div[dir="auto"]',
]
LINK_PATTERNS = ['/posts/', 'story_fbid', '/permalink/']
IMAGE_CDN_PATTERN = 'scontent'

PHONE_RE = re.compile(r'0[689]\d{8}')
LINE_RE  = re.compile(r'(?i)(line\s*[:\s]*[id]*\s*[:@]?\s*\S+)')


def _score_signals(text: str) -> dict:
    """คำนวณ signal scores ทั้งหมดจากข้อความโพส"""
    return {
        "rent":         sum(1 for kw in RENT_KEYWORDS if kw in text),
        "weak_rent":    sum(1 for kw in WEAK_RENT_KEYWORDS if kw in text),
        "sale":         sum(1 for kw in SALE_KEYWORDS if kw in text),
        "weak_sale":    sum(1 for kw in WEAK_SALE_KEYWORDS if kw in text),
        "seeker":       sum(1 for kw in SEEKER_KEYWORDS if kw in text),
        "weak_seeker":  sum(1 for kw in WEAK_SEEKER_KEYWORDS if kw in text),
        "first_person": sum(1 for kw in SEEKER_FIRST_PERSON if kw in text),
        "listing_struct": sum(1 for p in LISTING_STRUCTURE_PATTERNS if p.search(text)),
        "has_monthly_price": bool(MONTHLY_PRICE_PATTERN.search(text)),
        "has_budget":   bool(BUDGET_PATTERN.search(text)),
        "has_price":    bool(PRICE_PATTERN.search(text)),
    }


def _is_restricted_content(text: str) -> bool:
    text_lower = text.lower()
    return any(pattern in text_lower for pattern in FB_RESTRICTED_SIGNALS)


def _is_house_rental_post(text: str) -> bool:
    text_lower = text.lower()

    if any(kw in text_lower for kw in HOUSE_RENT_KEYWORDS):
        return True

    if any(kw in text_lower for kw in HOUSE_TYPE_KEYWORDS):
        return True

    has_standalone_house = bool(re.search(r"(?<![A-Za-z])บ้าน(?![A-Za-z])", text))
    has_condo_context = any(kw in text_lower for kw in CONDO_ALLOWLIST_KEYWORDS)
    has_rent_context = (
        any(kw in text for kw in RENT_KEYWORDS)
        or any(kw in text_lower for kw in WEAK_RENT_KEYWORDS)
        or bool(MONTHLY_PRICE_PATTERN.search(text))
    )
    if has_standalone_house and has_rent_context and not has_condo_context:
        return True

    return False


def _data_url_payload_bytes(data_url: str) -> int:
    if not data_url or "," not in data_url:
        return 0

    _, payload = data_url.split(",", 1)
    if not payload:
        return 0

    try:
        return len(base64.b64decode(payload, validate=True))
    except (binascii.Error, ValueError):
        return 0


def _is_usable_image_data_url(data_url: str, min_bytes: int | None = None) -> bool:
    threshold = min_bytes if min_bytes is not None else config.FB_IMAGE_MIN_BYTES
    return _data_url_payload_bytes(data_url) >= threshold


def classify_post_intent(text: str) -> str:
    """
    แยกประเภทโพส:
      "restricted" — โพสถูก Facebook ล็อก/ลบ เนื้อหาใช้งานไม่ได้
      "for_rent"  — ประกาศให้เช่า (เจ้าของ/นายหน้าโพส)
      "seeking"   — ประกาศหาห้อง (ผู้เช่าโพสหา)
      "sale"      — ประกาศขาย (รวม blacklist ที่ตัดทิ้งทันที)
      "ambiguous" — ไม่ชัดเจน / signal ผสม

    ลำดับการตัดสิน:
      1. Restricted guard — เนื้อหาโดน Facebook ล็อก/ลบ → skip ทันที
      2. Blacklist guard — ตัดขยะชัดเจนก่อนทุก logic (หารูมเมท, รายวัน, presale ฯลฯ)
      3. Signal scoring
      4. Budget price fast-pass — ถ้าผ่าน blacklist และเจอราคาในงบ → ambiguous ทันที
      5. Rules ปกติ (weighted supply vs demand)
    """
    text_lower = text.lower()

    # --- ขั้น 1: Restricted Guard ---
    if _is_restricted_content(text):
        return "restricted"

    # --- ขั้น 2: Blacklist Guard ---
    # ทำก่อน fast-pass เพราะโพส "หารูมเมท ตกคนละ 8,500" มีตัวเลขในงบแต่ไม่ใช่ห้องเช่าจริง
    if any(kw in text or kw in text_lower for kw in BLACKLIST_KEYWORDS):
        return "sale"  # reuse branch skip ที่มีอยู่แล้วใน scrape_group()
    if any(pattern.search(text) for pattern in BLACKLIST_PATTERNS):
        return "sale"  # reuse branch skip ที่มีอยู่แล้วใน scrape_group()
    if _is_house_rental_post(text):
        return "sale"  # house rentals are out of scope for condo-only groups

    # --- ขั้น 3: คำนวณ signal scores ---
    s = _score_signals(text)

    # --- ขั้น 4: Budget Price Fast-Pass (Layer 2) ---
    # โพสสั้น "8,500/เดือน ติดต่อ LINE @xxx" ไม่มี RENT_KEYWORDS แต่คือห้องจริง
    # ถ้าผ่าน blacklist แล้วเจอราคาในช่วงงบ → ส่ง DeepSeek ทันที ไม่รอคะแนน keyword
    if _has_budget_price(text, config.TARGET_BUDGET * 0.5, config.MAX_BUDGET * 1.1):
        return "ambiguous"

    # --- คำนวณ supply_weight และ demand_weight ---
    # supply = คนมีห้อง (landlord/agent)
    supply_weight = (
        s["rent"] * 3          # "ให้เช่า", "ปล่อยเช่า" — แรงมาก
        + s["weak_rent"] * 1   # "เช่า" คำเดียว — กลาง
        + s["listing_struct"] * 2  # โครงสร้าง listing: ชั้น, sqm, มัดจำ
        + (2 if s["has_monthly_price"] else 0)  # ราคา/เดือน บ่งบอก listing
    )
    # demand = คนหาห้อง (seeker/renter)
    demand_weight = (
        s["first_person"] * 4  # "ผมกำลังหา" — แรงมาก
        + s["seeker"] * 3      # "หาห้อง", "รบกวนแนะนำ" — แรง
        + s["weak_seeker"] * 1 # "งบประมาณ", "มองหา" — อ่อน
        + (2 if s["has_budget"] else 0)  # "งบไม่เกิน 10,000"
    )

    # --- Rule 0: first-person seeker คือ signal แรงสุด ---
    # "ผมกำลังหา..." แทบ confirm ได้ว่าเป็น seeker เสมอ
    if s["first_person"] >= 1 and supply_weight < 4:
        return "seeking"

    # --- Rule 1: sale ชัดเจน (ไม่มีสัญญาณเช่าเลย) ---
    # ต้องมี strong sale keyword และไม่มี rent/seeker signal
    if s["sale"] >= 1 and s["rent"] == 0 and s["seeker"] == 0 and s["first_person"] == 0:
        # แต่ถ้ามี weak_sale อย่างเดียว (เช่น "ดาวน์", "โอน") อาจเป็น deposit/transfer ก็ได้
        return "sale"

    # --- Rule 2: demand ชนะ supply อย่างชัดเจน → seeking ---
    if demand_weight > 0 and supply_weight == 0:
        return "seeking"
    if demand_weight >= 4 and demand_weight > supply_weight * 1.5:
        return "seeking"

    # --- Rule 3: supply ชนะ demand อย่างชัดเจน → for_rent ---
    # ต้องมี supply_weight สูงพอ (≥3) หรือมี listing structure ช่วย
    # ถ้ามีแค่ "เช่า" คำเดียว (weak_rent=1, supply=1) ยังไม่พอ
    if supply_weight >= 3 and supply_weight > demand_weight * 1.5:
        # ถ้ามีคำขายด้วย (เช่น "ขายหรือให้เช่า") → ambiguous
        if s["sale"] >= 1:
            return "ambiguous"
        return "for_rent"

    # --- Rule 4: มี listing structure signal แรง = for_rent เกือบแน่ ---
    # เช่น "ชั้น 15, 28 sqm, มัดจำ 2 เดือน" ชัดเจนว่าเป็น listing
    # ต้องมี struct ≥ 2 เพื่อให้มั่นใจ ไม่ใช่แค่เจอ "ชั้น" คำเดียว
    if s["listing_struct"] >= 2 and s["seeker"] == 0 and s["first_person"] == 0:
        if s["sale"] >= 1:
            return "ambiguous"
        return "for_rent"

    # --- Rule 5: budget pattern + seeker keyword → seeking ---
    if s["has_budget"] and (s["seeker"] >= 1 or s["weak_seeker"] >= 2):
        if supply_weight < 3:
            return "seeking"

    # --- Rule 6: ราคา/เดือน + ไม่มี seeker signal = น่าจะ for_rent ---
    if s["has_monthly_price"] and s["seeker"] == 0 and s["first_person"] == 0:
        if s["sale"] >= 1:
            return "ambiguous"
        return "for_rent"

    # --- Rule 7: sale + rent ปนกัน = ambiguous ---
    if s["sale"] >= 1 and (s["rent"] >= 1 or s["weak_rent"] >= 1):
        return "ambiguous"

    # --- Fallback: ต้องการ supply_weight ≥ 4 ถึงจะ for_rent ---
    # supply_weight=1 (แค่ "เช่า" คำเดียว) ไม่พอ → ambiguous
    # tie (supply == demand) → ambiguous เสมอ ให้ AI ชี้ขาด ไม่ใช้ Python ตัดสิน
    if supply_weight > demand_weight and supply_weight >= 4:
        return "for_rent"
    if demand_weight > supply_weight and demand_weight >= 4:
        return "seeking"

    return "ambiguous"


def _normalize_comment_text(text: str) -> str:
    text = text.replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


COMMENT_UI_NOISE = {
    "ตอบกลับ", "แชร์", "ถูกใจ", "แสดงความเห็น", "แสดงความคิดเห็น", "เขียนความคิดเห็น",
    "ดูความคิดเห็นเพิ่มเติม", "view more comments", "most relevant", "all comments",
    "แก้ไข", "edit", "like", "reply", "send",
}

COMMENT_TIME_PATTERN = re.compile(
    r"^(?:\d+\s*(?:นาที|ชั่วโมง|วัน|สัปดาห์|เดือน|ปี|ชม\.?|hr|hrs|hour|hours|min|mins)"
    r"|เมื่อวานนี้|just now)$",
    re.IGNORECASE,
)


def _looks_like_noise_line(line: str) -> bool:
    lower = line.lower().strip()
    if not lower:
        return True
    if lower in COMMENT_UI_NOISE:
        return True
    if COMMENT_TIME_PATTERN.match(lower):
        return True
    if lower.startswith("author"):
        return True
    if lower in {"เจ้าของโพสต์", "original poster"}:
        return True
    return False


def _clean_comment_fragments(fragments: list[str], post_text: str | None = None) -> str | None:
    post_norm = _normalize_text_for_hash(post_text or "")
    cleaned: list[str] = []
    seen: set[str] = set()

    for fragment in fragments:
        line = _normalize_comment_text(fragment)
        if not line or _looks_like_noise_line(line):
            continue
        norm_line = _normalize_text_for_hash(line)
        if not norm_line or norm_line in seen:
            continue
        if post_norm and (
            norm_line == post_norm
            or (norm_line in post_norm and len(norm_line) >= max(20, int(len(post_norm) * 0.6)))
        ):
            continue
        seen.add(norm_line)
        cleaned.append(line)

    if not cleaned:
        return None

    comment_text = "\n".join(cleaned).strip()
    comment_norm = _normalize_text_for_hash(comment_text)
    if not comment_norm:
        return None

    if post_norm and (
        comment_norm == post_norm
        or (comment_norm in post_norm and len(comment_norm) >= max(20, int(len(post_norm) * 0.6)))
    ):
        return None

    return comment_text


def _extract_comment_blocks_from_fragments(
    fragment_groups: list[list[str]],
    post_text: str | None = None,
) -> list[str]:
    comments: list[str] = []
    seen: set[str] = set()
    for fragments in fragment_groups:
        comment = _clean_comment_fragments(fragments, post_text=post_text)
        if not comment:
            continue
        norm = _normalize_text_for_hash(comment)
        if norm in seen:
            continue
        seen.add(norm)
        comments.append(comment)
    return comments


def _comment_supply_score(comment: str) -> int:
    lower = comment.lower()
    score = 0
    score += sum(2 for kw in COMMENT_SUPPLY_SIGNALS if kw.lower() in lower)
    score += sum(2 for pattern in LISTING_STRUCTURE_PATTERNS if pattern.search(comment))
    if MONTHLY_PRICE_PATTERN.search(comment):
        score += 3
    elif PRICE_PATTERN.search(comment):
        score += 1
    if PHONE_RE.search(comment) or LINE_RE.search(comment):
        score += 2
    if any(kw in lower for kw in ["mrt", "bts", "arl", "สถานี", "พระราม 9", "พระราม9"]):
        score += 1
    if any(token in comment for token in ["คอนโด", "Condo", "Tower", "Place", "Asoke", "Rama 9", "Ratchada"]):
        score += 1
    return score


def filter_listing_comments(comments: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """
    กรองเมนต์ในโพส seeking — เอาเฉพาะที่คน "มีห้องเสนอ" จริงๆ

    Return:
      accepted_comments, dropped[(comment, reason)]
    """
    accepted: list[str] = []
    dropped: list[tuple[str, str]] = []

    for comment in comments:
        c_lower = comment.lower()

        if any(kw in c_lower for kw in COMMENT_PARASITE_SIGNALS):
            dropped.append((comment, "parasite_seeker"))
            continue

        score = _comment_supply_score(comment)
        if score >= 2:
            accepted.append(comment)
            continue

        dropped.append((comment, "low_supply_signal"))

    return accepted, dropped


def _redact(text: str) -> str:
    text = PHONE_RE.sub('[PHONE]', text)
    text = LINE_RE.sub('[LINE]', text)
    return text


_HASH_NOISE_TRANSLATION = str.maketrans({
    "\u00a0": "",
    "\u200b": "",
    "\u200c": "",
    "\u200d": "",
    "\ufeff": "",
})


def _normalize_text_for_hash(text: str) -> str:
    cleaned = text.translate(_HASH_NOISE_TRANSLATION)
    # Ignore whitespace-only formatting drift between cross-group copy-pastes.
    return re.sub(r"\s+", "", cleaned).strip()


def _content_hash(text: str) -> str | None:
    normalized = _normalize_text_for_hash(text)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode()).hexdigest()


def _extract_post_id(url: str) -> str | None:
    # /posts/123456  or  story_fbid=123456  or  /permalink/123456
    m = re.search(r'/posts/(\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'story_fbid[=/](\d+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/permalink/(\d+)', url)
    if m:
        return m.group(1)
    return None


async def _get_post_text(post_el) -> str:
    for sel in TEXT_SELECTORS:
        try:
            els = await post_el.query_selector_all(sel)
            parts = []
            for el in els:
                t = await el.inner_text()
                if t.strip():
                    parts.append(t.strip())
            if parts:
                return unicodedata.normalize("NFKC", "\n".join(parts))
        except Exception:
            continue
    try:
        return unicodedata.normalize("NFKC", (await post_el.inner_text()).strip())
    except Exception:
        return ""


async def _get_post_url(post_el) -> str | None:
    try:
        links = await post_el.query_selector_all('a[href]')
        for link in links:
            href = await link.get_attribute('href')
            if href and any(p in href for p in LINK_PATTERNS):
                # Clean tracking params
                return href.split('?')[0]
    except Exception:
        pass
    return None


async def _get_image_urls(post_el) -> list[str]:
    try:
        imgs = await post_el.query_selector_all('img[src]')
        urls = []
        for img in imgs:
            src = await img.get_attribute('src')
            if src and IMAGE_CDN_PATTERN in src:
                urls.append(src)
        return urls
    except Exception:
        return []


async def download_image_as_base64(page: Page, url: str) -> str | None:
    """Download image through logged-in browser context (FB CDN requires session cookies)."""
    try:
        result = await page.evaluate(f"""
            async () => {{
                const r = await fetch('{url}', {{credentials: 'include'}});
                const b = await r.blob();
                return new Promise(res => {{
                    const reader = new FileReader();
                    reader.onloadend = () => res(reader.result);
                    reader.readAsDataURL(b);
                }});
            }}
        """)
        return result
    except Exception:
        return None


COMMENT_CONTAINER_SELECTORS = [
    'div[role="article"]',
    'ul li',
]


async def scrape_comments(
    context: BrowserContext,
    post_url: str,
    max_comments: int = 30,
    post_text: str | None = None,
    max_clicks: int | None = None,
) -> list[str]:
    """Open post URL and extract comment texts. Returns list of non-empty strings."""
    if not post_url:
        return []
    _max_clicks = max_clicks if max_clicks is not None else config.FB_COMMENT_MAX_CLICKS
    page = await context.new_page()
    comments: list[str] = []
    try:
        await page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        if any(kw in page.url for kw in ["login", "checkpoint"]):
            return []

        # Scroll down to load more comments (infinite scroll)
        for _ in range(_max_clicks):
            try:
                prev_height = await page.evaluate("document.body.scrollHeight")
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(
                    random.uniform(config.FB_COMMENT_CLICK_DELAY_MIN, config.FB_COMMENT_CLICK_DELAY_MAX)
                )
                new_height = await page.evaluate("document.body.scrollHeight")
                if new_height == prev_height:
                    break
            except Exception:
                break

        fragment_groups: list[list[str]] = []
        for sel in COMMENT_CONTAINER_SELECTORS:
            try:
                containers = await page.query_selector_all(sel)
                for container in containers:
                    # Skip parent containers that wrap other comment articles —
                    # those are post/thread wrappers, not individual comments.
                    # Only process leaf-level containers.
                    if sel == 'div[role="article"]':
                        nested = await container.query_selector_all('div[role="article"]')
                        if nested:
                            continue
                    text_nodes = await container.query_selector_all('div[dir="auto"]')
                    fragments: list[str] = []
                    for node in text_nodes:
                        t = (await node.inner_text()).strip()
                        if t:
                            fragments.append(_redact(t))
                    if fragments:
                        fragment_groups.append(fragments)
                    if len(fragment_groups) >= max_comments:
                        break
            except Exception:
                continue
            if len(fragment_groups) >= max_comments:
                break
        comments = _extract_comment_blocks_from_fragments(fragment_groups, post_text=post_text)[:max_comments]
    except Exception as e:
        print(f"  [!] Comment scrape error {post_url}: {e}")
    finally:
        await page.close()
    return comments


async def scrape_group(
    context: BrowserContext,
    group_url: str,
    max_scroll_rounds: int = 8,
    max_posts: int = 150,
    enable_vision: bool = False,
    scrape_post_comments: bool = True,
    max_comments_per_post: int = 30,
) -> list[dict]:
    """Scrape a single FB group feed. Returns list of raw post dicts."""
    page = await context.new_page()
    results = []
    seen_texts: set[str] = set()
    skipped_sale_only = 0
    skipped_low_signal = 0
    seeking_posts = 0
    fast_pass_count = 0

    try:
        print(f"[group-open] url={group_url}")
        await page.goto(group_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        session_ok, session_info = await is_session_valid(context, page=page)
        if not session_ok:
            print("  [!] Session expired - Please run --login again")
            print(f"      {describe_session_status(session_info)}")
            raise SessionExpiredError("Facebook session expired before scraping group feed")

        for scroll_round in range(max_scroll_rounds):
            if len(results) >= max_posts:
                break

            print(
                f"[scroll] round={scroll_round + 1}/{max_scroll_rounds} "
                f"accepted={len(results)} seen={len(seen_texts)}"
            )
            session_ok, session_info = await is_session_valid(context, page=page)
            if not session_ok:
                print("  [!] Session expired mid-run - Please run --login again")
                print(f"      {describe_session_status(session_info)}")
                raise SessionExpiredError("Facebook session expired during scraping")

            # Find post elements — try selectors in order
            post_els = []
            for sel in POST_SELECTORS:
                try:
                    post_els = await page.query_selector_all(sel)
                    if post_els:
                        break
                except Exception:
                    continue

            for post_el in post_els:
                if len(results) >= max_posts:
                    break

                text = await _get_post_text(post_el)
                if not text or len(text) < 20:
                    continue

                # Dedup within this scrape session
                text_key = text[:200]
                if text_key in seen_texts:
                    continue
                seen_texts.add(text_key)

                intent = classify_post_intent(text)
                if intent == "ambiguous" and _has_budget_price(
                    text, config.TARGET_BUDGET * 0.5, config.MAX_BUDGET * 1.1
                ):
                    fast_pass_count += 1
                    print(f"[post-fastpass] budget_price_detected seen={len(seen_texts)}")

                if intent == "restricted":
                    results.append({
                        "raw_text": text,
                        "raw_text_redacted": None,
                        "post_url": None,
                        "group_url": group_url,
                        "post_id": None,
                        "post_content_hash": _content_hash(text),
                        "image_urls": [],
                        "status": "prefiltered_skip",
                        "post_intent": "restricted",
                        "skip_reason": "restricted_content",
                    })
                    print(f"[post-skip] reason=restricted_content seen={len(seen_texts)}")
                    continue

                # ข้ามโพสขาย
                if intent == "sale":
                    skipped_sale_only += 1
                    results.append({
                        "raw_text": text,
                        "raw_text_redacted": None,
                        "post_url": None,
                        "group_url": group_url,
                        "post_id": None,
                        "post_content_hash": _content_hash(text),
                        "image_urls": [],
                        "status": "prefiltered_skip",
                        "post_intent": "sale",
                        "skip_reason": "sale_only",
                    })
                    print(f"[post-skip] reason=sale_only seen={len(seen_texts)}")
                    continue

                # โพสหาคอนโด (demand side) — อ่านเมนต์เพื่อดูว่ามีคนมาเสนอห้องไหม
                if intent == "seeking":
                    post_url = await _get_post_url(post_el)
                    post_id_val = _extract_post_id(post_url) if post_url else None
                    raw_comments: list[str] = []
                    if scrape_post_comments and post_url:
                        raw_comments = await scrape_comments(
                            context,
                            post_url,
                            max_comments=config.SEEKING_MAX_COMMENTS_PER_POST,
                            post_text=text,
                        )

                    # กรองเฉพาะเมนต์ที่มี listing signal
                    listing_comments, dropped_comments = filter_listing_comments(raw_comments)

                    if not listing_comments:
                        skipped_low_signal += 1
                        print(
                            f"[post-skip] reason=seeking_no_listing_comments "
                            f"raw_comments={len(raw_comments)} seen={len(seen_texts)}"
                        )
                        continue

                    print(
                        f"[post-seeking] post_id={post_id_val or 'unknown'} "
                        f"raw_comments={len(raw_comments)} listing_comments={len(listing_comments)} "
                        f"dropped_comments={len(dropped_comments)} "
                        f"accepted={len(results) + 1}"
                    )
                    results.append({
                        "raw_text": text,
                        "raw_text_redacted": _redact(text),
                        "post_url": post_url,
                        "group_url": group_url,
                        "post_id": post_id_val,
                        "post_content_hash": _content_hash(text),
                        "image_urls": [],
                        "base64_images": [],
                        "comments": listing_comments,
                        "status": "new",
                        "post_intent": "seeking",
                    })
                    continue

                # for_rent หรือ ambiguous — เก็บทั้งหมด ส่ง DeepSeek ตัดสิน (Layer 1 Safety Net)
                post_url = await _get_post_url(post_el)
                post_id = _extract_post_id(post_url) if post_url else None
                image_urls = await _get_image_urls(post_el)

                base64_images = []
                filtered_image_count = 0
                if enable_vision and image_urls:
                    for img_url in image_urls[:4]:
                        b64 = await download_image_as_base64(page, img_url)
                        if b64 and _is_usable_image_data_url(b64):
                            base64_images.append(b64)
                        elif b64:
                            filtered_image_count += 1

                if filtered_image_count:
                    print(
                        f"[vision-guard] post_id={post_id or 'unknown'} "
                        f"filtered={filtered_image_count} usable={len(base64_images)}"
                    )

                comments: list[str] = []
                if scrape_post_comments and post_url:
                    comments = await scrape_comments(
                        context,
                        post_url,
                        max_comments=max_comments_per_post,
                        post_text=text,
                    )

                print(
                    f"[post-found] intent={intent} post_id={post_id or 'unknown'} "
                    f"images={len(image_urls)} comments={len(comments)} "
                    f"accepted={len(results) + 1}"
                )
                results.append({
                    "raw_text": text,
                    "raw_text_redacted": _redact(text),
                    "post_url": post_url,
                    "group_url": group_url,
                    "post_id": post_id,
                    "post_content_hash": _content_hash(text),
                    "image_urls": image_urls,
                    "base64_images": base64_images,
                    "broken_images_filtered": filtered_image_count > 0,
                    "comments": comments,
                    "status": "new",
                    "post_intent": intent,
                })

            # Scroll down
            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await asyncio.sleep(2)

    except Exception as e:
        print(f"  [!] Error scraping {group_url}: {e}")
    finally:
        for_rent_count = sum(1 for r in results if r.get("post_intent") == "for_rent")
        seeking_count  = sum(1 for r in results if r.get("post_intent") == "seeking")
        ambiguous_count = sum(1 for r in results if r.get("post_intent") == "ambiguous")
        print(
            f"[group-summary] url={group_url} "
            f"for_rent={for_rent_count} seeking={seeking_count} ambiguous={ambiguous_count} "
            f"fast_pass={fast_pass_count} "
            f"skipped_sale={skipped_sale_only} skipped_low_signal={skipped_low_signal} "
            f"unique_seen={len(seen_texts)}"
        )
        await page.close()

    return results
