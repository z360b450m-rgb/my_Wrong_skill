from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
CLI = SCRIPT_DIR / "exam_error_cli.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CliSmokeTests(unittest.TestCase):
    def test_generates_one_deterministic_teacher_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "teacher-report.html", root / "teacher-report-second.html"
            for output in (first, second):
                result = subprocess.run(
                    [sys.executable, "-B", str(CLI), str(FIXTURES / "v1-sample.json"), str(output)],
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(result.stdout.strip()), output)
            self.assertTrue(first.is_file())
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
