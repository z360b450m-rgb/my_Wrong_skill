#!/usr/bin/env python3
"""Commercial-grade deterministic core for the analyze-exam-errors skill."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

from exam_error_app.audit import AuditChainService
from exam_error_app.grading import ObjectiveGradingService


SCHEMA_VERSION = "2.0"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_TEXT_LENGTH = 100_000
MAX_QUESTIONS = 10_000
MAX_ATTEMPTS = 100_000
MAX_RESPONSES_PER_ATTEMPT = 10_000
MAX_EVIDENCE_PER_RESPONSE = 1_000
MAX_ANSWER_ITEMS = 1_000
QUESTION_TYPES = {
    "single_choice",
    "multiple_choice",
    "true_false",
    "fill_blank",
    "numeric",
    "formula",
    "ordered_steps",
    "subjective",
}
OBJECTIVE_TYPES = QUESTION_TYPES - {"subjective"}
REVIEW_STATUSES = {
    "unreviewed",
    "provisional",
    "needs_review",
    "auto_confirmed",
    "teacher_confirmed",
    "rejected",
}
MANDATORY_REVIEW_REASONS = {
    "ocr_math_symbol_changed",
    "multiple_valid_answers",
    "rubric_method_uncovered",
    "rubric_gap",
    "answer_misaligned",
    "critical_field_conflict",
}
MANDATORY_REVIEW_TAGS = {
    "ocr-symbol-error",
    "answer-misaligned",
    "rubric-gap",
    "extraction-error",
}
INFORMATIONAL_REVIEW_REASONS = {"migrated_from_v1"}
COGNITIVE_TAGS = {"remember", "understand", "apply", "analyze", "evaluate", "create"}
ERROR_TAGS = {
    "concept-missing",
    "concept-confusion",
    "theorem-misuse",
    "formula-misuse",
    "invalid-inference",
    "condition-omitted",
    "case-incomplete",
    "strategy-mismatch",
    "calculation-error",
    "sign-error",
    "transformation-error",
    "step-omitted",
    "requirement-misread",
    "condition-missed",
    "diagram-misread",
    "unit-missing",
    "notation-invalid",
    "conclusion-incomplete",
    "explanation-insufficient",
    "unanswered",
    "illegible",
    "answer-misaligned",
    "ocr-symbol-error",
    "extraction-error",
    "rubric-gap",
    "unclassified",
}
ROOT_FIELDS = {
    "schema_version",
    "organization_id",
    "analysis_id",
    "created_at",
    "paper",
    "attempts",
    "provenance",
    "review_queue",
    "audit_log",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def safe_id(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if ID_PATTERN.fullmatch(text):
        return text
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    if cleaned and cleaned[0].isalnum():
        return cleaned[:128]
    return fallback


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def decimal_json(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value.normalize())


def percentage(part: int | Decimal, whole: int | Decimal) -> float:
    denominator = Decimal(str(whole))
    if denominator == 0:
        return 0.0
    result = Decimal(str(part)) / denominator * Decimal("100")
    return float(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(normalize_text(item) for item in value)
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"[\s\u3000]+", " ", text).strip()
    text = re.sub(r"[，。；：、,.!?！？;:]+$", "", text)
    return text


def default_source(source: Any = None) -> dict[str, Any] | None:
    if source is None:
        return None
    source = source if isinstance(source, dict) else {}
    file_hash = source.get("file_hash")
    if file_hash and not str(file_hash).startswith("sha256:"):
        file_hash = f"sha256:{file_hash}"
    return {
        "document_id": source.get("document_id"),
        "page": source.get("page"),
        "bbox": source.get("bbox"),
        "bbox_unit": source.get("bbox_unit", "pixel" if source.get("bbox") else None),
        "raw_text": source.get("raw_text"),
        "file_hash": file_hash,
    }


_AUDIT_CHAIN = AuditChainService(
    hash_value=sha256_value,
    safe_actor_id=safe_id,
    clock=utc_now,
    event_id_factory=lambda: f"evt-{uuid.uuid4().hex}",
)


def append_audit_event(
    data: dict[str, Any],
    event_type: str,
    payload: Any,
    actor_ref: str = "system",
    timestamp: str | None = None,
) -> dict[str, Any]:
    return _AUDIT_CHAIN.append(
        data,
        event_type,
        payload,
        actor_ref=actor_ref,
        timestamp=timestamp,
    )


def verify_audit_chain(data: dict[str, Any]) -> list[str]:
    return _AUDIT_CHAIN.verify(data)


def recompute_audit_chain(
    data: dict[str, Any],
    actor_ref: str = "system",
    timestamp: str | None = None,
) -> dict[str, Any]:
    return _AUDIT_CHAIN.recompute(data, actor_ref=actor_ref, timestamp=timestamp)


def document_state_hash(data: dict[str, Any]) -> str:
    """Return the audit-compatible hash used to bind external review decisions."""
    return _AUDIT_CHAIN.state_hash(data)


def _validate_nullable_text(value: Any, location: str, errors: list[str]) -> None:
    if value is not None and not isinstance(value, str):
        errors.append(f"{location}: must be a string or null")
    elif isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
        errors.append(f"{location}: exceeds {MAX_TEXT_LENGTH} characters")


def _validate_answer_value(value: Any, location: str, errors: list[str]) -> None:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
            errors.append(f"{location}: exceeds {MAX_TEXT_LENGTH} characters")
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"{location}: must be finite")
        return
    if isinstance(value, list):
        if len(value) > MAX_ANSWER_ITEMS:
            errors.append(f"{location}: exceeds {MAX_ANSWER_ITEMS} items")
        for index, item in enumerate(value):
            if item is None or isinstance(item, (str, int, float, bool)):
                if isinstance(item, str) and len(item) > MAX_TEXT_LENGTH:
                    errors.append(
                        f"{location}[{index}]: exceeds {MAX_TEXT_LENGTH} characters"
                    )
                if isinstance(item, float) and not math.isfinite(item):
                    errors.append(f"{location}[{index}]: must be finite")
            else:
                errors.append(f"{location}[{index}]: must be a scalar or null")
        return
    errors.append(f"{location}: must be a scalar, a scalar list or null")


def _map_question_type(value: Any) -> str:
    aliases = {
        "choice": "single_choice",
        "single-choice": "single_choice",
        "multiple-choice": "multiple_choice",
        "boolean": "true_false",
        "blank": "fill_blank",
        "calculation": "numeric",
        "math": "formula",
        "essay": "subjective",
    }
    normalized = normalize_text(value).replace(" ", "_")
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in QUESTION_TYPES else "subjective"


def _migrate_evidence(question_id: str, old_items: Any, source: Any) -> list[dict[str, Any]]:
    if not isinstance(old_items, list):
        return []
    output = []
    for index, item in enumerate(old_items):
        item = item if isinstance(item, dict) else {"observed": str(item)}
        output.append(
            {
                "evidence_id": f"ev-{question_id}-{index + 1}",
                "observed": str(item.get("observed") or ""),
                "explanation": str(item.get("explanation") or ""),
                "causal_role": "causal" if index == 0 else "consequential",
                "step": item.get("step") if isinstance(item.get("step"), int) else None,
                "confidence": item.get("confidence"),
                "source": default_source(
                    {
                        **(source if isinstance(source, dict) else {}),
                        "bbox": item.get("bbox", (source or {}).get("bbox") if isinstance(source, dict) else None),
                    }
                ),
            }
        )
    return output


def migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v1 document to v2. A v2 input is returned unchanged."""
    if not isinstance(data, dict):
        raise ValueError("input root must be an object")
    if data.get("schema_version") == SCHEMA_VERSION:
        return copy.deepcopy(data)

    original_hash = sha256_value(data)
    warnings: list[str] = []
    organization_id = safe_id(data.get("organization_id"), "org-default")
    if not data.get("organization_id"):
        warnings.append("organization_id missing; used org-default")
    paper_id = safe_id(data.get("paper_id"), "paper-unknown")
    student_ref = safe_id(data.get("student_id"), f"student-{original_hash[-12:]}")
    questions_out: list[dict[str, Any]] = []
    responses_out: list[dict[str, Any]] = []

    old_questions = data.get("questions", [])
    if not isinstance(old_questions, list):
        raise ValueError("v1 questions must be a list")
    for index, old in enumerate(old_questions):
        if not isinstance(old, dict):
            warnings.append(f"questions[{index}] was not an object and was skipped")
            continue
        question_id = safe_id(old.get("question_id"), f"q{index + 1}")
        max_score = to_decimal(old.get("max_score")) or Decimal("0")
        all_tags = old.get("tags") if isinstance(old.get("tags"), list) else []
        question_tags = []
        error_tags = []
        for tag in all_tags:
            if not isinstance(tag, dict) or not tag.get("name"):
                continue
            migrated_tag = {
                "dimension": tag.get("dimension"),
                "name": str(tag["name"]),
                "confidence": tag.get("confidence"),
            }
            if migrated_tag["dimension"] in {"knowledge", "cognitive"}:
                question_tags.append(migrated_tag)
            elif migrated_tag["dimension"] == "error":
                migrated_tag["evidence_ids"] = []
                error_tags.append(migrated_tag)

        source = default_source(old.get("source"))
        evidence = _migrate_evidence(question_id, old.get("error_evidence"), old.get("source"))
        evidence_ids = [item["evidence_id"] for item in evidence]
        for tag in error_tags:
            tag["evidence_ids"] = evidence_ids[:1]
        known_error_names = {tag["name"] for tag in error_tags}
        for evidence_index, old_evidence in enumerate(
            old.get("error_evidence", []) if isinstance(old.get("error_evidence"), list) else []
        ):
            if not isinstance(old_evidence, dict):
                continue
            error_type = old_evidence.get("error_type")
            if error_type in ERROR_TAGS and error_type not in known_error_names:
                error_tags.append(
                    {
                        "dimension": "error",
                        "name": error_type,
                        "confidence": old_evidence.get("confidence"),
                        "evidence_ids": [evidence[evidence_index]["evidence_id"]],
                    }
                )
                known_error_names.add(error_type)

        score = to_decimal(old.get("score"))
        score_value = decimal_json(score) if score is not None else None
        is_correct = bool(score == max_score) if score is not None else None
        grading_confidence = old.get("grading_confidence")
        tag_confidences = [
            tag.get("confidence")
            for tag in all_tags
            if isinstance(tag, dict) and isinstance(tag.get("confidence"), (int, float))
        ]
        tagging_confidence = min(tag_confidences) if tag_confidences else None
        old_review = old.get("review_status")
        review_status = (
            "needs_review"
            if old_review == "needs_review"
            else "provisional"
            if old_review in {"confirmed", "unreviewed", None}
            else old_review
        )
        if review_status not in REVIEW_STATUSES:
            review_status = "provisional"
        review_reasons = ["migrated_from_v1"]

        questions_out.append(
            {
                "question_id": question_id,
                "question_type": _map_question_type(old.get("question_type")),
                "question_text": old.get("question_text"),
                "max_score": decimal_json(max_score),
                "reference_answer": old.get("reference_answer"),
                "answer_options": old.get("answer_options"),
                "scoring_config": old.get("scoring_config"),
                "rubric_points": old.get("rubric_points", []),
                "difficulty": old.get("difficulty") if old.get("difficulty") in {"easy", "medium", "hard"} else "unknown",
                "tags": question_tags,
                "source": source,
            }
        )
        responses_out.append(
            {
                "question_id": question_id,
                "raw_ocr_text": old.get("raw_ocr_text", old.get("student_answer")),
                "normalized_answer": old.get("corrected_text", old.get("student_answer")),
                "score": score_value,
                "is_correct": is_correct,
                "confidence": {
                    "ocr": old.get("ocr_confidence"),
                    "grading": grading_confidence,
                    "tagging": tagging_confidence,
                },
                "review_status": review_status,
                "review_reasons": review_reasons,
                "rubric_results": old.get("rubric_results", []),
                "first_error_step": old.get("first_error_step"),
                "evidence": evidence,
                "error_tags": error_tags,
                "suggested_tags": [],
                "source": source,
                "teacher_review": None,
            }
        )

    paper_total = sum((to_decimal(q["max_score"]) or Decimal("0")) for q in questions_out)
    created_at = data.get("created_at") or utc_now()
    migrated = {
        "schema_version": SCHEMA_VERSION,
        "organization_id": organization_id,
        "analysis_id": safe_id(data.get("analysis_id"), f"analysis-{original_hash[-12:]}"),
        "created_at": created_at,
        "paper": {
            "paper_id": paper_id,
            "subject": str(data.get("subject") or "unknown"),
            "grade": data.get("grade"),
            "curriculum_version": data.get("curriculum_version"),
            "max_score": decimal_json(paper_total),
            "questions": questions_out,
        },
        "attempts": [
            {
                "attempt_id": safe_id(data.get("attempt_id"), f"attempt-{original_hash[-12:]}"),
                "class_id": data.get("class_id"),
                "student_ref": student_ref,
                "submitted_at": data.get("submitted_at"),
                "responses": responses_out,
            }
        ],
        "provenance": {
            "input_hash": original_hash,
            "source_files": data.get("source_files", []),
            "adapters": data.get("adapters", []),
            "model_calls": data.get("model_calls", []),
            "migration_warnings": warnings,
        },
        "review_queue": [],
        "audit_log": [],
    }
    if data.get("student_name") is not None:
        migrated["attempts"][0]["student_name"] = str(data["student_name"]).strip()[:100]
    refresh_review_queue(migrated, created_at=created_at)
    append_audit_event(
        migrated,
        "migration.v1_to_v2",
        {"input_hash": original_hash, "warnings": warnings},
        timestamp=created_at,
    )
    return migrated


def _valid_datetime(value: Any, allow_none: bool = False) -> bool:
    if value is None:
        return allow_none
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_source(source: Any, location: str, errors: list[str]) -> None:
    if source is None:
        return
    if not isinstance(source, dict):
        errors.append(f"{location}: must be an object or null")
        return
    required = {"document_id", "page", "bbox", "bbox_unit", "raw_text", "file_hash"}
    for key in sorted(set(source) - required):
        errors.append(f"{location}: unsupported field {key}")
    missing = required - set(source)
    for key in sorted(missing):
        errors.append(f"{location}: missing {key}")
    bbox = source.get("bbox")
    if bbox is not None:
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in bbox
        ):
            errors.append(f"{location}.bbox: must contain four numbers")
        elif bbox[2] < bbox[0] or bbox[3] < bbox[1]:
            errors.append(f"{location}.bbox: coordinates are inverted")
    if source.get("bbox_unit") not in {"pixel", "normalized", None}:
        errors.append(f"{location}.bbox_unit: invalid value")
    if source.get("page") is not None and (
        not isinstance(source["page"], int) or isinstance(source["page"], bool) or source["page"] < 1
    ):
        errors.append(f"{location}.page: must be a positive integer or null")
    file_hash = source.get("file_hash")
    if file_hash is not None and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(file_hash)):
        errors.append(f"{location}.file_hash: must be a sha256 digest or null")


def _validate_confidence(value: Any, location: str, errors: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        errors.append(f"{location}: must be null or a number between 0 and 1")


def _reject_unknown_fields(
    value: dict[str, Any],
    allowed: set[str],
    location: str,
    errors: list[str],
) -> None:
    for key in sorted(set(value) - allowed):
        errors.append(f"{location}: unsupported field {key}")


def _validate_tag(
    tag: Any,
    location: str,
    errors: list[str],
    allowed_dimensions: set[str],
    evidence_ids: set[str] | None = None,
    controlled_names: bool = True,
    require_evidence: bool = True,
) -> None:
    if not isinstance(tag, dict):
        errors.append(f"{location}: must be an object")
        return
    _reject_unknown_fields(
        tag,
        {"dimension", "name", "confidence", "evidence_ids"},
        location,
        errors,
    )
    for key in ("dimension", "name", "confidence"):
        if key not in tag:
            errors.append(f"{location}: missing {key}")
    dimension = tag.get("dimension")
    name = tag.get("name")
    if dimension not in allowed_dimensions:
        errors.append(f"{location}.dimension: invalid value")
    if not isinstance(name, str) or not name:
        errors.append(f"{location}.name: must be a non-empty string")
    if controlled_names and dimension == "cognitive" and name not in COGNITIVE_TAGS:
        errors.append(f"{location}.name: unsupported cognitive tag {name}")
    if controlled_names and dimension == "error" and name not in ERROR_TAGS:
        errors.append(f"{location}.name: unsupported error tag {name}")
    _validate_confidence(tag.get("confidence"), f"{location}.confidence", errors)
    if dimension == "error" and require_evidence:
        refs = tag.get("evidence_ids")
        if not isinstance(refs, list):
            errors.append(f"{location}.evidence_ids: must be a list")
        elif evidence_ids is not None:
            for ref in refs:
                if ref not in evidence_ids:
                    errors.append(f"{location}.evidence_ids: unknown evidence {ref}")
            if name != "unclassified" and not refs:
                errors.append(f"{location}.evidence_ids: evidence is required")


def validate_v2(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["root: must be an object"]
    for key in sorted(ROOT_FIELDS - set(data)):
        errors.append(f"root: missing {key}")
    for key in sorted(set(data) - ROOT_FIELDS):
        errors.append(f"root: unsupported field {key}")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append("root.schema_version: must be 2.0")
    for key in ("organization_id", "analysis_id"):
        if not isinstance(data.get(key), str) or not ID_PATTERN.fullmatch(data.get(key, "")):
            errors.append(f"root.{key}: invalid identifier")
    if not _valid_datetime(data.get("created_at")):
        errors.append("root.created_at: must be an ISO 8601 datetime with timezone")

    paper = data.get("paper")
    if not isinstance(paper, dict):
        errors.append("paper: must be an object")
        return errors + verify_audit_chain(data)
    _reject_unknown_fields(
        paper,
        {"paper_id", "subject", "grade", "curriculum_version", "max_score", "questions"},
        "paper",
        errors,
    )
    for key in ("paper_id", "subject", "grade", "curriculum_version", "max_score", "questions"):
        if key not in paper:
            errors.append(f"paper: missing {key}")
    if not isinstance(paper.get("paper_id"), str) or not ID_PATTERN.fullmatch(paper.get("paper_id", "")):
        errors.append("paper.paper_id: invalid identifier")
    if not isinstance(paper.get("subject"), str) or not paper.get("subject"):
        errors.append("paper.subject: must be a non-empty string")
    declared_max = to_decimal(paper.get("max_score"))
    if declared_max is None or declared_max < 0:
        errors.append("paper.max_score: must be a non-negative number")

    questions = paper.get("questions")
    if not isinstance(questions, list):
        errors.append("paper.questions: must be a list")
        questions = []
    elif len(questions) > MAX_QUESTIONS:
        errors.append(f"paper.questions: exceeds {MAX_QUESTIONS} items")
    if not questions:
        errors.append("paper.questions: must not be empty")
    question_map: dict[str, dict[str, Any]] = {}
    calculated_max = Decimal("0")
    for index, question in enumerate(questions):
        location = f"paper.questions[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{location}: must be an object")
            continue
        _reject_unknown_fields(
            question,
            {
                "question_id",
                "question_type",
                "question_text",
                "max_score",
                "reference_answer",
                "answer_options",
                "scoring_config",
                "rubric_points",
                "difficulty",
                "tags",
                "source",
            },
            location,
            errors,
        )
        required = {
            "question_id",
            "question_type",
            "question_text",
            "max_score",
            "reference_answer",
            "rubric_points",
            "difficulty",
            "tags",
            "source",
        }
        for key in sorted(required - set(question)):
            errors.append(f"{location}: missing {key}")
        question_id = question.get("question_id")
        if not isinstance(question_id, str) or not ID_PATTERN.fullmatch(question_id):
            errors.append(f"{location}.question_id: invalid identifier")
        elif question_id in question_map:
            errors.append(f"{location}.question_id: duplicate {question_id}")
        else:
            question_map[question_id] = question
        if question.get("question_type") not in QUESTION_TYPES:
            errors.append(f"{location}.question_type: unsupported value")
        _validate_nullable_text(
            question.get("question_text"), f"{location}.question_text", errors
        )
        _validate_answer_value(
            question.get("reference_answer"),
            f"{location}.reference_answer",
            errors,
        )
        maximum = to_decimal(question.get("max_score"))
        if maximum is None or maximum < 0:
            errors.append(f"{location}.max_score: invalid value")
        else:
            calculated_max += maximum
        if question.get("difficulty") not in {"easy", "medium", "hard", "unknown"}:
            errors.append(f"{location}.difficulty: invalid value")
        tags = question.get("tags")
        if not isinstance(tags, list):
            errors.append(f"{location}.tags: must be a list")
        else:
            for tag_index, tag in enumerate(tags):
                _validate_tag(tag, f"{location}.tags[{tag_index}]", errors, {"knowledge", "cognitive"})
        rubric_points = question.get("rubric_points")
        if not isinstance(rubric_points, list):
            errors.append(f"{location}.rubric_points: must be a list")
        else:
            seen_rubric: set[str] = set()
            rubric_total = Decimal("0")
            for rubric_index, rubric in enumerate(rubric_points):
                rubric_location = f"{location}.rubric_points[{rubric_index}]"
                if not isinstance(rubric, dict):
                    errors.append(f"{rubric_location}: must be an object")
                    continue
                _reject_unknown_fields(
                    rubric,
                    {"rubric_id", "description", "max_score"},
                    rubric_location,
                    errors,
                )
                rubric_id = rubric.get("rubric_id")
                if not isinstance(rubric_id, str) or not ID_PATTERN.fullmatch(rubric_id):
                    errors.append(f"{rubric_location}.rubric_id: invalid identifier")
                elif rubric_id in seen_rubric:
                    errors.append(f"{rubric_location}.rubric_id: duplicate")
                seen_rubric.add(rubric_id)
                rubric_score = to_decimal(rubric.get("max_score"))
                if rubric_score is None or rubric_score < 0:
                    errors.append(f"{rubric_location}.max_score: invalid value")
                else:
                    rubric_total += rubric_score
            if rubric_points and maximum is not None and rubric_total != maximum:
                errors.append(f"{location}.rubric_points: total must equal question max_score")
        _validate_source(question.get("source"), f"{location}.source", errors)

    if declared_max is not None and declared_max != calculated_max:
        errors.append("paper.max_score: does not equal the sum of question max scores")

    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        errors.append("attempts: must be a list")
        attempts = []
    elif len(attempts) > MAX_ATTEMPTS:
        errors.append(f"attempts: exceeds {MAX_ATTEMPTS} items")
    attempt_ids: set[str] = set()
    review_required: set[tuple[str, str]] = set()
    for attempt_index, attempt in enumerate(attempts):
        location = f"attempts[{attempt_index}]"
        if not isinstance(attempt, dict):
            errors.append(f"{location}: must be an object")
            continue
        _reject_unknown_fields(
            attempt,
            {
                "attempt_id",
                "class_id",
                "student_ref",
                "student_name",
                "submitted_at",
                "responses",
            },
            location,
            errors,
        )
        for key in ("attempt_id", "class_id", "student_ref", "submitted_at", "responses"):
            if key not in attempt:
                errors.append(f"{location}: missing {key}")
        attempt_id = attempt.get("attempt_id")
        if not isinstance(attempt_id, str) or not ID_PATTERN.fullmatch(attempt_id):
            errors.append(f"{location}.attempt_id: invalid identifier")
        elif attempt_id in attempt_ids:
            errors.append(f"{location}.attempt_id: duplicate")
        attempt_ids.add(attempt_id)
        if not isinstance(attempt.get("student_ref"), str) or not ID_PATTERN.fullmatch(
            attempt.get("student_ref", "")
        ):
            errors.append(f"{location}.student_ref: invalid identifier")
        student_name = attempt.get("student_name")
        if student_name is not None and (
            not isinstance(student_name, str)
            or not student_name.strip()
            or len(student_name) > 100
            or any(ord(character) < 32 for character in student_name)
        ):
            errors.append(f"{location}.student_name: invalid student name")
        if attempt.get("class_id") is not None and (
            not isinstance(attempt.get("class_id"), str)
            or not ID_PATTERN.fullmatch(attempt.get("class_id", ""))
        ):
            errors.append(f"{location}.class_id: invalid identifier")
        if not _valid_datetime(attempt.get("submitted_at"), allow_none=True):
            errors.append(f"{location}.submitted_at: invalid datetime")
        responses = attempt.get("responses")
        if not isinstance(responses, list):
            errors.append(f"{location}.responses: must be a list")
            continue
        if len(responses) > MAX_RESPONSES_PER_ATTEMPT:
            errors.append(
                f"{location}.responses: exceeds {MAX_RESPONSES_PER_ATTEMPT} items"
            )
        seen_responses: set[str] = set()
        for response_index, response in enumerate(responses):
            response_location = f"{location}.responses[{response_index}]"
            if not isinstance(response, dict):
                errors.append(f"{response_location}: must be an object")
                continue
            _reject_unknown_fields(
                response,
                {
                    "question_id",
                    "raw_ocr_text",
                    "normalized_answer",
                    "score",
                    "is_correct",
                    "confidence",
                    "review_status",
                    "review_reasons",
                    "rubric_results",
                    "first_error_step",
                    "evidence",
                    "error_tags",
                    "suggested_tags",
                    "source",
                    "teacher_review",
                },
                response_location,
                errors,
            )
            required = {
                "question_id",
                "raw_ocr_text",
                "normalized_answer",
                "score",
                "is_correct",
                "confidence",
                "review_status",
                "review_reasons",
                "rubric_results",
                "first_error_step",
                "evidence",
                "error_tags",
                "suggested_tags",
                "source",
                "teacher_review",
            }
            for key in sorted(required - set(response)):
                errors.append(f"{response_location}: missing {key}")
            question_id = response.get("question_id")
            if question_id not in question_map:
                errors.append(f"{response_location}.question_id: unknown question")
                question = None
            else:
                question = question_map[question_id]
            if question_id in seen_responses:
                errors.append(f"{response_location}.question_id: duplicate response")
            seen_responses.add(question_id)
            _validate_nullable_text(
                response.get("raw_ocr_text"),
                f"{response_location}.raw_ocr_text",
                errors,
            )
            _validate_answer_value(
                response.get("normalized_answer"),
                f"{response_location}.normalized_answer",
                errors,
            )
            confidence = response.get("confidence")
            if not isinstance(confidence, dict):
                errors.append(f"{response_location}.confidence: must be an object")
                confidence = {}
            else:
                _reject_unknown_fields(
                    confidence,
                    {"ocr", "grading", "tagging"},
                    f"{response_location}.confidence",
                    errors,
                )
            for dimension in ("ocr", "grading", "tagging"):
                if dimension not in confidence:
                    errors.append(f"{response_location}.confidence: missing {dimension}")
                _validate_confidence(confidence.get(dimension), f"{response_location}.confidence.{dimension}", errors)
            status = response.get("review_status")
            if status not in REVIEW_STATUSES:
                errors.append(f"{response_location}.review_status: invalid value")
            score = to_decimal(response.get("score"))
            if response.get("score") is not None and score is None:
                errors.append(f"{response_location}.score: must be a finite number or null")
            if question is not None:
                maximum = to_decimal(question.get("max_score")) or Decimal("0")
                if score is not None and (score < 0 or score > maximum):
                    errors.append(f"{response_location}.score: outside question range")
                expected_correct = score == maximum if score is not None else None
                if response.get("is_correct") is not expected_correct:
                    errors.append(f"{response_location}.is_correct: inconsistent with score")
                if question.get("question_type") == "subjective" and status == "auto_confirmed":
                    errors.append(f"{response_location}.review_status: subjective answers cannot auto-confirm")
            evidence = response.get("evidence")
            evidence_ids: set[str] = set()
            if not isinstance(evidence, list):
                errors.append(f"{response_location}.evidence: must be a list")
                evidence = []
            elif len(evidence) > MAX_EVIDENCE_PER_RESPONSE:
                errors.append(
                    f"{response_location}.evidence: exceeds {MAX_EVIDENCE_PER_RESPONSE} items"
                )
            for evidence_index, item in enumerate(evidence):
                evidence_location = f"{response_location}.evidence[{evidence_index}]"
                if not isinstance(item, dict):
                    errors.append(f"{evidence_location}: must be an object")
                    continue
                _reject_unknown_fields(
                    item,
                    {
                        "evidence_id",
                        "observed",
                        "explanation",
                        "causal_role",
                        "step",
                        "confidence",
                        "source",
                    },
                    evidence_location,
                    errors,
                )
                for key in (
                    "evidence_id",
                    "observed",
                    "explanation",
                    "causal_role",
                    "step",
                    "confidence",
                    "source",
                ):
                    if key not in item:
                        errors.append(f"{evidence_location}: missing {key}")
                evidence_id = item.get("evidence_id")
                if not isinstance(evidence_id, str) or not ID_PATTERN.fullmatch(evidence_id):
                    errors.append(f"{evidence_location}.evidence_id: invalid identifier")
                elif evidence_id in evidence_ids:
                    errors.append(f"{evidence_location}.evidence_id: duplicate")
                evidence_ids.add(evidence_id)
                if item.get("causal_role") not in {"causal", "consequential", "context"}:
                    errors.append(f"{evidence_location}.causal_role: invalid value")
                if not isinstance(item.get("observed"), str):
                    errors.append(f"{evidence_location}.observed: must be a string")
                elif len(item["observed"]) > MAX_TEXT_LENGTH:
                    errors.append(
                        f"{evidence_location}.observed: exceeds {MAX_TEXT_LENGTH} characters"
                    )
                if not isinstance(item.get("explanation"), str):
                    errors.append(f"{evidence_location}.explanation: must be a string")
                elif len(item["explanation"]) > MAX_TEXT_LENGTH:
                    errors.append(
                        f"{evidence_location}.explanation: exceeds {MAX_TEXT_LENGTH} characters"
                    )
                step = item.get("step")
                if step is not None and (
                    not isinstance(step, int) or isinstance(step, bool) or step < 1
                ):
                    errors.append(f"{evidence_location}.step: must be a positive integer or null")
                _validate_confidence(item.get("confidence"), f"{evidence_location}.confidence", errors)
                _validate_source(item.get("source"), f"{evidence_location}.source", errors)
            tags = response.get("error_tags")
            if not isinstance(tags, list):
                errors.append(f"{response_location}.error_tags: must be a list")
                tags = []
            else:
                for tag_index, tag in enumerate(tags):
                    _validate_tag(
                        tag,
                        f"{response_location}.error_tags[{tag_index}]",
                        errors,
                        {"error"},
                        evidence_ids,
                    )
            suggested = response.get("suggested_tags")
            if not isinstance(suggested, list):
                errors.append(f"{response_location}.suggested_tags: must be a list")
            else:
                for tag_index, tag in enumerate(suggested):
                    _validate_tag(
                        tag,
                        f"{response_location}.suggested_tags[{tag_index}]",
                        errors,
                        {"error"},
                        evidence_ids,
                        controlled_names=False,
                        require_evidence=False,
                    )
                tag_values = [
                    tag.get("confidence")
                    for tag in tags
                    if isinstance(tag, dict) and tag.get("confidence") is not None
                ]
                if tag_values and min(tag_values) < 0.70 and status not in {
                    "needs_review",
                    "teacher_confirmed",
                    "rejected",
                }:
                    errors.append(
                        f"{response_location}.review_status: low tag confidence requires review"
                    )
            rubric_results = response.get("rubric_results")
            if not isinstance(rubric_results, list):
                errors.append(f"{response_location}.rubric_results: must be a list")
            else:
                rubric_ids = {
                    item.get("rubric_id")
                    for item in (question or {}).get("rubric_points", [])
                    if isinstance(item, dict)
                }
                for rubric_index, rubric_result in enumerate(rubric_results):
                    rubric_location = f"{response_location}.rubric_results[{rubric_index}]"
                    if not isinstance(rubric_result, dict):
                        errors.append(f"{rubric_location}: must be an object")
                        continue
                    _reject_unknown_fields(
                        rubric_result,
                        {"rubric_id", "awarded_score", "evidence_ids", "status"},
                        rubric_location,
                        errors,
                    )
                    if rubric_result.get("rubric_id") not in rubric_ids:
                        errors.append(f"{rubric_location}.rubric_id: unknown rubric point")
                    if rubric_result.get("status") not in {
                        "suggested",
                        "teacher_confirmed",
                        "rejected",
                    }:
                        errors.append(f"{rubric_location}.status: invalid value")
                    for evidence_ref in rubric_result.get("evidence_ids", []):
                        if evidence_ref not in evidence_ids:
                            errors.append(
                                f"{rubric_location}.evidence_ids: unknown evidence {evidence_ref}"
                            )
            teacher_review = response.get("teacher_review")
            if teacher_review is not None:
                if not isinstance(teacher_review, dict):
                    errors.append(f"{response_location}.teacher_review: must be an object or null")
                else:
                    _reject_unknown_fields(
                        teacher_review,
                        {"actor_ref", "reviewed_at", "decision", "reason", "before", "after"},
                        f"{response_location}.teacher_review",
                        errors,
                    )
                    if not isinstance(teacher_review.get("actor_ref"), str) or not ID_PATTERN.fullmatch(
                        teacher_review.get("actor_ref", "")
                    ):
                        errors.append(f"{response_location}.teacher_review.actor_ref: invalid identifier")
                    if not _valid_datetime(teacher_review.get("reviewed_at")):
                        errors.append(f"{response_location}.teacher_review.reviewed_at: invalid datetime")
                    if teacher_review.get("decision") not in {"confirm", "modify", "reject"}:
                        errors.append(f"{response_location}.teacher_review.decision: invalid value")
                    if not isinstance(teacher_review.get("reason"), str) or not teacher_review.get("reason"):
                        errors.append(f"{response_location}.teacher_review.reason: required")
            _validate_source(response.get("source"), f"{response_location}.source", errors)
            reasons = response.get("review_reasons")
            if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
                errors.append(f"{response_location}.review_reasons: must be a string list")
            critical = [confidence.get(key) for key in ("ocr", "grading", "tagging")]
            if any(value is not None and value < 0.70 for value in critical):
                if status not in {"needs_review", "teacher_confirmed", "rejected"}:
                    errors.append(f"{response_location}.review_status: low confidence requires review")
            if status in {"needs_review", "provisional"} or (
                question is not None and question.get("question_type") == "subjective" and status != "teacher_confirmed"
            ):
                review_required.add((attempt_id, question_id))

    review_queue = data.get("review_queue")
    queue_pairs: set[tuple[str, str]] = set()
    if not isinstance(review_queue, list):
        errors.append("review_queue: must be a list")
        review_queue = []
    seen_review_ids: set[str] = set()
    for index, item in enumerate(review_queue):
        location = f"review_queue[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{location}: must be an object")
            continue
        _reject_unknown_fields(
            item,
            {"review_id", "attempt_id", "question_id", "reasons", "status", "created_at"},
            location,
            errors,
        )
        review_id = item.get("review_id")
        if not isinstance(review_id, str) or not ID_PATTERN.fullmatch(review_id):
            errors.append(f"{location}.review_id: invalid identifier")
        elif review_id in seen_review_ids:
            errors.append(f"{location}.review_id: duplicate")
        seen_review_ids.add(review_id)
        pair = (item.get("attempt_id"), item.get("question_id"))
        queue_pairs.add(pair)
        if pair[0] not in attempt_ids or pair[1] not in question_map:
            errors.append(f"{location}: references an unknown attempt or question")
        if not isinstance(item.get("reasons"), list) or not item.get("reasons"):
            errors.append(f"{location}.reasons: must be a non-empty list")
        if item.get("status") not in {"open", "resolved", "rejected"}:
            errors.append(f"{location}.status: invalid value")
        if not _valid_datetime(item.get("created_at")):
            errors.append(f"{location}.created_at: invalid datetime")
    missing_queue = review_required - queue_pairs
    for attempt_id, question_id in sorted(missing_queue):
        errors.append(f"review_queue: missing {attempt_id}/{question_id}")
    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance: must be an object")
    else:
        provenance_fields = {
            "input_hash",
            "source_files",
            "adapters",
            "model_calls",
            "migration_warnings",
        }
        _reject_unknown_fields(provenance, provenance_fields, "provenance", errors)
        for key in provenance_fields:
            if key not in provenance:
                errors.append(f"provenance: missing {key}")
        for key in ("source_files", "adapters", "model_calls", "migration_warnings"):
            if key in provenance and not isinstance(provenance[key], list):
                errors.append(f"provenance.{key}: must be a list")
    for index, event in enumerate(data.get("audit_log", []) if isinstance(data.get("audit_log"), list) else []):
        location = f"audit_log[{index}]"
        if not isinstance(event, dict):
            continue
        event_fields = {
            "event_id",
            "timestamp",
            "event_type",
            "actor_ref",
            "payload_hash",
            "previous_hash",
            "state_hash",
            "event_hash",
        }
        required_event_fields = event_fields - {"state_hash"}
        _reject_unknown_fields(event, event_fields, location, errors)
        for key in required_event_fields:
            if key not in event:
                errors.append(f"{location}: missing {key}")
        for key in ("event_id", "actor_ref"):
            if not isinstance(event.get(key), str) or not ID_PATTERN.fullmatch(event.get(key, "")):
                errors.append(f"{location}.{key}: invalid identifier")
        if not _valid_datetime(event.get("timestamp")):
            errors.append(f"{location}.timestamp: invalid datetime")
        for key in ("payload_hash", "event_hash"):
            if not isinstance(event.get(key), str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", event.get(key, "")
            ):
                errors.append(f"{location}.{key}: invalid sha256 digest")
        if event.get("state_hash") is not None and (
            not isinstance(event.get("state_hash"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", event.get("state_hash", ""))
        ):
            errors.append(f"{location}.state_hash: invalid sha256 digest")
        if event.get("previous_hash") is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(event.get("previous_hash"))
        ):
            errors.append(f"{location}.previous_hash: invalid sha256 digest")
    errors.extend(verify_audit_chain(data))
    return errors


def score_objective(
    question: dict[str, Any],
    response: dict[str, Any],
) -> tuple[Decimal | None, float | None, int | None, list[str]]:
    return ObjectiveGradingService(normalize_text, to_decimal).score(
        question,
        response,
    ).as_tuple()


def _ensure_error_evidence(question: dict[str, Any], response: dict[str, Any]) -> None:
    if response.get("is_correct") is not False:
        return
    evidence = response.setdefault("evidence", [])
    if not evidence:
        evidence_id = f"ev-{safe_id(response.get('question_id'), 'q')}-auto"
        evidence.append(
            {
                "evidence_id": evidence_id,
                "observed": f"作答：{normalize_text(response.get('normalized_answer')) or '[未作答]'}",
                "explanation": "与参考答案或评分规则不一致。",
                "causal_role": "causal",
                "step": response.get("first_error_step"),
                "confidence": response.get("confidence", {}).get("grading"),
                "source": response.get("source"),
            }
        )
    if not response.get("error_tags"):
        tag_name = "unanswered" if normalize_text(response.get("normalized_answer")) == "" else "unclassified"
        response["error_tags"] = [
            {
                "dimension": "error",
                "name": tag_name,
                "confidence": response.get("confidence", {}).get("tagging"),
                "evidence_ids": [evidence[0]["evidence_id"]],
            }
        ]


def refresh_review_queue(data: dict[str, Any], created_at: str | None = None) -> None:
    existing = {
        (item.get("attempt_id"), item.get("question_id")): item
        for item in data.get("review_queue", [])
        if isinstance(item, dict)
    }
    queue = []
    timestamp = created_at or utc_now()
    question_types = {
        question.get("question_id"): question.get("question_type")
        for question in data.get("paper", {}).get("questions", [])
        if isinstance(question, dict)
    }
    for attempt in data.get("attempts", []):
        for response in attempt.get("responses", []):
            status = response.get("review_status")
            is_subjective = question_types.get(response.get("question_id")) == "subjective"
            if status not in {"needs_review", "provisional"} and not (
                is_subjective and status != "teacher_confirmed"
            ):
                continue
            key = (attempt.get("attempt_id"), response.get("question_id"))
            old = existing.get(key, {})
            reasons = list(dict.fromkeys(response.get("review_reasons") or ["teacher_confirmation_required"]))
            queue.append(
                {
                    "review_id": old.get(
                        "review_id",
                        f"review-{safe_id(key[0], 'attempt')}-{safe_id(key[1], 'question')}",
                    ),
                    "attempt_id": key[0],
                    "question_id": key[1],
                    "reasons": reasons,
                    "status": "open",
                    "created_at": old.get("created_at", timestamp),
                }
            )
    data["review_queue"] = queue


def analyze_document(data: dict[str, Any]) -> dict[str, Any]:
    result = migrate_v1(data) if data.get("schema_version") != SCHEMA_VERSION else copy.deepcopy(data)
    questions = {question["question_id"]: question for question in result["paper"]["questions"]}
    changed = []
    for attempt in result.get("attempts", []):
        for response in attempt.get("responses", []):
            question = questions.get(response.get("question_id"))
            if not question:
                continue
            if response.get("review_status") == "teacher_confirmed":
                continue
            response.setdefault("confidence", {"ocr": None, "grading": None, "tagging": None})
            response.setdefault("review_reasons", [])
            response.setdefault("rubric_results", [])
            response.setdefault("evidence", [])
            response.setdefault("error_tags", [])
            response.setdefault("suggested_tags", [])
            response.setdefault("source", None)
            response.setdefault("teacher_review", None)

            reasons: list[str] = [
                reason
                for reason in response.get("review_reasons", [])
                if isinstance(reason, str) and reason
            ]
            question_type = question.get("question_type")
            if question_type in OBJECTIVE_TYPES:
                score, grading_confidence, first_error_step, score_reasons = score_objective(question, response)
                causal_steps = [
                    item.get("step")
                    for item in response.get("evidence", [])
                    if isinstance(item, dict)
                    and item.get("causal_role") == "causal"
                    and isinstance(item.get("step"), int)
                    and item.get("step") > 0
                ]
                if causal_steps:
                    first_error_step = min(causal_steps)
                elif isinstance(response.get("first_error_step"), int):
                    first_error_step = response["first_error_step"]
                response["score"] = decimal_json(score) if score is not None else None
                response["is_correct"] = (
                    score == (to_decimal(question.get("max_score")) or Decimal("0"))
                    if score is not None
                    else None
                )
                response["first_error_step"] = first_error_step
                response["confidence"]["grading"] = grading_confidence
                reasons.extend(score_reasons)
            else:
                rubric_results = response.get("rubric_results", [])
                suggested_scores = [
                    to_decimal(item.get("awarded_score"))
                    for item in rubric_results
                    if isinstance(item, dict) and item.get("awarded_score") is not None
                ]
                if suggested_scores:
                    score = sum((item for item in suggested_scores if item is not None), Decimal("0"))
                    response["score"] = decimal_json(score)
                    response["is_correct"] = score == (to_decimal(question.get("max_score")) or Decimal("0"))
                    response["confidence"]["grading"] = response["confidence"].get("grading") or 0.75
                else:
                    response["score"] = None
                    response["is_correct"] = None
                    response["confidence"]["grading"] = None
                reasons.append("subjective_teacher_confirmation_required")

            confidences = response["confidence"]
            if confidences.get("ocr") is None:
                reasons.append("ocr_confidence_missing")
            if response.get("is_correct") is False and confidences.get("tagging") is None:
                reasons.append("tagging_confidence_missing")
            low_dimensions = [
                key for key, value in confidences.items() if value is not None and value < 0.70
            ]
            reasons.extend(f"low_{key}_confidence" for key in low_dimensions)
            reasons = list(dict.fromkeys(reasons))
            response["review_reasons"] = reasons
            blocking_reasons = [
                reason
                for reason in reasons
                if reason not in INFORMATIONAL_REVIEW_REASONS
            ]
            mandatory_review = bool(
                MANDATORY_REVIEW_REASONS.intersection(reasons)
                or MANDATORY_REVIEW_TAGS.intersection(
                    {
                        tag.get("name")
                        for tag in response.get("error_tags", [])
                        if isinstance(tag, dict)
                    }
                )
            )

            if (
                mandatory_review
                or low_dimensions
                or "numeric_parse_failed" in reasons
            ):
                response["review_status"] = "needs_review"
            elif question_type == "subjective":
                response["review_status"] = "provisional"
            elif (
                confidences.get("ocr") is not None
                and confidences["ocr"] >= 0.90
                and confidences.get("grading") is not None
                and confidences["grading"] >= 0.90
                and not blocking_reasons
            ):
                response["review_status"] = "auto_confirmed"
            else:
                response["review_status"] = "provisional"
            _ensure_error_evidence(question, response)
            changed.append(
                {
                    "attempt_id": attempt.get("attempt_id"),
                    "question_id": response.get("question_id"),
                    "status": response.get("review_status"),
                }
            )
    refresh_review_queue(result)
    append_audit_event(result, "analysis.scored", {"responses": changed})
    return result


def _iter_joined(data: dict[str, Any]):
    questions = {question["question_id"]: question for question in data["paper"]["questions"]}
    for attempt in data.get("attempts", []):
        for response in attempt.get("responses", []):
            question = questions.get(response.get("question_id"))
            if question:
                yield attempt, question, response


def compute_statistics(data: dict[str, Any]) -> dict[str, Any]:
    total_responses = 0
    scored = 0
    incorrect = 0
    earned = Decimal("0")
    possible = Decimal("0")
    difficulty = Counter()
    distributions = {"knowledge": Counter(), "cognitive": Counter(), "error": Counter()}
    combinations = Counter()
    class_stats: dict[str, Counter] = defaultdict(Counter)

    for attempt, question, response in _iter_joined(data):
        total_responses += 1
        difficulty[question.get("difficulty", "unknown")] += 1
        class_key = attempt.get("class_id") or "unassigned"
        class_stats[class_key]["responses"] += 1
        score = to_decimal(response.get("score"))
        maximum = to_decimal(question.get("max_score")) or Decimal("0")
        if score is not None:
            scored += 1
            earned += score
            possible += maximum
            class_stats[class_key]["scored"] += 1
            if response.get("is_correct") is False:
                incorrect += 1
                class_stats[class_key]["incorrect"] += 1
                for tag in question.get("tags", []):
                    if tag.get("dimension") in {"knowledge", "cognitive"}:
                        distributions[tag["dimension"]][tag.get("name", "unknown")] += 1
                error_names = sorted(
                    {
                        tag.get("name")
                        for tag in response.get("error_tags", [])
                        if tag.get("dimension") == "error" and tag.get("name")
                    }
                )
                for name in error_names:
                    distributions["error"][name] += 1
                for pair in itertools.combinations(error_names, 2):
                    combinations[" + ".join(pair)] += 1

    distribution_output = {}
    for dimension, counter in distributions.items():
        distribution_output[dimension] = {
            name: {
                "count": count,
                "denominator": incorrect,
                "percentage": percentage(count, incorrect),
            }
            for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        }
    return {
        "analysis_id": data.get("analysis_id"),
        "total_responses": total_responses,
        "scored_responses": scored,
        "unscored_responses": total_responses - scored,
        "incorrect_responses": incorrect,
        "incorrect_rate": {
            "value": percentage(incorrect, scored),
            "numerator": incorrect,
            "denominator": scored,
        },
        "earned_score": decimal_json(earned),
        "possible_score": decimal_json(possible),
        "score_rate": {
            "value": percentage(earned, possible),
            "numerator": decimal_json(earned),
            "denominator": decimal_json(possible),
        },
        "difficulty_distribution": dict(sorted(difficulty.items())),
        "tag_distribution": distribution_output,
        "error_combinations": [
            {"tags": name.split(" + "), "count": count, "denominator": incorrect}
            for name, count in combinations.most_common()
        ],
        "review_queue_count": len([item for item in data.get("review_queue", []) if item.get("status") == "open"]),
        "class_summary": {
            key: dict(value) for key, value in sorted(class_stats.items())
        },
        "multi_tag_note": "标签以错误作答数为分母，多标签比例之和可能超过100%。",
    }


def _tag_map(tags: list[dict[str, Any]], dimension: str) -> dict[str, float]:
    result = {}
    for tag in tags:
        if tag.get("dimension") == dimension and tag.get("name"):
            confidence = tag.get("confidence")
            result[tag["name"]] = float(confidence) if confidence is not None else 1.0
    return result


def weighted_jaccard(left: dict[str, float], right: dict[str, float]) -> float | None:
    keys = set(left) | set(right)
    if not keys:
        return None
    denominator = sum(max(left.get(key, 0.0), right.get(key, 0.0)) for key in keys)
    if denominator == 0:
        return 0.0
    return sum(min(left.get(key, 0.0), right.get(key, 0.0)) for key in keys) / denominator


def cosine_similarity(left: Any, right: Any) -> float | None:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right) or not left:
        return None
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) ** 2 for a in left))
    right_norm = math.sqrt(sum(float(b) ** 2 for b in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def build_graph(data: dict[str, Any], threshold: float = 0.2, index_version: str = "unindexed") -> dict[str, Any]:
    question_stats: dict[str, dict[str, Any]] = {}
    for question in data["paper"]["questions"]:
        question_stats[question["question_id"]] = {
            "question": question,
            "responses": 0,
            "scored": 0,
            "lost_score": Decimal("0"),
            "correct": 0,
            "review": 0,
            "error_tags": {},
        }
    for _, question, response in _iter_joined(data):
        stats = question_stats[question["question_id"]]
        stats["responses"] += 1
        score = to_decimal(response.get("score"))
        maximum = to_decimal(question.get("max_score")) or Decimal("0")
        if score is not None:
            stats["scored"] += 1
            stats["lost_score"] += maximum - score
        if response.get("is_correct") is True:
            stats["correct"] += 1
        if response.get("review_status") in {"needs_review", "provisional"}:
            stats["review"] += 1
        for tag in response.get("error_tags", []):
            if tag.get("name"):
                current = stats["error_tags"].get(tag["name"], 0.0)
                confidence = float(tag.get("confidence") if tag.get("confidence") is not None else 1.0)
                stats["error_tags"][tag["name"]] = max(current, confidence)

    nodes = []
    relation_items = []
    all_groups = set()
    for question_id, stats in sorted(question_stats.items()):
        question = stats["question"]
        knowledge = _tag_map(question.get("tags", []), "knowledge")
        cognitive = _tag_map(question.get("tags", []), "cognitive")
        primary = max(knowledge, key=knowledge.get) if knowledge else "unclassified"
        all_groups.add(primary)
        responses = stats["responses"]
        scored_responses = stats["scored"]
        nodes.append(
            {
                "id": question_id,
                "label": question_id,
                "group": primary,
                "size": max(8, 8 + float(stats["lost_score"]) * 2),
                "response_count": responses,
                "scored_response_count": scored_responses,
                "correct_rate": (
                    percentage(stats["correct"], scored_responses)
                    if scored_responses
                    else None
                ),
                "review_count": stats["review"],
                "lost_score": decimal_json(stats["lost_score"]),
                "tags": question.get("tags", []),
            }
        )
        relation_items.append(
            {
                "id": question_id,
                "knowledge": knowledge,
                "cognitive": cognitive,
                "error": stats["error_tags"],
            }
        )
    edges = []
    weights = {"knowledge": 0.55, "cognitive": 0.28, "error": 0.17}
    for left, right in itertools.combinations(relation_items, 2):
        values = []
        for dimension in ("knowledge", "cognitive", "error"):
            value = weighted_jaccard(left[dimension], right[dimension])
            if value is not None:
                values.append((dimension, value, weights[dimension]))
        total_weight = sum(item[2] for item in values)
        score = sum(item[1] * item[2] for item in values) / total_weight if total_weight else 0.0
        if score >= threshold:
            edges.append(
                {
                    "source": left["id"],
                    "target": right["id"],
                    "weight": round(score, 4),
                    "components": {name: round(value, 4) for name, value, _ in values},
                }
            )
    layout_seed = int(hashlib.sha256(str(data["analysis_id"]).encode()).hexdigest()[:8], 16)
    return {
        "schema_version": "1.0",
        "analysis_id": data["analysis_id"],
        "aggregation": "anonymous_attempts_by_question",
        "layout_seed": layout_seed,
        "index_version": index_version,
        "nodes": nodes,
        "edges": edges,
        "filters": {
            "group": sorted(all_groups),
            "correctness": ["all", "has_errors", "fully_correct"],
            "review": ["all", "needs_review", "confirmed"],
        },
        "legend": {
            "color": {"field": "group", "label": "主要知识点"},
            "size": {"field": "lost_score", "label": "累计失分"},
            "edge": {"field": "weight", "label": "题目关系相似度"},
        },
    }


def export_review_queue(data: dict[str, Any]) -> dict[str, Any]:
    lookup = {
        (attempt["attempt_id"], response["question_id"]): (attempt, response)
        for attempt in data.get("attempts", [])
        for response in attempt.get("responses", [])
    }
    items = []
    for review in data.get("review_queue", []):
        pair = (review.get("attempt_id"), review.get("question_id"))
        attempt, response = lookup.get(pair, ({}, {}))
        items.append(
            {
                **review,
                "student_ref": attempt.get("student_ref"),
                "student_name": attempt.get("student_name"),
                "answer": response.get("normalized_answer"),
                "score": response.get("score"),
                "confidence": response.get("confidence"),
                "evidence": response.get("evidence", []),
            }
        )
    return {
        "organization_id": data.get("organization_id"),
        "analysis_id": data.get("analysis_id"),
        "document_state_hash": document_state_hash(data),
        "items": items,
    }


def apply_review_decisions(
    data: dict[str, Any], review_document: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(review_document, dict):
        raise ValueError("review decisions must be a bound review document")
    expected_bindings = {
        "organization_id": data.get("organization_id"),
        "analysis_id": data.get("analysis_id"),
        "document_state_hash": document_state_hash(data),
    }
    for field, expected in expected_bindings.items():
        if review_document.get(field) != expected:
            raise ValueError(f"review document {field} does not match the analysis document")
    decisions = review_document.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("review document decisions must be a list")
    result = copy.deepcopy(data)
    lookup = {
        (attempt["attempt_id"], response["question_id"]): response
        for attempt in result.get("attempts", [])
        for response in attempt.get("responses", [])
    }
    questions = {question["question_id"]: question for question in result["paper"]["questions"]}
    open_review_pairs = {
        (item.get("attempt_id"), item.get("question_id"))
        for item in result.get("review_queue", [])
        if isinstance(item, dict) and item.get("status") == "open"
    }
    applied = []
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise ValueError(f"decisions[{index}] must be an object")
        pair = (decision.get("attempt_id"), decision.get("question_id"))
        if pair not in open_review_pairs:
            raise ValueError(
                f"response {pair[0]}/{pair[1]} is not in the open review queue"
            )
        response = lookup.get(pair)
        if response is None:
            raise ValueError(f"unknown response {pair[0]}/{pair[1]}")
        if decision.get("decision") not in {"confirm", "modify", "reject"}:
            raise ValueError(f"decisions[{index}].decision is invalid")
        if not decision.get("reason"):
            raise ValueError(f"decisions[{index}].reason is required")
        actor_ref = safe_id(decision.get("actor_ref"), "")
        if not actor_ref:
            raise ValueError(f"decisions[{index}].actor_ref is invalid")
        before = {
            "score": response.get("score"),
            "is_correct": response.get("is_correct"),
            "error_tags": response.get("error_tags"),
            "review_status": response.get("review_status"),
        }
        if decision["decision"] == "modify":
            if "score" in decision:
                score = to_decimal(decision.get("score"))
                maximum = to_decimal(questions[pair[1]]["max_score"]) or Decimal("0")
                if score is None or score < 0 or score > maximum:
                    raise ValueError(f"decisions[{index}].score is invalid")
                response["score"] = decimal_json(score)
                response["is_correct"] = score == maximum
            if "error_tags" in decision:
                response["error_tags"] = decision["error_tags"]
        response["review_status"] = (
            "rejected" if decision["decision"] == "reject" else "teacher_confirmed"
        )
        after = {
            "score": response.get("score"),
            "is_correct": response.get("is_correct"),
            "error_tags": response.get("error_tags"),
            "review_status": response.get("review_status"),
        }
        response["teacher_review"] = {
            "actor_ref": actor_ref,
            "reviewed_at": decision.get("reviewed_at") or utc_now(),
            "decision": decision["decision"],
            "reason": decision["reason"],
            "before": before,
            "after": after,
        }
        applied.append({"attempt_id": pair[0], "question_id": pair[1], "before": before, "after": after})
    refresh_review_queue(result)
    append_audit_event(
        result,
        "review.applied",
        {"decisions": applied},
        actor_ref=safe_id(decisions[0].get("actor_ref") if decisions else None, "system"),
    )
    return result
