import json
import tempfile
import unittest
from pathlib import Path

from measurement_path import load_measurement_path, offset_db_at_frequency


class MeasurementPathTests(unittest.TestCase):
    def test_interpolates_and_clamps_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "path.json"
            path.write_text(
                json.dumps(
                    {
                        "offset_direction": "set_on_sensor_db",
                        "fixed_attenuator_db": 30.0,
                        "level_offset_db_by_frequency_mhz": {"1000": 1.0, "2000": 0.0},
                    }
                ),
                encoding="utf-8",
            )
            profile = load_measurement_path(path)
        self.assertEqual(offset_db_at_frequency(profile, 500), 31.0)
        self.assertEqual(offset_db_at_frequency(profile, 2500), 30.0)
        self.assertEqual(offset_db_at_frequency(profile, 1500), 30.5)


if __name__ == "__main__":
    unittest.main()
