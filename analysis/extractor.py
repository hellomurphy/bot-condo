import json
import re
import unicodedata
import urllib.request
import config

RELEVANCE_SYSTEM_PROMPT = """You are a Thai real-estate filter. Given a Facebook post and its comments, decide if anyone is offering a room/condo for rent.
Return JSON only: {"relevant": true/false, "reason": "one line"}"""

SYSTEM_PROMPT = """You are a Thai real-estate listing parser. Extract structured data from Facebook rental posts.

Rules:
- Return valid JSON with a top-level "listings" array.
- A single post may contain multiple listings. Extract ALL.
- If a field is not stated or strongly implied, return null.
- rent: THB number only, convert ล้าน/แสน to number.
- size_sqm: number only (square metres).
- location_tags: BTS/MRT station or area name in Thai, e.g. "BTS อ่อนนุช" or "ย่านอ่อนนุช".
  If multiple, comma-separate. Use null if not mentioned.
- status: "owner" if posted directly by owner, "agent" if agent/broker.
  Append any concise risk flags (e.g. "agent, ไม่มีชื่อคอนโด"). Keep it ≤ 30 chars.
- summary: Write in Thai. One or two sentences covering ALL details found:
  คอนโด ราคา ขนาด ชั้น เฟอร์นิเจอร์ เครื่องซัก ตู้เย็น แอร์ วายฟาย สัตว์เลี้ยง
  ที่จอดรถ เงินประกัน ค่าล่วงหน้า สัญญาขั้นต่ำ วันพร้อมเข้าอยู่
  ถ้าไม่มีข้อมูลใดให้ใส่ "-"

Output schema per listing (all fields required, use null if unknown):
{
  "condo_name": null,
  "room_type": "studio|1br|2br|3br|unknown",
  "size_sqm": null,
  "floor": null,
  "rent": null,
  "location_tags": null,
  "status": null,
  "summary": "-"
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


def _call_api(text: str, temperature: float, system_prompt: str = SYSTEM_PROMPT) -> str:
    payload = json.dumps({
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
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

        # Ensure summary is a non-empty string
        if not listing.get("summary") or not str(listing["summary"]).strip():
            listing["summary"] = "-"

    return result


def extract_listings(post_text_redacted: str) -> list[dict]:
    """Extract listings from a redacted post. Returns list of listing dicts."""
    raw = _call_api(unicodedata.normalize("NFKC", post_text_redacted), 0.0)

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


_MAX_COMMENTS_IN_PAYLOAD = 50


def _build_post_with_comments_payload(post_text: str, comments: list[str]) -> str:
    parts = ["[POST]", post_text.strip()]
    if comments:
        parts.append("\n[COMMENTS]")
        for i, c in enumerate(comments[:_MAX_COMMENTS_IN_PAYLOAD], 1):
            parts.append(f"Comment {i}:\n{c.strip()}")
    return "\n\n".join(parts)


def check_relevance(post_text: str, comments: list[str]) -> bool:
    """Step 1: Ask DeepSeek if this post+comments contains a rental offer. Cheap call."""
    payload = _build_post_with_comments_payload(post_text, comments)
    raw = _call_api(unicodedata.normalize("NFKC", payload), 0.0, system_prompt=RELEVANCE_SYSTEM_PROMPT)
    if not raw.strip():
        return False
    clean = raw.replace("```json", "").replace("```", "").strip()
    m = re.search(r'\{.*\}', clean, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            return bool(data.get("relevant", False))
        except json.JSONDecodeError:
            pass
    return False


def extract_listings_with_comments(post_text: str, comments: list[str]) -> list[dict]:
    """Step 2: Extract structured listings from post + comments combined."""
    payload = _build_post_with_comments_payload(post_text, comments)
    raw = _call_api(unicodedata.normalize("NFKC", payload), 0.0)
    if not raw.strip():
        raw = _call_api(payload, 0.3)
    result = _parse_and_validate(raw)
    return result["listings"]


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
