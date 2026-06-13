import json
import re
import hashlib
import urllib.request
import config

SYSTEM_PROMPT = """You are a Thai real-estate listing parser. Extract structured data from Facebook rental posts.

Rules:
- You MUST return valid JSON with a top-level "listings" array.
- A single post may describe multiple listings. Extract ALL.
- If a field is not explicitly stated or strongly implied, return null.
- Do NOT infer has_washer/has_parking from "fully furnished".
- Do NOT compute move_in_cost — return deposit_months and advance_months separately.
- If post says a lump sum like "เข้าอยู่ 25,000 จบ" set move_in_cost_stated only.
- questions_to_ask: 3-5 practical questions the renter should ask before viewing.
- station_name: always normalize to standard English BTS/MRT station name.
  Examples: "อ่อนนุช" → "BTS On Nut", "ห้วยขวาง" → "MRT Huai Khwang", "mrtห้วยขวาง" → "MRT Huai Khwang", "รถไฟฟ้า" (generic) → null.
  If only "ใกล้รถไฟฟ้า" without station name, set station_name=null and add "station_name" to missing_fields.
- monthly_rent: THB number only, convert ล้าน/แสน to number.

Output schema per listing (all fields required, use null if unknown):
{
  "listing_type": "rent|sale|unknown",
  "condo_name": null,
  "location_text": null,
  "station_name": null,
  "monthly_rent": null,
  "size_sqm": null,
  "room_type": "studio|1br|2br|3br|unknown",
  "floor": null,
  "furnishing": "fully|partly|none|unknown",
  "deposit_months": null,
  "advance_months": null,
  "move_in_cost_stated": null,
  "other_fee_text": null,
  "contract_min_months": null,
  "available_date": null,
  "has_parking": null,
  "has_washer": null,
  "has_fridge": null,
  "has_wifi": null,
  "pet_allowed": null,
  "near_transit": null,
  "agent_or_owner": "agent|owner|unknown",
  "risk_flags": [],
  "missing_fields": [],
  "questions_to_ask": [],
  "confidence": 0.8
}

Return JSON only. No markdown, no explanation."""


ROOM_TYPE_MAP = {
    "1 bedroom": "1br", "2 bedroom": "2br", "3 bedroom": "3br",
    "one bedroom": "1br", "two bedroom": "2br",
    "studio room": "studio", "studio apartment": "studio",
    "สตูดิโอ": "studio", "1ห้องนอน": "1br", "2ห้องนอน": "2br",
}


class ExtractionError(Exception):
    pass


def _call_api(text: str, temperature: float) -> str:
    payload = json.dumps({
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 2048,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        },
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp["choices"][0]["message"]["content"] or ""


def _parse_and_validate(raw: str) -> dict:
    # Layer 1: strip markdown backticks
    clean = raw.replace("```json", "").replace("```", "").strip()

    result = None
    try:
        result = json.loads(clean)
    except json.JSONDecodeError:
        # Layer 2: regex extract first {...} block
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        if m:
            try:
                result = json.loads(m.group())
            except json.JSONDecodeError:
                pass

    # Layer 3: validate top-level structure
    if not result or not isinstance(result.get("listings"), list):
        raise ExtractionError("missing listings array")

    # Layer 4: normalize enum values
    for listing in result["listings"]:
        rt = (listing.get("room_type") or "").lower().strip()
        listing["room_type"] = ROOM_TYPE_MAP.get(rt, rt) or "unknown"

        # Clamp confidence
        conf = listing.get("confidence")
        if conf is not None:
            listing["confidence"] = max(0.0, min(1.0, float(conf)))

        # Ensure arrays are lists
        for arr_field in ("risk_flags", "missing_fields", "questions_to_ask"):
            if not isinstance(listing.get(arr_field), list):
                listing[arr_field] = []

    return result


def extract_listings(post_text_redacted: str) -> list[dict]:
    """Extract listings from a redacted post. Returns list of listing dicts."""
    raw = _call_api(post_text_redacted, 0.0)

    # Layer 5: retry on empty content (DeepSeek known bug)
    if not raw.strip():
        raw = _call_api(post_text_redacted, 0.3)

    result = _parse_and_validate(raw)
    return result["listings"]


def build_comment_context_payload(post_text_redacted: str, comment_text_redacted: str) -> str:
    return (
        "Original Post Request:\n"
        f"{post_text_redacted.strip()}\n\n"
        "Comment to Extract:\n"
        f"{comment_text_redacted.strip()}\n\n"
        "Instructions:\n"
        "- Extract listings described in the comment only.\n"
        "- Use the original post request only as context to resolve implied area/station fit.\n"
        "- Do not extract the original post request itself as a listing.\n"
    )


def extract_listings_from_comment(post_text_redacted: str, comment_text_redacted: str) -> list[dict]:
    payload = build_comment_context_payload(post_text_redacted, comment_text_redacted)
    return extract_listings(payload)


def compute_move_in_cost(listing: dict) -> float | None:
    if listing.get("move_in_cost_stated") is not None:
        return float(listing["move_in_cost_stated"])
    rent = listing.get("monthly_rent")
    deposit = listing.get("deposit_months")
    if rent and deposit is not None:
        advance = listing.get("advance_months") or 0
        return rent * (deposit + advance)
    return None


def compute_fingerprint(listing: dict) -> str:
    parts = "|".join(str(listing.get(f) or "") for f in [
        "condo_name", "monthly_rent", "size_sqm", "room_type",
        "floor", "location_text", "station_name", "agent_or_owner"
    ])
    return hashlib.sha256(parts.encode()).hexdigest()


def validate_model():
    """Startup check — confirm the configured model name is valid."""
    try:
        _call_api("ping", 0.0)
    except Exception as e:
        if "model" in str(e).lower():
            raise RuntimeError(
                f"Model '{config.DEEPSEEK_MODEL}' not found. "
                "Check DEEPSEEK_MODEL in .env. Valid: deepseek-chat, deepseek-reasoner"
            ) from e
        raise
