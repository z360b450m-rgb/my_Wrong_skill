"""Stable contracts shared by orchestration, reporting and retrieval adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypedDict


JsonObject = dict[str, Any]
ValidationErrors = list[str]


class Validator(Protocol):
    def __call__(self, document: Any) -> ValidationErrors: ...


class DocumentTransform(Protocol):
    def __call__(self, document: JsonObject) -> JsonObject: ...


class AuditVerifier(Protocol):
    def __call__(self, document: JsonObject) -> ValidationErrors: ...


class StatisticsBuilder(Protocol):
    def __call__(self, document: JsonObject) -> JsonObject: ...


class GraphBuilder(Protocol):
    def __call__(
        self,
        document: JsonObject,
        threshold: float = 0.2,
        index_version: str = "unindexed",
    ) -> JsonObject: ...


class ReviewExporter(Protocol):
    def __call__(self, document: JsonObject) -> JsonObject: ...


class TeacherReportProjector(Protocol):
    def __call__(self, document: JsonObject) -> JsonObject: ...


class TeacherReportRenderer(Protocol):
    def __call__(self, model: JsonObject) -> str: ...


@dataclass(frozen=True)
class AnalysisRun:
    """Result returned by the application pipeline, independent of CLI/file I/O."""

    document: JsonObject
    statistics: JsonObject
    graph: JsonObject
    audit_errors: tuple[str, ...]


class AnalysisRunner(Protocol):
    def run(self, document: JsonObject) -> AnalysisRun: ...


@dataclass(frozen=True)
class TeacherReportArtifact:
    """In-memory teacher report; file output remains an outer adapter concern."""

    model: JsonObject
    html: str


@dataclass(frozen=True)
class RepresentativeError:
    student_ref: str | None
    question_id: str | None
    observed: str
    explanation: str


@dataclass(frozen=True)
class ReportViewModel:
    """Renderer-facing projection; renderers do not calculate domain values."""

    organization_id: str | None
    analysis_id: str | None
    document_state_hash: str | None
    statistics: JsonObject
    graph: JsonObject
    review_items: tuple[JsonObject, ...]
    representative_errors: tuple[RepresentativeError, ...]


class IndexRecord(TypedDict):
    """Storage-neutral record emitted by the analysis-to-index projection."""

    record_id: str
    organization_id: str
    analysis_id: str
    paper_id: str
    class_id: str | None
    student_ref: str
    student_name: str | None
    attempt_id: str
    question_id: str
    subject: str
    grade: str | None
    curriculum_version: str | None
    question_type: str
    question_text: str | None
    answer_text: str
    reference_answer: str
    error_text: str
    review_status: str
    score: int | float | None
    max_score: int | float
    event_date: str | None
    evidence_json: str
    source_refs_json: str
    content_text: str
    tags: list[JsonObject]


IndexProjector = Callable[[JsonObject], list[IndexRecord]]
