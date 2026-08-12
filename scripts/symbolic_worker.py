#!/usr/bin/env python3
"""Isolated SymPy worker. It accepts one bounded JSON request on stdin."""

from __future__ import annotations

import json
import re
import sys


MAX_OPERATORS = 128


def parse_expression(sympy, text: str):
    safe_functions = {
        "sin": sympy.sin,
        "cos": sympy.cos,
        "tan": sympy.tan,
        "sqrt": sympy.sqrt,
        "exp": sympy.exp,
        "log": sympy.log,
        "Abs": sympy.Abs,
        "pi": sympy.pi,
        "E": sympy.E,
    }
    identifiers = set(re.findall(r"[A-Za-z]+", text))
    locals_map = {
        name: safe_functions.get(name, sympy.Symbol(name)) for name in identifiers
    }
    expression = text.replace("^", "**")
    if expression.count("=") == 1:
        lhs, rhs = expression.split("=")
        expression = f"({lhs})-({rhs})"
    elif "=" in expression:
        raise ValueError("multiple equality operators")
    parsed = sympy.sympify(expression, locals=locals_map, evaluate=False)
    if int(sympy.count_ops(parsed)) > MAX_OPERATORS:
        raise ValueError("symbolic expression is too complex")
    return parsed


def main() -> int:
    request = json.loads(sys.stdin.read(4096))
    student = request.get("student")
    reference = request.get("reference")
    if not isinstance(student, str) or not isinstance(reference, str):
        raise ValueError("worker inputs must be strings")
    try:
        import sympy  # type: ignore
    except ImportError:
        return 3
    left = parse_expression(sympy, student)
    right = parse_expression(sympy, reference)
    equivalent = bool(sympy.simplify(left - right) == 0)
    sys.stdout.write(json.dumps({"equivalent": equivalent}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, TypeError, json.JSONDecodeError):
        raise SystemExit(2)
