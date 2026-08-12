#!/usr/bin/env python3
"""Compatibility facade for the decoupled teacher-report components."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from teacher_report_projection import build_teacher_report_model
from teacher_report_renderer import (
    build_teacher_report_manifest,
    render_teacher_report_html,
    write_teacher_report_html,
)


def write_teacher_report(
    data: dict[str, Any],
    output_path: str | Path,
    template_path: str | Path,
    *,
    statistics_builder: Callable[[dict[str, Any]], dict[str, Any]],
    review_exporter: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    """Preserve the legacy call while delegating to projection and rendering ports."""
    model = build_teacher_report_model(
        data,
        statistics_builder=statistics_builder,
        review_exporter=review_exporter,
    )
    html = render_teacher_report_html(model, template_path)
    return write_teacher_report_html(html, output_path)


__all__ = [
    "build_teacher_report_manifest",
    "build_teacher_report_model",
    "render_teacher_report_html",
    "write_teacher_report",
    "write_teacher_report_html",
]
