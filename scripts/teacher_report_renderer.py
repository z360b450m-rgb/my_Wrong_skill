#!/usr/bin/env python3
"""Deterministic renderer for the version-locked teacher report UI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from exam_error_app.teacher_report_contract import (
    TEACHER_REPORT_VIEW_VERSION,
    validate_teacher_report_model,
)


TEACHER_REPORT_RENDERER_VERSION = "teacher-report-renderer-v15"
TEACHER_REPORT_TEMPLATE_VERSION = "teacher-report-ui-v15"
TEACHER_REPORT_TEMPLATE_SHA256 = (
    "24df082ec6acc0cf55268f9de7cd6912d4852e1b0370bb01f9f342cd35dffb83"
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_locked_template(template_path: str | Path) -> str:
    template = Path(template_path).read_text(encoding="utf-8")
    actual_hash = _sha256_text(template)
    if actual_hash != TEACHER_REPORT_TEMPLATE_SHA256:
        raise ValueError(
            "teacher report template is not the version-locked UI asset: "
            f"expected {TEACHER_REPORT_TEMPLATE_SHA256}, got {actual_hash}"
        )
    if template.count("{{REPORT_DATA}}") != 1:
        raise ValueError("teacher report template must contain one REPORT_DATA slot")
    if template.count("{{CSP_NONCE}}") < 1:
        raise ValueError("teacher report template must contain a CSP_NONCE slot")
    return template


def render_teacher_report_html(
    model: dict[str, Any], template_path: str | Path
) -> str:
    errors = validate_teacher_report_model(model)
    if errors:
        raise ValueError("invalid teacher report model:\n" + "\n".join(errors))
    template = load_locked_template(template_path)
    payload = json.dumps(
        model,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload = (
        payload.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    nonce = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return template.replace("{{REPORT_DATA}}", payload).replace(
        "{{CSP_NONCE}}", nonce
    )


def write_teacher_report_html(html: str, output_path: str | Path) -> str:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return str(output)


def build_teacher_report_manifest(
    html: str,
    output_path: str | Path,
) -> dict[str, str]:
    return {
        "teacher_report_html": str(output_path),
        "view_schema_version": TEACHER_REPORT_VIEW_VERSION,
        "renderer_version": TEACHER_REPORT_RENDERER_VERSION,
        "template_version": TEACHER_REPORT_TEMPLATE_VERSION,
        "template_sha256": TEACHER_REPORT_TEMPLATE_SHA256,
        "output_sha256": _sha256_text(html),
    }
