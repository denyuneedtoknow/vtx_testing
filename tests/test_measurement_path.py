import json
import tempfile
import unittest
from pathlib import Path

from measurement_path import load_measurement_path, offset_db_at_frequency, validate_sensor_frequencies


class MeasurementPathTests(unittest.TestCase):
    def test_interpolates_and_clamps_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "path.json"
            path.write_text(
                json.dumps(
                    {
                        "offset_direction": "set_on_sensor_db",
                        "fixed_attenuator_db": 30.0,
                        "sensor_max_frequency_mhz": 2000,
                        "level_offset_db_by_frequency_mhz": {"1000": 1.0, "2000": 0.0},
                    }
                ),
                encoding="utf-8",
            )
            profile = load_measurement_path(path)
        self.assertEqual(offset_db_at_frequency(profile, 500), 31.0)
        self.assertEqual(offset_db_at_frequency(profile, 2500), 30.0)
        self.assertEqual(offset_db_at_frequency(profile, 1500), 30.5)
        self.assertEqual(offset_db_at_frequency(profile, 1500, path_adjustment="subtract"), 29.5)

    def test_rejects_frequency_above_sensor_limit_before_measurement(self):
        profile = {"sensor_max_frequency_mhz": 8000}
        with self.assertRaisesRegex(ValueError, "8475 MHz"):
            validate_sensor_frequencies(profile, [7060, 8475])


if __name__ == "__main__":
    unittest.main()
