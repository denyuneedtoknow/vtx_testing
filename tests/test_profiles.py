import json
import unittest
from pathlib import Path

from vtx_profiles import load_profile
from vtx_three_point_test import load_plan, plan_for_vtx


ROOT = Path(__file__).resolve().parents[1]


class VtxProfileTests(unittest.TestCase):
    def assert_reference_matches_profile(self, profile_name: str, reference_name: str):
        profile = load_profile(ROOT / "configs/vtx_profiles" / profile_name)
        reference = json.loads((ROOT / "configs/reference_measurements" / reference_name).read_text(encoding="utf-8"))
        self.assertEqual(set(reference["bands"]), set(profile["bands"]))
        for band, frequencies in profile["bands"].items():
            self.assertEqual(len(reference["bands"][band]), len(frequencies))

    def test_7_2_reference_values_match_profile_bands_and_channels(self):
        self.assert_reference_matches_profile("ft_vtx_7_2_5w.json", "ft_vtx_7_2_5w_reference.json")
        self.assert_reference_matches_profile("ft_vtx_7_2_5w.json", "ft_vtx_7_2_5w_reference_new.json")

    def test_3_3_reference_values_match_profile_bands_and_channels(self):
        self.assert_reference_matches_profile("ft_vtx_3_3_4w.json", "ft_vtx_3_3_4w_reference.json")

    def test_5_8_reference_values_match_profile_bands_and_channels(self):
        self.assert_reference_matches_profile("ft_vtx_5_8_4w.json", "ft_vtx_5_8_4w_reference.json")

    def test_three_point_plans_match_their_vtx_profiles(self):
        for name in (
            "ft_vtx_3_3_three_point_4w.json",
            "ft_vtx_5_8_three_point_4w.json",
            "ft_vtx_7_2_three_point_5w.json",
        ):
            plan = load_plan(ROOT / "configs/test_plans" / name)
            self.assertEqual(len(plan["points"]), 3)

    def test_each_short_vtx_option_resolves_to_a_plan(self):
        for vtx in ("3.3", "5.8", "7.2"):
            self.assertTrue(plan_for_vtx(vtx).is_file())


if __name__ == "__main__":
    unittest.main()
