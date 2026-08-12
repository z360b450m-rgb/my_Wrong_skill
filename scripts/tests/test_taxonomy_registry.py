from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from taxonomy_registry import (  # noqa: E402
    apply_teacher_decisions,
    approved_labels,
    candidates_from_document,
    load_registry,
    upsert_pending_candidates,
)


class TaxonomyRegistryTests(unittest.TestCase):
    def test_agent_candidate_requires_teacher_approval_before_reuse(self) -> None:
        document = {
            "attempts": [
                {
                    "student_ref": "s1",
                    "responses": [
                        {
                            "question_id": "q2",
                            "suggested_tags": [
                                {
                                    "dimension": "error",
                                    "name": "unit_conversion_omitted",
                                    "display_name": "漏写单位换算",
                                    "definition": "计算时没有统一物理量单位。",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "extensions.json"
            registry, pending = upsert_pending_candidates(path, candidates_from_document(document))
            self.assertEqual(len(pending), 1)
            self.assertEqual(approved_labels(registry)["error"], {})

            result = apply_teacher_decisions(
                path,
                {
                    "decisions": [
                        {
                            "id": pending[0]["id"],
                            "action": "approve",
                            "display_name": "单位换算遗漏",
                        }
                    ]
                },
            )
            self.assertEqual(result, {"applied": 1, "ignored": 0})
            reloaded = load_registry(path)
            self.assertEqual(
                approved_labels(reloaded)["error"]["unit_conversion_omitted"],
                "单位换算遗漏",
            )

    def test_rejected_candidate_is_not_reopened(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "extensions.json"
            candidate = {
                "id": "knowledge-1111111111111111",
                "dimension": "knowledge",
                "name": "vector_projection",
                "display_name": "向量投影",
                "definition": "",
                "evidence": [],
            }
            _registry, pending = upsert_pending_candidates(path, [candidate])
            apply_teacher_decisions(path, {"decisions": [{"id": pending[0]["id"], "action": "reject"}]})
            _registry, later_pending = upsert_pending_candidates(path, [candidate])
            self.assertEqual(later_pending, [])


if __name__ == "__main__":
    unittest.main()
