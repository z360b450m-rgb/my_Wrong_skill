"""Fail-closed, time-bounded adapter for optional symbolic equivalence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from exam_error_app.grading import _safe_symbolic_text


SYMBOLIC_TIMEOUT_SECONDS = 2.0
WORKER = Path(__file__).resolve().with_name("symbolic_worker.py")


def safe_symbolic_equivalent(
    student: Any,
    reference: Any,
    *,
    timeout_seconds: float = SYMBOLIC_TIMEOUT_SECONDS,
) -> tuple[bool | None, float, str | None]:
    student_text = _safe_symbolic_text(student)
    reference_text = _safe_symbolic_text(reference)
    if student_text is None or reference_text is None:
        return None, 0.0, "unsafe_symbolic_input"
    creationflags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0
    )
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(WORKER)],
            input=json.dumps(
                {"student": student_text, "reference": reference_text},
                ensure_ascii=True,
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            creationflags=creationflags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, 0.0, "symbolic_timeout"
    if result.returncode == 3:
        return student_text == reference_text, 0.75, "symbolic_equivalence_unavailable"
    if result.returncode != 0 or len(result.stdout) > 4096:
        return None, 0.60, "symbolic_parse_failed"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, 0.60, "symbolic_parse_failed"
    if not isinstance(payload.get("equivalent"), bool):
        return None, 0.60, "symbolic_parse_failed"
    return payload["equivalent"], 0.98, None
