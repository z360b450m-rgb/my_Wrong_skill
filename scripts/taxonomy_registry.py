"""Teacher-governed extension registry for error causes and knowledge points.

The registry is intentionally data-only.  An agent can add a *pending* candidate,
but only an explicit teacher decision can make a label available for later use.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable


REGISTRY_VERSION = "1.0"
VALID_DIMENSIONS = frozenset({"error", "knowledge"})
VALID_STATUSES = frozenset({"pending", "approved", "rejected"})
MAX_LABEL_LENGTH = 80
MAX_DEFINITION_LENGTH = 300


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()[:limit]


def _candidate_id(dimension: str, name: str) -> str:
    digest = hashlib.sha256(f"{dimension}:{name.casefold()}".encode("utf-8")).hexdigest()[:16]
    return f"{dimension}-{digest}"


def empty_registry() -> dict[str, Any]:
    return {"schema_version": REGISTRY_VERSION, "items": []}


def validate_registry(registry: Any) -> list[str]:
    if not isinstance(registry, dict):
        return ["词库必须是 JSON 对象"]
    if registry.get("schema_version") != REGISTRY_VERSION:
        return [f"词库 schema_version 必须为 {REGISTRY_VERSION}"]
    items = registry.get("items")
    if not isinstance(items, list):
        return ["词库 items 必须是数组"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        place = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{place} 必须是对象")
            continue
        required = {"id", "dimension", "name", "display_name", "definition", "status", "created_at", "updated_at"}
        missing = required - set(item)
        if missing:
            errors.append(f"{place} 缺少字段: {', '.join(sorted(missing))}")
        if item.get("dimension") not in VALID_DIMENSIONS:
            errors.append(f"{place}.dimension 无效")
        if not _text(item.get("name"), limit=MAX_LABEL_LENGTH):
            errors.append(f"{place}.name 不能为空")
        if not _text(item.get("display_name"), limit=MAX_LABEL_LENGTH):
            errors.append(f"{place}.display_name 不能为空")
        if item.get("status") not in VALID_STATUSES:
            errors.append(f"{place}.status 无效")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{place}.id 不能为空")
        elif identifier in seen:
            errors.append(f"{place}.id 重复")
        else:
            seen.add(identifier)
    return errors


def load_registry(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return empty_registry()
    data = json.loads(file_path.read_text(encoding="utf-8"))
    errors = validate_registry(data)
    if errors:
        raise ValueError("扩展词库校验失败：\n" + "\n".join(errors))
    return data


def save_registry(path: str | Path, registry: dict[str, Any]) -> Path:
    errors = validate_registry(registry)
    if errors:
        raise ValueError("拒绝写入无效扩展词库：\n" + "\n".join(errors))
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(file_path)
    return file_path


def approved_labels(registry: dict[str, Any]) -> dict[str, dict[str, str]]:
    labels: dict[str, dict[str, str]] = {"error": {}, "knowledge": {}}
    for item in registry.get("items", []):
        if item.get("status") == "approved" and item.get("dimension") in labels:
            labels[item["dimension"]][item["name"]] = item["display_name"]
    return labels


def candidates_from_document(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract only explicit agent suggestions; normal tags are never auto-proposed."""
    candidates: dict[str, dict[str, Any]] = {}
    for attempt in document.get("attempts", []):
        student_ref = _text(attempt.get("student_ref"), limit=80)
        for response in attempt.get("responses", []):
            question_id = _text(response.get("question_id"), limit=80)
            for tag in response.get("suggested_tags", []):
                if not isinstance(tag, dict) or tag.get("dimension") not in VALID_DIMENSIONS:
                    continue
                dimension = tag["dimension"]
                name = _text(tag.get("name"), limit=MAX_LABEL_LENGTH)
                if not name:
                    continue
                identifier = _candidate_id(dimension, name)
                candidate = candidates.setdefault(
                    identifier,
                    {
                        "id": identifier,
                        "dimension": dimension,
                        "name": name,
                        "display_name": _text(tag.get("display_name"), limit=MAX_LABEL_LENGTH) or name,
                        "definition": _text(tag.get("definition"), limit=MAX_DEFINITION_LENGTH),
                        "evidence": [],
                    },
                )
                evidence = {"question_id": question_id, "student_ref": student_ref}
                if evidence not in candidate["evidence"]:
                    candidate["evidence"].append(evidence)
    return list(candidates.values())


def upsert_pending_candidates(
    path: str | Path,
    candidates: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry = load_registry(path)
    existing = {item["id"]: item for item in registry["items"]}
    pending: list[dict[str, Any]] = []
    now = _now()
    for candidate in candidates:
        identifier = candidate["id"]
        item = existing.get(identifier)
        if item is None:
            item = {
                "id": identifier,
                "dimension": candidate["dimension"],
                "name": candidate["name"],
                "display_name": candidate["display_name"],
                "definition": candidate["definition"],
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "evidence": candidate["evidence"],
            }
            registry["items"].append(item)
            existing[identifier] = item
        elif item["status"] == "pending":
            known = {(row.get("question_id"), row.get("student_ref")) for row in item.get("evidence", [])}
            item.setdefault("evidence", []).extend(
                row for row in candidate["evidence"]
                if (row.get("question_id"), row.get("student_ref")) not in known
            )
            if not item.get("definition") and candidate.get("definition"):
                item["definition"] = candidate["definition"]
            item["updated_at"] = now
        if item["status"] == "pending":
            pending.append(item)
    save_registry(path, registry)
    return registry, pending


def apply_teacher_decisions(path: str | Path, decisions: Any) -> dict[str, int]:
    if not isinstance(decisions, dict) or not isinstance(decisions.get("decisions"), list):
        raise ValueError("审核决定必须包含 decisions 数组")
    registry = load_registry(path)
    items = {item["id"]: item for item in registry["items"]}
    applied = 0
    ignored = 0
    now = _now()
    for decision in decisions["decisions"]:
        if not isinstance(decision, dict):
            ignored += 1
            continue
        item = items.get(decision.get("id"))
        action = decision.get("action")
        if item is None or item.get("status") != "pending" or action not in {"approve", "reject"}:
            ignored += 1
            continue
        if action == "approve":
            display_name = _text(decision.get("display_name"), limit=MAX_LABEL_LENGTH)
            definition = _text(decision.get("definition"), limit=MAX_DEFINITION_LENGTH)
            if display_name:
                item["display_name"] = display_name
            if definition:
                item["definition"] = definition
            item["status"] = "approved"
        else:
            item["status"] = "rejected"
        item["reviewed_at"] = now
        item["updated_at"] = now
        item["review_source"] = "teacher_report"
        applied += 1
    save_registry(path, registry)
    return {"applied": applied, "ignored": ignored}
