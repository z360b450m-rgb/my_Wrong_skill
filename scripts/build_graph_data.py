#!/usr/bin/env python3
"""Compatibility wrapper for complete v2 star-map graph data."""

import argparse
import json
from pathlib import Path

from exam_error_core import build_graph, validate_v2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("analysis", type=Path)
    parser.add_argument("similarities", type=Path, nargs="?")
    parser.add_argument("output", type=Path)
    parser.add_argument("--threshold", type=float, default=0.2)
    parser.add_argument("--index-version", default="unindexed")
    args = parser.parse_args()
    data = json.loads(args.analysis.read_text(encoding="utf-8"))
    errors = validate_v2(data)
    if errors:
        raise SystemExit("\n".join(errors))
    graph = build_graph(data, threshold=args.threshold, index_version=args.index_version)
    if args.similarities and args.similarities.is_file():
        supplied = json.loads(args.similarities.read_text(encoding="utf-8"))
        if isinstance(supplied.get("edges"), list):
            graph["edges"] = supplied["edges"]
    args.output.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
