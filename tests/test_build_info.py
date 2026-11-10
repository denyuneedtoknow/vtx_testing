import tempfile
import unittest
from pathlib import Path

from build_info import load_build_number


class BuildInfoTests(unittest.TestCase):
    def test_reads_packaged_build_number(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "BUILD_NUMBER").write_text("0911.1714\n", encoding="ascii")
            self.assertEqual(load_build_number(Path(directory)), "0911.1714")

    def test_invalid_or_missing_build_number_is_dev(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_build_number(Path(directory)), "dev")
            Path(directory, "BUILD_NUMBER").write_text("long-invalid-version", encoding="ascii")
            self.assertEqual(load_build_number(Path(directory)), "dev")
