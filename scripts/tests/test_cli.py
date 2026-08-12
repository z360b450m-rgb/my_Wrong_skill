from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
CLI = SCRIPT_DIR / "exam_error_cli.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exam_error_core import recompute_audit_chain  # noqa: E402


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

    def test_candidate_enters_pending_registry_and_report(self):
        source = json.loads((FIXTURES / "v2-class-sample.json").read_text(encoding="utf-8"))
        source["attempts"][0]["responses"][0]["suggested_tags"] = [
            {
                "dimension": "knowledge",
                "name": "equation_domain_check",
                "display_name": "方程定义域检验",
                "definition": "解分式方程后检验分母不为零。",
                "confidence": 0.82,
            }
        ]
        source = recompute_audit_chain(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_file = root / "input.json"
            report = root / "report.html"
            taxonomy = root / "extensions.json"
            input_file.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", str(CLI), str(input_file), str(report), "--taxonomy", str(taxonomy)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("方程定义域检验", report.read_text(encoding="utf-8"))
            registry = json.loads(taxonomy.read_text(encoding="utf-8"))
            self.assertEqual(registry["items"][0]["status"], "pending")
            decision_file = root / "decisions.json"
            decision_file.write_text(
                json.dumps(
                    {"decisions": [{"id": registry["items"][0]["id"], "action": "approve"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            applied = subprocess.run(
                [sys.executable, "-B", str(CLI), "taxonomy", "apply", str(decision_file), "--taxonomy", str(taxonomy)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(json.loads(taxonomy.read_text(encoding="utf-8"))["items"][0]["status"], "approved")
            source["attempts"][0]["responses"][0]["suggested_tags"] = []
            source["paper"]["questions"][0]["tags"].append(
                {"dimension": "knowledge", "name": "equation_domain_check", "confidence": 0.9}
            )
            reused_input = recompute_audit_chain(source)
            input_file.write_text(json.dumps(reused_input, ensure_ascii=False), encoding="utf-8")
            reused_report = root / "reused-report.html"
            reused = subprocess.run(
                [sys.executable, "-B", str(CLI), str(input_file), str(reused_report), "--taxonomy", str(taxonomy)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(reused.returncode, 0, reused.stderr)
            self.assertIn("方程定义域检验", reused_report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
