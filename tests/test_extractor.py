import hashlib
import json
import os
import sys
import unittest
from pathlib import Path

ROOT_DIR = str(Path(__file__).parents[1])
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

from analysis.extractor import (
    ExtractionError,
    _parse_and_validate,
    compute_fingerprint,
    compute_move_in_cost,
)


# ---------------------------------------------------------------------------
# compute_move_in_cost
# ---------------------------------------------------------------------------

class ComputeMoveInCostTests(unittest.TestCase):
    def test_move_in_cost_stated_takes_precedence(self):
        listing = {"move_in_cost_stated": 25000, "monthly_rent": 10000, "deposit_months": 2}
        self.assertEqual(compute_move_in_cost(listing), 25000.0)

    def test_stated_cost_returned_as_float(self):
        listing = {"move_in_cost_stated": 30000}
        self.assertIsInstance(compute_move_in_cost(listing), float)

    def test_deposit_only_no_advance(self):
        listing = {"monthly_rent": 10000, "deposit_months": 2, "advance_months": None}
        self.assertEqual(compute_move_in_cost(listing), 20000.0)

    def test_deposit_and_advance(self):
        listing = {"monthly_rent": 10000, "deposit_months": 2, "advance_months": 1}
        self.assertEqual(compute_move_in_cost(listing), 30000.0)

    def test_advance_defaults_to_zero_when_missing(self):
        listing = {"monthly_rent": 10000, "deposit_months": 2}
        self.assertEqual(compute_move_in_cost(listing), 20000.0)

    def test_no_rent_returns_none(self):
        listing = {"monthly_rent": None, "deposit_months": 2}
        self.assertIsNone(compute_move_in_cost(listing))

    def test_no_deposit_returns_none(self):
        listing = {"monthly_rent": 10000, "deposit_months": None}
        self.assertIsNone(compute_move_in_cost(listing))

    def test_all_fields_missing_returns_none(self):
        self.assertIsNone(compute_move_in_cost({}))

    def test_deposit_zero_months_computes_advance_only(self):
        listing = {"monthly_rent": 10000, "deposit_months": 0, "advance_months": 1}
        self.assertEqual(compute_move_in_cost(listing), 10000.0)

    def test_stated_cost_zero_is_valid(self):
        listing = {"move_in_cost_stated": 0, "monthly_rent": 10000, "deposit_months": 2}
        self.assertEqual(compute_move_in_cost(listing), 0.0)


# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------

class ComputeFingerprintTests(unittest.TestCase):
    def _expected(self, **fields) -> str:
        defaults = {
            "condo_name": None, "monthly_rent": None, "size_sqm": None,
            "room_type": None, "floor": None, "location_text": None,
            "station_name": None, "agent_or_owner": None,
        }
        defaults.update(fields)
        parts = "|".join(str(defaults[f] or "") for f in [
            "condo_name", "monthly_rent", "size_sqm", "room_type",
            "floor", "location_text", "station_name", "agent_or_owner",
        ])
        return hashlib.sha256(parts.encode()).hexdigest()

    def test_returns_64_char_hex(self):
        fp = compute_fingerprint({})
        self.assertEqual(len(fp), 64)
        self.assertRegex(fp, r'^[0-9a-f]+$')

    def test_identical_listings_same_fingerprint(self):
        listing = {"condo_name": "Lumpini", "monthly_rent": 12000, "room_type": "studio"}
        self.assertEqual(compute_fingerprint(listing), compute_fingerprint(listing))

    def test_different_rent_different_fingerprint(self):
        a = {"monthly_rent": 10000}
        b = {"monthly_rent": 11000}
        self.assertNotEqual(compute_fingerprint(a), compute_fingerprint(b))

    def test_empty_dict_stable(self):
        self.assertEqual(compute_fingerprint({}), self._expected())

    def test_none_and_missing_key_treated_same(self):
        self.assertEqual(
            compute_fingerprint({"condo_name": None}),
            compute_fingerprint({}),
        )

    def test_extra_fields_ignored(self):
        a = {"monthly_rent": 10000, "confidence": 0.9}
        b = {"monthly_rent": 10000, "has_washer": True}
        self.assertEqual(compute_fingerprint(a), compute_fingerprint(b))

    def test_all_fields_contribute(self):
        full = {
            "condo_name": "X", "monthly_rent": 1, "size_sqm": 25, "room_type": "studio",
            "floor": 5, "location_text": "On Nut", "station_name": "BTS On Nut",
            "agent_or_owner": "owner",
        }
        for key in full:
            partial = {k: v for k, v in full.items() if k != key}
            self.assertNotEqual(
                compute_fingerprint(full), compute_fingerprint(partial),
                msg=f"removing {key!r} should change the fingerprint",
            )


# ---------------------------------------------------------------------------
# _parse_and_validate
# ---------------------------------------------------------------------------

def _make_listing(**overrides) -> dict:
    base = {
        "listing_type": "rent",
        "condo_name": None,
        "location_text": None,
        "station_name": None,
        "monthly_rent": 10000,
        "size_sqm": 28,
        "room_type": "studio",
        "floor": None,
        "furnishing": "partly",
        "deposit_months": 2,
        "advance_months": 1,
        "move_in_cost_stated": None,
        "other_fee_text": None,
        "contract_min_months": None,
        "available_date": None,
        "has_parking": None,
        "has_washer": None,
        "has_fridge": None,
        "has_wifi": None,
        "pet_allowed": None,
        "near_transit": None,
        "agent_or_owner": "unknown",
        "risk_flags": [],
        "missing_fields": [],
        "questions_to_ask": [],
        "confidence": 0.8,
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
        raw = _wrap(_make_listing(), _make_listing(monthly_rent=12000))
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

    # --- confidence clamping ---

    def test_confidence_above_1_clamped(self):
        raw = _wrap(_make_listing(confidence=1.5))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["confidence"], 1.0)

    def test_confidence_below_0_clamped(self):
        raw = _wrap(_make_listing(confidence=-0.3))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["confidence"], 0.0)

    def test_confidence_within_range_unchanged(self):
        raw = _wrap(_make_listing(confidence=0.75))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["confidence"], 0.75)

    def test_confidence_none_not_set(self):
        raw = _wrap(_make_listing(confidence=None))
        result = _parse_and_validate(raw)
        self.assertIsNone(result["listings"][0]["confidence"])

    # --- array field normalization ---

    def test_risk_flags_non_list_becomes_empty(self):
        raw = _wrap(_make_listing(risk_flags="scam_signal"))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["risk_flags"], [])

    def test_missing_fields_non_list_becomes_empty(self):
        raw = _wrap(_make_listing(missing_fields=None))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["missing_fields"], [])

    def test_questions_to_ask_non_list_becomes_empty(self):
        raw = _wrap(_make_listing(questions_to_ask=42))
        result = _parse_and_validate(raw)
        self.assertEqual(result["listings"][0]["questions_to_ask"], [])

    def test_valid_list_fields_preserved(self):
        raw = _wrap(_make_listing(
            risk_flags=["flag_a"],
            missing_fields=["monthly_rent"],
            questions_to_ask=["Is parking included?"],
        ))
        result = _parse_and_validate(raw)
        listing = result["listings"][0]
        self.assertEqual(listing["risk_flags"], ["flag_a"])
        self.assertEqual(listing["missing_fields"], ["monthly_rent"])
        self.assertEqual(listing["questions_to_ask"], ["Is parking included?"])

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
