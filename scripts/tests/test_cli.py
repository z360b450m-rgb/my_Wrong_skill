from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
CLI = SCRIPT_DIR / "exam_error_cli.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CliSmokeTests(unittest.TestCase):
    def run_cli(self, *args: str, expect: int = 0):
        result = subprocess.run(
            [sys.executable, "-B", str(CLI), *map(str, args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if result.returncode != expect:
            self.fail(
                f"CLI returned {result.returncode}, expected {expect}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def test_end_to_end_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrated = root / "migrated.json"
            analyzed = root / "analyzed.json"
            stats = root / "statistics.json"
            graph = root / "graph.json"
            database = root / "school.sqlite3"
            reports = root / "reports"

            self.run_cli("migrate", FIXTURES / "v1-sample.json", migrated)
            validation = self.run_cli("validate", migrated)
            self.assertTrue(json.loads(validation.stdout)["valid"])
            self.run_cli("analyze", migrated, analyzed)
            pipelined = root / "pipelined.json"
            pipeline_stats = root / "pipeline-statistics.json"
            pipeline_graph = root / "pipeline-graph.json"
            self.run_cli(
                "pipeline",
                FIXTURES / "v1-sample.json",
                pipelined,
                "--statistics",
                pipeline_stats,
                "--graph",
                pipeline_graph,
            )
            self.assertTrue(pipelined.is_file())
            self.assertTrue(pipeline_stats.is_file())
            self.assertTrue(pipeline_graph.is_file())
            teacher_report = root / "teacher-report.html"
            teacher_result = self.run_cli(
                "teacher-report",
                FIXTURES / "v1-sample.json",
                teacher_report,
            )
            self.assertTrue(teacher_report.is_file())
            teacher_manifest = json.loads(teacher_result.stdout)
            self.assertEqual(
                teacher_manifest["view_schema_version"], "teacher-report-view-v6"
            )
            self.assertEqual(
                teacher_manifest["renderer_version"], "teacher-report-renderer-v14"
            )
            self.assertEqual(len(teacher_manifest["output_sha256"]), 64)
            second_teacher_report = root / "teacher-report-second.html"
            second_teacher_result = self.run_cli(
                "teacher-report",
                FIXTURES / "v1-sample.json",
                second_teacher_report,
            )
            second_teacher_manifest = json.loads(second_teacher_result.stdout)
            self.assertEqual(
                teacher_report.read_bytes(), second_teacher_report.read_bytes()
            )
            self.assertEqual(
                teacher_manifest["output_sha256"],
                second_teacher_manifest["output_sha256"],
            )
            self.run_cli("statistics", analyzed, stats)
            self.run_cli("graph", analyzed, graph)
            report = self.run_cli(
                "report",
                analyzed,
                reports,
                "--index-version",
                "idx-test",
            )
            self.assertIn("star_map_html", json.loads(report.stdout))
            star_map = json.loads(
                (reports / "star-map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(star_map["index_version"], "idx-test")
            indexed = self.run_cli("index", "rebuild", analyzed, database)
            self.assertEqual(json.loads(indexed.stdout)["records_indexed"], 3)
            searched = self.run_cli("search", database, "计算错误", "--top-k", "5")
            search_data = json.loads(searched.stdout)
            self.assertEqual(search_data["results"][0]["question_id"], "q2")
            self.run_cli("audit", "verify", analyzed)
            self.run_cli("audit", "verify", database)
            recomputed = root / "recomputed.json"
            self.run_cli(
                "audit",
                "recompute",
                analyzed,
                "--actor-ref",
                "audit-admin",
                "--confirm-new-baseline",
                "--output",
                recomputed,
            )
            self.run_cli("audit", "verify", recomputed)
            review_export = root / "review.json"
            self.run_cli("review", "export", analyzed, review_export)
            review_payload = json.loads(review_export.read_text(encoding="utf-8"))
            review_item = review_payload["items"][0]
            decision_payload = {
                key: review_payload[key]
                for key in (
                    "organization_id",
                    "analysis_id",
                    "document_state_hash",
                )
            }
            decision_payload["decisions"] = [
                {
                    "attempt_id": review_item["attempt_id"],
                    "question_id": review_item["question_id"],
                    "decision": "confirm",
                    "reason": "教师确认",
                }
            ]
            decisions = root / "decisions.json"
            decisions.write_text(
                json.dumps(decision_payload, ensure_ascii=False), encoding="utf-8"
            )
            reviewed = root / "reviewed.json"
            self.run_cli(
                "review",
                "apply",
                analyzed,
                reviewed,
                "--decisions",
                decisions,
                "--actor-ref",
                "teacher-01",
            )
            reviewed_data = json.loads(reviewed.read_text(encoding="utf-8"))
            self.assertEqual(reviewed_data["audit_log"][-1]["event_type"], "review.applied")

            mismatched = dict(decision_payload)
            mismatched["analysis_id"] = "another-analysis"
            decisions.write_text(
                json.dumps(mismatched, ensure_ascii=False), encoding="utf-8"
            )
            rejected = self.run_cli(
                "review",
                "apply",
                analyzed,
                root / "must-not-exist.json",
                "--decisions",
                decisions,
                "--actor-ref",
                "teacher-01",
                expect=2,
            )
            self.assertIn("analysis_id", rejected.stderr)
            benchmark = self.run_cli(
                "benchmark",
                "--records",
                "100",
                "--queries",
                "2",
            )
            self.assertEqual(json.loads(benchmark.stdout)["records"], 100)
            capabilities = self.run_cli("capabilities")
            capability_data = json.loads(capabilities.stdout)
            self.assertEqual(capability_data["service"], "analyze-exam-errors")
        self.assertNotIn("install_recommended", capability_data["commands"])


if __name__ == "__main__":
    unittest.main()
