from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from retrieval_engine import (  # noqa: E402
    ComponentUnavailable,
    build_embedding_provider,
    fts_tokens,
    index_document,
    purge_records,
    search_index,
    verify_database_audit,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "school.sqlite3"
        self.data = load_fixture("v2-class-sample.json")
        self.index_result = index_document(self.data, self.db, mode="rebuild")

    def tearDown(self):
        self.temp.cleanup()

    def test_chinese_ngram_tokenization(self):
        tokens = fts_tokens("二次方程")
        self.assertIn("二次", tokens)
        self.assertIn("二次方", tokens)

    def test_search_resource_limits_are_enforced(self):
        index_document(self.data, self.db, mode="rebuild")
        for value in (0, -1, 5001):
            with self.subTest(candidate_limit=value):
                with self.assertRaisesRegex(ValueError, "candidate_limit"):
                    search_index(self.db, "计算错误", candidate_limit=value)
        with self.assertRaisesRegex(ValueError, "query"):
            search_index(self.db, "x" * 4097)

    def test_local_model_requires_an_approved_fingerprint(self):
        model_dir = Path(self.temp.name) / "model"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ComponentUnavailable, "model-sha256"):
            build_embedding_provider(
                "sentence-transformers", str(model_dir), "approved-license"
            )

    def test_index_is_tenant_bound_and_audited(self):
        self.assertEqual(self.index_result["records_indexed"], 4)
        self.assertEqual(verify_database_audit(self.db), [])
        with closing(sqlite3.connect(self.db)) as connection:
            org = connection.execute(
                "SELECT value FROM metadata WHERE key='organization_id'"
            ).fetchone()[0]
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(records)").fetchall()
            }
        self.assertEqual(org, "school-demo")
        self.assertIn("student_name", columns)
        with closing(sqlite3.connect(self.db)) as connection:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT student_name FROM records"
                ).fetchall()
            }
        self.assertEqual(names, {"张晨", "李雨桐"})

    def test_wholesale_audit_deletion_is_detected_by_anchor(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DELETE FROM index_audit")
            connection.commit()
        errors = verify_database_audit(self.db)
        self.assertTrue(any("audit anchor" in error for error in errors))

    def test_lexical_and_tag_search(self):
        result = search_index(self.db, "二次方程 概念混淆", top_k=10)
        self.assertTrue(result["results"])
        self.assertTrue(
            any(item["question_id"] == "q2" for item in result["results"])
        )
        self.assertIn("semantic_embedding_provider", result["degraded_components"])

    def test_structured_filters_and_sql_injection_are_values(self):
        result = search_index(
            self.db,
            "",
            filters={"class_id": "class-8a", "question_id": "q1"},
            top_k=10,
        )
        self.assertEqual(len(result["results"]), 2)
        injected = search_index(
            self.db,
            "",
            filters={"class_id": "class-8a' OR 1=1 --"},
            top_k=10,
        )
        self.assertEqual(injected["results"], [])
        named = search_index(
            self.db,
            "",
            filters={"student_name": "张晨"},
            top_k=10,
        )
        self.assertEqual(len(named["results"]), 2)
        self.assertTrue(
            all(item["student_name"] == "张晨" for item in named["results"])
        )

    def test_tenant_switch_is_rejected(self):
        with self.assertRaises(ValueError):
            search_index(
                self.db,
                "方程",
                filters={"organization_id": "another-school"},
            )

    def test_cross_tenant_rebuild_is_rejected_before_existing_data_is_replaced(self):
        other = json.loads(json.dumps(self.data))
        other["organization_id"] = "another-school"
        other["analysis_id"] = "another-analysis"
        with self.assertRaisesRegex(ValueError, "belongs to organization"):
            index_document(other, self.db, mode="rebuild")
        with closing(sqlite3.connect(self.db)) as connection:
            owner = connection.execute(
                "SELECT value FROM metadata WHERE key='organization_id'"
            ).fetchone()[0]
            count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        self.assertEqual(owner, "school-demo")
        self.assertEqual(count, 4)

    def test_same_tenant_rebuild_uses_atomic_replacement(self):
        result = index_document(self.data, self.db, mode="rebuild")
        self.assertEqual(
            result["rebuild_strategy"],
            "same-volume-temporary-then-atomic-replace",
        )
        self.assertEqual(verify_database_audit(self.db), [])

    def test_require_semantic_fails_visibly(self):
        with self.assertRaises(ComponentUnavailable):
            search_index(self.db, "方程", require_semantic=True)

    def test_purge_removes_all_student_records(self):
        result = purge_records(self.db, student_ref="student-a")
        self.assertEqual(result["purged"], 2)
        remaining = search_index(
            self.db,
            "",
            filters={"student_ref": "student-a"},
        )
        self.assertEqual(remaining["results"], [])
        self.assertEqual(verify_database_audit(self.db), [])


if __name__ == "__main__":
    unittest.main()
