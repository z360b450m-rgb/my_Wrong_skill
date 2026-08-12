"""Pure mapping from analysis documents to storage-neutral index records."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterator

from .contracts import IndexRecord, JsonObject


def _record_id(analysis_id: str, attempt_id: str, question_id: str) -> str:
    raw = f"{analysis_id}\0{attempt_id}\0{question_id}".encode("utf-8")
    return "rec-" + hashlib.sha256(raw).hexdigest()[:32]


def iter_index_records(
    data: JsonObject,
    *,
    normalize_text: Callable[[Any], str],
    canonical_json: Callable[[Any], str],
    tag_aliases: dict[str, str] | None = None,
) -> Iterator[IndexRecord]:
    aliases = tag_aliases or {}
    paper = data["paper"]
    questions = {question["question_id"]: question for question in paper["questions"]}
    for attempt in data.get("attempts", []):
        for response in attempt.get("responses", []):
            question = questions.get(response.get("question_id"))
            if not question:
                continue
            evidence_text = " ".join(
                f"{item.get('observed', '')} {item.get('explanation', '')}"
                for item in response.get("evidence", [])
                if isinstance(item, dict)
            )
            error_names = [
                tag.get("name", "")
                for tag in response.get("error_tags", [])
                if isinstance(tag, dict)
            ]
            answer_text = normalize_text(response.get("normalized_answer"))
            reference_text = normalize_text(question.get("reference_answer"))
            content = " ".join(
                part
                for part in [
                    question.get("question_text") or "",
                    answer_text,
                    reference_text,
                    evidence_text,
                    " ".join(f"{name} {aliases.get(name, '')}" for name in error_names),
                    " ".join(
                        f"{tag.get('name', '')} {aliases.get(tag.get('name'), '')}"
                        for tag in question.get("tags", [])
                    ),
                ]
                if part
            )
            tags = [
                tag
                for tag in question.get("tags", []) + response.get("error_tags", [])
                if isinstance(tag, dict) and tag.get("dimension") and tag.get("name")
            ]
            source_refs = [
                source
                for source in [
                    question.get("source"),
                    response.get("source"),
                    *[
                        item.get("source")
                        for item in response.get("evidence", [])
                        if isinstance(item, dict)
                    ],
                ]
                if source
            ]
            yield IndexRecord(
                    record_id=_record_id(
                        data["analysis_id"], attempt["attempt_id"], question["question_id"]
                    ),
                    organization_id=data["organization_id"],
                    analysis_id=data["analysis_id"],
                    paper_id=paper["paper_id"],
                    class_id=attempt.get("class_id"),
                    student_ref=attempt["student_ref"],
                    student_name=attempt.get("student_name"),
                    attempt_id=attempt["attempt_id"],
                    question_id=question["question_id"],
                    subject=paper["subject"],
                    grade=paper.get("grade"),
                    curriculum_version=paper.get("curriculum_version"),
                    question_type=question["question_type"],
                    question_text=question.get("question_text"),
                    answer_text=answer_text,
                    reference_answer=reference_text,
                    error_text=evidence_text,
                    review_status=response.get("review_status", "unreviewed"),
                    score=response.get("score"),
                    max_score=question["max_score"],
                    event_date=attempt.get("submitted_at"),
                    evidence_json=canonical_json(response.get("evidence", [])),
                    source_refs_json=canonical_json(source_refs),
                    content_text=content,
                    tags=tags,
                    embedding=question.get("semantic_embedding"),
                    embedding_model_fingerprint=question.get("embedding_model_fingerprint"),
            )


def project_index_records(
    data: JsonObject,
    *,
    normalize_text: Callable[[Any], str],
    canonical_json: Callable[[Any], str],
    tag_aliases: dict[str, str] | None = None,
) -> list[IndexRecord]:
    return list(
        iter_index_records(
            data,
            normalize_text=normalize_text,
            canonical_json=canonical_json,
            tag_aliases=tag_aliases,
        )
    )
