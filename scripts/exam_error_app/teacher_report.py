"""Application service that composes analysis, projection and rendering ports."""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import (
    AnalysisRunner,
    JsonObject,
    TeacherReportArtifact,
    TeacherReportProjector,
    TeacherReportRenderer,
)


@dataclass(frozen=True)
class TeacherReportApplication:
    """Generate a report without knowing templates, files, CLI or domain adapters."""

    analysis_pipeline: AnalysisRunner
    projector: TeacherReportProjector
    renderer: TeacherReportRenderer

    def generate(self, document: JsonObject) -> TeacherReportArtifact:
        analyzed = self.analysis_pipeline.run(document).document
        model = self.projector(analyzed)
        html = self.renderer(model)
        return TeacherReportArtifact(model=model, html=html)
