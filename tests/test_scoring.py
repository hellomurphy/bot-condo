import os
import sys
import unittest
from pathlib import Path

ROOT_DIR = str(Path(__file__).parents[1])
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")

from rules.preferences import Preferences
from rules.scoring import (
    apply_hard_filters,
    apply_vision,
    base_score,
    score_listing,
    tier_from_score,
)


def _prefs(**overrides) -> Preferences:
    defaults = dict(
        target_budget=12000,
        max_budget=15000,
        max_move_in_cost=None,
        preferred_areas=["On Nut", "Udom Suk"],
        preferred_stations=["BTS On Nut", "BTS Udom Suk"],
        min_size_sqm=24,
        must_have_washer=False,
        need_parking=False,
        pet_friendly=False,
        preferred_room_types=["studio", "1br"],
        alert_min_tier="shortlist",
    )
    defaults.update(overrides)
    return Preferences(**defaults)


def _listing(**overrides) -> dict:
    """Minimal passing listing — no field triggers a hard filter."""
    defaults = dict(
        listing_type="rent",
        monthly_rent=11000,
        size_sqm=28,
        move_in_cost=None,
        station_name=None,
        location_text=None,
        furnishing="partly",
        has_washer=None,
        has_parking=None,
        contract_min_months=None,
        available_date=None,
        room_type="studio",
        agent_or_owner="unknown",
        risk_flags=[],
        confidence=0.8,
    )
    defaults.update(overrides)
    return defaults


class TierFromScoreTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(tier_from_score(80), "must_call")
        self.assertEqual(tier_from_score(79.9), "shortlist")
        self.assertEqual(tier_from_score(65), "shortlist")
        self.assertEqual(tier_from_score(64.9), "maybe")
        self.assertEqual(tier_from_score(50), "maybe")
        self.assertEqual(tier_from_score(49.9), "skip")
        self.assertEqual(tier_from_score(0), "skip")


class HardFilterTests(unittest.TestCase):
    def test_over_budget_skipped(self):
        tier, reason = apply_hard_filters(_listing(monthly_rent=16000), _prefs())
        self.assertEqual(tier, "skip")
        self.assertIn("budget", reason)

    def test_at_max_budget_passes(self):
        tier, _ = apply_hard_filters(_listing(monthly_rent=15000), _prefs())
        self.assertIsNone(tier)

    def test_too_small_skipped(self):
        tier, reason = apply_hard_filters(_listing(size_sqm=20), _prefs())
        self.assertEqual(tier, "skip")
        self.assertIn("small", reason)

    def test_at_min_size_passes(self):
        tier, _ = apply_hard_filters(_listing(size_sqm=24), _prefs())
        self.assertIsNone(tier)

    def test_sale_listing_skipped(self):
        tier, reason = apply_hard_filters(_listing(listing_type="sale"), _prefs())
        self.assertEqual(tier, "skip")
        self.assertIn("rental", reason)

    def test_seeker_listing_skipped(self):
        tier, reason = apply_hard_filters(_listing(listing_type="seeking"), _prefs())
        self.assertEqual(tier, "skip")

    def test_no_rent_returns_need_info(self):
        tier, reason = apply_hard_filters(_listing(monthly_rent=None), _prefs())
        self.assertEqual(tier, "need_info")
        self.assertIn("rent", reason)

    def test_low_confidence_returns_need_info(self):
        tier, reason = apply_hard_filters(_listing(confidence=0.3), _prefs())
        self.assertEqual(tier, "need_info")
        self.assertIn("confidence", reason)

    def test_confidence_at_threshold_passes(self):
        # 0.45 is exactly the cutoff — should NOT trigger need_info
        tier, _ = apply_hard_filters(_listing(confidence=0.45), _prefs())
        self.assertIsNone(tier)

    def test_no_washer_skipped_when_required(self):
        tier, reason = apply_hard_filters(
            _listing(has_washer=False),
            _prefs(must_have_washer=True),
        )
        self.assertEqual(tier, "skip")
        self.assertIn("washer", reason)

    def test_washer_not_required_false_passes(self):
        tier, _ = apply_hard_filters(
            _listing(has_washer=False),
            _prefs(must_have_washer=False),
        )
        self.assertIsNone(tier)

    def test_no_parking_skipped_when_required(self):
        tier, reason = apply_hard_filters(
            _listing(has_parking=False),
            _prefs(need_parking=True),
        )
        self.assertEqual(tier, "skip")
        self.assertIn("parking", reason)

    def test_move_in_cost_too_high_skipped(self):
        tier, reason = apply_hard_filters(
            _listing(move_in_cost=50000),
            _prefs(max_move_in_cost=40000),
        )
        self.assertEqual(tier, "skip")
        self.assertIn("move-in", reason)

    def test_move_in_cost_no_cap_passes(self):
        tier, _ = apply_hard_filters(
            _listing(move_in_cost=99999),
            _prefs(max_move_in_cost=None),
        )
        self.assertIsNone(tier)

    def test_unknown_listing_type_passes(self):
        # "unknown" is allowed through hard filters
        tier, _ = apply_hard_filters(_listing(listing_type="unknown"), _prefs())
        self.assertIsNone(tier)


class CommuteScoreTests(unittest.TestCase):
    def test_preferred_station_exact_match_gets_25(self):
        result = base_score(_listing(station_name="BTS On Nut"), _prefs())
        self.assertEqual(result["commute_score"], 25)

    def test_preferred_station_case_insensitive(self):
        result = base_score(_listing(station_name="bts on nut"), _prefs())
        self.assertEqual(result["commute_score"], 25)

    def test_non_preferred_station_gets_14(self):
        result = base_score(_listing(station_name="BTS Asok"), _prefs())
        self.assertEqual(result["commute_score"], 14)

    def test_near_bts_keyword_in_text_gets_10(self):
        result = base_score(_listing(location_text="ใกล้ BTS 5 นาที"), _prefs())
        self.assertEqual(result["commute_score"], 10)

    def test_preferred_area_in_text_gets_18(self):
        result = base_score(_listing(location_text="แถว On Nut เดินทางสะดวก"), _prefs())
        self.assertEqual(result["commute_score"], 18)

    def test_known_location_no_match_gets_7(self):
        result = base_score(_listing(location_text="พระราม 9 ใกล้เซ็นทรัล"), _prefs())
        self.assertEqual(result["commute_score"], 7)

    def test_no_location_info_gets_0(self):
        result = base_score(_listing(station_name=None, location_text=None), _prefs())
        self.assertEqual(result["commute_score"], 0)


class TrustScoreTests(unittest.TestCase):
    def test_owner_adds_3_points(self):
        base = base_score(_listing(agent_or_owner="owner", risk_flags=[], confidence=0.75), _prefs())
        # base trust starts at 5, owner +3 = 8
        self.assertEqual(base["trust_score"], 8)

    def test_risk_flag_reduces_by_3_each(self):
        base_no_flags = base_score(_listing(risk_flags=[]), _prefs())
        base_two_flags = base_score(_listing(risk_flags=["flag1", "flag2"]), _prefs())
        self.assertEqual(base_no_flags["trust_score"] - base_two_flags["trust_score"], 6)

    def test_risk_flags_clamped_to_zero(self):
        result = base_score(_listing(risk_flags=["a", "b", "c", "d", "e"]), _prefs())
        self.assertEqual(result["trust_score"], 0)

    def test_high_confidence_adds_2(self):
        low_conf = base_score(_listing(confidence=0.75, risk_flags=[]), _prefs())
        high_conf = base_score(_listing(confidence=0.85, risk_flags=[]), _prefs())
        self.assertEqual(high_conf["trust_score"] - low_conf["trust_score"], 2)

    def test_risk_flags_as_json_string_parsed(self):
        import json
        # confidence=0.8 triggers +2 bonus; 5 base - 3 (flag) + 2 (conf) = 4
        result = base_score(_listing(risk_flags=json.dumps(["scam_signal"]), confidence=0.8), _prefs())
        self.assertEqual(result["trust_score"], 4)


class PriceScoreTests(unittest.TestCase):
    def test_under_target_budget_gets_20(self):
        result = base_score(_listing(monthly_rent=10000, move_in_cost=None), _prefs())
        self.assertEqual(result["price_score"], 20)

    def test_between_target_and_max_gets_12(self):
        result = base_score(_listing(monthly_rent=13000, move_in_cost=None), _prefs())
        self.assertEqual(result["price_score"], 12)

    def test_reasonable_move_in_adds_10(self):
        # move_in <= target*3 (36000) → +10
        result = base_score(_listing(monthly_rent=10000, move_in_cost=30000), _prefs())
        self.assertEqual(result["price_score"], 30)

    def test_high_move_in_deducts_10(self):
        # move_in > target*4 (48000) → -10; rent=10k → +20; total clamped 0..30
        result = base_score(_listing(monthly_rent=10000, move_in_cost=50000), _prefs())
        self.assertEqual(result["price_score"], 10)


class VisionBonusTests(unittest.TestCase):
    def _base_shortlist(self) -> dict:
        listing = _listing(
            station_name="BTS On Nut",
            furnishing="partly",
            monthly_rent=11000,
            room_type="studio",
            confidence=0.85,
            agent_or_owner="owner",
        )
        result = base_score(listing, _prefs())
        return result

    def test_vision_bonus_lifts_condition_score(self):
        br = self._base_shortlist()
        after = apply_vision(br, vision_score=10.0)
        self.assertGreater(after["condition_score"], br["condition_score"])

    def test_vision_bonus_max_condition_capped_at_20(self):
        br = self._base_shortlist()
        after = apply_vision(br, vision_score=10.0)
        self.assertLessEqual(after["condition_score"], 20)

    def test_vision_zero_no_change(self):
        br = self._base_shortlist()
        after = apply_vision(br, vision_score=0.0)
        self.assertEqual(after["condition_score"], br["condition_score"])
        self.assertEqual(after["final_total"], br["base_total"])

    def test_vision_sets_tier(self):
        br = self._base_shortlist()
        after = apply_vision(br, vision_score=10.0)
        self.assertIn(after["tier"], ["shortlist", "must_call"])


class ScoreListingPipelineTests(unittest.TestCase):
    def test_full_pipeline_hard_filter_returns_none_scores(self):
        result = score_listing(_listing(monthly_rent=20000), _prefs())
        self.assertEqual(result["tier"], "skip")
        self.assertIsNone(result["final_total"])
        self.assertIsNone(result["price_score"])

    def test_full_pipeline_good_listing_scores_all_fields(self):
        listing = _listing(
            station_name="BTS On Nut",
            monthly_rent=10000,
            furnishing="fully",
            agent_or_owner="owner",
            confidence=0.9,
            risk_flags=[],
            room_type="studio",
        )
        result = score_listing(listing, _prefs())
        self.assertIsNone(result["hard_filter"])
        self.assertIsNotNone(result["final_total"])
        self.assertIsNotNone(result["price_score"])
        self.assertIn(result["tier"], ["shortlist", "must_call"])

    def test_full_pipeline_must_call_score(self):
        listing = _listing(
            station_name="BTS On Nut",
            monthly_rent=9000,
            move_in_cost=25000,
            furnishing="fully",
            agent_or_owner="owner",
            confidence=0.95,
            risk_flags=[],
            room_type="studio",
            contract_min_months=3,
            available_date="2026-07-01",
            need_parking=False,
        )
        result = score_listing(listing, _prefs())
        self.assertEqual(result["tier"], "must_call")

    def test_full_pipeline_no_rent_returns_need_info(self):
        result = score_listing(_listing(monthly_rent=None), _prefs())
        self.assertEqual(result["tier"], "need_info")
        self.assertEqual(result["hard_filter"], "rent not stated")


if __name__ == "__main__":
    unittest.main()
