import unittest
from io import StringIO
import csv

from vtx_a1_measure import build_set_vtx_config_payload
from vtx_three_point_test import (
    ANSI_GREEN,
    ANSI_RED,
    ANSI_YELLOW,
    format_power,
    measure_and_report_point,
    power_color,
    reference_chart_line,
    select_manual_point,
    summarize_readings,
    verify_vtx_table,
)
from vtx_msp_probe import MSP_VTXTABLE_BAND, MSP_VTXTABLE_POWERLEVEL, MspError
from unittest.mock import patch


class VtxMeasurementTests(unittest.TestCase):
    def test_preserves_power_and_builds_explicit_a1_payload(self):
        current = {
            "power_index": 2,
            "pit_mode": False,
            "low_power_disarm": 0,
            "pit_mode_frequency_mhz": 0,
        }
        payload = build_set_vtx_config_payload(current, band=1, channel=1, frequency_mhz=3330)
        self.assertEqual(payload, bytes((0, 0, 2, 0, 0, 0, 0, 1, 1, 0x02, 0x0D)))
        self.assertEqual(len(payload), 11)

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
        self.assertEqual(len(payload), 11)

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
        self.assertEqual(len(payload), 11)

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

    def test_reference_chart_marks_target_and_reports_status(self):
        point = {"band": "A", "channel": 1, "frequency_mhz": 1080, "reference_output_mw": 1000}
        line = reference_chart_line(point, 900, {"green_min": 90.0, "yellow_min": 80.0}, use_color=False)
        self.assertIn("│", line)
        self.assertIn("900 / 1000 mW", line)
        self.assertIn("(90%) OK", line)

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
            summary = measure_and_report_point(
                sensor, point, power_index=4, timeout_ms=5000, observation_time_s=5, sample_interval_s=0.5,
                averages=16, csv_writer=writer, thresholds_percent={"green_min": 90.0, "yellow_min": 80.0},
                use_color=False,
            )
        self.assertEqual(sensor.frequency_hz, 5_510_000_000)
        self.assertIn("5510", output.getvalue())
        self.assertEqual(summary["average_mw"], 4550.0)

    def test_verifies_complete_vtx_table_without_writing(self):
        profile = {
            "bands": {"A": [1000, 1100], "B": [1200, 1300]},
            "power_levels_mw": [0, 25, 2000, 4000],
        }

        class ReadOnlyClient:
            def __init__(self):
                self.commands = []

            def request(self, command, payload=b""):
                self.commands.append(command)
                index = payload[0]
                if command == MSP_VTXTABLE_BAND:
                    letter, frequencies = list(profile["bands"].items())[index - 1]
                    name = letter.encode("ascii")
                    return (
                        bytes((index, len(name))) + name + bytes((0, 0, len(frequencies)))
                        + b"".join(frequency.to_bytes(2, "little") for frequency in frequencies)
                    )
                if command == MSP_VTXTABLE_POWERLEVEL:
                    value = profile["power_levels_mw"][index - 1]
                    label = ("PIT" if value == 0 else str(value)).encode("ascii").ljust(8, b"\0")
                    return bytes((index,)) + value.to_bytes(2, "little") + bytes((8,)) + label
                raise AssertionError(f"Unexpected write command {command}")

        client = ReadOnlyClient()
        current = {"vtx_table_bands": 2, "vtx_table_channels": 2, "vtx_table_power_levels": 4}
        verify_vtx_table(client, current, profile)
        self.assertEqual(client.commands, [137, 137, 138, 138, 138, 138])

    def test_accepts_firmware_specific_off_power_value_when_label_is_zero(self):
        profile = {"bands": {"A": [1000]}, "power_levels_mw": [0, 25, 2000, 4000]}

        class ReadOnlyClient:
            def request(self, command, payload=b""):
                index = payload[0]
                if command == MSP_VTXTABLE_BAND:
                    return bytes((index, 1)) + b"A" + bytes((0, 0, 1)) + (1000).to_bytes(2, "little")
                if command == MSP_VTXTABLE_POWERLEVEL:
                    value, label = ((9, b"0"), (25, b"25"), (2000, b"2W"), (4000, b"4W"))[index - 1]
                    return bytes((index,)) + value.to_bytes(2, "little") + bytes((len(label),)) + label
                raise AssertionError(f"Unexpected command {command}")

        verify_vtx_table(
            ReadOnlyClient(),
            {"vtx_table_bands": 1, "vtx_table_channels": 1, "vtx_table_power_levels": 4},
            profile,
        )

    def test_accepts_different_band_letter_when_frequencies_match(self):
        profile = {"bands": {"A": [1000]}, "power_levels_mw": [0]}

        class ReadOnlyClient:
            def request(self, command, payload=b""):
                if command == MSP_VTXTABLE_BAND:
                    return bytes((1, 8)) + b"BOSCAM_A" + bytes((ord("B"), 1, 1)) + (1000).to_bytes(2, "little")
                if command == MSP_VTXTABLE_POWERLEVEL:
                    return bytes((1,)) + (9).to_bytes(2, "little") + bytes((1,)) + b"0"
                raise AssertionError(f"Unexpected command {command}")

        verify_vtx_table(
            ReadOnlyClient(),
            {"vtx_table_bands": 1, "vtx_table_channels": 1, "vtx_table_power_levels": 1},
            profile,
        )

    def test_rejects_incorrect_power_values_even_when_labels_claim_watts(self):
        profile = {"bands": {"A": [1000]}, "power_levels_mw": [0, 25, 2000, 4000]}

        class ReadOnlyClient:
            def request(self, command, payload=b""):
                index = payload[0]
                if command == MSP_VTXTABLE_BAND:
                    return bytes((index, 1)) + b"A" + bytes((0, 0, 1)) + (1000).to_bytes(2, "little")
                if command == MSP_VTXTABLE_POWERLEVEL:
                    value, label = ((9, b"0"), (25, b"25"), (200, b"2W"), (400, b"4W"))[index - 1]
                    return bytes((index,)) + value.to_bytes(2, "little") + bytes((len(label),)) + label
                raise AssertionError(f"Unexpected command {command}")

        with self.assertRaisesRegex(MspError, r"expected \[0, 25, 2000, 4000\]; got \[9, 25, 200, 400\]"):
            verify_vtx_table(
                ReadOnlyClient(),
                {"vtx_table_bands": 1, "vtx_table_channels": 1, "vtx_table_power_levels": 4},
                profile,
            )

    def test_rejects_wrong_vtx_table_without_attempting_a_write(self):
        profile = {"bands": {"A": [1000, 1100]}, "power_levels_mw": [0, 25, 2000, 4000]}

        class NoWriteClient:
            def request(self, command, payload=b""):
                raise AssertionError(f"No MSP command expected, got {command}")

        current = {"vtx_table_bands": 0, "vtx_table_channels": 0, "vtx_table_power_levels": 0}
        with self.assertRaisesRegex(MspError, "FC settings were not changed"):
            verify_vtx_table(NoWriteClient(), current, profile)


if __name__ == "__main__":
    unittest.main()
