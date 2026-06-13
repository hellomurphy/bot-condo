import os
import sys
import unittest
from pathlib import Path

ROOT_DIR = str(Path(__file__).parents[1])
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

from web.forms import parse_form, json_debug_snapshot


def _base_data(**overrides) -> dict:
    """Minimal valid form submission."""
    base = dict(
        FB_GROUP_URLS="https://fb.com/groups/condo",
        TARGET_BUDGET="12000",
        MAX_BUDGET="15000",
        MIN_SIZE_SQM="24",
        MAX_SCROLL_ROUNDS="8",
        MAX_POSTS_PER_RUN="150",
    )
    base.update(overrides)
    return base


class BudgetValidationTests(unittest.TestCase):

    def test_valid_integer_budget(self):
        _, errors = parse_form(_base_data(TARGET_BUDGET="12000", MAX_BUDGET="15000"))
        self.assertEqual(errors, [])

    def test_valid_decimal_budget(self):
        _, errors = parse_form(_base_data(TARGET_BUDGET="12500.50", MAX_BUDGET="15000.00"))
        self.assertEqual(errors, [])

    def test_non_numeric_target_budget_error(self):
        _, errors = parse_form(_base_data(TARGET_BUDGET="twelve thousand"))
        self.assertTrue(any("TARGET_BUDGET" in e for e in errors))

    def test_non_numeric_max_budget_error(self):
        _, errors = parse_form(_base_data(MAX_BUDGET="fifteen-k"))
        self.assertTrue(any("MAX_BUDGET" in e for e in errors))

    def test_empty_target_budget_falls_back_to_default(self):
        env, errors = parse_form(_base_data(TARGET_BUDGET=""))
        self.assertNotIn("TARGET_BUDGET must be a number", errors)
        self.assertEqual(env.get("TARGET_BUDGET"), "12000")

    def test_empty_max_budget_falls_back_to_default(self):
        env, errors = parse_form(_base_data(MAX_BUDGET=""))
        self.assertNotIn("MAX_BUDGET must be a number", errors)
        self.assertEqual(env.get("MAX_BUDGET"), "15000")

    def test_valid_move_in_cost_accepted(self):
        env, errors = parse_form(_base_data(MAX_MOVE_IN_COST="40000"))
        self.assertNotIn("MAX_MOVE_IN_COST must be a number", errors)
        self.assertEqual(env.get("MAX_MOVE_IN_COST"), "40000")

    def test_non_numeric_move_in_cost_error(self):
        _, errors = parse_form(_base_data(MAX_MOVE_IN_COST="lots"))
        self.assertTrue(any("MAX_MOVE_IN_COST" in e for e in errors))

    def test_blank_move_in_cost_omitted_from_env(self):
        env, errors = parse_form(_base_data(MAX_MOVE_IN_COST=""))
        self.assertNotIn("MAX_MOVE_IN_COST", env)
        self.assertEqual(errors, [])

    def test_missing_move_in_cost_key_omitted(self):
        data = _base_data()
        data.pop("MAX_MOVE_IN_COST", None)
        env, errors = parse_form(data)
        self.assertNotIn("MAX_MOVE_IN_COST", env)
        self.assertEqual(errors, [])

    def test_headless_always_set_true(self):
        env, _ = parse_form(_base_data())
        self.assertEqual(env.get("HEADLESS"), "true")

    def test_negative_budget_string_rejected(self):
        _, errors = parse_form(_base_data(TARGET_BUDGET="-5000"))
        self.assertTrue(any("TARGET_BUDGET" in e for e in errors))

    def test_budget_with_currency_symbol_rejected(self):
        _, errors = parse_form(_base_data(MAX_BUDGET="฿15000"))
        self.assertTrue(any("MAX_BUDGET" in e for e in errors))


class AreaParsingTests(unittest.TestCase):

    def test_valid_sqm_accepted(self):
        env, errors = parse_form(_base_data(MIN_SIZE_SQM="28"))
        self.assertNotIn("Min sqm must be a number", errors)
        self.assertEqual(env.get("MIN_SIZE_SQM"), "28")

    def test_decimal_sqm_accepted(self):
        env, errors = parse_form(_base_data(MIN_SIZE_SQM="25.5"))
        self.assertNotIn("Min sqm must be a number", errors)
        self.assertEqual(env.get("MIN_SIZE_SQM"), "25.5")

    def test_non_numeric_sqm_error(self):
        _, errors = parse_form(_base_data(MIN_SIZE_SQM="big"))
        self.assertTrue(any("sqm" in e.lower() for e in errors))

    def test_empty_sqm_falls_back_to_default(self):
        env, errors = parse_form(_base_data(MIN_SIZE_SQM=""))
        self.assertNotIn("Min sqm must be a number", errors)
        self.assertEqual(env.get("MIN_SIZE_SQM"), "24")

    def test_negative_sqm_rejected(self):
        _, errors = parse_form(_base_data(MIN_SIZE_SQM="-10"))
        self.assertTrue(any("sqm" in e.lower() for e in errors))

    def test_washer_true_string(self):
        env, _ = parse_form(_base_data(MUST_HAVE_WASHER="true"))
        self.assertEqual(env["MUST_HAVE_WASHER"], "true")

    def test_washer_on_string(self):
        env, _ = parse_form(_base_data(MUST_HAVE_WASHER="on"))
        self.assertEqual(env["MUST_HAVE_WASHER"], "true")

    def test_washer_absent_is_false(self):
        data = _base_data()
        data.pop("MUST_HAVE_WASHER", None)
        env, _ = parse_form(data)
        self.assertEqual(env["MUST_HAVE_WASHER"], "false")

    def test_parking_true_bool(self):
        env, _ = parse_form(_base_data(NEED_PARKING=True))
        self.assertEqual(env["NEED_PARKING"], "true")

    def test_parking_false_string(self):
        env, _ = parse_form(_base_data(NEED_PARKING="false"))
        self.assertEqual(env["NEED_PARKING"], "false")

    def test_scroll_rounds_integer_required(self):
        _, errors = parse_form(_base_data(MAX_SCROLL_ROUNDS="8.5"))
        self.assertTrue(any("MAX_SCROLL_ROUNDS" in e for e in errors))

    def test_posts_per_run_integer_required(self):
        _, errors = parse_form(_base_data(MAX_POSTS_PER_RUN="100.0"))
        self.assertTrue(any("MAX_POSTS_PER_RUN" in e for e in errors))

    def test_valid_scroll_and_posts(self):
        env, errors = parse_form(_base_data(MAX_SCROLL_ROUNDS="5", MAX_POSTS_PER_RUN="200"))
        self.assertEqual(errors, [])
        self.assertEqual(env["MAX_SCROLL_ROUNDS"], "5")
        self.assertEqual(env["MAX_POSTS_PER_RUN"], "200")


class EmptyStationsTests(unittest.TestCase):
    """FB_GROUP_URLS is the only URL-type field in the form; these tests cover empty/multi-URL edge cases."""

    def test_no_urls_produces_error(self):
        data = _base_data()
        data["FB_GROUP_URLS"] = ""
        _, errors = parse_form(data)
        self.assertTrue(any("FB Group URL" in e for e in errors))

    def test_whitespace_only_url_rejected(self):
        data = _base_data()
        data["FB_GROUP_URLS"] = "   \n  "
        _, errors = parse_form(data)
        self.assertTrue(any("FB Group URL" in e for e in errors))

    def test_single_url_accepted(self):
        env, errors = parse_form(_base_data(FB_GROUP_URLS="https://fb.com/groups/test"))
        self.assertEqual(errors, [])
        self.assertEqual(env["FB_GROUP_URLS"], "https://fb.com/groups/test")

    def test_multiple_urls_via_multiline_string(self):
        urls = "https://fb.com/groups/a\nhttps://fb.com/groups/b"
        env, errors = parse_form(_base_data(FB_GROUP_URLS=urls))
        self.assertEqual(errors, [])
        parts = env["FB_GROUP_URLS"].split(",")
        self.assertEqual(len(parts), 2)
        self.assertIn("https://fb.com/groups/a", parts)
        self.assertIn("https://fb.com/groups/b", parts)

    def test_multiple_urls_via_list(self):
        env, errors = parse_form(_base_data(
            FB_GROUP_URLS=["https://fb.com/groups/a", "https://fb.com/groups/b"]
        ))
        self.assertEqual(errors, [])
        parts = env["FB_GROUP_URLS"].split(",")
        self.assertEqual(len(parts), 2)

    def test_list_with_blank_entries_skipped(self):
        env, errors = parse_form(_base_data(
            FB_GROUP_URLS=["https://fb.com/groups/a", "", "   "]
        ))
        self.assertEqual(errors, [])
        self.assertEqual(env["FB_GROUP_URLS"], "https://fb.com/groups/a")

    def test_missing_fb_urls_key_produces_error(self):
        data = _base_data()
        data.pop("FB_GROUP_URLS", None)
        _, errors = parse_form(data)
        self.assertTrue(any("FB Group URL" in e for e in errors))

    def test_fb_urls_not_in_env_when_missing(self):
        data = _base_data()
        data["FB_GROUP_URLS"] = ""
        env, _ = parse_form(data)
        self.assertNotIn("FB_GROUP_URLS", env)


class DebugSnapshotTests(unittest.TestCase):
    def test_snapshot_contains_present_keys(self):
        env = {
            "TARGET_BUDGET": "12000",
            "MAX_BUDGET": "15000",
            "MIN_SIZE_SQM": "24",
            "MUST_HAVE_WASHER": "false",
            "NEED_PARKING": "false",
            "MAX_SCROLL_ROUNDS": "8",
            "MAX_POSTS_PER_RUN": "150",
        }
        snapshot = json_debug_snapshot(env)
        self.assertIn("TARGET_BUDGET=12000", snapshot)
        self.assertIn("MAX_BUDGET=15000", snapshot)
        self.assertIn("MIN_SIZE_SQM=24", snapshot)

    def test_snapshot_omits_missing_keys(self):
        env = {"TARGET_BUDGET": "12000"}
        snapshot = json_debug_snapshot(env)
        self.assertNotIn("MAX_MOVE_IN_COST", snapshot)
        self.assertNotIn("MAX_BUDGET", snapshot)

    def test_snapshot_semicolon_separated(self):
        env = {"TARGET_BUDGET": "12000", "MAX_BUDGET": "15000"}
        snapshot = json_debug_snapshot(env)
        self.assertIn(";", snapshot)

    def test_snapshot_in_env_after_parse(self):
        env, errors = parse_form(_base_data())
        self.assertEqual(errors, [])
        self.assertIn("WEB_DEBUG_FORM_VALUES", env)
        self.assertIn("TARGET_BUDGET=", env["WEB_DEBUG_FORM_VALUES"])


if __name__ == "__main__":
    unittest.main()
