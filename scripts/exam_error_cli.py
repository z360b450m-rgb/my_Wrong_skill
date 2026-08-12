#!/usr/bin/env python3
"""Generate one audited teacher-facing HTML report from an exam JSON input."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from exam_error_core import SCHEMA_VERSION, analyze_document, compute_statistics, export_review_queue, migrate_v1, validate_v2, verify_audit_chain
from resource_limits import read_json_bounded
from teacher_report_projection import build_teacher_report_model
from teacher_report_renderer import render_teacher_report_html, write_teacher_report_html
from taxonomy_registry import apply_teacher_decisions, approved_labels, candidates_from_document, load_registry, upsert_pending_candidates


ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "taxonomy" / "extensions.json"


def generate_teacher_report(input_path: str | Path, output_path: str | Path, taxonomy_path: str | Path = DEFAULT_TAXONOMY_PATH) -> Path:
    document = read_json_bounded(input_path)
    if not isinstance(document, dict):
        raise ValueError("输入必须是考试 JSON 对象")
    registry = load_registry(taxonomy_path)
    extension_error_names = set(approved_labels(registry)["error"])
    migrated = migrate_v1(document) if document.get("schema_version") != SCHEMA_VERSION else document
    errors = validate_v2(migrated, extension_error_names)
    if errors:
        raise ValueError("输入数据校验失败：\n" + "\n".join(errors))
    analyzed = analyze_document(migrated)
    errors = validate_v2(analyzed, extension_error_names) + verify_audit_chain(analyzed)
    if errors:
        raise ValueError("分析结果校验失败：\n" + "\n".join(errors))
    registry, pending_candidates = upsert_pending_candidates(taxonomy_path, candidates_from_document(analyzed))
    model = build_teacher_report_model(
        analyzed,
        statistics_builder=compute_statistics,
        review_exporter=export_review_queue,
        taxonomy_labels=approved_labels(registry),
        taxonomy_candidates=pending_candidates,
    )
    html = render_teacher_report_html(model, ASSETS_DIR / "teacher-report.html")
    return write_teacher_report_html(html, output_path)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "taxonomy":
        parser = argparse.ArgumentParser(description="Apply teacher decisions to the local extension taxonomy.")
        parser.add_argument("action", choices=["apply"])
        parser.add_argument("decisions", help="teacher decisions JSON exported from the HTML report")
        parser.add_argument("--taxonomy", dest="taxonomy_path", default=str(DEFAULT_TAXONOMY_PATH), help="writable extensions.json path")
        args = parser.parse_args(argv[1:])
        try:
            result = apply_teacher_decisions(args.taxonomy_path, read_json_bounded(args.decisions))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description="Generate one self-contained teacher HTML report from a v1 or v2 exam JSON file.")
    parser.add_argument("input", help="v1 or v2 exam analysis JSON")
    parser.add_argument("output", help="output .html path")
    parser.add_argument("--taxonomy", dest="taxonomy_path", default=str(DEFAULT_TAXONOMY_PATH), help="writable extensions.json path")
    args = parser.parse_args(argv)
    try:
        print(generate_teacher_report(args.input, args.output, args.taxonomy_path))
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
