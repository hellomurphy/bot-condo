import subprocess
import database.db as db
from rules.preferences import Preferences
from rules.scoring import TIER_ORDER


def _osascript_notify(title: str, message: str):
    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def notify_listing(condo_name: str | None, score: float, rent: float | None):
    name = condo_name or "ห้องใหม่"
    rent_str = f"{int(rent):,}/เดือน" if rent else "ราคาไม่ระบุ"
    msg = f"{name} | {score:.0f}/100 | {rent_str}"
    _osascript_notify("Must Call พบแล้ว!", msg)


def _row_get(row, key: str, default=None):
    if row is None:
        return default

    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def alert_if_new(listing_row, score_row, prefs: Preferences):
    min_idx = TIER_ORDER.index(prefs.alert_min_tier.lower())
    tier = _row_get(score_row, "tier")
    alerted_at = _row_get(score_row, "alerted_at")

    try:
        tier_idx = TIER_ORDER.index(tier)
    except ValueError:
        return

    if tier_idx >= min_idx and alerted_at is None:
        rent = _row_get(listing_row, "monthly_rent")
        condo = _row_get(listing_row, "condo_name")
        final = _row_get(score_row, "final_total", 0)
        score_id = _row_get(score_row, "score_id", _row_get(score_row, "id"))
        if score_id is None:
            return
        notify_listing(condo, final or 0, rent)
        db.set_alerted(score_id)
