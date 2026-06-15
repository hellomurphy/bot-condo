import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT_DIR = str(Path(__file__).parents[1])
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")


def _make_db(tmp_path: str):
    """Point config at a temp DB and (re)init it. Returns the db module."""
    db_path = os.path.join(tmp_path, "test.db")
    import config as cfg
    cfg.DB_PATH = db_path
    import importlib
    import database.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


class RunRoundtripTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db = _make_db(self._tmpdir)

    def test_create_and_finish_run(self):
        run_id = self.db.create_run()
        self.assertIsNotNone(run_id)
        self.assertIsInstance(run_id, int)

        self.db.finish_run(run_id, {
            "groups_scanned": 3,
            "posts_found": 10,
            "listings_extracted": 7,
        })

        conn = self.db.get_connection()
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        conn.close()

        self.assertEqual(row["status"], "done")
        self.assertEqual(row["groups_scanned"], 3)
        self.assertEqual(row["posts_found"], 10)
        self.assertEqual(row["listings_extracted"], 7)
        self.assertIsNotNone(row["ended_at"])

    def test_fail_run(self):
        run_id = self.db.create_run()
        self.db.fail_run(run_id)

        conn = self.db.get_connection()
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "failed")


class PostRoundtripTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db = _make_db(self._tmpdir)
        self.run_id = self.db.create_run()

    def _post_data(self, **overrides) -> dict:
        base = dict(
            source="facebook",
            post_id="p_001",
            post_url="https://fb.com/post/001",
            group_url="https://fb.com/groups/test",
            post_content_hash="abc123",
            raw_text="ให้เช่า ราคาดี",
            raw_text_redacted=None,
            status="new",
        )
        base.update(overrides)
        return base

    def test_insert_and_fetch_post(self):
        post_id, is_new, dup = self.db.upsert_post(self.run_id, self._post_data())
        self.assertTrue(is_new)
        self.assertIsNone(dup)
        self.assertIsInstance(post_id, int)

        conn = self.db.get_connection()
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        conn.close()

        self.assertEqual(row["post_id"], "p_001")
        self.assertEqual(row["raw_text"], "ให้เช่า ราคาดี")
        self.assertEqual(row["status"], "new")

    def test_dedup_by_post_id(self):
        pid1, is_new1, _ = self.db.upsert_post(self.run_id, self._post_data())
        pid2, is_new2, reason = self.db.upsert_post(self.run_id, self._post_data())

        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(pid1, pid2)
        self.assertEqual(reason, "post_id")

    def test_dedup_by_content_hash(self):
        data_a = self._post_data(post_id="p_AAA", post_content_hash="hash_shared")
        data_b = self._post_data(post_id="p_BBB", post_content_hash="hash_shared")

        pid1, is_new1, _ = self.db.upsert_post(self.run_id, data_a)
        pid2, is_new2, reason = self.db.upsert_post(self.run_id, data_b)

        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(pid1, pid2)
        self.assertEqual(reason, "content_hash")

    def test_dedup_updates_last_seen_at(self):
        self.db.upsert_post(self.run_id, self._post_data())
        self.db.upsert_post(self.run_id, self._post_data())

        conn = self.db.get_connection()
        row = conn.execute("SELECT last_seen_at FROM posts WHERE post_id='p_001'").fetchone()
        conn.close()
        self.assertIsNotNone(row["last_seen_at"])

    def test_two_distinct_posts_both_inserted(self):
        self.db.upsert_post(self.run_id, self._post_data(post_id="p_A", post_content_hash="h_A"))
        self.db.upsert_post(self.run_id, self._post_data(post_id="p_B", post_content_hash="h_B"))

        conn = self.db.get_connection()
        count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_update_post_status(self):
        post_id, _, _ = self.db.upsert_post(self.run_id, self._post_data())
        self.db.update_post_status(post_id, "processed")

        conn = self.db.get_connection()
        row = conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "processed")


class ListingRoundtripTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db = _make_db(self._tmpdir)
        self.run_id = self.db.create_run()
        post_id, _, _ = self.db.upsert_post(self.run_id, dict(
            post_id="p_100",
            post_content_hash="hash_100",
            raw_text="test",
            status="new",
        ))
        self.post_id = post_id

    def _listing_data(self, **overrides) -> dict:
        base = dict(
            condo_name="Lumpini Suite",
            room_type="studio",
            size_sqm=28.0,
            floor="5",
            rent=11000.0,
            move_in_cost=33000.0,
            location_tags="BTS อ่อนนุช",
            status="owner",
            summary="ห้องสตูดิโอ คอนโด Lumpini Suite ชั้น 5 ราคา 11,000 บาท",
        )
        base.update(overrides)
        return base

    def test_insert_and_fetch_listing(self):
        listing_id = self.db.insert_listing(self.post_id, self._listing_data())
        self.assertIsInstance(listing_id, int)

        conn = self.db.get_connection()
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        conn.close()

        self.assertEqual(row["condo_name"], "Lumpini Suite")
        self.assertEqual(row["rent"], 11000.0)
        self.assertEqual(row["room_type"], "studio")
        self.assertEqual(row["status"], "owner")
        self.assertEqual(row["location_tags"], "BTS อ่อนนุช")

    def test_get_listings_returns_joined_row(self):
        self.db.insert_listing(self.post_id, self._listing_data())
        rows = self.db.get_listings()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["condo_name"], "Lumpini Suite")
        self.assertIn("post_url", rows[0].keys())

    def test_get_listings_empty(self):
        self.assertEqual(self.db.get_listings(), [])

    def test_move_in_cost_stored(self):
        self.db.insert_listing(self.post_id, self._listing_data(move_in_cost=33000.0))
        rows = self.db.get_listings()
        self.assertAlmostEqual(rows[0]["move_in_cost"], 33000.0)

    def test_null_fields_accepted(self):
        listing_id = self.db.insert_listing(self.post_id, self._listing_data(
            condo_name=None, rent=None, location_tags=None,
        ))
        conn = self.db.get_connection()
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        conn.close()
        self.assertIsNone(row["condo_name"])
        self.assertIsNone(row["rent"])


class CommentsRoundtripTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db = _make_db(self._tmpdir)
        run_id = self.db.create_run()
        post_id, _, _ = self.db.upsert_post(run_id, dict(
            post_id="p_200", post_content_hash="h_200", raw_text="x", status="new",
        ))
        self.post_id = post_id

    def test_insert_and_fetch_comments(self):
        self.db.insert_post_comments(self.post_id, ["ราคาเท่าไหร่?", "ว่างไหม?"])
        comments = self.db.get_comments_for_post(self.post_id)
        self.assertEqual(comments, ["ราคาเท่าไหร่?", "ว่างไหม?"])

    def test_empty_comments_list_no_op(self):
        self.db.insert_post_comments(self.post_id, [])
        self.assertEqual(self.db.get_comments_for_post(self.post_id), [])

    def test_empty_string_comments_ignored(self):
        self.db.insert_post_comments(self.post_id, ["", "valid"])
        comments = self.db.get_comments_for_post(self.post_id)
        self.assertEqual(comments, ["valid"])

    def test_comments_ordered_by_insertion(self):
        self.db.insert_post_comments(self.post_id, ["first", "second", "third"])
        comments = self.db.get_comments_for_post(self.post_id)
        self.assertEqual(comments, ["first", "second", "third"])


class RetentionPurgeTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.db = _make_db(self._tmpdir)

    def _insert_old_post(self, days_ago: int, post_id: str) -> tuple[int, int]:
        run_id = self.db.create_run()
        post_pk, _, _ = self.db.upsert_post(run_id, dict(
            post_id=post_id,
            post_content_hash=f"hash_{post_id}",
            raw_text="old",
            status="new",
        ))
        old_ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn = self.db.get_connection()
        conn.execute("UPDATE posts SET scraped_at=? WHERE id=?", (old_ts, post_pk))
        conn.commit()
        conn.close()

        listing_pk = self.db.insert_listing(post_pk, dict(rent=10000))
        return post_pk, listing_pk

    def test_old_posts_deleted(self):
        self._insert_old_post(days_ago=35, post_id="old_p")
        self.db.cleanup_old_posts(days=30)

        conn = self.db.get_connection()
        count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_recent_posts_not_deleted(self):
        self._insert_old_post(days_ago=5, post_id="recent_p")
        self.db.cleanup_old_posts(days=30)

        conn = self.db.get_connection()
        count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_cascade_deletes_listings(self):
        _, listing_pk = self._insert_old_post(days_ago=40, post_id="old_cascade")
        self.db.cleanup_old_posts(days=30)

        conn = self.db.get_connection()
        l_count = conn.execute("SELECT COUNT(*) FROM listings WHERE id=?", (listing_pk,)).fetchone()[0]
        conn.close()
        self.assertEqual(l_count, 0)

    def test_purge_only_removes_old_not_recent(self):
        self._insert_old_post(days_ago=40, post_id="stale")
        self._insert_old_post(days_ago=10, post_id="fresh")
        self.db.cleanup_old_posts(days=30)

        conn = self.db.get_connection()
        count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
