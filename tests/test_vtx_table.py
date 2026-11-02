import unittest

from vtx_table import _band_payload, _power_payload, load_vtx_table


class VtxTableTests(unittest.TestCase):
    def test_every_operator_vtx_has_a_betaflight_sized_table(self):
        for vtx in ("1.2", "2.7", "3.3", "5.8", "5.8.legacy", "5.8.test", "7.2", "8.5"):
            with self.subTest(vtx=vtx):
                table = load_vtx_table(vtx)
                self.assertLessEqual(len(table["bands"]), 8)
                self.assertEqual({len(channels) for channels in table["bands"].values()}, {8})
                self.assertIn(25, table["power_levels_mw"])

    def test_8_5_table_uses_64_channel_subset(self):
        table = load_vtx_table("8.5")
        self.assertEqual(list(table["bands"]), ["A", "B", "E", "F", "I", "R", "P", "H"])
        self.assertEqual(table["bands"]["A"][0], 7060)
        self.assertEqual(table["bands"]["H"][-1], 8340)

    def test_band_and_power_payloads_use_msp_wire_order(self):
        self.assertEqual(_band_payload(1, "A", [7060, 7080])[:5], bytes((1, 6)) + b"BAND_A"[:3])
        self.assertEqual(_power_payload(2, 25), bytes((2, 25, 0, 2)) + b"25")
