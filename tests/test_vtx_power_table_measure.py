import csv
import tempfile
import unittest
from pathlib import Path

from vtx_power_table_measure import load_direct_frequency_sweep_plan, measurement_points, measurement_power_levels, write_matrix


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

    def test_direct_sweep_plans_include_all_documented_channels(self):
        plans = {
            "ft_vtx_1_2_direct_sweep_4w.json": (2, 9),
            "ft_vtx_2_7_direct_sweep_4w.json": (2, 16),
            "vtx_4_9_7_2_direct_sweep_4w.json": (22, 176),
        }
        plan_directory = Path(__file__).resolve().parents[1] / "configs/test_plans"
        for filename, (bands, channels) in plans.items():
            with self.subTest(plan=filename):
                plan = load_direct_frequency_sweep_plan(plan_directory / filename)
                self.assertEqual(len(plan["_profile"]["bands"]), bands)
                self.assertEqual(len(measurement_points(plan["_profile"])), channels)
                self.assertEqual(measurement_power_levels(plan["_profile"]), [(2, 25), (3, 2000), (4, 4000)])


if __name__ == "__main__":
    unittest.main()
