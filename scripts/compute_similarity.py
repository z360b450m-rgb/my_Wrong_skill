#!/usr/bin/env python3
"""Compatibility wrapper for v2 relationship edges."""

import argparse
import json
from pathlib import Path

from exam_error_core import build_graph, validate_v2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--threshold", type=float, default=0.2)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    errors = validate_v2(data)
    if errors:
        raise SystemExit("\n".join(errors))
    graph = build_graph(data, threshold=args.threshold)
    args.output.write_text(
        json.dumps({"edges": graph["edges"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
