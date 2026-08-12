#!/usr/bin/env python3
"""Report runtime capabilities and degraded components without network access."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sqlite3
import sys
from pathlib import Path
from typing import Any

from exam_error_core import SCHEMA_VERSION
from resource_limits import DEFAULT_MAX_INPUT_MB, DEFAULT_MEMORY_THRESHOLD_MB


MIN_PYTHON = (3, 10)
MIN_SQLITE = (3, 35, 0)
SERVICE_NAME = "analyze-exam-errors"


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def symbolic_adapter_available() -> bool:
    from symbolic_adapter import safe_symbolic_equivalent

    _equivalent, _confidence, reason = safe_symbolic_equivalent("x+1", "x+1")
    return reason is None


def fts5_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def _optional_components() -> dict[str, dict[str, Any]]:
    return {
        "symbolic_equivalence": {
            "available": symbolic_adapter_available(),
            "modules": ["sympy"],
            "purpose": "math_formula_equivalence",
        },
        "hnsw_usearch": {
            "available": has_module("usearch") and has_module("numpy"),
            "modules": ["usearch", "numpy"],
            "purpose": "semantic_ann_backend",
        },
        "hnsw_hnswlib": {
            "available": has_module("hnswlib") and has_module("numpy"),
            "modules": ["hnswlib", "numpy"],
            "purpose": "semantic_ann_backend",
        },
        "sentence_transformers": {
            "available": has_module("sentence_transformers"),
            "modules": ["sentence_transformers"],
            "purpose": "embedding_runtime",
        },
        "onnx_embedding": {
            "available": all(
                has_module(name)
                for name in ("onnxruntime", "transformers", "numpy")
            ),
            "modules": ["onnxruntime", "transformers", "numpy"],
            "purpose": "embedding_runtime",
        },
    }


def _mode(core_ready: bool, symbolic_ready: bool, semantic_ready: bool) -> str:
    if not core_ready:
        return "blocked"
    if symbolic_ready and semantic_ready:
        return "full"
    if semantic_ready:
        return "semantic"
    if symbolic_ready:
        return "math"
    return "core"


def collect_runtime_capabilities(skill_dir: str | Path | None = None) -> dict[str, Any]:
    skill_root = Path(skill_dir).resolve() if skill_dir else Path(__file__).resolve().parents[1]
    sqlite_fts5 = fts5_available()
    core_ready = (
        sqlite_fts5
        and sys.version_info >= MIN_PYTHON
        and sqlite3.sqlite_version_info >= MIN_SQLITE
    )
    optional = _optional_components()
    hnsw_ready = optional["hnsw_usearch"]["available"] or optional["hnsw_hnswlib"]["available"]
    embedding_ready = optional["sentence_transformers"]["available"] or optional["onnx_embedding"]["available"]
    semantic_ready = hnsw_ready and embedding_ready
    symbolic_ready = optional["symbolic_equivalence"]["available"]

    degraded: list[str] = []
    if not symbolic_ready:
        degraded.append("sympy_equivalence")
    if not hnsw_ready:
        degraded.append("hnsw_index")
    if not embedding_ready:
        degraded.append("embedding_model")
    if not semantic_ready:
        degraded.append("semantic_retrieval")

    return {
        "service": SERVICE_NAME,
        "schema_version": SCHEMA_VERSION,
        "mode": _mode(core_ready, symbolic_ready, semantic_ready),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "minimums": {
                "python": ".".join(str(part) for part in MIN_PYTHON),
                "sqlite": ".".join(str(part) for part in MIN_SQLITE),
            },
            "core": {
                "sqlite_fts5": sqlite_fts5,
                "ready": core_ready,
            },
        },
        "capabilities": {
            "validate": True,
            "migrate_v1_to_v2": True,
            "analyze": True,
            "safe_pipeline": True,
            "statistics": True,
            "graph": True,
            "report": True,
            "teacher_report": True,
            "review_queue": True,
            "audit_verify": True,
            "audit_recompute": True,
            "disk_backed_spill": True,
            "fts5_search": sqlite_fts5,
            "tag_relations_search": core_ready,
            "symbolic_equivalence": symbolic_ready,
            "hnsw_index": hnsw_ready,
            "embedding_model_runtime": embedding_ready,
            "semantic_retrieval": semantic_ready,
        },
        "optional_components": optional,
        "degraded_components": degraded,
        "constraints": {
            "network_default": "disabled",
            "pii_indexing": False,
            "cross_org_query": False,
            "review_document_binding": [
                "organization_id",
                "analysis_id",
                "document_state_hash",
            ],
            "max_input_mb": DEFAULT_MAX_INPUT_MB,
            "intermediate_memory_threshold_mb": DEFAULT_MEMORY_THRESHOLD_MB,
            "spill_scope": "index_records_and_json_outputs",
        },
        "providers": {
            "embedding": {
                "supported": ["sentence-transformers", "onnx", "hashing"],
                "runtime_ready": [
                    provider
                    for provider, available in (
                        ("sentence-transformers", optional["sentence_transformers"]["available"]),
                        ("onnx", optional["onnx_embedding"]["available"]),
                        ("hashing", True),
                    )
                    if available
                ],
                "requires_request_arguments": [
                    "--embedding-provider",
                    "--model-path",
                    "--model-license",
                    "--model-sha256",
                ],
            },
            "hnsw": {
                "supported": ["usearch", "hnswlib"],
                "runtime_ready": [
                    backend
                    for backend, available in (
                        ("usearch", optional["hnsw_usearch"]["available"]),
                        ("hnswlib", optional["hnsw_hnswlib"]["available"]),
                    )
                    if available
                ],
            },
        },
        "commands": {
            "capabilities": "python scripts/exam_error_cli.py capabilities",
            "pipeline": "python scripts/exam_error_cli.py pipeline <input> <output>",
            "teacher_report": "python scripts/exam_error_cli.py teacher-report <input> <output.html>",
            "audit_recompute": "python scripts/exam_error_cli.py audit recompute <input.json> --actor-ref <admin-id> --confirm-new-baseline --output <output.json>",
            "install_recommended": "python -m scripts.install_runtime",
            "install_math": "python -m scripts.install_runtime --profile math",
            "install_semantic_onnx": "python -m scripts.install_runtime --profile semantic-onnx",
            "requirements_file": str(skill_root / "scripts" / "runtime_optional_requirements.txt"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report runtime capabilities and degraded components for analyze-exam-errors."
    )
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    result = collect_runtime_capabilities()
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output in {"-", None}:
        print(payload)
    else:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
    return 0 if result["runtime"]["core"]["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
