import unittest

from vtx_msp_probe import MspError, msp_v1_request, parse_vtx_config


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


if __name__ == "__main__":
    unittest.main()
