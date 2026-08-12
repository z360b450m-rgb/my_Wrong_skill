#!/usr/bin/env python3
"""Unified command-line interface for the analyze-exam-errors skill."""

from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path
from typing import Any

from check_runtime import collect_runtime_capabilities
from exam_error_app.pipeline import AnalysisPipeline
from exam_error_app.teacher_report import TeacherReportApplication
from exam_error_core import (
    SCHEMA_VERSION,
    analyze_document,
    apply_review_decisions,
    build_graph,
    compute_statistics,
    export_review_queue,
    migrate_v1,
    recompute_audit_chain,
    validate_v2,
    verify_audit_chain,
)
from reporting import write_report_bundle
from teacher_report_projection import build_teacher_report_model
from teacher_report_renderer import (
    build_teacher_report_manifest,
    render_teacher_report_html,
    write_teacher_report_html,
)
from retrieval_engine import benchmark, index_document, purge_records, search_index, verify_database_audit
from resource_limits import read_json_bounded, write_json_spooled


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSETS_DIR = SKILL_DIR / "assets"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def read_json(path: str | Path) -> Any:
    return read_json_bounded(path)


def write_json(path: str | Path | None, value: Any) -> None:
    write_json_spooled(value, path)


def require_valid(data: Any) -> None:
    errors = validate_v2(data)
    if errors:
        raise ValueError("schema validation failed:\n" + "\n".join(errors))


def default_pipeline() -> AnalysisPipeline:
    return AnalysisPipeline(
        validate=validate_v2,
        migrate=migrate_v1,
        analyze=analyze_document,
        verify_audit=verify_audit_chain,
        build_statistics=compute_statistics,
        build_graph=build_graph,
        schema_version=SCHEMA_VERSION,
    )


def parse_filters(args: argparse.Namespace) -> dict[str, Any]:
    if args.filters and args.filters_file:
        raise ValueError("use only one of --filters and --filters-file")
    if args.filters_file:
        value = read_json(args.filters_file)
    elif args.filters:
        if len(args.filters) > 65_536:
            raise ValueError("inline filters exceed 65536 characters")
        value = json.loads(args.filters)
    else:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("filters must be a JSON object")
    return value


def cmd_validate(args: argparse.Namespace) -> int:
    data = read_json(args.input)
    errors = validate_v2(data)
    result = {
        "valid": not errors,
        "schema_version": data.get("schema_version") if isinstance(data, dict) else None,
        "errors": errors,
    }
    write_json(args.output, result)
    return 0 if not errors else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    migrated = migrate_v1(read_json(args.input))
    require_valid(migrated)
    write_json(args.output, migrated)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    analyzed = analyze_document(read_json(args.input))
    require_valid(analyzed)
    write_json(args.output, analyzed)
    return 0


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Run the ordered workflow through explicit, replaceable domain ports."""
    result = default_pipeline().run(
        read_json(args.input),
        graph_threshold=args.threshold,
        index_version=args.index_version,
    )
    write_json(args.output, result.document)
    if args.statistics:
        write_json(args.statistics, result.statistics)
    if args.graph:
        write_json(args.graph, result.graph)
    return 0


def cmd_statistics(args: argparse.Namespace) -> int:
    data = read_json(args.input)
    require_valid(data)
    write_json(args.output, compute_statistics(data))
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    data = read_json(args.input)
    require_valid(data)
    write_json(
        args.output,
        build_graph(data, threshold=args.threshold, index_version=args.index_version),
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    data = read_json(args.input)
    require_valid(data)
    result = write_report_bundle(
        data,
        args.output_dir,
        ASSETS_DIR,
        report_kind=args.kind,
        output_format=args.format,
        index_version=args.index_version,
    )
    write_json(args.manifest, result)
    return 0


def cmd_teacher_report(args: argparse.Namespace) -> int:
    template_path = ASSETS_DIR / "teacher-report.html"
    application = TeacherReportApplication(
        analysis_pipeline=default_pipeline(),
        projector=partial(
            build_teacher_report_model,
            statistics_builder=compute_statistics,
            review_exporter=export_review_queue,
        ),
        renderer=partial(
            render_teacher_report_html,
            template_path=template_path,
        ),
    )
    artifact = application.generate(read_json(args.input))
    path = write_teacher_report_html(artifact.html, args.output)
    write_json(args.manifest, build_teacher_report_manifest(artifact.html, path))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    data = read_json(args.input)
    require_valid(data)
    result = index_document(
        data,
        args.database,
        mode=args.action,
        memory_threshold_mb=args.memory_threshold_mb,
        spill_directory=args.spill_dir,
    )
    write_json(args.output, result)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    result = search_index(
        args.database,
        args.query,
        filters=parse_filters(args),
        top_k=args.top_k,
        candidate_limit=args.candidate_limit,
    )
    write_json(args.output, result)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    data = read_json(args.input)
    require_valid(data)
    if args.action == "export":
        write_json(args.output, export_review_queue(data))
    else:
        review_document = read_json(args.decisions)
        if not isinstance(review_document, dict):
            raise ValueError("review decisions must be a bound review document")
        decisions = review_document.get("decisions")
        if not isinstance(decisions, list):
            raise ValueError("review document decisions must be a list")
        for decision in decisions:
            if isinstance(decision, dict):
                decision["actor_ref"] = args.actor_ref
        result = apply_review_decisions(data, review_document)
        require_valid(result)
        write_json(args.output, result)
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    result = purge_records(
        args.database,
        student_ref=args.student_ref,
        attempt_id=args.attempt_id,
    )
    write_json(args.output, result)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    path = Path(args.target)
    if args.action == "recompute":
        if path.suffix.casefold() != ".json":
            raise ValueError("audit recompute currently supports analysis JSON only")
        repaired = recompute_audit_chain(read_json(path), actor_ref=args.actor_ref)
        require_valid(repaired)
        write_json(args.output, repaired)
        return 0
    if path.suffix.casefold() == ".json":
        data = read_json(path)
        errors = verify_audit_chain(data)
        audit_type = "analysis_json"
    else:
        errors = verify_database_audit(path)
        audit_type = "index_database"
    write_json(
        args.output,
        {"valid": not errors, "audit_type": audit_type, "errors": errors},
    )
    return 0 if not errors else 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    result = benchmark(
        args.records,
        args.queries,
        args.database,
    )
    write_json(args.output, result)
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    result = collect_runtime_capabilities(SKILL_DIR)
    write_json(args.output, result)
    return 0 if result["runtime"]["core"]["ready"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evidence-backed exam error analysis and local hybrid retrieval."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s schema {SCHEMA_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="strictly validate a v2 JSON document")
    validate.add_argument("input")
    validate.add_argument("--output", default="-")
    validate.set_defaults(func=cmd_validate)

    migrate = subparsers.add_parser("migrate", help="migrate v1 JSON to v2")
    migrate.add_argument("input")
    migrate.add_argument("output")
    migrate.set_defaults(func=cmd_migrate)

    analyze = subparsers.add_parser("analyze", help="run deterministic grading and review gates")
    analyze.add_argument("input")
    analyze.add_argument("output")
    analyze.set_defaults(func=cmd_analyze)

    pipeline = subparsers.add_parser(
        "pipeline",
        help="run migrate, validate, analyze, revalidate and audit in a fixed order",
    )
    pipeline.add_argument("input")
    pipeline.add_argument("output")
    pipeline.add_argument("--statistics")
    pipeline.add_argument("--graph")
    pipeline.add_argument("--threshold", type=float, default=0.2)
    pipeline.add_argument("--index-version", default="unindexed")
    pipeline.set_defaults(func=cmd_pipeline)

    statistics_parser = subparsers.add_parser("statistics", help="compute audited statistics")
    statistics_parser.add_argument("input")
    statistics_parser.add_argument("output")
    statistics_parser.set_defaults(func=cmd_statistics)

    graph = subparsers.add_parser("graph", help="build relationship graph JSON")
    graph.add_argument("input")
    graph.add_argument("output")
    graph.add_argument("--threshold", type=float, default=0.2)
    graph.add_argument("--index-version", default="unindexed")
    graph.set_defaults(func=cmd_graph)

    report = subparsers.add_parser("report", help="render Markdown, HTML and star-map outputs")
    report.add_argument("input")
    report.add_argument("output_dir")
    report.add_argument("--kind", choices=["all", "error", "lesson", "graph"], default="all")
    report.add_argument("--format", choices=["markdown", "html", "both"], default="both")
    report.add_argument("--index-version", default="unindexed")
    report.add_argument("--manifest", default="-")
    report.set_defaults(func=cmd_report)

    teacher_report = subparsers.add_parser(
        "teacher-report",
        help="render one self-contained teacher-facing HTML report",
    )
    teacher_report.add_argument("input")
    teacher_report.add_argument("output")
    teacher_report.add_argument("--manifest", default="-")
    teacher_report.set_defaults(func=cmd_teacher_report)

    index = subparsers.add_parser("index", help="build or update a tenant-isolated index")
    index.add_argument("action", choices=["build", "update", "rebuild"])
    index.add_argument("input")
    index.add_argument("database")
    index.add_argument("--output", default="-")
    index.add_argument("--memory-threshold-mb", type=int, default=None)
    index.add_argument("--spill-dir")
    index.set_defaults(func=cmd_index)

    search = subparsers.add_parser("search", help="search the local hybrid index")
    search.add_argument("database")
    search.add_argument("query")
    search.add_argument("--filters", help="inline JSON object")
    search.add_argument("--filters-file")
    search.add_argument("--top-k", type=int, default=20)
    search.add_argument("--candidate-limit", type=int, default=200)
    search.add_argument("--output", default="-")
    search.set_defaults(func=cmd_search)

    review = subparsers.add_parser("review", help="export or apply teacher review decisions")
    review.add_argument("action", choices=["export", "apply"])
    review.add_argument("input")
    review.add_argument("output")
    review.add_argument("--decisions")
    review.add_argument("--actor-ref")
    review.set_defaults(func=cmd_review)

    purge = subparsers.add_parser("purge", help="purge indexed anonymous student data")
    purge.add_argument("database")
    selector = purge.add_mutually_exclusive_group(required=True)
    selector.add_argument("--student-ref")
    selector.add_argument("--attempt-id")
    purge.add_argument("--output", default="-")
    purge.set_defaults(func=cmd_purge)

    audit = subparsers.add_parser("audit", help="verify an analysis or index audit chain")
    audit.add_argument("action", choices=["verify", "recompute"])
    audit.add_argument("target")
    audit.add_argument("--actor-ref")
    audit.add_argument(
        "--confirm-new-baseline",
        action="store_true",
        help="confirm that audit recompute intentionally establishes a new baseline",
    )
    audit.add_argument("--output", default="-")
    audit.set_defaults(func=cmd_audit)

    bench = subparsers.add_parser("benchmark", help="benchmark FTS5 retrieval")
    bench.add_argument("--records", type=int, default=10000)
    bench.add_argument("--queries", type=int, default=25)
    bench.add_argument("--database")
    bench.add_argument("--output", default="-")
    bench.set_defaults(func=cmd_benchmark)

    capabilities = subparsers.add_parser(
        "capabilities",
        help="report packaged core-runtime capabilities",
    )
    capabilities.add_argument("--output", default="-")
    capabilities.set_defaults(func=cmd_capabilities)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "review" and args.action == "apply" and not args.decisions:
        parser.error("review apply requires --decisions")
    if args.command == "review" and args.action == "apply" and not args.actor_ref:
        parser.error("review apply requires --actor-ref")
    if args.command == "audit" and args.action == "recompute":
        if not args.actor_ref:
            parser.error("audit recompute requires --actor-ref")
        if not args.confirm_new_baseline:
            parser.error("audit recompute requires --confirm-new-baseline")
    try:
        return int(args.func(args))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
