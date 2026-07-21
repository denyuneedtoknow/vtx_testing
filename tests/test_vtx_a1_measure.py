import unittest
from io import StringIO
import csv

from vtx_a1_measure import build_set_vtx_config_payload
from vtx_three_point_test import (
    ANSI_GREEN,
    ANSI_RED,
    ANSI_YELLOW,
    format_power,
    build_vtx_table_band_payload,
    build_vtx_table_power_payload,
    measure_and_report_point,
    power_color,
    select_manual_point,
    summarize_readings,
)
from unittest.mock import patch


class VtxA1MeasureTests(unittest.TestCase):
    def test_preserves_power_and_builds_explicit_a1_payload(self):
        current = {
            "power_index": 2,
            "pit_mode": False,
            "low_power_disarm": 0,
            "pit_mode_frequency_mhz": 0,
        }
        payload = build_set_vtx_config_payload(current, band=1, channel=1, frequency_mhz=3330)
        self.assertEqual(payload, bytes((0, 0, 2, 0, 0, 0, 0, 1, 1, 0x02, 0x0D, 0, 0, 0, 0)))
        self.assertEqual(len(payload), 15)

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
        self.assertEqual(len(payload), 15)

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
        self.assertEqual(len(payload), 15)

    def test_summarizes_power_drop(self):
        summary = summarize_readings([(0.0, 7000.0), (0.5, 6900.0), (1.0, 6800.0)])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["average_mw"], 6900.0)
        self.assertEqual(summary["minimum_mw"], 6800.0)
        self.assertEqual(summary["change_mw"], -200.0)

    def test_colors_power_against_reference_thresholds(self):
        thresholds = {"green_min": 90.0, "yellow_min": 80.0}
        self.assertEqual(power_color(900, 1000, thresholds), ANSI_GREEN)
        self.assertEqual(power_color(850, 1000, thresholds), ANSI_YELLOW)
        self.assertEqual(power_color(799, 1000, thresholds), ANSI_RED)

    def test_formats_plain_power_when_color_is_disabled(self):
        self.assertEqual(
            format_power(900, 1000, {"green_min": 90.0, "yellow_min": 80.0}, use_color=False),
            "900 mW",
        )

    def test_manual_mode_selects_a_configured_point_or_finishes(self):
        points = [
            {"band": "X", "channel": 1, "frequency_mhz": 4990},
            {"band": "D", "channel": 5, "frequency_mhz": 5510},
        ]
        with patch("builtins.input", return_value="2"):
            self.assertEqual(select_manual_point(points), points[1])
        with patch("builtins.input", return_value="0"):
            self.assertIsNone(select_manual_point(points))

    def test_manual_measurement_reports_and_logs_selected_point(self):
        class Sensor:
            def __init__(self):
                self.frequency_hz = None

            def configure(self, frequency_hz, averages):
                self.frequency_hz = frequency_hz

        sensor = Sensor()
        output = StringIO()
        writer = csv.writer(output)
        point = {"band": "D", "channel": 5, "frequency_mhz": 5510, "reference_output_mw": 4550}
        with patch("vtx_three_point_test.observe_power", return_value=[(0.0, 4500.0), (0.5, 4600.0)]):
            measure_and_report_point(
                sensor, point, power_index=4, timeout_ms=5000, observation_time_s=5, sample_interval_s=0.5,
                averages=16, csv_writer=writer, thresholds_percent={"green_min": 90.0, "yellow_min": 80.0},
                use_color=False,
            )
        self.assertEqual(sensor.frequency_hz, 5_510_000_000)
        self.assertIn("5510", output.getvalue())

    def test_builds_betaflight_vtx_table_payloads(self):
        self.assertEqual(build_vtx_table_power_payload(4, 4000), b"\x04\xa0\x0f\x044000")
        self.assertEqual(
            build_vtx_table_band_payload(7, "X", [4990, 5020]),
            b"\x07\x01X\x58\x00\x02\x7e\x13\x9c\x13",
        )


if __name__ == "__main__":
    unittest.main()
