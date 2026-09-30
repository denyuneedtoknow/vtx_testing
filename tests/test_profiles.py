import json
import unittest
from pathlib import Path

from vtx_profiles import load_profile
from vtx_three_point_test import load_plan, plan_for_vtx
from vtx_power_table_measure import POWER_TABLE_PLAN_FILENAMES, load_power_table_plan, measurement_points, measurement_power_levels


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

    def test_3_3_reference_values_match_profile_bands_and_channels(self):
        self.assert_reference_matches_profile("ft_vtx_3_3_4w.json", "ft_vtx_3_3_4w_reference.json")

    def test_2_7_reference_values_match_profile_bands_and_channels(self):
        self.assert_reference_matches_profile("ft_vtx_2_7_4w.json", "ft_vtx_2_7_4w_reference.json")

    def test_1_2_reference_values_match_profile_bands_and_channels(self):
        self.assert_reference_matches_profile("ft_vtx_1_2_4w.json", "ft_vtx_1_2_4w_reference.json")

    def test_5_8_reference_values_match_profile_bands_and_channels(self):
        self.assert_reference_matches_profile("ft_vtx_5_8_4w.json", "ft_vtx_5_8_4w_reference.json")

    def test_three_point_plans_match_their_vtx_profiles(self):
        for name in (
            "ft_vtx_1_2_three_point_4w.json",
            "ft_vtx_2_7_three_point_4w.json",
            "ft_vtx_3_3_three_point_4w.json",
            "ft_vtx_5_8_three_point_4w.json",
            "ft_vtx_7_2_three_point_5w.json",
            "ft_vtx_8_5_three_point_5w.json",
        ):
            plan = load_plan(ROOT / "configs/test_plans" / name)
            self.assertEqual(len(plan["points"]), 3)

    def test_three_point_references_have_one_source(self):
        for path in sorted((ROOT / "configs/test_plans").glob("*three_point*.json")):
            raw_plan = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("reference_measurements", raw_plan)
            self.assertTrue(all("reference_output_mw" not in point for point in raw_plan["points"]))
            loaded_plan = load_plan(path)
            self.assertTrue(all(point["reference_output_mw"] > 0 for point in loaded_plan["points"]))

    def test_5_8_plan_uses_betaflight_supported_bands(self):
        plan = load_plan(ROOT / "configs/test_plans" / "ft_vtx_5_8_three_point_4w.json")
        self.assertEqual(
            [(point["band"], point["channel"], point["frequency_mhz"]) for point in plan["points"]],
            [("X", 1, 4990), ("E", 8, 5945), ("H", 8, 7210)],
        )
        self.assertEqual(plan["points"][1]["reference_output_mw"], 2150)
        self.assertEqual(set(plan["_profile"]["bands"]), {"X", "D", "L", "E", "A", "B", "F", "H"})
        self.assertEqual(plan["test_power"], {"index": 4, "nominal_mw": 4000})

    def test_5_8_wideband_reference_comes_from_the_4_8_to_7_2_5w_sweep(self):
        reference = json.loads(
            (ROOT / "configs/reference_measurements" / "ft_vtx_5_8_4w_reference.json").read_text(encoding="utf-8")
        )
        self.assertEqual(reference["nominal_power_mw"], 4000)
        self.assertEqual(reference["bands"]["X"][0], 5020)
        self.assertEqual(reference["bands"]["E"][7], 2150)
        self.assertEqual(reference["bands"]["H"][7], 4070)

    def test_new_plans_use_the_supported_1_2_and_2_7_points(self):
        expected_points = {
            "ft_vtx_1_2_three_point_4w.json": [("A", 1, 1080), ("A", 4, 1200), ("A", 7, 1320)],
            "ft_vtx_2_7_three_point_4w.json": [("A", 1, 2700), ("B", 1, 2860), ("B", 8, 3000)],
        }
        for plan_name, expected in expected_points.items():
            plan = load_plan(ROOT / "configs/test_plans" / plan_name)
            actual = [(point["band"], point["channel"], point["frequency_mhz"]) for point in plan["points"]]
            self.assertEqual(actual, expected)

    def test_profiles_have_valid_band_counts(self):
        for profile_path in (ROOT / "configs/vtx_profiles").glob("*.json"):
            with self.subTest(profile=profile_path.name):
                self.assertLessEqual(len(load_profile(profile_path)["bands"]), 255)

    def test_each_short_vtx_option_resolves_to_a_plan(self):
        for vtx in ("1.2", "2.7", "3.3", "5.8", "7.2", "8.5"):
            self.assertTrue(plan_for_vtx(vtx).is_file())

    def test_new_full_table_plans_match_their_betaflight_tables(self):
        expected = {"low": (8, 64), "mid": (7, 56), "high": (7, 56)}
        for vtx, (bands, channels) in expected.items():
            with self.subTest(vtx=vtx):
                plan = load_power_table_plan(ROOT / "configs/test_plans" / POWER_TABLE_PLAN_FILENAMES[vtx])
                profile = plan["_profile"]
                self.assertEqual(len(profile["bands"]), bands)
                self.assertEqual(len(measurement_points(profile)), channels)
                self.assertEqual(measurement_power_levels(profile), [(2, 25), (3, 2000), (4, 4000)])


if __name__ == "__main__":
    unittest.main()
