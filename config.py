from dotenv import load_dotenv
import os

load_dotenv()

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")

def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))

def _list(key: str, default: str = "") -> list[str]:
    raw = os.getenv(key, default)
    return [x.strip() for x in raw.split(",") if x.strip()]

DEEPSEEK_API_KEY: str = _require("DEEPSEEK_API_KEY")
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")

FB_GROUP_URLS: list[str] = _list("FB_GROUP_URLS")

TARGET_BUDGET: float = _float("TARGET_BUDGET", 12000)
MAX_BUDGET: float = _float("MAX_BUDGET", 15000)
MAX_MOVE_IN_COST: float | None = float(os.getenv("MAX_MOVE_IN_COST")) if os.getenv("MAX_MOVE_IN_COST") else None

PREFERRED_AREAS: list[str] = _list("PREFERRED_AREAS", "On Nut,Udom Suk")
PREFERRED_STATIONS: list[str] = _list("PREFERRED_STATIONS", "BTS On Nut,BTS Udom Suk")

MIN_SIZE_SQM: float = _float("MIN_SIZE_SQM", 24)
MUST_HAVE_WASHER: bool = _bool("MUST_HAVE_WASHER", False)
NEED_PARKING: bool = _bool("NEED_PARKING", False)
PET_FRIENDLY: bool = _bool("PET_FRIENDLY", False)
PREFERRED_ROOM_TYPES: list[str] = _list("PREFERRED_ROOM_TYPES", "studio,1br")

MAX_SCROLL_ROUNDS: int = _int("MAX_SCROLL_ROUNDS", 8)
MAX_POSTS_PER_RUN: int = _int("MAX_POSTS_PER_RUN", 150)
HEADLESS: bool = _bool("HEADLESS", False)
ENABLE_VISION: bool = _bool("ENABLE_VISION", False)
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
VISION_MODEL: str = os.getenv("VISION_MODEL", "gpt-4o-mini")
FB_IMAGE_MIN_BYTES: int = _int("FB_IMAGE_MIN_BYTES", 2500)
SEEKING_MAX_COMMENTS_PER_POST: int = _int("SEEKING_MAX_COMMENTS_PER_POST", 80)
FB_COMMENT_MAX_CLICKS: int = _int("FB_COMMENT_MAX_CLICKS", 5)
FB_COMMENT_CLICK_DELAY_MIN: float = _float("FB_COMMENT_CLICK_DELAY_MIN", 1.0)
FB_COMMENT_CLICK_DELAY_MAX: float = _float("FB_COMMENT_CLICK_DELAY_MAX", 3.0)
SEEKING_COMMENT_EXTRACT_CONCURRENCY: int = _int("SEEKING_COMMENT_EXTRACT_CONCURRENCY", 5)

ALERT_MIN_TIER: str = os.getenv("ALERT_MIN_TIER", "shortlist")
DATA_RETENTION_DAYS: int = _int("DATA_RETENTION_DAYS", 30)

AUTO_LOOP: bool = _bool("AUTO_LOOP", False)
LOOP_INTERVAL_MINUTES: int = _int("LOOP_INTERVAL_MINUTES", 60)

DB_PATH: str = os.getenv("DB_PATH", "data/condo.db")
RESULTS_DIR: str = "data/results"
