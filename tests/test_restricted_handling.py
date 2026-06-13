import os
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT_DIR = str(Path(__file__).parents[1])
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

_TMP_DIR = tempfile.mkdtemp(prefix="bot-condo-tests-")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "condo-test.db")

from fastapi.testclient import TestClient

import database.db as db
import main as bot_main
from scraper.feed import (
    _data_url_payload_bytes,
    _is_usable_image_data_url,
    _content_hash,
    _extract_comment_blocks_from_fragments,
    classify_post_intent,
    filter_listing_comments,
    _is_house_rental_post,
    scrape_comments,
)
from web.app import app
from web.db_queries import _is_restricted_display


def _data_url_for_size(byte_count: int) -> str:
    payload = ("a" * byte_count).encode("utf-8")
    import base64

    return "data:image/jpeg;base64," + base64.b64encode(payload).decode("ascii")


class RestrictedHandlingTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    def setUp(self):
        if os.path.exists(os.environ["DB_PATH"]):
            os.remove(os.environ["DB_PATH"])
        db.init_db()
        self.client = TestClient(app)

    def _insert_listing(self, *, raw_text: str, risk_flags, tier: str, condo_name=None, location_text=None):
        run_id = db.create_run()
        post_id, is_new, duplicate_reason = db.upsert_post(run_id, {
            "post_id": None,
            "post_url": "https://facebook.example/post/1",
            "group_url": "https://facebook.example/group/1",
            "post_content_hash": None,
            "raw_text": raw_text,
            "raw_text_redacted": raw_text,
            "status": "extracted",
        })
        self.assertTrue(is_new)
        self.assertIsNone(duplicate_reason)

        listing_id = db.insert_listing(post_id, {
            "listing_type": "unknown",
            "condo_name": condo_name,
            "location_text": location_text,
            "station_name": None,
            "monthly_rent": None,
            "size_sqm": None,
            "room_type": "unknown",
            "floor": None,
            "furnishing": "unknown",
            "deposit_months": None,
            "advance_months": None,
            "move_in_cost": None,
            "move_in_cost_stated": None,
            "other_fee_text": None,
            "contract_min_months": None,
            "available_date": None,
            "has_parking": None,
            "has_washer": None,
            "has_fridge": None,
            "has_wifi": None,
            "pet_allowed": None,
            "agent_or_owner": "unknown",
            "near_transit": None,
            "confidence": 0.4,
            "risk_flags": json.dumps(risk_flags, ensure_ascii=False),
            "duplicate_flag": 0,
            "missing_fields": "[]",
            "questions_to_ask": "[]",
            "listing_fingerprint": f"fp-{tier}-{condo_name or 'none'}-{location_text or 'none'}",
        })

        db.insert_score(listing_id, {
            "hard_filter": "rent not stated",
            "price_score": None,
            "commute_score": None,
            "condition_score": None,
            "terms_score": None,
            "trust_score": None,
            "base_total": None,
            "vision_score": None,
            "final_total": None,
            "tier": tier,
        })
        return listing_id

    def test_classify_post_intent_returns_restricted_for_facebook_lock_text(self):
        text = "THIS CONTENT ISN'T AVAILABLE RIGHT NOW because the owner only shared it with a small group."
        self.assertEqual(classify_post_intent(text), "restricted")

    def test_classify_post_intent_regression_paths_still_work(self):
        self.assertEqual(classify_post_intent("ให้เช่า คอนโด 1 ห้องนอน 28 sqm 18,000 บาท/เดือน"), "for_rent")
        self.assertEqual(classify_post_intent("ผมกำลังหาห้อง ช่วยแนะนำคอนโดใกล้ BTS On Nut"), "seeking")
        self.assertEqual(classify_post_intent("ขายดาวน์คอนโดใหม่ presale ราคาขายดีมาก"), "sale")
        self.assertEqual(classify_post_intent("บ้านเดี่ยวให้เช่า 2 ชั้น 25,000 บาท/เดือน"), "sale")
        self.assertEqual(classify_post_intent("ปล่อยเช่าบ้าน ใกล้ MRT 18,000 บาท/เดือน"), "sale")
        self.assertEqual(classify_post_intent("ให้เช่า apartment ใกล้ BTS 9,000 บาท/เดือน"), "sale")
        self.assertEqual(classify_post_intent("ห้อง apartments พร้อมเข้าอยู่"), "sale")
        self.assertEqual(classify_post_intent("ห้องแมนชั่นให้เช่า ใกล้ MRT"), "sale")
        self.assertEqual(classify_post_intent("อพาร์ทเม้นท์ใกล้ BTS ราคา 7,500"), "sale")
        self.assertEqual(classify_post_intent("อพาร์ตเมนต์ พร้อมอยู่ ใกล้มหาลัย"), "sale")
        self.assertEqual(classify_post_intent("หอพัก ใกล้มหาลัย เดินทางสะดวก"), "sale")
        self.assertEqual(classify_post_intent("ให้เช่า หอ ราคา 4,500"), "sale")
        self.assertEqual(classify_post_intent("8,500/เดือน ติดต่อ line @abc"), "ambiguous")

    def test_house_prefilter_does_not_block_baan_named_condo(self):
        self.assertTrue(_is_house_rental_post("บ้านเดี่ยวให้เช่า ใกล้ BTS 22,000 บาท/เดือน"))
        self.assertFalse(_is_house_rental_post("ให้เช่า คอนโด Baan Sansiri 1 ห้องนอน 23,000 บาท/เดือน"))
        self.assertEqual(
            classify_post_intent("ให้เช่า คอนโด Baan Sansiri 1 ห้องนอน 23,000 บาท/เดือน"),
            "for_rent",
        )

    def test_image_guard_rejects_tiny_or_malformed_payloads(self):
        self.assertEqual(_data_url_payload_bytes("not-a-data-url"), 0)
        self.assertFalse(_is_usable_image_data_url("not-a-data-url", min_bytes=2500))
        self.assertFalse(_is_usable_image_data_url(_data_url_for_size(2400), min_bytes=2500))
        self.assertTrue(_is_usable_image_data_url(_data_url_for_size(2501), min_bytes=2500))
        self.assertTrue(_is_usable_image_data_url(_data_url_for_size(12000), min_bytes=2500))

    def test_content_hash_normalizes_thai_whitespace_and_hidden_spaces(self):
        variants = [
            "ให้เช่า Ideo Mobi อ่อนนุช 1 ห้องนอน 12k พร้อมอยู่",
            "ให้เช่า  Ideo Mobi อ่อนนุช 1 ห้องนอน 12k พร้อมอยู่",
            "ให้เช่า\nIdeo Mobi อ่อนนุช 1 ห้องนอน 12k พร้อมอยู่",
            "ให้เช่า\tIdeo Mobi อ่อนนุช 1 ห้องนอน 12k พร้อมอยู่",
            "ให้เช่า\u200bIdeo Mobi อ่อนนุช 1 ห้องนอน 12k พร้อมอยู่",
            "ให้เช่า\xa0Ideo Mobi อ่อนนุช 1 ห้องนอน 12k พร้อมอยู่",
        ]
        hashes = {_content_hash(text) for text in variants}
        self.assertEqual(len(hashes), 1)
        self.assertNotIn(None, hashes)

    def test_short_posts_now_receive_content_hash(self):
        short_post = "Ideo Mobi อ่อนนุช 12k line: abc"
        self.assertIsNotNone(_content_hash(short_post))

    def test_upsert_post_returns_post_id_duplicate_reason(self):
        run_id = db.create_run()
        first_id, is_new, duplicate_reason = db.upsert_post(run_id, {
            "post_id": "12345",
            "post_url": "https://facebook.example/post/12345",
            "group_url": "https://facebook.example/group/1",
            "post_content_hash": _content_hash("ให้เช่า Ideo 12k"),
            "raw_text": "ให้เช่า Ideo 12k",
            "raw_text_redacted": "ให้เช่า Ideo 12k",
            "status": "new",
        })
        self.assertTrue(is_new)
        self.assertIsNone(duplicate_reason)

        second_id, is_new, duplicate_reason = db.upsert_post(run_id, {
            "post_id": "12345",
            "post_url": "https://facebook.example/post/12345?dup=1",
            "group_url": "https://facebook.example/group/2",
            "post_content_hash": _content_hash("ให้เช่า Ideo 12k ปรับข้อความนิดหน่อย"),
            "raw_text": "ให้เช่า Ideo 12k ปรับข้อความนิดหน่อย",
            "raw_text_redacted": "ให้เช่า Ideo 12k ปรับข้อความนิดหน่อย",
            "status": "new",
        })
        self.assertEqual(second_id, first_id)
        self.assertFalse(is_new)
        self.assertEqual(duplicate_reason, "post_id")

    def test_upsert_post_returns_content_hash_duplicate_reason(self):
        run_id = db.create_run()
        first_id, is_new, duplicate_reason = db.upsert_post(run_id, {
            "post_id": "11111",
            "post_url": "https://facebook.example/post/11111",
            "group_url": "https://facebook.example/group/1",
            "post_content_hash": _content_hash("ให้เช่า Ideo Mobi อ่อนนุช 12k พร้อมอยู่"),
            "raw_text": "ให้เช่า Ideo Mobi อ่อนนุช 12k พร้อมอยู่",
            "raw_text_redacted": "ให้เช่า Ideo Mobi อ่อนนุช 12k พร้อมอยู่",
            "status": "new",
        })
        self.assertTrue(is_new)
        self.assertIsNone(duplicate_reason)

        second_id, is_new, duplicate_reason = db.upsert_post(run_id, {
            "post_id": "22222",
            "post_url": "https://facebook.example/post/22222",
            "group_url": "https://facebook.example/group/2",
            "post_content_hash": _content_hash("ให้เช่า\u200bIdeo Mobi อ่อนนุช 12k\nพร้อมอยู่"),
            "raw_text": "ให้เช่า\u200bIdeo Mobi อ่อนนุช 12k\nพร้อมอยู่",
            "raw_text_redacted": "ให้เช่า Ideo Mobi อ่อนนุช 12k พร้อมอยู่",
            "status": "new",
        })
        self.assertEqual(second_id, first_id)
        self.assertFalse(is_new)
        self.assertEqual(duplicate_reason, "content_hash")

    def test_run_scrape_skips_cross_group_duplicate_before_second_extract(self):
        class DummyContext:
            async def close(self):
                return None

        class DummyPw:
            async def stop(self):
                return None

        post_text = "Ideo Mobi อ่อนนุช 12k line: abc"
        scrape_results = [
            [{
                "post_id": "10001",
                "post_url": "https://facebook.example/post/10001",
                "group_url": "https://facebook.example/group/1",
                "post_content_hash": _content_hash(post_text),
                "raw_text": post_text,
                "raw_text_redacted": post_text,
                "image_urls": [],
                "comments": [],
                "status": "new",
                "post_intent": "for_rent",
            }],
            [{
                "post_id": "20002",
                "post_url": "https://facebook.example/post/20002",
                "group_url": "https://facebook.example/group/2",
                "post_content_hash": _content_hash("Ideo\xa0Mobi อ่อนนุช 12k line: abc"),
                "raw_text": "Ideo\xa0Mobi อ่อนนุช 12k line: abc",
                "raw_text_redacted": "Ideo Mobi อ่อนนุช 12k line: abc",
                "image_urls": [],
                "comments": [],
                "status": "new",
                "post_intent": "for_rent",
            }],
        ]
        prefs = SimpleNamespace()
        args = SimpleNamespace(max_posts=None, enable_vision=False)

        async def fake_scrape_group(*_args, **_kwargs):
            return scrape_results.pop(0)

        with patch.object(bot_main.config, "FB_GROUP_URLS", ["group-1", "group-2"]), \
             patch("scraper.browser.launch_context", AsyncMock(return_value=(DummyPw(), DummyContext()))), \
             patch("scraper.browser.is_session_valid", AsyncMock(return_value=(True, {}))), \
             patch("scraper.feed.scrape_group", new=AsyncMock(side_effect=fake_scrape_group)), \
             patch.object(bot_main, "extract_listings", return_value=[]) as extract_mock, \
             patch.object(bot_main.random, "uniform", return_value=0), \
             patch.object(bot_main.asyncio, "sleep", AsyncMock(return_value=None)):
            stats = asyncio.run(bot_main.run_scrape(args, prefs))

        self.assertEqual(extract_mock.call_count, 1)
        self.assertEqual(stats["posts_found"], 2)
        self.assertEqual(stats["listings_extracted"], 0)

    def test_index_template_includes_cross_group_duplicate_status(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("duplicate_cross_group", response.text)
        self.assertIn("duplicate_post_id", response.text)

    def test_legacy_raw_text_signal_marks_restricted_display(self):
        row = {
            "tier": "need_info",
            "risk_flags": "[]",
            "raw_text": "ไม่สามารถดูเนื้อหานี้ได้ในขณะนี้ กรุณาลองใหม่ภายหลัง",
            "condo_name": None,
            "location_text": None,
            "station_name": None,
            "monthly_rent": None,
            "size_sqm": None,
            "first_image_url": None,
        }
        self.assertTrue(_is_restricted_display(row))

    def test_results_page_renders_muted_badge_and_tooltip_for_restricted_need_info(self):
        self._insert_listing(
            raw_text="โพสต์ธรรมดา มีข้อมูลไม่พอ",
            risk_flags=[],
            tier="need_info",
            condo_name="Need Info Condo",
            location_text="On Nut",
        )
        self._insert_listing(
            raw_text="โพสต์โดนล็อก This content isn't available right now",
            risk_flags=["restricted_content"],
            tier="need_info",
            condo_name=None,
            location_text=None,
        )

        response = self.client.get("/results")
        self.assertEqual(response.status_code, 200)
        html = response.text

        self.assertIn("Need Info Condo", html)
        self.assertIn("ℹ Need Info", html)
        self.assertIn("Broken Link / Restricted", html)
        self.assertIn('title="Restricted Content by Facebook"', html)

    def test_delete_listing_route_removes_listing_from_results(self):
        listing_id = self._insert_listing(
            raw_text="โพสต์ธรรมดา มีข้อมูลพอประมาณ",
            risk_flags=[],
            tier="maybe",
            condo_name="Delete Me Condo",
            location_text="Bangkok",
        )

        before = self.client.get("/results")
        self.assertEqual(before.status_code, 200)
        self.assertIn("Delete Me Condo", before.text)

        res = self.client.post(f"/results/delete/{listing_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "deleted")

        after = self.client.get("/results")
        self.assertEqual(after.status_code, 200)
        self.assertNotIn("Delete Me Condo", after.text)

    def test_extract_comment_blocks_reassembles_fragmented_nodes(self):
        fragments = [
            ["Phun Phun", "6 ชั่วโมง", "ตอบกลับ", "แชร์", "ให้เช่า Centric ห้วยขวาง", "14,000 บาท/เดือน", "27 ตร.ม. ชั้น 18"],
            ["เจ้าของโพสต์", "หาคอนโดใกล้mrtพระราม9ค่า งบไม่เกิน14,000"],
            ["Phun Phun", "6 ชั่วโมง", "ให้เช่า Centric ห้วยขวาง", "14,000 บาท/เดือน", "27 ตร.ม. ชั้น 18"],
        ]
        comments = _extract_comment_blocks_from_fragments(
            fragments,
            post_text="หาคอนโดใกล้mrtพระราม9ค่า งบไม่เกิน14,000",
        )
        self.assertEqual(
            comments,
            ["Phun Phun\nให้เช่า Centric ห้วยขวาง\n14,000 บาท/เดือน\n27 ตร.ม. ชั้น 18"],
        )

    def test_filter_listing_comments_keeps_real_supply_and_drops_parasites(self):
        accepted, dropped = filter_listing_comments([
            "หาด้วยคนครับ งบ 14,000",
            "มี Rhythm Asoke 13,500 บาท/เดือน 22 ตร.ม. ใกล้ MRT เพชรบุรี สนใจทักไลน์",
            "ตามด้วยค่ะ",
        ])
        self.assertEqual(len(accepted), 1)
        self.assertIn("Rhythm Asoke", accepted[0])
        self.assertEqual(
            [reason for _comment, reason in dropped],
            ["parasite_seeker", "parasite_seeker"],
        )

    def test_extract_listings_from_seeking_comments_uses_bounded_concurrency(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_extract(_post_text, comment_text):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return [{
                "listing_type": "rent",
                "condo_name": comment_text,
                "location_text": None,
                "station_name": None,
                "monthly_rent": 13000,
                "size_sqm": 30,
                "room_type": "1br",
                "floor": None,
                "furnishing": "fully",
                "deposit_months": 2,
                "advance_months": 1,
                "move_in_cost_stated": None,
                "other_fee_text": None,
                "contract_min_months": 12,
                "available_date": None,
                "has_parking": None,
                "has_washer": True,
                "has_fridge": True,
                "has_wifi": None,
                "pet_allowed": None,
                "near_transit": True,
                "agent_or_owner": "agent",
                "risk_flags": [],
                "missing_fields": [],
                "questions_to_ask": [],
                "confidence": 0.9,
            }]

        comments = [f"comment-{i}" for i in range(10)]
        with patch.object(bot_main, "extract_listings_from_comment", side_effect=fake_extract), \
             patch.object(bot_main.config, "SEEKING_COMMENT_EXTRACT_CONCURRENCY", 3):
            listings = asyncio.run(
                bot_main.extract_listings_from_seeking_comments(
                    "หาห้องแถวพระราม9",
                    comments,
                    "post-1",
                )
            )

        self.assertEqual(len(listings), 10)
        self.assertLessEqual(max_active, 3)

    def test_extract_listings_from_comment_includes_original_post_context(self):
        observed = {}

        def fake_extract(payload):
            observed["payload"] = payload
            return []

        with patch("analysis.extractor.extract_listings", side_effect=fake_extract):
            from analysis.extractor import extract_listings_from_comment

            extract_listings_from_comment(
                "หาห้องแถวอ่อนนุช งบ 15,000",
                "มี Rhythm ค่ะ 13,500 สนใจทักไลน์",
            )

        self.assertIn("Original Post Request:", observed["payload"])
        self.assertIn("Comment to Extract:", observed["payload"])
        self.assertIn("หาห้องแถวอ่อนนุช งบ 15,000", observed["payload"])
        self.assertIn("มี Rhythm ค่ะ 13,500 สนใจทักไลน์", observed["payload"])

    def test_run_scrape_seeking_comment_failure_does_not_mark_post_extract_failed(self):
        class DummyContext:
            async def close(self):
                return None

        class DummyPw:
            async def stop(self):
                return None

        seeking_post = {
            "post_id": "seek-1",
            "post_url": "https://facebook.example/post/seek-1",
            "group_url": "https://facebook.example/group/1",
            "post_content_hash": _content_hash("หาห้องพระราม9 งบไม่เกิน 14,000"),
            "raw_text": "หาห้องพระราม9 งบไม่เกิน 14,000",
            "raw_text_redacted": "หาห้องพระราม9 งบไม่เกิน 14,000",
            "image_urls": [],
            "base64_images": [],
            "comments": ["คอมเมนต์พัง", "มี Rhythm Asoke 13,500 บาท/เดือน"],
            "status": "new",
            "post_intent": "seeking",
        }

        def fake_extract(_post_text, comment_text):
            if "พัง" in comment_text:
                raise bot_main.ExtractionError("missing listings array")
            return [{
                "listing_type": "rent",
                "condo_name": "Rhythm Asoke 1",
                "location_text": "พระราม 9",
                "station_name": "MRT Phra Ram 9",
                "monthly_rent": 13500,
                "size_sqm": 22,
                "room_type": "studio",
                "floor": 36,
                "furnishing": "fully",
                "deposit_months": 2,
                "advance_months": 1,
                "move_in_cost_stated": None,
                "other_fee_text": None,
                "contract_min_months": 12,
                "available_date": None,
                "has_parking": None,
                "has_washer": True,
                "has_fridge": True,
                "has_wifi": None,
                "pet_allowed": None,
                "near_transit": True,
                "agent_or_owner": "agent",
                "risk_flags": [],
                "missing_fields": [],
                "questions_to_ask": [],
                "confidence": 0.9,
            }]

        prefs = bot_main.load_preferences()
        args = SimpleNamespace(max_posts=None, enable_vision=False)

        async def fake_scrape_group(*_args, **_kwargs):
            return [seeking_post]

        with patch.object(bot_main.config, "FB_GROUP_URLS", ["group-1"]), \
             patch("scraper.browser.launch_context", AsyncMock(return_value=(DummyPw(), DummyContext()))), \
             patch("scraper.browser.is_session_valid", AsyncMock(return_value=(True, {}))), \
             patch("scraper.feed.scrape_group", new=AsyncMock(side_effect=fake_scrape_group)), \
             patch.object(bot_main, "extract_listings_from_comment", side_effect=fake_extract), \
             patch.object(bot_main.random, "uniform", return_value=0), \
             patch.object(bot_main.asyncio, "sleep", AsyncMock(return_value=None)):
            stats = asyncio.run(bot_main.run_scrape(args, prefs))

        conn = db.get_connection()
        row = conn.execute("SELECT status FROM posts WHERE post_id='seek-1'").fetchone()
        conn.close()

        self.assertEqual(stats["listings_extracted"], 1)
        self.assertEqual(row["status"], "scored")

    def test_scrape_comments_stops_after_max_clicks(self):
        class DummyButton:
            def __init__(self):
                self.clicks = 0

            async def click(self):
                self.clicks += 1

        class DummyNode:
            def __init__(self, text):
                self.text = text

            async def inner_text(self):
                return self.text

        class DummyContainer:
            async def query_selector_all(self, _selector):
                return [DummyNode("Agent"), DummyNode("ให้เช่า Rhythm Asoke 13,500 บาท/เดือน")]

        class DummyPage:
            def __init__(self, button):
                self.button = button
                self.url = "https://facebook.example/post/1"

            async def goto(self, *_args, **_kwargs):
                return None

            async def close(self):
                return None

            async def query_selector(self, _selector):
                return self.button

            async def query_selector_all(self, _selector):
                return [DummyContainer()]

        class DummyContext:
            def __init__(self, page):
                self.page = page

            async def new_page(self):
                return self.page

        button = DummyButton()
        page = DummyPage(button)
        context = DummyContext(page)

        with patch("scraper.feed.asyncio.sleep", AsyncMock(return_value=None)), \
             patch("scraper.feed.random.uniform", return_value=1.0), \
             patch("scraper.feed.config.FB_COMMENT_MAX_CLICKS", 5):
            comments = asyncio.run(
                scrape_comments(
                    context,
                    "https://facebook.example/post/1",
                    max_comments=80,
                    post_text="หาห้องพระราม9",
                )
            )

        self.assertEqual(button.clicks, 5)
        self.assertEqual(len(comments), 1)


if __name__ == "__main__":
    unittest.main()
