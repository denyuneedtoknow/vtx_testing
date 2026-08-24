import signal
import unittest
from unittest.mock import patch

from vtx_tester_gui import VtxTesterGui


class VtxTesterGuiTests(unittest.TestCase):
    def test_soft_interrupt_sends_sigint_to_running_process(self):
        class Process:
            sent_signal = None

            @staticmethod
            def poll():
                return None

            def send_signal(self, value):
                self.sent_signal = value

        process = Process()
        with patch("vtx_tester_gui.sys.platform", "linux"):
            VtxTesterGui._interrupt_process(process)
        self.assertEqual(process.sent_signal, signal.SIGINT)

    def test_soft_interrupt_ignores_finished_process(self):
        class Process:
            sent_signal = None

            @staticmethod
            def poll():
                return 0

            def send_signal(self, value):
                self.sent_signal = value

        process = Process()
        VtxTesterGui._interrupt_process(process)
        self.assertIsNone(process.sent_signal)


if __name__ == "__main__":
    unittest.main()
