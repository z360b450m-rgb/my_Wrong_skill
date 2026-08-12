#!/usr/bin/env python3
"""Report the capabilities included in the offline core package."""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
from pathlib import Path

from exam_error_core import SCHEMA_VERSION
from resource_limits import DEFAULT_MAX_INPUT_MB, DEFAULT_MEMORY_THRESHOLD_MB

MIN_PYTHON = (3, 10)
MIN_SQLITE = (3, 35, 0)
SERVICE_NAME = "analyze-exam-errors"


def fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def collect_runtime_capabilities(skill_dir: str | Path | None = None) -> dict:
    sqlite_fts5 = fts5_available()
    ready = sqlite_fts5 and sys.version_info >= MIN_PYTHON and sqlite3.sqlite_version_info >= MIN_SQLITE
    return {
        "service": SERVICE_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": "core" if ready else "blocked",
        "runtime": {"python": platform.python_version(), "platform": platform.platform(), "sqlite": sqlite3.sqlite_version, "minimums": {"python": ".".join(map(str, MIN_PYTHON)), "sqlite": ".".join(map(str, MIN_SQLITE))}, "core": {"sqlite_fts5": sqlite_fts5, "ready": ready}},
        "capabilities": {"validate": True, "migrate_v1_to_v2": True, "analyze": True, "safe_pipeline": True, "statistics": True, "graph": True, "report": True, "teacher_report": True, "review_queue": True, "audit_verify": True, "audit_recompute": True, "disk_backed_spill": True, "fts5_search": sqlite_fts5, "tag_relations_search": ready},
        "degraded_components": [],
        "constraints": {"network_default": "disabled", "pii_indexing": False, "cross_org_query": False, "review_document_binding": ["organization_id", "analysis_id", "document_state_hash"], "max_input_mb": DEFAULT_MAX_INPUT_MB, "intermediate_memory_threshold_mb": DEFAULT_MEMORY_THRESHOLD_MB, "spill_scope": "index_records_and_json_outputs"},
        "commands": {"capabilities": "python scripts/exam_error_cli.py capabilities", "pipeline": "python scripts/exam_error_cli.py pipeline <input> <output>", "teacher_report": "python scripts/exam_error_cli.py teacher-report <input> <output.html>", "audit_recompute": "python scripts/exam_error_cli.py audit recompute <input.json> --actor-ref <admin-id> --confirm-new-baseline --output <output.json>"},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report core runtime capabilities for analyze-exam-errors.")
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    payload = json.dumps(collect_runtime_capabilities(), ensure_ascii=False, indent=2)
    if args.output in {"-", None}: print(payload)
    else: Path(args.output).write_text(payload + "\n", encoding="utf-8")
    return 0 if collect_runtime_capabilities()["runtime"]["core"]["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
