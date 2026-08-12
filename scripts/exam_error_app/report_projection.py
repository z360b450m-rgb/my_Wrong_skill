"""Convert the domain document into a renderer-only view model."""

from __future__ import annotations

from .contracts import (
    GraphBuilder,
    JsonObject,
    RepresentativeError,
    ReportViewModel,
    ReviewExporter,
    StatisticsBuilder,
)


def build_report_view(
    document: JsonObject,
    *,
    statistics_builder: StatisticsBuilder,
    graph_builder: GraphBuilder,
    review_exporter: ReviewExporter,
    index_version: str = "unindexed",
    representative_limit: int = 8,
) -> ReportViewModel:
    representatives: list[RepresentativeError] = []
    for attempt in document.get("attempts", []):
        for response in attempt.get("responses", []):
            if response.get("is_correct") is not False:
                continue
            evidence = response.get("evidence", [])
            first = evidence[0] if evidence and isinstance(evidence[0], dict) else {}
            representatives.append(
                RepresentativeError(
                    student_ref=attempt.get("student_ref"),
                    question_id=response.get("question_id"),
                    observed=first.get("observed") or "无可用证据",
                    explanation=first.get("explanation") or "",
                )
            )
            if len(representatives) >= representative_limit:
                break
        if len(representatives) >= representative_limit:
            break

    review = review_exporter(document)
    return ReportViewModel(
        organization_id=review.get("organization_id") or document.get("organization_id"),
        analysis_id=document.get("analysis_id"),
        document_state_hash=review.get("document_state_hash"),
        statistics=statistics_builder(document),
        graph=graph_builder(document, index_version=index_version),
        review_items=tuple(review.get("items", [])),
        representative_errors=tuple(representatives),
    )
