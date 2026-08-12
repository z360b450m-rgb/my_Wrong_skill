from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SKILL_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exam_error_core import compute_statistics, export_review_queue  # noqa: E402
from exam_error_app import display_labels as display  # noqa: E402
from exam_error_app.teacher_report_contract import (  # noqa: E402
    TEACHER_REPORT_VIEW_VERSION,
)
from teacher_reporting import build_teacher_report_model, write_teacher_report  # noqa: E402
from teacher_report_renderer import (  # noqa: E402
    TEACHER_REPORT_RENDERER_VERSION,
    TEACHER_REPORT_TEMPLATE_SHA256,
    TEACHER_REPORT_TEMPLATE_VERSION,
    build_teacher_report_manifest,
    render_teacher_report_html,
)


class TeacherReportTests(unittest.TestCase):
    def test_single_file_teacher_report_is_safe_and_complete(self):
        data = json.loads(
            (SCRIPT_DIR / "tests" / "fixtures" / "v2-class-sample.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "teacher-report.html"
            write_teacher_report(
                data,
                output,
                SKILL_DIR / "assets" / "teacher-report.html",
                statistics_builder=compute_statistics,
                review_exporter=export_review_queue,
            )
            html = output.read_text(encoding="utf-8")
            self.assertIn("教师错题分析报告", html)
            self.assertIn("优先讲解清单", html)
            self.assertIn("待复核项目", html)
            self.assertIn("导出复核清单", html)
            self.assertIn("概念混淆", html)
            self.assertIn("一元一次方程", html)
            self.assertIn("第 1 题", html)
            self.assertIn("单项选择题", html)
            self.assertIn("系统已确认", html)
            self.assertIn("学生情况", html)
            self.assertIn("学生姓名", html)
            self.assertIn("张晨", html)
            self.assertIn("李雨桐", html)
            self.assertIn("已评分得分", html)
            self.assertIn("薄弱知识点", html)
            self.assertIn("确认分数", html)
            self.assertIn("最终成绩", html)
            self.assertIn("导出复核记录", html)
            self.assertIn("导出最终成绩", html)
            self.assertIn("data-review-confirm", html)
            self.assertIn("data-review-student-filter", html)
            self.assertIn("data-review-question-filter", html)
            self.assertIn("data-student-filter", html)
            self.assertIn("筛选学生情况", html)
            self.assertIn("错题题号", html)
            self.assertNotIn("该生错题题号", html)
            self.assertIn("finalIncorrectQuestions", html)
            self.assertIn("questions.delete(item.question_code)", html)
            self.assertIn("按得分从低到高", html)
            self.assertIn("a.score-b.score", html)
            self.assertIn("function csvCell(value)", html)
            self.assertIn("csvCell(value).replace", html)
            self.assertIn("全部学生", html)
            self.assertIn("全部题目", html)
            self.assertIn('class="bar-chart"', html)
            self.assertIn('class="bar-fill${kind?', html)
            self.assertNotIn('style="width:', html)
            self.assertIn('"subject":"数学"', html)
            self.assertIn('"grade":"八年级"', html)
            self.assertIn('"organization_id":"school-demo"', html)
            self.assertIn('"document_state_hash":"sha256:', html)
            self.assertIn("DATA.document_state_hash", html)
            self.assertNotIn('"name":"concept-confusion"', html)
            self.assertNotIn('"name":"math/algebra/equation/linear"', html)
            self.assertNotIn("{{REPORT_DATA}}", html)
            self.assertNotIn("{{CSP_NONCE}}", html)
            self.assertNotIn("'unsafe-inline'", html)
            self.assertNotIn("cdn", html.casefold())
            self.assertNotIn("<script>alert(1)</script>", html)

    def test_all_teacher_labels_use_the_canonical_chinese_standard(self):
        data = json.loads(
            (SCRIPT_DIR / "tests" / "fixtures" / "v2-class-sample.json").read_text(
                encoding="utf-8"
            )
        )

        def fake_review_exporter(_data):
            return {
                "items": [
                    {
                        "student_ref": "student-a",
                        "question_id": "q2",
                        "score": 0,
                        "created_at": "2026-08-07T10:00:00+00:00",
                        "reasons": [
                            "symbolic_equivalence_unavailable",
                            "new_review_reason",
                        ],
                    }
                ]
            }

        model = build_teacher_report_model(
            data,
            statistics_builder=compute_statistics,
            review_exporter=fake_review_exporter,
        )
        students = model["student_summaries"]
        self.assertEqual(len(students), 2)
        self.assertEqual(
            {student["student_ref"] for student in students},
            {"student-a", "student-b"},
        )
        self.assertTrue(
            all(
                {
                    "scored_score",
                    "scored_max",
                    "score_rate",
                    "incorrect_count",
                    "unscored_count",
                    "review_count",
                    "student_name",
                    "incorrect_question_codes",
                    "incorrect_questions",
                    "main_errors",
                    "weak_knowledge",
                }
                <= set(student)
                for student in students
            )
        )
        self.assertIn(
            "概念混淆",
            {name for student in students for name in student["main_errors"]},
        )
        self.assertIn(
            "一元二次方程",
            {name for student in students for name in student["weak_knowledge"]},
        )
        student_a = next(
            student for student in students if student["student_ref"] == "student-a"
        )
        self.assertEqual(student_a["incorrect_question_codes"], ["q2"])
        self.assertEqual(student_a["incorrect_questions"], ["第 2 题"])
        review = model["review_items"][0]
        self.assertEqual(review["student_name"], "张晨")
        self.assertEqual(review["question_code"], "q2")
        self.assertEqual(review["question_id"], "第 2 题")
        self.assertEqual(review["max_score"], 5)
        self.assertNotIn("created_at", review)
        self.assertEqual(
            review["reasons"],
            ["其他需要人工确认的情况", "符号等价校验能力不可用"],
        )
        self.assertEqual(
            review["reason_codes"],
            ["new_review_reason", "symbolic_equivalence_unavailable"],
        )
        self.assertEqual(display.error_label("new_error"), "其他错因")
        self.assertEqual(display.knowledge_label("new/knowledge"), "自定义知识点")
        self.assertEqual(display.review_status_label("new_status"), "状态待确认")
        self.assertEqual(display.question_type_label("new_type"), "其他题型")
        self.assertEqual(display.subject_label("new_subject"), "其他学科")
        self.assertEqual(display.grade_label("new_grade"), "年级待确认")
        self.assertEqual(display.subject_label("八年级数学"), "数学")
        self.assertEqual(display.grade_label("八年级"), "八年级")
        self.assertEqual(display.grade_label("初二"), "八年级")

    def test_same_view_always_renders_identical_html(self):
        data = json.loads(
            (SCRIPT_DIR / "tests" / "fixtures" / "v2-class-sample.json").read_text(
                encoding="utf-8"
            )
        )
        model = build_teacher_report_model(
            data,
            statistics_builder=compute_statistics,
            review_exporter=export_review_queue,
        )
        reordered_model = dict(reversed(list(model.items())))
        template = SKILL_DIR / "assets" / "teacher-report.html"
        first = render_teacher_report_html(model, template)
        second = render_teacher_report_html(reordered_model, template)
        self.assertEqual(first, second)

        manifest = build_teacher_report_manifest(first, "teacher-report.html")
        self.assertEqual(manifest["view_schema_version"], TEACHER_REPORT_VIEW_VERSION)
        self.assertEqual(manifest["renderer_version"], TEACHER_REPORT_RENDERER_VERSION)
        self.assertEqual(manifest["template_version"], TEACHER_REPORT_TEMPLATE_VERSION)
        self.assertEqual(manifest["template_sha256"], TEACHER_REPORT_TEMPLATE_SHA256)
        self.assertEqual(len(manifest["output_sha256"]), 64)

    def test_student_summary_merges_attempts_by_stable_reference(self):
        data = json.loads(
            (SCRIPT_DIR / "tests" / "fixtures" / "v2-class-sample.json").read_text(
                encoding="utf-8"
            )
        )
        repeated = copy.deepcopy(data["attempts"][0])
        repeated["attempt_id"] = "attempt-a-repeated"
        data["attempts"].append(repeated)
        model = build_teacher_report_model(
            data,
            statistics_builder=compute_statistics,
            review_exporter=export_review_queue,
        )
        student = next(
            item
            for item in model["student_summaries"]
            if item["student_ref"] == repeated["student_ref"]
        )
        self.assertEqual(len(model["student_summaries"]), 2)
        self.assertEqual(student["attempt_count"], 2)
        self.assertEqual(student["student_name"], "张晨")
        self.assertFalse(student["name_conflict"])
        self.assertEqual(student["incorrect_question_codes"], ["q2"])
        self.assertEqual(student["incorrect_questions"], ["第 2 题"])

    def test_renderer_rejects_contract_and_template_drift(self):
        data = json.loads(
            (SCRIPT_DIR / "tests" / "fixtures" / "v2-class-sample.json").read_text(
                encoding="utf-8"
            )
        )
        model = build_teacher_report_model(
            data,
            statistics_builder=compute_statistics,
            review_exporter=export_review_queue,
        )
        template = SKILL_DIR / "assets" / "teacher-report.html"

        invalid_model = dict(model)
        invalid_model["agent_custom_panel"] = []
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            render_teacher_report_html(invalid_model, template)

        missing_student_summary = dict(model)
        missing_student_summary.pop("student_summaries")
        with self.assertRaisesRegex(ValueError, "missing fields"):
            render_teacher_report_html(missing_student_summary, template)

        with tempfile.TemporaryDirectory() as directory:
            changed_template = Path(directory) / "teacher-report.html"
            changed_template.write_text(
                template.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "version-locked"):
                render_teacher_report_html(model, changed_template)


if __name__ == "__main__":
    unittest.main()
