from dataclasses import dataclass, field
import config


@dataclass
class Preferences:
    target_budget: float
    max_budget: float
    max_move_in_cost: float | None
    preferred_areas: list[str]
    preferred_stations: list[str]
    min_size_sqm: float
    must_have_washer: bool
    need_parking: bool
    pet_friendly: bool
    preferred_room_types: list[str]
    alert_min_tier: str


def load_preferences() -> Preferences:
    return Preferences(
        target_budget=config.TARGET_BUDGET,
        max_budget=config.MAX_BUDGET,
        max_move_in_cost=config.MAX_MOVE_IN_COST,
        preferred_areas=config.PREFERRED_AREAS,
        preferred_stations=config.PREFERRED_STATIONS,
        min_size_sqm=config.MIN_SIZE_SQM,
        must_have_washer=config.MUST_HAVE_WASHER,
        need_parking=config.NEED_PARKING,
        pet_friendly=config.PET_FRIENDLY,
        preferred_room_types=config.PREFERRED_ROOM_TYPES,
        alert_min_tier=config.ALERT_MIN_TIER,
    )
