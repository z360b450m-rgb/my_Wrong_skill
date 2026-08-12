"""Application orchestration with explicit, replaceable domain ports."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    AnalysisRun,
    AuditVerifier,
    DocumentTransform,
    GraphBuilder,
    JsonObject,
    StatisticsBuilder,
    Validator,
)


class PipelineValidationError(ValueError):
    def __init__(self, stage: str, errors: list[str]):
        self.stage = stage
        self.errors = tuple(errors)
        super().__init__(f"{stage} validation failed:\n" + "\n".join(errors))


@dataclass(frozen=True)
class AnalysisPipeline:
    """Coordinates domain services without knowing their implementations or storage."""

    validate: Validator
    migrate: DocumentTransform
    analyze: DocumentTransform
    verify_audit: AuditVerifier
    build_statistics: StatisticsBuilder
    build_graph: GraphBuilder
    schema_version: str = "2.0"

    def run(
        self,
        document: JsonObject,
        *,
        graph_threshold: float = 0.2,
        index_version: str = "unindexed",
    ) -> AnalysisRun:
        migrated = (
            self.migrate(document)
            if document.get("schema_version") != self.schema_version
            else document
        )
        input_errors = self.validate(migrated)
        if input_errors:
            raise PipelineValidationError("input", input_errors)

        analyzed = self.analyze(migrated)
        output_errors = self.validate(analyzed)
        if output_errors:
            raise PipelineValidationError("output", output_errors)

        audit_errors = self.verify_audit(analyzed)
        if audit_errors:
            raise PipelineValidationError("audit", audit_errors)

        return AnalysisRun(
            document=analyzed,
            statistics=self.build_statistics(analyzed),
            graph=self.build_graph(
                analyzed,
                threshold=graph_threshold,
                index_version=index_version,
            ),
            audit_errors=tuple(audit_errors),
        )
