"""Disk-backed spooling utilities for bounded intermediate memory use."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator


DEFAULT_MEMORY_THRESHOLD_MB = 256
MIN_MEMORY_THRESHOLD_MB = 1
MEMORY_THRESHOLD_ENV = "EXAM_ERROR_MEMORY_THRESHOLD_MB"
SPILL_DIRECTORY_ENV = "EXAM_ERROR_SPILL_DIR"
DEFAULT_MAX_INPUT_MB = 256
MAX_INPUT_ENV = "EXAM_ERROR_MAX_INPUT_MB"


def resolve_memory_threshold_bytes(value_mb: int | None = None) -> int:
    raw = value_mb if value_mb is not None else os.environ.get(
        MEMORY_THRESHOLD_ENV, DEFAULT_MEMORY_THRESHOLD_MB
    )
    try:
        amount = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{MEMORY_THRESHOLD_ENV} must be an integer") from exc
    if amount < MIN_MEMORY_THRESHOLD_MB:
        raise ValueError(
            f"memory threshold must be at least {MIN_MEMORY_THRESHOLD_MB} MB"
        )
    return amount * 1024 * 1024


def resolve_max_input_bytes(value_mb: int | None = None) -> int:
    raw = value_mb if value_mb is not None else os.environ.get(
        MAX_INPUT_ENV, DEFAULT_MAX_INPUT_MB
    )
    try:
        amount = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{MAX_INPUT_ENV} must be an integer") from exc
    if amount < 1:
        raise ValueError("maximum input size must be at least 1 MB")
    return amount * 1024 * 1024


def read_json_bounded(path: str | Path, *, max_bytes: int | None = None) -> Any:
    source = Path(path)
    limit = max_bytes or resolve_max_input_bytes()
    size = source.stat().st_size
    if size > limit:
        raise ValueError(
            f"input JSON is {size} bytes; maximum allowed size is {limit} bytes"
        )
    return json.loads(source.read_text(encoding="utf-8"))


def resolve_spill_directory(
    requested: str | Path | None = None,
    *,
    preferred: str | Path | None = None,
) -> Path:
    selected = requested or os.environ.get(SPILL_DIRECTORY_ENV) or preferred
    directory = Path(selected) if selected else Path(tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError(f"spill directory is not a directory: {directory}")
    return directory.resolve()


class SpillableJsonRows:
    """Keep JSON rows in memory until the threshold, then roll to local storage."""

    def __init__(self, threshold_bytes: int, directory: str | Path):
        if threshold_bytes < 1:
            raise ValueError("threshold_bytes must be positive")
        self.threshold_bytes = threshold_bytes
        self.directory = Path(directory)
        self._stream = tempfile.SpooledTemporaryFile(
            max_size=threshold_bytes,
            mode="w+t",
            encoding="utf-8",
            newline="\n",
            dir=self.directory,
        )
        self.count = 0

    @property
    def spilled_to_disk(self) -> bool:
        return bool(getattr(self._stream, "_rolled", False))

    def append(self, value: dict[str, Any]) -> None:
        self._stream.write(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        self._stream.write("\n")
        self.count += 1

    def iter_batches(self, batch_size: int = 512) -> Iterator[list[dict[str, Any]]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._stream.flush()
        self._stream.seek(0)
        batch: list[dict[str, Any]] = []
        for line in self._stream:
            if not line.strip():
                continue
            batch.append(json.loads(line))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "SpillableJsonRows":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def write_json_spooled(
    value: Any,
    destination: str | Path | None,
    *,
    threshold_bytes: int | None = None,
    spill_directory: str | Path | None = None,
) -> bool:
    """Serialize JSON through a spooled file and return whether disk spill occurred."""
    threshold = threshold_bytes or resolve_memory_threshold_bytes()
    target = None if destination in {None, "-"} else Path(destination)
    preferred = target.parent if target else None
    directory = resolve_spill_directory(spill_directory, preferred=preferred)
    with tempfile.SpooledTemporaryFile(
        max_size=threshold,
        mode="w+t",
        encoding="utf-8",
        newline="\n",
        dir=directory,
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        spilled = bool(getattr(stream, "_rolled", False))
        stream.flush()
        stream.seek(0)
        if target is None:
            import sys

            for chunk in iter(lambda: stream.read(1024 * 1024), ""):
                sys.stdout.write(chunk)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    for chunk in iter(lambda: stream.read(1024 * 1024), ""):
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return spilled
