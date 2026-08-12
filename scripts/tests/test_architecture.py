from __future__ import annotations

import ast
import copy
import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exam_error_app.audit import AuditChainService  # noqa: E402
from exam_error_app.grading import ObjectiveGradingService  # noqa: E402
from exam_error_app.pipeline import AnalysisPipeline, PipelineValidationError  # noqa: E402
from exam_error_app.report_projection import build_report_view  # noqa: E402
from exam_error_app.retrieval_projection import project_index_records  # noqa: E402
from exam_error_app.teacher_report import TeacherReportApplication  # noqa: E402
from exam_error_core import (  # noqa: E402
    MAX_ATTEMPTS,
    MAX_EMBEDDING_DIMENSION,
    MAX_EVIDENCE_PER_RESPONSE,
    MAX_QUESTIONS,
    MAX_RESPONSES_PER_ATTEMPT,
    build_graph,
    canonical_json,
    compute_statistics,
    export_review_queue,
    normalize_text,
    recompute_audit_chain,
    validate_v2,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ArchitectureTests(unittest.TestCase):
    def test_json_schema_resource_limits_match_runtime_contract(self):
        schema = json.loads(
            (SCRIPT_DIR.parent / "references" / "exam-analysis-v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        definitions = schema["$defs"]
        self.assertEqual(schema["properties"]["attempts"]["maxItems"], MAX_ATTEMPTS)
        self.assertEqual(
            definitions["paper"]["properties"]["questions"]["maxItems"],
            MAX_QUESTIONS,
        )
        self.assertEqual(
            definitions["attempt"]["properties"]["responses"]["maxItems"],
            MAX_RESPONSES_PER_ATTEMPT,
        )
        self.assertEqual(
            definitions["response"]["properties"]["evidence"]["maxItems"],
            MAX_EVIDENCE_PER_RESPONSE,
        )
        self.assertEqual(
            definitions["question"]["properties"]["semantic_embedding"]["maxItems"],
            MAX_EMBEDDING_DIMENSION,
        )

    def test_application_pipeline_uses_ports_in_fixed_order(self):
        calls = []

        def migrate(document):
            calls.append("migrate")
            return {**document, "schema_version": "2.0"}

        def validate(document):
            calls.append("validate")
            return []

        def analyze(document):
            calls.append("analyze")
            return {**document, "analyzed": True}

        def audit(document):
            calls.append("audit")
            return []

        def statistics(document):
            calls.append("statistics")
            return {"ok": True}

        def graph(document, threshold=0.2, index_version="unindexed"):
            calls.append("graph")
            return {"threshold": threshold, "index_version": index_version}

        result = AnalysisPipeline(
            validate=validate,
            migrate=migrate,
            analyze=analyze,
            verify_audit=audit,
            build_statistics=statistics,
            build_graph=graph,
        ).run({"schema_version": "1.0"}, graph_threshold=0.4, index_version="idx-1")

        self.assertTrue(result.document["analyzed"])
        self.assertEqual(
            calls,
            ["migrate", "validate", "analyze", "validate", "audit", "statistics", "graph"],
        )

    def test_pipeline_stops_at_validation_boundary(self):
        pipeline = AnalysisPipeline(
            validate=lambda document: ["invalid"],
            migrate=lambda document: document,
            analyze=lambda document: self.fail("analyzer must not run"),
            verify_audit=lambda document: [],
            build_statistics=lambda document: {},
            build_graph=lambda document, threshold=0.2, index_version="unindexed": {},
        )
        with self.assertRaises(PipelineValidationError):
            pipeline.run({"schema_version": "2.0"})

    def test_report_projection_is_renderer_facing(self):
        data = load_fixture("v2-class-sample.json")
        view = build_report_view(
            data,
            statistics_builder=compute_statistics,
            graph_builder=build_graph,
            review_exporter=export_review_queue,
        )
        self.assertEqual(view.analysis_id, data["analysis_id"])
        self.assertEqual(view.statistics["total_responses"], 4)
        self.assertTrue(view.representative_errors)

    def test_index_projection_has_no_database_dependency(self):
        records = project_index_records(
            load_fixture("v2-class-sample.json"),
            normalize_text=normalize_text,
            canonical_json=canonical_json,
            tag_aliases={"concept-confusion": "概念混淆"},
        )
        self.assertEqual(len(records), 4)
        self.assertTrue(records[0]["record_id"].startswith("rec-"))

    def test_audit_service_accepts_deterministic_infrastructure(self):
        service = AuditChainService(
            hash_value=lambda value: "hash:" + canonical_json(value),
            safe_actor_id=lambda value, fallback: str(value or fallback),
            clock=lambda: "2026-01-01T00:00:00+00:00",
            event_id_factory=lambda: "evt-fixed",
        )
        document = {"audit_log": []}
        service.append(document, "test", {"value": 1})
        self.assertEqual(service.verify(document), [])
        document["value"] = 2
        self.assertIn("audit_log: document state hash mismatch", service.verify(document))

    def test_formula_grading_falls_back_to_normalized_text(self):
        from decimal import Decimal

        grader = ObjectiveGradingService(
            normalize_text,
            lambda value: Decimal(str(value)) if value is not None else None,
        )
        result = grader.score(
            {"question_type": "formula", "reference_answer": "x^2+1", "max_score": 5},
            {"normalized_answer": "x^2+1"},
        )
        self.assertEqual(result.score, Decimal("5"))
        self.assertIn(result.confidence, {0.75, 0.98})

    def test_application_package_does_not_import_outer_adapters(self):
        forbidden = {"exam_error_cli", "exam_error_core", "reporting", "retrieval_engine"}
        for path in (SCRIPT_DIR / "exam_error_app").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(forbidden & imported, f"{path.name}: forbidden imports")

    def test_teacher_report_projection_does_not_import_legacy_core(self):
        path = SCRIPT_DIR / "teacher_report_projection.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        self.assertNotIn("exam_error_core", imported)

    def test_teacher_report_application_keeps_ports_in_fixed_order(self):
        calls = []

        def validate(document):
            calls.append("validate")
            return []

        pipeline = AnalysisPipeline(
            validate=validate,
            migrate=lambda document: self.fail("v2 input must not migrate"),
            analyze=lambda document: calls.append("analyze") or document,
            verify_audit=lambda document: calls.append("audit") or [],
            build_statistics=lambda document: calls.append("statistics") or {},
            build_graph=lambda document, threshold=0.2, index_version="unindexed": (
                calls.append("graph") or {}
            ),
        )
        application = TeacherReportApplication(
            analysis_pipeline=pipeline,
            projector=lambda document: calls.append("project") or {"model": True},
            renderer=lambda model: calls.append("render") or "<html></html>",
        )
        artifact = application.generate({"schema_version": "2.0"})
        self.assertEqual(artifact.html, "<html></html>")
        self.assertEqual(
            calls,
            [
                "validate",
                "analyze",
                "validate",
                "audit",
                "statistics",
                "graph",
                "project",
                "render",
            ],
        )


class ValidationRegressionTests(unittest.TestCase):
    def test_formal_error_tags_are_controlled(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["responses"][1]["error_tags"][0]["name"] = "careless"
        self.assertTrue(any("unsupported error tag" in error for error in validate_v2(data)))

    def test_suggested_error_tags_allow_new_names(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["responses"][1]["suggested_tags"].append(
            {"dimension": "error", "name": "new-local-pattern", "confidence": 0.8}
        )
        self.assertEqual(validate_v2(recompute_audit_chain(data)), [])

    def test_malformed_error_tags_do_not_crash(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["responses"][0]["error_tags"] = None
        errors = validate_v2(data)
        self.assertTrue(any("error_tags: must be a list" in error for error in errors))

    def test_invalid_evidence_step_is_rejected(self):
        data = load_fixture("v2-class-sample.json")
        data["attempts"][0]["responses"][1]["evidence"][0]["step"] = 0
        self.assertTrue(any("step" in error for error in validate_v2(data)))


if __name__ == "__main__":
    unittest.main()
