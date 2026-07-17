import unittest

from vtx_a1_measure import build_set_vtx_config_payload


class VtxA1MeasureTests(unittest.TestCase):
    def test_preserves_power_and_builds_explicit_a1_payload(self):
        current = {
            "power_index": 2,
            "pit_mode": False,
            "low_power_disarm": 0,
            "pit_mode_frequency_mhz": 0,
        }
        payload = build_set_vtx_config_payload(current, band=1, channel=1, frequency_mhz=3330)
        self.assertEqual(payload, bytes((0, 0, 2, 0, 0, 0, 0, 1, 1, 0x02, 0x0D)))

    def test_rejects_invalid_band_or_channel(self):
        current = {
            "power_index": 2,
            "pit_mode": False,
            "low_power_disarm": 0,
            "pit_mode_frequency_mhz": 0,
        }
        with self.assertRaises(ValueError):
            build_set_vtx_config_payload(current, band=0, channel=1, frequency_mhz=3330)

    def test_uses_requested_power_index_when_provided(self):
        current = {
            "power_index": 2,
            "pit_mode": False,
            "low_power_disarm": 0,
            "pit_mode_frequency_mhz": 0,
        }
        payload = build_set_vtx_config_payload(current, band=2, channel=1, frequency_mhz=3170, power_index=4)
        self.assertEqual(payload[2], 4)


if __name__ == "__main__":
    unittest.main()
