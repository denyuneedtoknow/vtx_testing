import csv
import tempfile
import unittest
from pathlib import Path

from vtx_power_table_measure import (
    load_direct_frequency_sweep_plan, load_power_table_plan, measurement_points, measurement_power_levels, observe_power_trend,
    selected_power_levels, write_matrix,
)


class VtxPowerTableMeasureTests(unittest.TestCase):
    def test_skips_unused_channel_slots_and_off_power(self):
        profile = {
            "bands": {"A": [1080, 1120], "B": [1258, 0]},
            "power_levels_mw": [0, 25, 2000, 4000],
        }
        points = measurement_points(profile)
        self.assertEqual(
            [(point["band"], point["channel"], point["frequency_mhz"]) for point in points],
            [("A", 1, 1080), ("A", 2, 1120), ("B", 1, 1258)],
        )
        self.assertEqual(measurement_power_levels(profile), [(2, 25), (3, 2000), (4, 4000)])

    def test_writes_power_by_channel_matrix_with_blank_incomplete_cells(self):
        points = [
            {"band": "A", "channel": 1, "frequency_mhz": 1080},
            {"band": "B", "channel": 1, "frequency_mhz": 1258},
        ]
        powers = [(2, 25), (3, 2000)]
        values = {(2, "A1"): 24.5, (3, "B1"): 1990.25}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.csv"
            write_matrix(path, points, powers, values)
            with path.open(newline="", encoding="utf-8-sig") as file:
                rows = list(csv.reader(file, delimiter=";"))
        self.assertEqual(rows[0], ["power_mw", "A1 (1080 MHz)", "B1 (1258 MHz)"])
        self.assertEqual(rows[1], ["25", "24,50", ""])
        self.assertEqual(rows[2], ["2000", "", "1990,25"])

    def test_selects_only_requested_power_level(self):
        profile = {"bands": {"A": [1080]}, "power_levels_mw": [0, 25, 2000, 4000]}
        self.assertEqual(selected_power_levels(profile, "2000"), [(3, 2000)])
        self.assertEqual(selected_power_levels(profile, "all"), [(2, 25), (3, 2000), (4, 4000)])

    def test_observes_rising_and_falling_output_without_failing(self):
        class Sensor:
            def __init__(self, values):
                self.values = iter(values)

            def read_watts(self, timeout_ms):
                return next(self.values)

        rising = observe_power_trend(Sensor((1.000, 1.010, 1.020)), timeout_ms=1, sample_interval_s=0, samples=3, threshold_percent=1.0)
        falling = observe_power_trend(Sensor((1.020, 1.010, 1.000)), timeout_ms=1, sample_interval_s=0, samples=3, threshold_percent=1.0)
        self.assertEqual(rising["trend"], "rising")
        self.assertEqual(falling["trend"], "falling")

    def test_direct_sweep_plans_include_all_documented_channels(self):
        plans = {
            "ft_vtx_1_2_direct_sweep_4w.json": (2, 9),
            "ft_vtx_2_7_direct_sweep_4w.json": (2, 16),
            "vtx_4_9_7_2_direct_sweep_4w.json": (22, 176),
            "ft_vtx_8_5_direct_sweep_5w.json": (10, 80),
        }
        plan_directory = Path(__file__).resolve().parents[1] / "configs/test_plans"
        for filename, (bands, channels) in plans.items():
            with self.subTest(plan=filename):
                plan = load_direct_frequency_sweep_plan(plan_directory / filename)
                self.assertEqual(len(plan["_profile"]["bands"]), bands)
                self.assertEqual(len(measurement_points(plan["_profile"])), channels)
                expected_powers = [(2, 25), (3, 2500), (4, 5000)] if filename == "ft_vtx_8_5_direct_sweep_5w.json" else [(2, 25), (3, 2000), (4, 4000)]
                self.assertEqual(measurement_power_levels(plan["_profile"]), expected_powers)

    def test_graph58_p5_full_plan_uses_its_own_bands_and_power_levels(self):
        plan_directory = Path(__file__).resolve().parents[1] / "configs/test_plans"
        plan = load_power_table_plan(plan_directory / "ft_vtx_graph58_p5_full_table_5w.json")
        profile = plan["_profile"]
        self.assertEqual(list(profile["bands"]), ["A", "B", "E", "F", "R", "L", "U", "X"])
        self.assertEqual(profile["bands"]["U"], [5325, 5348, 5366, 5384, 5402, 5420, 5438, 5456])
        self.assertEqual(len(measurement_points(profile)), 64)
        self.assertEqual(measurement_power_levels(profile), [(2, 25), (3, 500), (4, 3000), (5, 5000)])


if __name__ == "__main__":
    unittest.main()
