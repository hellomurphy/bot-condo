import json
from rules.preferences import Preferences


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def tier_from_score(score: float) -> str:
    if score >= 80:
        return "must_call"
    if score >= 65:
        return "shortlist"
    if score >= 50:
        return "maybe"
    return "skip"


TIER_ORDER = ["skip", "maybe", "need_info", "shortlist", "must_call"]


def apply_hard_filters(listing: dict, prefs: Preferences) -> tuple[str | None, str | None]:
    """Returns (tier_slug, reason) or (None, None) if listing passes."""
    if listing.get("listing_type") not in ("rent", "unknown"):
        return "skip", "not a rental"
    rent = listing.get("monthly_rent")
    if rent and rent > prefs.max_budget:
        return "skip", "over budget"
    size = listing.get("size_sqm")
    if size and size < prefs.min_size_sqm:
        return "skip", "too small"
    move_in = listing.get("move_in_cost")
    if move_in and prefs.max_move_in_cost is not None and move_in > prefs.max_move_in_cost:
        return "skip", "move-in cost too high"
    if prefs.must_have_washer and listing.get("has_washer") is False:
        return "skip", "no washer"
    if prefs.need_parking and listing.get("has_parking") is False:
        return "skip", "no parking"
    if rent is None:
        return "need_info", "rent not stated"
    conf = listing.get("confidence")
    if conf is not None and conf < 0.45:
        return "need_info", "low AI confidence"
    return None, None


def calc_commute_score(listing: dict, prefs: Preferences) -> float:
    station = listing.get("station_name")
    if station:
        for sta in prefs.preferred_stations:
            if sta.lower() == station.lower():
                return 25
        return 14  # known station but not preferred

    loc = (listing.get("location_text") or "").lower()
    if any(kw in loc for kw in ["ใกล้รถไฟฟ้า", "ใกล้ bts", "ใกล้ mrt", "walk bts", "near bts"]):
        return 10

    for area in prefs.preferred_areas:
        if area.lower() in loc:
            return 18

    if listing.get("location_text") and len(listing["location_text"]) > 3:
        return 7

    return 0


def base_score(listing: dict, prefs: Preferences) -> dict:
    rent = listing.get("monthly_rent") or 0
    move_in = listing.get("move_in_cost")

    # --- Price /30 ---
    price = 0
    if rent <= prefs.target_budget:
        price += 20
    elif rent <= prefs.max_budget:
        price += 12
    if move_in is not None:
        if move_in <= prefs.target_budget * 3:
            price += 10
        elif move_in > prefs.target_budget * 4:
            price -= 10
    price_score = clamp(price, 0, 30)

    # --- Commute /25 ---
    commute_score = clamp(calc_commute_score(listing, prefs), 0, 25)

    # --- Condition /20 (vision adds up to 8 in Pass 2; cap at 12 here) ---
    cond = 0
    furnishing = listing.get("furnishing")
    if furnishing == "fully":
        cond += 12
    elif furnishing == "partly":
        cond += 6
    if prefs.must_have_washer and listing.get("has_washer"):
        cond += 5
    condition_score = clamp(cond, 0, 12)

    # --- Terms /15 ---
    terms = 0
    contract = listing.get("contract_min_months")
    if contract is not None:
        if contract <= 6:
            terms += 8
        elif contract >= 12:
            terms -= 3
    if listing.get("available_date"):
        terms += 4
    if prefs.need_parking and listing.get("has_parking"):
        terms += 5
    room_type = listing.get("room_type")
    if room_type in prefs.preferred_room_types:
        terms += 3
    elif room_type not in ("unknown", None):
        terms -= 2
    terms_score = clamp(terms, 0, 15)

    # --- Trust /10 ---
    trust = 5
    if listing.get("agent_or_owner") == "owner":
        trust += 3
    risk_flags = listing.get("risk_flags") or []
    if isinstance(risk_flags, str):
        risk_flags = json.loads(risk_flags)
    trust -= len(risk_flags) * 3
    conf = listing.get("confidence")
    if conf and conf >= 0.8:
        trust += 2
    trust_score = clamp(trust, 0, 10)

    base_total = price_score + commute_score + condition_score + terms_score + trust_score
    return {
        "price_score": price_score,
        "commute_score": commute_score,
        "condition_score": condition_score,
        "terms_score": terms_score,
        "trust_score": trust_score,
        "base_total": base_total,
        "base_tier": tier_from_score(base_total),
    }


def apply_vision(base_result: dict, vision_score: float) -> dict:
    vision_bonus = (vision_score / 10) * 8
    final_condition = clamp(base_result["condition_score"] + vision_bonus, 0, 20)
    delta = final_condition - base_result["condition_score"]
    final_total = clamp(base_result["base_total"] + delta, 0, 100)
    return {
        **base_result,
        "vision_score": vision_score,
        "condition_score": final_condition,
        "final_total": final_total,
        "tier": tier_from_score(final_total),
    }


def score_listing(listing: dict, prefs: Preferences) -> dict:
    """Full scoring pipeline. Returns score dict ready for DB insert."""
    tier, reason = apply_hard_filters(listing, prefs)
    if tier:
        return {
            "hard_filter": reason,
            "tier": tier,
            "price_score": None,
            "commute_score": None,
            "condition_score": None,
            "terms_score": None,
            "trust_score": None,
            "base_total": None,
            "vision_score": None,
            "final_total": None,
        }

    result = base_score(listing, prefs)
    return {
        "hard_filter": None,
        "price_score": result["price_score"],
        "commute_score": result["commute_score"],
        "condition_score": result["condition_score"],
        "terms_score": result["terms_score"],
        "trust_score": result["trust_score"],
        "base_total": result["base_total"],
        "vision_score": None,
        "final_total": result["base_total"],
        "tier": result["base_tier"],
    }
