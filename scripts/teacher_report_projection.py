#!/usr/bin/env python3
"""Project analyzed exam data into the stable teacher-report view contract."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any, Callable

from exam_error_app import display_labels as display
from exam_error_app.numbers import decimal_json, to_decimal
from exam_error_app.teacher_report_contract import TEACHER_REPORT_VIEW_VERSION


def _distribution_rows(
    values: dict[str, dict[str, Any]],
    labeler: Callable[[str], str],
) -> list[dict[str, Any]]:
    rows = [
        {"code": code, "name": labeler(code), **value}
        for code, value in values.items()
    ]
    rows.sort(key=lambda item: (-int(item.get("count", 0)), item["code"]))
    return rows


def _top_codes(
    counts: Counter[str],
    labeler: Callable[[str], str],
    *,
    limit: int = 2,
) -> tuple[list[str], list[str]]:
    codes = [
        code
        for code, _count in sorted(
            counts.items(), key=lambda pair: (-pair[1], pair[0])
        )[:limit]
    ]
    return codes, [labeler(code) for code in codes]


def _subjective_suggestions(question: dict[str, Any], response: dict[str, Any]) -> list[str]:
    """Create review guidance from the student's response and declared rubric only."""
    answer = str(response.get("normalized_answer") or "").strip()
    rubric_by_id = {
        item.get("rubric_id"): item for item in question.get("rubric_points", [])
        if isinstance(item, dict) and item.get("rubric_id")
    }
    results = {
        item.get("rubric_id"): item for item in response.get("rubric_results", [])
        if isinstance(item, dict) and item.get("rubric_id")
    }
    suggestions: list[str] = []
    if not answer:
        suggestions.append("学生未提供可识别作答；请先补写后再按评分点评阅。")
    for rubric_id, rubric in rubric_by_id.items():
        result = results.get(rubric_id)
        maximum = to_decimal(rubric.get("max_score")) or Decimal("0")
        awarded = to_decimal(result.get("awarded_score")) if result else None
        if result and result.get("status") == "teacher_confirmed" and awarded == maximum:
            continue
        description = str(rubric.get("description") or rubric_id)
        if awarded is None:
            suggestions.append(f"请对照评分点“{description}”核对作答是否覆盖，并由教师给出分数。")
        elif awarded < maximum:
            suggestions.append(f"建议完善“{description}”（当前建议 {decimal_json(awarded)}/{decimal_json(maximum)} 分）。")
    if not rubric_by_id:
        reference = str(question.get("reference_answer") or "").strip()
        suggestions.append(
            f"请将学生作答与参考答案“{reference[:120]}”逐项比对，再给出教师评分。"
            if reference else "题目未提供评分点或参考答案；请教师依据题意补充评分建议。"
        )
    return suggestions


def build_teacher_report_model(
    data: dict[str, Any],
    *,
    statistics_builder: Callable[[dict[str, Any]], dict[str, Any]],
    review_exporter: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Build the only model accepted by the fixed teacher-report renderer."""
    stats = statistics_builder(data)
    review_export = review_exporter(data)
    paper = data["paper"]
    questions = {item["question_id"]: item for item in paper["questions"]}
    student_name_sets: dict[str, set[str]] = {}
    for attempt in data.get("attempts", []):
        if attempt.get("student_ref") and attempt.get("student_name"):
            student_name_sets.setdefault(attempt["student_ref"], set()).add(
                attempt["student_name"]
            )
    student_names = {
        student_ref: sorted(names)[0]
        for student_ref, names in student_name_sets.items()
    }
    order = {
        item["question_id"]: index + 1
        for index, item in enumerate(paper["questions"])
    }
    insight: dict[str, dict[str, Any]] = {}
    for question_id, question in questions.items():
        insight[question_id] = {
            "question_id": question_id,
            "number": order[question_id],
            "label": display.question_label(order[question_id]),
            "question_text": question.get("question_text") or "",
            "question_type": display.question_type_label(
                question.get("question_type")
            ),
            "max_score": question.get("max_score"),
            "knowledge": sorted(
                {
                    display.knowledge_label(tag.get("name"))
                    for tag in question.get("tags", [])
                    if tag.get("dimension") == "knowledge" and tag.get("name")
                }
            ),
            "responses": 0,
            "incorrect": 0,
            "scored": 0,
            "lost_score": Decimal("0"),
            "review_count": 0,
            "error_counts": Counter(),
            "examples": [],
        }

    for attempt in data.get("attempts", []):
        for response in attempt.get("responses", []):
            item = insight.get(response.get("question_id"))
            if not item:
                continue
            item["responses"] += 1
            score = to_decimal(response.get("score"))
            maximum = to_decimal(item["max_score"]) or Decimal("0")
            if score is not None:
                item["scored"] += 1
                item["lost_score"] += maximum - score
            if response.get("is_correct") is False:
                item["incorrect"] += 1
            if response.get("review_status") in {"needs_review", "provisional"}:
                item["review_count"] += 1
            error_names = sorted(
                tag.get("name")
                for tag in response.get("error_tags", [])
                if isinstance(tag, dict) and tag.get("name")
            )
            item["error_counts"].update(error_names)
            if response.get("is_correct") is False and len(item["examples"]) < 3:
                evidence = response.get("evidence", [])
                first = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
                item["examples"].append(
                    {
                        "student_ref": attempt.get("student_ref"),
                        "student_name": attempt.get("student_name")
                        or "姓名待补充",
                        "answer": response.get("normalized_answer"),
                        "score": response.get("score"),
                        "review_status": display.review_status_label(
                            response.get("review_status")
                        ),
                        "observed": first.get("observed") or "无可用证据",
                        "explanation": first.get("explanation") or "",
                        "error_tags": [
                            display.error_label(name) for name in error_names
                        ],
                        "confidence": response.get("confidence", {}),
                    }
                )

    question_rows = []
    for item in insight.values():
        item["lost_score"] = decimal_json(item["lost_score"])
        item["error_counts"] = [
            {"code": name, "name": display.error_label(name), "count": count}
            for name, count in sorted(
                item["error_counts"].items(), key=lambda pair: (-pair[1], pair[0])
            )
        ]
        item["examples"].sort(
            key=lambda example: (
                example.get("student_ref") or "",
                str(example.get("answer") or ""),
            )
        )
        item["incorrect_rate"] = (
            round(item["incorrect"] / item["responses"] * 100, 1)
            if item["responses"]
            else 0.0
        )
        question_rows.append(item)
    question_rows.sort(
        key=lambda item: (
            -float(item["lost_score"]),
            -item["incorrect"],
            -item["review_count"],
            item["number"],
        )
    )

    knowledge = _distribution_rows(
        stats["tag_distribution"]["knowledge"], display.knowledge_label
    )
    errors = _distribution_rows(
        stats["tag_distribution"]["error"], display.error_label
    )

    review_by_student = Counter(
        item.get("student_ref")
        for item in review_export.get("items", [])
        if item.get("student_ref")
    )
    student_aggregate: dict[str, dict[str, Any]] = {}
    for attempt in data.get("attempts", []):
        student_ref = attempt.get("student_ref") or "anonymous-student"
        student = student_aggregate.setdefault(
            student_ref,
            {
                "student_ref": student_ref,
                "student_names": set(),
                "attempt_count": 0,
                "scored_score": Decimal("0"),
                "scored_max": Decimal("0"),
                "incorrect_count": 0,
                "incorrect_question_codes": set(),
                "unscored_count": 0,
                "error_counts": Counter(),
                "knowledge_counts": Counter(),
            },
        )
        if attempt.get("student_name"):
            student["student_names"].add(attempt["student_name"])
        student["attempt_count"] += 1
        for response in attempt.get("responses", []):
            question = questions.get(response.get("question_id"))
            if not question:
                continue
            score = to_decimal(response.get("score"))
            maximum = to_decimal(question.get("max_score")) or Decimal("0")
            if score is None:
                student["unscored_count"] += 1
            else:
                student["scored_score"] += score
                student["scored_max"] += maximum
            if response.get("is_correct") is False:
                student["incorrect_count"] += 1
                student["incorrect_question_codes"].add(
                    response.get("question_id")
                )
                error_codes = [
                    tag.get("name")
                    for tag in response.get("error_tags", [])
                    if isinstance(tag, dict) and tag.get("name")
                ]
                student["error_counts"].update(error_codes)
                student["knowledge_counts"].update(
                    tag.get("name")
                    for tag in question.get("tags", [])
                    if tag.get("dimension") == "knowledge" and tag.get("name")
                )

    student_summaries = []
    for student in student_aggregate.values():
        names = sorted(student.pop("student_names"))
        student["student_name"] = names[0] if names else "姓名待补充"
        student["name_conflict"] = len(names) > 1
        error_codes, main_errors = _top_codes(
            student.pop("error_counts"), display.error_label
        )
        knowledge_codes, weak_knowledge = _top_codes(
            student.pop("knowledge_counts"), display.knowledge_label
        )
        incorrect_question_codes = sorted(
            student.pop("incorrect_question_codes"),
            key=lambda code: (order.get(code, 10**9), code or ""),
        )
        scored_score = student["scored_score"]
        scored_max = student["scored_max"]
        student["scored_score"] = decimal_json(scored_score)
        student["scored_max"] = decimal_json(scored_max)
        student["score_rate"] = (
            round(float(scored_score / scored_max * 100), 1)
            if scored_max > 0
            else None
        )
        student["review_count"] = review_by_student.get(
            student["student_ref"], 0
        )
        student["main_error_codes"] = error_codes
        student["main_errors"] = main_errors
        student["weak_knowledge_codes"] = knowledge_codes
        student["weak_knowledge"] = weak_knowledge
        student["incorrect_question_codes"] = incorrect_question_codes
        student["incorrect_questions"] = [
            display.question_label(order.get(code))
            for code in incorrect_question_codes
        ]
        student_summaries.append(student)
    student_summaries.sort(
        key=lambda student: (
            student["score_rate"] is None,
            student["score_rate"] if student["score_rate"] is not None else 101,
            -student["incorrect_count"],
            -student["review_count"],
            student["student_ref"],
        )
    )

    actions = []
    for item in question_rows[:3]:
        leading_error = (
            item["error_counts"][0]["name"]
            if item["error_counts"]
            else "待补充归因"
        )
        actions.append(
            f"优先讲解{item['label']}：围绕“{leading_error}”定位第一处错误，"
            "并安排一道同构变式当堂检查。"
        )
    if stats["review_queue_count"]:
        actions.append(
            f"发布成绩前处理 {stats['review_queue_count']} 个待复核项目。"
        )
    if knowledge:
        actions.append(
            f"课后作业重点覆盖 {knowledge[0]['name']}，采用基础、变式、解释题各一组。"
        )

    review_items = []
    response_lookup = {
        (attempt.get("attempt_id"), response.get("question_id")): response
        for attempt in data.get("attempts", [])
        for response in attempt.get("responses", [])
        if isinstance(response, dict)
    }
    for review in review_export.get("items", []):
        question_id = review.get("question_id")
        reason_codes = sorted(review.get("reasons", []))
        question = questions.get(question_id, {})
        response = response_lookup.get((review.get("attempt_id"), question_id), {})
        suggestions = _subjective_suggestions(question, response) if question.get("question_type") == "subjective" else []
        review_items.append(
            {
                "attempt_id": review.get("attempt_id"),
                "student_ref": review.get("student_ref"),
                "score": review.get("score"),
                "student_name": review.get("student_name")
                or student_names.get(review.get("student_ref"))
                or "姓名待补充",
                "question_code": question_id,
                "max_score": (
                    questions.get(question_id, {}).get("max_score")
                    if question_id
                    else None
                ),
                "question_id": (
                    display.question_label(order[question_id])
                    if question_id in order
                    else display.question_label(None)
                ),
                "reason_codes": reason_codes,
                "reasons": [display.review_reason_label(code) for code in reason_codes] + suggestions,
                "suggestions": suggestions,
            }
        )
    review_items.sort(
        key=lambda review: (
            order.get(review.get("question_code"), 10**9),
            review.get("student_ref") or "",
            review.get("attempt_id") or "",
            tuple(review.get("reason_codes", [])),
        )
    )

    return {
        "view_schema_version": TEACHER_REPORT_VIEW_VERSION,
        "title": "教师错题分析报告",
        "organization_id": review_export.get("organization_id")
        or data.get("organization_id"),
        "analysis_id": data.get("analysis_id"),
        "document_state_hash": review_export.get("document_state_hash"),
        "subject": display.subject_label(paper.get("subject")),
        "grade": display.grade_label(paper.get("grade")),
        "curriculum_version": paper.get("curriculum_version"),
        "student_count": len(data.get("attempts", [])),
        "class_count": len(
            {
                attempt.get("class_id")
                for attempt in data.get("attempts", [])
                if attempt.get("class_id")
            }
        ),
        "metrics": {
            "responses": stats["total_responses"],
            "scored": stats["scored_responses"],
            "unscored": stats["unscored_responses"],
            "score_rate": stats["score_rate"]["value"],
            "incorrect_rate": stats["incorrect_rate"]["value"],
            "review_count": stats["review_queue_count"],
        },
        "knowledge_distribution": knowledge,
        "error_distribution": errors,
        "priority_questions": question_rows,
        "student_summaries": student_summaries,
        "review_items": review_items,
        "teaching_actions": actions,
        "notes": [
            stats.get("multi_tag_note", ""),
            "主观题和低置信度结果在教师确认前均标记为待复核。",
            "报告包含学生姓名，仅限已获授权的校内教学场景使用，请妥善保管。",
        ],
    }
