from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from check_runtime import collect_runtime_capabilities  # noqa: E402


class RuntimeCapabilityTests(unittest.TestCase):
    def test_capability_report_contains_modes_and_commands(self):
        report = collect_runtime_capabilities(SKILL_DIR)
        self.assertEqual(report["service"], "analyze-exam-errors")
        self.assertIn(report["mode"], {"core", "blocked"})
        self.assertIn("capabilities", report)
        self.assertTrue(report["capabilities"]["safe_pipeline"])
        self.assertTrue(report["capabilities"]["teacher_report"])
        self.assertIn("pipeline", report["commands"])
        self.assertIn("teacher_report", report["commands"])
        self.assertTrue(report["capabilities"]["audit_recompute"])
        self.assertTrue(report["capabilities"]["disk_backed_spill"])
        self.assertEqual(report["constraints"]["intermediate_memory_threshold_mb"], 256)
        self.assertIsInstance(report["degraded_components"], list)
