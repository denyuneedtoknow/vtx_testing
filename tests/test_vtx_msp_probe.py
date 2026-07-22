import unittest
from unittest.mock import patch

from vtx_msp_probe import (
    FlightControllerInfo,
    MspError,
    find_flight_controller_port,
    msp_v1_request,
    parse_vtx_band,
    parse_vtx_power_level,
    parse_vtx_config,
    resolve_port,
)


class MspProbeTests(unittest.TestCase):
    def test_encodes_empty_request(self):
        self.assertEqual(msp_v1_request(88), b"$M<\x00XX")

    def test_parses_vtx_core_fields(self):
        payload = bytes((3, 2, 5, 1, 0, 0xDC, 0x0A, 1, 0))
        vtx = parse_vtx_config(payload)
        self.assertEqual(vtx["device_type"], 3)
        self.assertEqual(vtx["band"], 2)
        self.assertEqual(vtx["channel"], 5)
        self.assertEqual(vtx["frequency_mhz"], 2780)
        self.assertTrue(vtx["ready"])

    def test_rejects_short_vtx_response(self):
        with self.assertRaises(MspError):
            parse_vtx_config(b"\x00" * 8)

    def test_parses_vtx_power_table_level(self):
        self.assertEqual(
            parse_vtx_power_level(bytes((4, 36, 0, 5)) + b"4000\0"),
            {"index": 4, "value": 36, "label": "4000"},
        )

    def test_parses_vtx_band_with_all_frequencies(self):
        payload = (
            bytes((2, 8)) + b"BAND_B\0\0" + bytes((ord("B"), 0, 2))
            + (2860).to_bytes(2, "little") + (2880).to_bytes(2, "little")
        )
        self.assertEqual(
            parse_vtx_band(payload),
            {
                "index": 2,
                "name": "BAND_B",
                "letter": "B",
                "is_factory": False,
                "frequencies_mhz": [2860, 2880],
            },
        )

    def test_resolve_port_uses_explicit_value(self):
        self.assertEqual(resolve_port("COM83", 115200, 2.0), "COM83")

    def test_resolve_port_auto_detects_single_controller(self):
        detected = FlightControllerInfo(
            port="COM83",
            variant="BTFL",
            firmware_version="4.5.0",
            msp_api_version="1.46",
        )
        with patch("vtx_msp_probe.find_flight_controller_port", return_value=detected) as finder:
            self.assertEqual(resolve_port(None, 115200, 2.0), "COM83")
        finder.assert_called_once_with(115200, 1.0)

    def test_find_port_raises_when_no_serial_ports(self):
        with patch("vtx_msp_probe.list_serial_ports", return_value=[]):
            with self.assertRaisesRegex(MspError, "No serial ports found"):
                find_flight_controller_port()

    def test_find_port_raises_when_multiple_controllers(self):
        info = FlightControllerInfo("COM83", "BTFL", "4.5.0", "1.46")
        with patch("vtx_msp_probe.list_serial_ports", return_value=["COM83", "COM84"]):
            with patch("vtx_msp_probe.probe_flight_controller", return_value=info):
                with self.assertRaisesRegex(MspError, "Multiple Flight Controllers found"):
                    find_flight_controller_port()


if __name__ == "__main__":
    unittest.main()
