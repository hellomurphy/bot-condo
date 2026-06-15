import json
import os
import sys
import unittest
from pathlib import Path

ROOT_DIR = str(Path(__file__).parents[1])
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

from analysis.extractor import ExtractionError, _parse_and_validate


def _make_listing(**overrides) -> dict:
    base = {
        "condo_name": None,
        "room_type": "studio",
        "size_sqm": 28,
        "floor": None,
        "rent": 10000,
        "location_tags": None,
        "status": None,
        "summary": "ห้องสตูดิโอ ราคา 10,000 บาท",
    }
    base.update(overrides)
    return base


def _wrap(*listings) -> str:
    return json.dumps({"listings": list(listings)})


class ParseAndValidateTests(unittest.TestCase):
    # --- happy path ---

    def test_valid_json_returns_dict_with_listings(self):
        raw = _wrap(_make_listing())
        result = _parse_and_validate(raw)
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result["listings"], list)
        self.assertEqual(len(result["listings"]), 1)

    def test_strips_markdown_backticks(self):
        inner = _wrap(_make_listing())
        raw = f"```json\n{inner}\n```"
        result = _parse_and_validate(raw)
        self.assertEqual(len(result["listings"]), 1)

    def test_strips_bare_backticks(self):
        inner = _wrap(_make_listing())
        raw = f"```{inner}```"
        result = _parse_and_validate(raw)
        self.assertEqual(len(result["listings"]), 1)

    def test_regex_fallback_extracts_embedded_json(self):
        inner = _wrap(_make_listing())
        raw = f"Here is the result: {inner} end."
        result = _parse_and_validate(raw)
        self.assertEqual(len(result["listings"]), 1)

    def test_multiple_listings_all_returned(self):
        raw = _wrap(_make_listing(), _make_listing(rent=12000))
        result = _parse_and_validate(raw)
        self.assertEqual(len(result["listings"]), 2)

    def test_empty_listings_array_is_valid(self):
        raw = json.dumps({"listings": []})
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"], [])

    # --- room_type normalization ---

    def test_room_type_verbose_mapped(self):
        raw = _wrap(_make_listing(room_type="1 bedroom"))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["room_type"], "1br")

    def test_room_type_thai_studio_mapped(self):
        raw = _wrap(_make_listing(room_type="สตูดิโอ"))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["room_type"], "studio")

    def test_room_type_case_insensitive(self):
        raw = _wrap(_make_listing(room_type="STUDIO ROOM"))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["room_type"], "studio")

    def test_room_type_unrecognized_kept_as_is(self):
        raw = _wrap(_make_listing(room_type="penthouse"))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["room_type"], "penthouse")

    def test_room_type_none_becomes_unknown(self):
        raw = _wrap(_make_listing(room_type=None))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["room_type"], "unknown")

    # --- summary fallback ---

    def test_summary_empty_string_becomes_dash(self):
        raw = _wrap(_make_listing(summary=""))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["summary"], "-")

    def test_summary_none_becomes_dash(self):
        raw = _wrap(_make_listing(summary=None))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["summary"], "-")

    def test_summary_whitespace_only_becomes_dash(self):
        raw = _wrap(_make_listing(summary="   "))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["summary"], "-")

    def test_summary_valid_preserved(self):
        raw = _wrap(_make_listing(summary="คอนโด ราคาดี"))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["summary"], "คอนโด ราคาดี")

    # --- error cases ---

    def test_plain_garbage_raises_extraction_error(self):
        with self.assertRaises(ExtractionError):
            _parse_and_validate("this is not json at all")

    def test_json_without_listings_key_raises(self):
        with self.assertRaises(ExtractionError):
            _parse_and_validate(json.dumps({"data": []}))

    def test_listings_not_a_list_raises(self):
        with self.assertRaises(ExtractionError):
            _parse_and_validate(json.dumps({"listings": "oops"}))

    def test_empty_string_raises_extraction_error(self):
        with self.assertRaises(ExtractionError):
            _parse_and_validate("")

    def test_deeply_broken_json_raises(self):
        with self.assertRaises(ExtractionError):
            _parse_and_validate("{listings: [}")


if __name__ == "__main__":
    unittest.main()
