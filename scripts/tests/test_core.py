from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exam_error_core import (  # noqa: E402
    analyze_document,
    apply_review_decisions,
    build_graph,
    compute_statistics,
    export_review_queue,
    migrate_v1,
    recompute_audit_chain,
    validate_v2,
    verify_audit_chain,
)
from reporting import write_report_bundle  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures"
ASSETS = SCRIPT_DIR.parent / "assets"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SchemaMigrationTests(unittest.TestCase):
    def test_v2_fixture_is_valid(self):
        self.assertEqual(validate_v2(load_fixture("v2-class-sample.json")), [])

    def test_v1_migration_is_valid_idempotent_and_audited(self):
        migrated = migrate_v1(load_fixture("v1-sample.json"))
        self.assertEqual(validate_v2(migrated), [])
        self.assertEqual(migrate_v1(migrated), migrated)
        self.assertEqual(verify_audit_chain(migrated), [])
        self.assertEqual(migrated["provenance"]["input_hash"][:7], "sha256:")
        self.assertIsNone(migrated["paper"]["questions"][2]["reference_answer"])

    def test_validator_rejects_score_and_correctness_conflict(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["responses"][0]["is_correct"] = False
        errors = validate_v2(data)
        self.assertTrue(any("is_correct" in error for error in errors))

    def test_validator_rejects_malformed_objects_without_crashing(self):
        data = load_fixture("v2-class-sample.json")
        data["paper"]["questions"][0] = None
        errors = validate_v2(data)
        self.assertTrue(any("must be an object" in error for error in errors))

    def test_validator_rejects_unknown_tag_dimension(self):
        data = load_fixture("v2-class-sample.json")
        data["paper"]["questions"][0]["tags"][0]["dimension"] = "other"
        errors = validate_v2(data)
        self.assertTrue(any("dimension" in error for error in errors))

    def test_validator_rejects_unknown_nested_fields(self):
        data = load_fixture("v2-class-sample.json")
        data["paper"]["questions"][0]["student_name"] = "不应出现"
        errors = validate_v2(data)
        self.assertTrue(any("unsupported field student_name" in error for error in errors))

    def test_validator_rejects_empty_audit_chain(self):
        data = load_fixture("v2-class-sample.json")
        data["audit_log"] = []
        errors = validate_v2(data)
        self.assertIn("audit_log: must contain at least one event", errors)

    def test_audit_binds_the_current_document_state(self):
        data = load_fixture("v2-class-sample.json")
        response = data["attempts"][0]["responses"][0]
        response["normalized_answer"] = "篡改后的答案"
        errors = verify_audit_chain(data)
        self.assertIn("audit_log: document state hash mismatch", errors)

    def test_audit_recompute_establishes_a_new_state_baseline(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["responses"][0]["normalized_answer"] = "受控修复后的答案"
        repaired = recompute_audit_chain(data, actor_ref="audit-admin")
        self.assertEqual(validate_v2(repaired), [])
        self.assertEqual(verify_audit_chain(repaired), [])
        self.assertEqual(repaired["audit_log"][-1]["event_type"], "audit.recomputed")


class AnalysisTests(unittest.TestCase):
    def test_deterministic_analysis_and_review_gates(self):
        migrated = migrate_v1(load_fixture("v1-sample.json"))
        analyzed = analyze_document(migrated)
        self.assertEqual(validate_v2(analyzed), [])
        responses = analyzed["attempts"][0]["responses"]
        self.assertEqual(responses[0]["review_status"], "auto_confirmed")
        self.assertEqual(responses[1]["review_status"], "auto_confirmed")
        self.assertEqual(responses[1]["first_error_step"], 2)
        self.assertEqual(responses[2]["review_status"], "provisional")
        self.assertIsNone(responses[2]["score"])
        self.assertTrue(
            any(item["question_id"] == "q3" for item in analyzed["review_queue"])
        )
        self.assertEqual(verify_audit_chain(analyzed), [])

    def test_low_confidence_requires_review(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["responses"][0]["confidence"]["ocr"] = 0.4
        data["attempts"][0]["responses"][0]["review_status"] = "auto_confirmed"
        errors = validate_v2(data)
        self.assertTrue(any("low confidence requires review" in error for error in errors))

    def test_mandatory_adapter_review_signal_is_preserved(self):
        data = load_fixture("v2-class-sample.json")
        response = data["attempts"][0]["responses"][0]
        response["review_reasons"] = ["ocr_math_symbol_changed"]
        analyzed = analyze_document(data)
        checked = analyzed["attempts"][0]["responses"][0]
        self.assertEqual(checked["review_status"], "needs_review")
        self.assertIn("ocr_math_symbol_changed", checked["review_reasons"])

    def test_unsafe_formula_is_never_parsed_as_code(self):
        data = load_fixture("v2-class-sample.json")
        question = data["paper"]["questions"][0]
        response = data["attempts"][0]["responses"][0]
        question["question_type"] = "formula"
        question["reference_answer"] = "x+1"
        response["normalized_answer"] = "__import__('os').system('whoami')"
        analyzed = analyze_document(data)
        checked = analyzed["attempts"][0]["responses"][0]
        self.assertIsNone(checked["score"])
        self.assertEqual(checked["review_status"], "needs_review")
        self.assertIn("unsafe_symbolic_input", checked["review_reasons"])

    def test_symbolic_complexity_limits_reject_exponent_bombs(self):
        data = migrate_v1(load_fixture("v1-sample.json"))
        question = data["paper"]["questions"][0]
        response = data["attempts"][0]["responses"][0]
        question["question_type"] = "formula"
        question["reference_answer"] = "x^1001"
        response["normalized_answer"] = "x^1001"
        analyzed = analyze_document(data)
        checked = analyzed["attempts"][0]["responses"][0]
        self.assertIsNone(checked["score"])
        self.assertIn("unsafe_symbolic_input", checked["review_reasons"])

    def test_teacher_review_is_applied_and_audited(self):
        analyzed = analyze_document(migrate_v1(load_fixture("v1-sample.json")))
        review_document = export_review_queue(analyzed)
        review_document["decisions"] = [
            {
                "attempt_id": analyzed["attempts"][0]["attempt_id"],
                "question_id": "q3",
                "actor_ref": "teacher-01",
                "decision": "modify",
                "reason": "根据评分点人工确认",
                "score": 4,
            }
        ]
        reviewed = apply_review_decisions(
            analyzed,
            review_document,
        )
        response = reviewed["attempts"][0]["responses"][2]
        self.assertEqual(response["review_status"], "teacher_confirmed")
        self.assertEqual(response["score"], 4)
        self.assertEqual(validate_v2(reviewed), [])
        self.assertEqual(verify_audit_chain(reviewed), [])

    def test_review_decisions_are_bound_to_analysis_and_open_queue(self):
        analyzed = analyze_document(migrate_v1(load_fixture("v1-sample.json")))
        review_document = export_review_queue(analyzed)
        review_document["decisions"] = [
            {
                "attempt_id": analyzed["attempts"][0]["attempt_id"],
                "question_id": "q3",
                "actor_ref": "teacher-01",
                "decision": "confirm",
                "reason": "人工确认",
            }
        ]
        mismatched = copy.deepcopy(review_document)
        mismatched["analysis_id"] = "another-analysis"
        with self.assertRaisesRegex(ValueError, "analysis_id"):
            apply_review_decisions(analyzed, mismatched)

        unqueued = copy.deepcopy(review_document)
        unqueued["decisions"][0]["question_id"] = "q1"
        with self.assertRaisesRegex(ValueError, "open review queue"):
            apply_review_decisions(analyzed, unqueued)

    def test_validator_rejects_type_drift_and_oversized_embeddings(self):
        data = load_fixture("v2-class-sample.json")
        data["paper"]["questions"][0]["question_text"] = {"unexpected": True}
        data["attempts"][0]["responses"][0]["raw_ocr_text"] = {"unexpected": True}
        data["paper"]["questions"][1]["semantic_embedding"] = [0.0] * 8193
        data = recompute_audit_chain(data)
        errors = validate_v2(data)
        self.assertTrue(any("question_text" in error for error in errors))
        self.assertTrue(any("raw_ocr_text" in error for error in errors))
        self.assertTrue(any("semantic_embedding" in error for error in errors))

    def test_statistics_have_explicit_denominators(self):
        stats = compute_statistics(load_fixture("v2-class-sample.json"))
        self.assertEqual(stats["total_responses"], 4)
        self.assertEqual(stats["incorrect_rate"]["denominator"], 4)
        self.assertIn("denominator", stats["tag_distribution"]["error"]["concept-confusion"])

    def test_graph_contains_filters_legend_and_semantic_component(self):
        graph = build_graph(load_fixture("v2-class-sample.json"), threshold=0)
        self.assertIn("filters", graph)
        self.assertIn("legend", graph)
        self.assertTrue(graph["edges"])
        self.assertIn("semantic", graph["edges"][0]["components"])
        self.assertNotIn("student_ref", json.dumps(graph, ensure_ascii=False))
        self.assertNotIn("student_name", json.dumps(graph, ensure_ascii=False))

    def test_student_name_is_validated_as_a_local_display_field(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["student_name"] = ""
        self.assertTrue(
            any("student_name" in error for error in validate_v2(data))
        )

    def test_graph_does_not_treat_unscored_as_incorrect(self):
        analyzed = analyze_document(migrate_v1(load_fixture("v1-sample.json")))
        graph = build_graph(analyzed)
        q3 = next(item for item in graph["nodes"] if item["id"] == "q3")
        self.assertEqual(q3["scored_response_count"], 0)
        self.assertIsNone(q3["correct_rate"])

    def test_review_export_contains_anonymous_reference(self):
        analyzed = analyze_document(migrate_v1(load_fixture("v1-sample.json")))
        exported = export_review_queue(analyzed)
        self.assertTrue(exported["items"])
        self.assertEqual(exported["items"][0]["student_ref"], "student-001")


class ReportingTests(unittest.TestCase):
    def test_reports_are_complete_and_escape_html(self):
        data = load_fixture("v2-class-sample.json")
        with tempfile.TemporaryDirectory() as directory:
            result = write_report_bundle(data, directory, ASSETS)
            for path in result.values():
                self.assertTrue(Path(path).is_file())
            error_html = Path(result["error_report_html"]).read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", error_html)
            self.assertIn("已评分 / 未评分", error_html)
            self.assertIn("(2 / 4)", error_html)
            self.assertNotIn("`", error_html)
            self.assertNotIn("'unsafe-inline'", error_html)
            self.assertNotIn("{{CSP_NONCE}}", error_html)
            graph_html = Path(result["star_map_html"]).read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", graph_html)
            self.assertNotIn("'unsafe-inline'", graph_html)
            self.assertNotIn("{{CSP_NONCE}}", graph_html)
            lesson_md = Path(result["lesson_summary_markdown"]).read_text(encoding="utf-8")
            self.assertIn("课堂活动", lesson_md)
            error_md = Path(result["error_report_markdown"]).read_text(encoding="utf-8")
            self.assertIn("分母", error_md)


if __name__ == "__main__":
    unittest.main()
