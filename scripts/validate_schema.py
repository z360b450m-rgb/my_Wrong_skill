#!/usr/bin/env python3
"""Compatibility wrapper for strict v2 validation."""

import argparse
import json
import sys
from pathlib import Path

from exam_error_core import validate_v2


def validate(data):
    return validate_v2(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    errors = validate_v2(data)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Schema v2 validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
