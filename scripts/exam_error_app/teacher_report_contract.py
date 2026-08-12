"""Stable contract and identity for the teacher-facing report UI."""

from __future__ import annotations

from typing import Any


TEACHER_REPORT_VIEW_VERSION = "teacher-report-view-v7"

TEACHER_REPORT_FIELDS = frozenset(
    {
        "view_schema_version",
        "title",
        "organization_id",
        "analysis_id",
        "document_state_hash",
        "subject",
        "grade",
        "curriculum_version",
        "student_count",
        "class_count",
        "metrics",
        "knowledge_distribution",
        "error_distribution",
        "priority_questions",
        "student_summaries",
        "review_items",
        "taxonomy_candidates",
        "teaching_actions",
        "notes",
    }
)


def validate_teacher_report_model(model: Any) -> list[str]:
    """Reject models that bypass or drift from the fixed projection contract."""
    if not isinstance(model, dict):
        return ["teacher report model must be an object"]

    errors: list[str] = []
    if model.get("view_schema_version") != TEACHER_REPORT_VIEW_VERSION:
        errors.append(
            f"view_schema_version must be {TEACHER_REPORT_VIEW_VERSION}"
        )

    missing = sorted(TEACHER_REPORT_FIELDS - set(model))
    unknown = sorted(set(model) - TEACHER_REPORT_FIELDS)
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    if unknown:
        errors.append("unknown fields: " + ", ".join(unknown))

    for field in (
        "title",
        "organization_id",
        "analysis_id",
        "document_state_hash",
        "subject",
        "grade",
    ):
        if not isinstance(model.get(field), str):
            errors.append(f"{field} must be a string")
    for field in ("student_count", "class_count"):
        if not isinstance(model.get(field), int) or isinstance(model.get(field), bool):
            errors.append(f"{field} must be an integer")
    if not isinstance(model.get("metrics"), dict):
        errors.append("metrics must be an object")
    for field in (
        "knowledge_distribution",
        "error_distribution",
        "priority_questions",
        "student_summaries",
        "review_items",
        "taxonomy_candidates",
        "teaching_actions",
        "notes",
    ):
        if not isinstance(model.get(field), list):
            errors.append(f"{field} must be a list")
    return errors
