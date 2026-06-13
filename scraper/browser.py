"""Playwright persistent browser context and Facebook session validation."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
USER_DATA_DIR = BASE_DIR / "user_data"
_CREDENTIAL_MARKER = USER_DATA_DIR / "Default" / "Cookies"
STORAGE_STATE_PATH = USER_DATA_DIR / "facebook_storage_state.json"

LOCALE = "th-TH"
TIMEZONE = "Asia/Bangkok"

LOGIN_WALL_KEYWORDS = ["login", "checkpoint", "two_step", "recover"]
FACEBOOK_HOME_URL = "https://www.facebook.com/"


class SessionExpiredError(RuntimeError):
    """Raised when the persisted Facebook session is no longer authenticated."""


def has_credentials() -> bool:
    return _CREDENTIAL_MARKER.exists()


def describe_session_status(session_info: dict) -> str:
    cookie_bits = []
    if session_info.get("has_c_user"):
        cookie_bits.append("c_user=yes")
    else:
        cookie_bits.append("c_user=no")
    if session_info.get("has_xs"):
        cookie_bits.append("xs=yes")
    else:
        cookie_bits.append("xs=no")

    url = session_info.get("current_url") or "(unknown)"
    return f"url={url} | {' '.join(cookie_bits)}"


def format_session_debug_dump(session_info: dict) -> str:
    cookie_names = ", ".join(session_info.get("cookie_names", [])) or "(none)"
    debug_lines = [
        "---- session debug ----",
        f"url: {session_info.get('current_url') or '(unknown)'}",
        f"path: {session_info.get('url_path') or '(unknown)'}",
        f"title: {session_info.get('page_title') or '(unknown)'}",
        f"is_facebook_url: {session_info.get('is_facebook_url')}",
        f"is_blocked_url: {session_info.get('is_blocked_url')}",
        f"has_c_user: {session_info.get('has_c_user')}",
        f"has_xs: {session_info.get('has_xs')}",
        f"cookie_names: {cookie_names}",
        "-----------------------",
    ]
    return "\n".join(debug_lines)


def _is_blocked_facebook_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "/").lower()
    if not path or path == "/":
        return False

    path_parts = [part for part in path.split("/") if part]
    return any(keyword in part for part in path_parts for keyword in LOGIN_WALL_KEYWORDS)


async def _restore_storage_state(context: BrowserContext) -> bool:
    if not STORAGE_STATE_PATH.exists():
        return False

    try:
        state = json.loads(STORAGE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False

    cookies = state.get("cookies") or []
    if not cookies:
        return False

    try:
        await context.add_cookies(cookies)
        return True
    except Exception:
        return False


async def is_session_valid(
    context: BrowserContext,
    page: Page | None = None,
) -> tuple[bool, dict]:
    created_page = False
    probe_page = page

    if probe_page is None:
        pages = context.pages
        if pages:
            probe_page = pages[0]
        else:
            probe_page = await context.new_page()
            created_page = True

    try:
        if probe_page.url in ("", "about:blank"):
            await probe_page.goto(FACEBOOK_HOME_URL, wait_until="domcontentloaded", timeout=30000)

        cookies = await context.cookies()
        cookie_map = {cookie["name"]: cookie for cookie in cookies}
        cookie_names = sorted(cookie_map.keys())
        current_url = probe_page.url or ""
        parsed_url = urlparse(current_url)
        lowered_url = current_url.lower()
        is_facebook_url = "facebook.com" in lowered_url
        is_blocked_url = _is_blocked_facebook_url(current_url)
        c_user_value = cookie_map.get("c_user", {}).get("value")
        page_title = await probe_page.title()

        session_info = {
            "has_c_user": "c_user" in cookie_map,
            "has_xs": "xs" in cookie_map,
            "current_url": current_url,
            "url_path": parsed_url.path or "/",
            "page_title": page_title,
            "is_facebook_url": is_facebook_url,
            "is_blocked_url": is_blocked_url,
            "c_user_value": c_user_value,
            "cookie_names": cookie_names,
        }
        is_valid = (
            session_info["has_c_user"]
            and session_info["has_xs"]
            and session_info["is_facebook_url"]
            and not session_info["is_blocked_url"]
        )
        return is_valid, session_info
    finally:
        if created_page and probe_page is not None:
            await probe_page.close()


async def launch_context(headless: bool = False) -> tuple:
    """Returns (playwright, context). Caller must close both."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR.as_posix(),
        headless=headless,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        viewport={"width": 1280, "height": 900},
    )
    restored = await _restore_storage_state(context)
    if restored:
        print(f"Restored Facebook session cookies from: {STORAGE_STATE_PATH}")
    return pw, context


async def run_login():
    """Open browser for FB login. Close on Enter to flush session to user_data/."""
    print(f"กำลังเปิด Facebook... profile จะถูกเก็บที่: {USER_DATA_DIR}")
    print("กรุณา login ในหน้าต่าง Chrome ที่เปิดขึ้นมา")
    pw, context = await launch_context(headless=False)
    page = await context.new_page()
    await page.goto(FACEBOOK_HOME_URL, wait_until="domcontentloaded", timeout=30000)

    print("\nlogin เสร็จแล้วเห็น feed แล้ว กด Enter เพื่อบันทึก session และปิด browser")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, ">>> ")

    await page.wait_for_load_state("domcontentloaded")
    is_valid, session_info = await is_session_valid(context, page=page)
    status = describe_session_status(session_info)

    if is_valid:
        await context.storage_state(path=STORAGE_STATE_PATH.as_posix())
        print(
            "✅ Session saved successfully "
            f"(User ID: {session_info.get('c_user_value')}) | {status}"
        )
        print(f"Saved storage state: {STORAGE_STATE_PATH}")
    else:
        print("❌ Session not saved: Facebook login is not fully authenticated yet.")
        print(f"   {status}")
        if session_info.get("is_blocked_url"):
            print("   ยังอยู่ในหน้า login/checkpoint/recovery ของ Facebook")
        else:
            print("   auth cookies ยังไม่ครบ ต้องมีทั้ง c_user และ xs")
        print(format_session_debug_dump(session_info))

    await context.close()
    await pw.stop()
