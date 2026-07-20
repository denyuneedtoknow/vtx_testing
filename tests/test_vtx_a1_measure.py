import unittest

from vtx_a1_measure import build_set_vtx_config_payload
from vtx_three_point_test import summarize_readings


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

    def test_encodes_explicit_band_above_legacy_eight_band_limit(self):
        current = {
            "power_index": 2,
            "pit_mode": False,
            "low_power_disarm": 0,
            "pit_mode_frequency_mhz": 0,
        }
        payload = build_set_vtx_config_payload(current, band=9, channel=1, frequency_mhz=4867, power_index=4)
        self.assertEqual(payload[:2], b"\0\0")
        self.assertEqual(payload[7:9], bytes((9, 1)))

    def test_encodes_direct_frequency_for_extra_band(self):
        current = {
            "power_index": 2,
            "pit_mode": False,
            "low_power_disarm": 0,
            "pit_mode_frequency_mhz": 0,
        }
        payload = build_set_vtx_config_payload(
            current, band=9, channel=1, frequency_mhz=4867, power_index=4, direct_frequency=True
        )
        self.assertEqual(payload[:2], (4867).to_bytes(2, "little"))
        self.assertEqual(payload[7:9], b"\0\0")

    def test_summarizes_power_drop(self):
        summary = summarize_readings([(0.0, 7000.0), (0.5, 6900.0), (1.0, 6800.0)])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["average_mw"], 6900.0)
        self.assertEqual(summary["minimum_mw"], 6800.0)
        self.assertEqual(summary["change_mw"], -200.0)


if __name__ == "__main__":
    unittest.main()
