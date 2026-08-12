#!/usr/bin/env python3
"""Generate one audited teacher-facing HTML report from an exam JSON input."""

from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path

from exam_error_app.pipeline import AnalysisPipeline
from exam_error_app.teacher_report import TeacherReportApplication
from exam_error_core import SCHEMA_VERSION, analyze_document, build_graph, compute_statistics, export_review_queue, migrate_v1, validate_v2, verify_audit_chain
from resource_limits import read_json_bounded
from teacher_report_projection import build_teacher_report_model
from teacher_report_renderer import render_teacher_report_html, write_teacher_report_html


ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"


def generate_teacher_report(input_path: str | Path, output_path: str | Path) -> Path:
    pipeline = AnalysisPipeline(
        validate=validate_v2, migrate=migrate_v1, analyze=analyze_document,
        verify_audit=verify_audit_chain, build_statistics=compute_statistics,
        build_graph=build_graph, schema_version=SCHEMA_VERSION,
    )
    application = TeacherReportApplication(
        analysis_pipeline=pipeline,
        projector=partial(build_teacher_report_model, statistics_builder=compute_statistics, review_exporter=export_review_queue),
        renderer=partial(render_teacher_report_html, template_path=ASSETS_DIR / "teacher-report.html"),
    )
    return write_teacher_report_html(application.generate(read_json_bounded(input_path)).html, output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one self-contained teacher HTML report from a v1 or v2 exam JSON file.")
    parser.add_argument("input", help="v1 or v2 exam analysis JSON")
    parser.add_argument("output", help="output .html path")
    args = parser.parse_args(argv)
    try:
        print(generate_teacher_report(args.input, args.output))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
