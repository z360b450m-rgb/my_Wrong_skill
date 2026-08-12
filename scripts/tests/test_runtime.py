from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from check_runtime import collect_runtime_capabilities  # noqa: E402
from install_runtime import build_install_plan  # noqa: E402
from symbolic_adapter import safe_symbolic_equivalent  # noqa: E402


class RuntimeCapabilityTests(unittest.TestCase):
    def test_capability_report_contains_modes_and_commands(self):
        report = collect_runtime_capabilities(SKILL_DIR)
        self.assertEqual(report["service"], "analyze-exam-errors")
        self.assertIn(report["mode"], {"core", "math", "semantic", "full", "blocked"})
        self.assertIn("capabilities", report)
        self.assertTrue(report["capabilities"]["safe_pipeline"])
        self.assertTrue(report["capabilities"]["teacher_report"])
        self.assertIn("pipeline", report["commands"])
        self.assertIn("teacher_report", report["commands"])
        self.assertIn("install_recommended", report["commands"])
        self.assertTrue(report["capabilities"]["audit_recompute"])
        self.assertTrue(report["capabilities"]["disk_backed_spill"])
        self.assertEqual(report["constraints"]["intermediate_memory_threshold_mb"], 256)
        self.assertIsInstance(report["degraded_components"], list)

    def test_full_install_plan_includes_recommended_packages(self):
        plan = build_install_plan(profile="full")
        self.assertIn("sympy>=1.13,<2", plan["packages"])
        self.assertIn("usearch>=2.13,<3", plan["packages"])
        self.assertIn("sentence-transformers>=3,<4", plan["packages"])
        self.assertNotIn("--upgrade", plan["command"])

    def test_module_dry_run_prints_json(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "scripts.install_runtime",
                "--profile",
                "semantic-onnx",
                "--dry-run",
                "--json",
            ],
            cwd=SKILL_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["profile"], "semantic-onnx")
        self.assertIn("onnxruntime>=1.19,<2", payload["packages"])
        self.assertIn("transformers>=4.44,<5", payload["packages"])

    def test_symbolic_adapter_fails_closed_on_timeout(self):
        with patch(
            "symbolic_adapter.subprocess.run",
            side_effect=subprocess.TimeoutExpired([sys.executable], 2),
        ):
            equivalent, confidence, reason = safe_symbolic_equivalent("x+1", "x+1")
        self.assertIsNone(equivalent)
        self.assertEqual(confidence, 0.0)
        self.assertEqual(reason, "symbolic_timeout")
