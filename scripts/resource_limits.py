"""Bounded JSON input reader for the teacher-report command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_MAX_INPUT_MB = 256


def read_json_bounded(path: str | Path, *, max_bytes: int | None = None) -> Any:
    source = Path(path)
    limit = max_bytes or int(os.environ.get("EXAM_ERROR_MAX_INPUT_MB", DEFAULT_MAX_INPUT_MB)) * 1024 * 1024
    if source.stat().st_size > limit:
        raise ValueError(f"输入 JSON 超过大小上限（{limit} 字节）")
    return json.loads(source.read_text(encoding="utf-8"))
