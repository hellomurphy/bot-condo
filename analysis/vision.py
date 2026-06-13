"""Phase 3 — optional GPT-4o-mini image scoring (--enable-vision only)."""
import json
import urllib.request
import config

VISION_SYSTEM_PROMPT = """You are scoring rental room photos for a prospective tenant.
For each image, decide:
1. Is this a room interior photo? (not exterior, map, floor plan, or agent logo)
2. If yes, score it 0-10 on: natural light, room condition/cleanliness, furnishing quality, space perception.
   Penalise: fisheye distortion, cluttered/dirty rooms, dark photos.

Respond in JSON: {"scores": [{"image_index": 0, "is_room": true, "score": 7.5}, ...]}
Skip non-room images (set is_room: false, score: null). No explanation."""


def score_images(base64_images: list[str]) -> float | None:
    """Score up to 4 base64 images. Returns average non-null score 0-10, or None."""
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set — required for --enable-vision")

    images_to_send = base64_images[:4]
    content = [{"type": "text", "text": "Score these rental room photos:"}]
    for b64 in images_to_send:
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
        })

    payload = json.dumps({
        "model": config.VISION_MODEL,
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 512,
        "temperature": 0.0,
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {config.OPENAI_API_KEY}"},
    )
    response = json.loads(urllib.request.urlopen(req).read())

    try:
        result = json.loads(response["choices"][0]["message"]["content"])
        scores = [
            item["score"]
            for item in result.get("scores", [])
            if item.get("is_room") and item.get("score") is not None
        ]
        if not scores:
            return None
        return sum(scores) / len(scores)
    except Exception:
        return None
