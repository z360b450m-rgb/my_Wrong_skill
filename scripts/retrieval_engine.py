#!/usr/bin/env python3
"""SQLite FTS5, tag and optional HNSW retrieval for exam-error records."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import platform
import re
import sqlite3
import statistics
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Protocol

from exam_error_core import canonical_json, normalize_text, sha256_value, utc_now
from exam_error_app.retrieval_projection import iter_index_records, project_index_records
from resource_limits import (
    SpillableJsonRows,
    resolve_memory_threshold_bytes,
    resolve_spill_directory,
)


RRF_K = 60
FTS_TOKENIZER_VERSION = "2"
HNSW_EXPANSION_SEARCH = 8192
RETRIEVAL_WEIGHTS = {"lexical": 0.45, "semantic": 0.35, "tag": 0.20}
ALLOWED_EXACT_FILTERS = {
    "class_id",
    "student_ref",
    "student_name",
    "subject",
    "grade",
    "curriculum_version",
    "paper_id",
    "question_id",
    "question_type",
    "review_status",
}
TAG_FILTERS = {
    "knowledge_tags": "knowledge",
    "cognitive_tags": "cognitive",
    "error_tags": "error",
}
MAX_QUERY_LENGTH = 4_096
MAX_CANDIDATE_LIMIT = 5_000
MAX_FILTER_VALUES = 100
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
TAG_ALIASES = {
    "concept-missing": "概念缺失",
    "concept-confusion": "概念混淆",
    "theorem-misuse": "定理误用",
    "formula-misuse": "公式误用",
    "invalid-inference": "无效推理",
    "condition-omitted": "条件遗漏",
    "case-incomplete": "情况不完整",
    "strategy-mismatch": "策略不匹配",
    "calculation-error": "计算错误",
    "sign-error": "符号错误",
    "transformation-error": "变形错误",
    "step-omitted": "步骤遗漏",
    "requirement-misread": "要求误读",
    "condition-missed": "条件漏读",
    "diagram-misread": "图表误读",
    "unit-missing": "单位缺失",
    "notation-invalid": "符号不规范",
    "conclusion-incomplete": "结论不完整",
    "explanation-insufficient": "解释不足",
    "unanswered": "未作答",
    "illegible": "字迹不清",
    "answer-misaligned": "答案错位",
    "ocr-symbol-error": "OCR符号错误",
    "extraction-error": "提取错误",
    "rubric-gap": "评分标准缺口",
    "unclassified": "未分类",
    "remember": "记忆",
    "understand": "理解",
    "apply": "应用",
    "analyze": "分析",
    "evaluate": "评价",
    "create": "创造",
}


class ComponentUnavailable(RuntimeError):
    pass


class ManagedConnection(sqlite3.Connection):
    """A sqlite connection whose context manager also closes file handles."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class EmbeddingProvider(Protocol):
    model_id: str
    model_fingerprint: str
    license_id: str
    dimension: int

    def encode(self, texts: list[str]) -> list[list[float]]:
        ...


def _file_tree_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(str(file_path.relative_to(path)).replace("\\", "/").encode("utf-8"))
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _verified_model_fingerprint(path: Path, expected: str | None) -> str:
    normalized = str(expected or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", normalized):
        normalized = "sha256:" + normalized
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        raise ComponentUnavailable("an approved --model-sha256 fingerprint is required")
    actual = _file_tree_fingerprint(path)
    if not hmac.compare_digest(actual, normalized):
        raise ComponentUnavailable(
            f"local model fingerprint mismatch: expected {normalized}, got {actual}"
        )
    return actual


class SentenceTransformerProvider:
    def __init__(
        self, model_path: str | Path, license_id: str, expected_fingerprint: str | None
    ):
        path = Path(model_path).resolve()
        if not path.is_dir():
            raise ComponentUnavailable(f"local sentence-transformers model not found: {path}")
        if not license_id:
            raise ComponentUnavailable("model license_id is required")
        fingerprint = _verified_model_fingerprint(path, expected_fingerprint)
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise ComponentUnavailable("sentence-transformers is not installed") from exc
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        try:
            self._model = SentenceTransformer(
                str(path),
                local_files_only=True,
                trust_remote_code=False,
            )
        except TypeError as exc:
            raise ComponentUnavailable(
                "installed sentence-transformers does not support fail-closed local loading"
            ) from exc
        self.model_id = path.name
        self.model_fingerprint = fingerprint
        self.license_id = license_id
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [[float(value) for value in vector] for vector in vectors]


class OnnxEmbeddingProvider:
    def __init__(
        self, model_path: str | Path, license_id: str, expected_fingerprint: str | None
    ):
        path = Path(model_path).resolve()
        model_file = path / "model.onnx"
        if not model_file.is_file():
            raise ComponentUnavailable(f"local ONNX model not found: {model_file}")
        if not license_id:
            raise ComponentUnavailable("model license_id is required")
        fingerprint = _verified_model_fingerprint(path, expected_fingerprint)
        try:
            import numpy as np  # type: ignore
            import onnxruntime as ort  # type: ignore
            from transformers import AutoTokenizer  # type: ignore
        except ImportError as exc:
            raise ComponentUnavailable("onnxruntime, numpy and transformers are required") from exc
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        self._np = np
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(path), local_files_only=True, trust_remote_code=False
        )
        self._session = ort.InferenceSession(
            str(model_file),
            providers=["CPUExecutionProvider"],
        )
        self.model_id = path.name
        self.model_fingerprint = fingerprint
        self.license_id = license_id
        probe = self.encode(["dimension probe"])
        self.dimension = len(probe[0])

    def encode(self, texts: list[str]) -> list[list[float]]:
        tokens = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="np",
        )
        input_names = {item.name for item in self._session.get_inputs()}
        feeds = {key: value for key, value in tokens.items() if key in input_names}
        hidden = self._session.run(None, feeds)[0]
        mask = tokens["attention_mask"][..., None].astype(self._np.float32)
        pooled = (hidden * mask).sum(axis=1) / self._np.clip(mask.sum(axis=1), 1e-9, None)
        pooled /= self._np.clip(self._np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
        return [[float(value) for value in vector] for vector in pooled]


class HashingEmbeddingProvider:
    """Dependency-free deterministic vectorizer for tests and offline smoke checks."""

    model_id = "char-ngram-hashing"
    model_fingerprint = "builtin:char-ngram-hashing-v1"
    license_id = "built-in"

    def __init__(self, dimension: int = 256):
        self.dimension = dimension

    def encode(self, texts: list[str]) -> list[list[float]]:
        output = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in fts_tokens(text):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                index = value % self.dimension
                vector[index] += 1.0 if value & 1 else -1.0
            norm = math.sqrt(sum(item * item for item in vector)) or 1.0
            output.append([item / norm for item in vector])
        return output


def build_embedding_provider(
    provider_name: str | None,
    model_path: str | None = None,
    license_id: str | None = None,
    expected_fingerprint: str | None = None,
) -> EmbeddingProvider | None:
    if not provider_name or provider_name == "none":
        return None
    if provider_name == "sentence-transformers":
        return SentenceTransformerProvider(
            model_path or "", license_id or "", expected_fingerprint
        )
    if provider_name == "onnx":
        return OnnxEmbeddingProvider(
            model_path or "", license_id or "", expected_fingerprint
        )
    if provider_name == "hashing":
        return HashingEmbeddingProvider()
    raise ComponentUnavailable(f"unsupported embedding provider: {provider_name}")


def fts_tokens(text: Any) -> list[str]:
    normalized = normalize_text(text)
    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(normalized):
        part = match.group(0)
        if CJK_PATTERN.fullmatch(part):
            if len(part) == 1:
                tokens.append(part)
            else:
                tokens.extend(part[index : index + 2] for index in range(len(part) - 1))
                if len(part) >= 3:
                    tokens.extend(part[index : index + 3] for index in range(len(part) - 2))
        else:
            tokens.append(part)
    return list(dict.fromkeys(tokens))


def fts_document(text: Any) -> str:
    return " ".join(fts_tokens(text))


def _fts_query(text: str) -> str:
    clauses = []
    for match in TOKEN_PATTERN.finditer(normalize_text(text)):
        part = match.group(0)
        if CJK_PATTERN.fullmatch(part):
            tokens = (
                [part]
                if len(part) <= 2
                else list(dict.fromkeys([part[:2], part[-2:]]))
            )
        else:
            tokens = [part]
        clauses.extend(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
        )
    return " AND ".join(clauses)


def _scope_token(field: str, value: Any) -> str:
    digest = hashlib.sha256(f"{field}\0{value}".encode("utf-8")).hexdigest()[:24]
    return f"scope{field.replace('_', '')}{digest}"


def _record_scope_tokens(record: dict[str, Any]) -> list[str]:
    tokens = []
    for field in sorted(ALLOWED_EXACT_FILTERS):
        value = record.get(field)
        if value is not None:
            tokens.append(_scope_token(field, value))
    for tag in record.get("tags", []):
        tokens.append(_scope_token(f"tag{tag['dimension']}", tag["name"]))
    return tokens


def _scope_query(filters: dict[str, Any]) -> str:
    clauses = []
    for field in sorted(ALLOWED_EXACT_FILTERS):
        if field not in filters or filters[field] is None:
            continue
        values = filters[field] if isinstance(filters[field], list) else [filters[field]]
        if len(values) > MAX_FILTER_VALUES:
            raise ValueError(f"filter {field} exceeds {MAX_FILTER_VALUES} values")
        tokens = [f'"{_scope_token(field, value)}"' for value in values]
        if tokens:
            clauses.append("(" + " OR ".join(tokens) + ")")
    for key, dimension in TAG_FILTERS.items():
        values = filters.get(key)
        if values is None:
            continue
        values = values if isinstance(values, list) else [values]
        if len(values) > MAX_FILTER_VALUES:
            raise ValueError(f"filter {key} exceeds {MAX_FILTER_VALUES} values")
        for value in values:
            clauses.append(f'"{_scope_token(f"tag{dimension}", value)}"')
    return " AND ".join(clauses)


def connect_database(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path), timeout=30, factory=ManagedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def _read_database_organization(path: str | Path) -> str | None:
    database_path = Path(path)
    if not database_path.is_file():
        return None
    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        has_metadata = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if not has_metadata:
            raise ValueError("existing database is not an analyze-exam-errors index")
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='organization_id'"
        ).fetchone()
        if not row:
            raise ValueError("existing index database has no organization owner")
        return str(row[0])


def initialize_database(connection: sqlite3.Connection, organization_id: str, reset: bool = False) -> None:
    if reset:
        existing_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if existing_tables:
            if "metadata" not in existing_tables:
                raise ValueError("refusing to rebuild a database not owned by this skill")
            current = connection.execute(
                "SELECT value FROM metadata WHERE key='organization_id'"
            ).fetchone()
            if not current or current["value"] != organization_id:
                owner = current["value"] if current else "unknown"
                raise ValueError(
                    f"database belongs to organization {owner}, not {organization_id}"
                )
        connection.executescript(
            """
            DROP TABLE IF EXISTS records_fts;
            DROP TABLE IF EXISTS record_tags;
            DROP TABLE IF EXISTS vector_labels;
            DROP TABLE IF EXISTS records;
            DROP TABLE IF EXISTS index_audit;
            DROP TABLE IF EXISTS metadata;
            """
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS records (
            record_id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            analysis_id TEXT NOT NULL,
            paper_id TEXT NOT NULL,
            class_id TEXT,
            student_ref TEXT NOT NULL,
            student_name TEXT,
            attempt_id TEXT NOT NULL,
            question_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            grade TEXT,
            curriculum_version TEXT,
            question_type TEXT NOT NULL,
            question_text TEXT,
            answer_text TEXT,
            reference_answer TEXT,
            error_text TEXT,
            review_status TEXT NOT NULL,
            score REAL,
            max_score REAL NOT NULL,
            event_date TEXT,
            evidence_json TEXT NOT NULL,
            source_refs_json TEXT NOT NULL,
            content_text TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_records_scope
            ON records (paper_id, class_id, student_ref, question_id);
        CREATE INDEX IF NOT EXISTS idx_records_curriculum
            ON records (subject, grade, curriculum_version);
        CREATE INDEX IF NOT EXISTS idx_records_review_score
            ON records (review_status, score);
        CREATE INDEX IF NOT EXISTS idx_records_date ON records (event_date);
        CREATE TABLE IF NOT EXISTS record_tags (
            record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
            dimension TEXT NOT NULL,
            name TEXT NOT NULL,
            confidence REAL,
            PRIMARY KEY (record_id, dimension, name)
        );
        CREATE INDEX IF NOT EXISTS idx_record_tags_lookup
            ON record_tags (dimension, name, record_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
            record_id UNINDEXED,
            content,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS vector_labels (
            record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
            label INTEGER NOT NULL UNIQUE,
            model_fingerprint TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS index_audit (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            previous_hash TEXT,
            event_hash TEXT NOT NULL
        );
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(records)").fetchall()
    }
    if "student_name" not in columns:
        connection.execute("ALTER TABLE records ADD COLUMN student_name TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_records_student_name ON records (student_name)"
    )
    current = connection.execute(
        "SELECT value FROM metadata WHERE key='organization_id'"
    ).fetchone()
    if current and current["value"] != organization_id:
        raise ValueError(
            f"database belongs to organization {current['value']}, not {organization_id}"
        )
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('organization_id',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (organization_id,),
    )
    anchors = {
        row["key"]: row["value"]
        for row in connection.execute(
            "SELECT key,value FROM metadata "
            "WHERE key IN ('audit_event_count','audit_head_hash')"
        )
    }
    if len(anchors) == 1:
        raise ValueError("database audit anchor is incomplete")
    if not anchors:
        audit_state = connection.execute(
            "SELECT COUNT(*) AS event_count,"
            "COALESCE((SELECT event_hash FROM index_audit "
            "ORDER BY sequence DESC LIMIT 1),'') AS head_hash "
            "FROM index_audit"
        ).fetchone()
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            [
                ("audit_event_count", str(audit_state["event_count"])),
                ("audit_head_hash", audit_state["head_hash"]),
            ],
        )


def _database_audit(connection: sqlite3.Connection, event_type: str, payload: Any) -> None:
    anchor_rows = {
        row["key"]: row["value"]
        for row in connection.execute(
            "SELECT key,value FROM metadata "
            "WHERE key IN ('audit_event_count','audit_head_hash')"
        )
    }
    if set(anchor_rows) != {"audit_event_count", "audit_head_hash"}:
        raise ValueError("database audit anchor is missing or incomplete")
    previous_row = connection.execute(
        "SELECT event_hash FROM index_audit ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    previous = previous_row["event_hash"] if previous_row else None
    actual_count = connection.execute(
        "SELECT COUNT(*) AS n FROM index_audit"
    ).fetchone()["n"]
    if (
        int(anchor_rows["audit_event_count"]) != actual_count
        or anchor_rows["audit_head_hash"] != (previous or "")
    ):
        raise ValueError("database audit chain does not match its anchor")
    timestamp = utc_now()
    payload_hash = sha256_value(payload)
    unsigned = {
        "timestamp": timestamp,
        "event_type": event_type,
        "payload_hash": payload_hash,
        "previous_hash": previous,
    }
    event_hash = sha256_value(unsigned)
    connection.execute(
        "INSERT INTO index_audit(timestamp,event_type,payload_hash,previous_hash,event_hash) "
        "VALUES(?,?,?,?,?)",
        (timestamp, event_type, payload_hash, previous, event_hash),
    )
    connection.executemany(
        "UPDATE metadata SET value=? WHERE key=?",
        [
            (str(actual_count + 1), "audit_event_count"),
            (event_hash, "audit_head_hash"),
        ],
    )


def verify_database_audit(path: str | Path) -> list[str]:
    errors = []
    with connect_database(path) as connection:
        anchors = {
            row["key"]: row["value"]
            for row in connection.execute(
                "SELECT key,value FROM metadata "
                "WHERE key IN ('audit_event_count','audit_head_hash')"
            )
        }
        if set(anchors) != {"audit_event_count", "audit_head_hash"}:
            errors.append("index_audit: audit anchor is missing or incomplete")
        previous = None
        rows = connection.execute(
            "SELECT * FROM index_audit ORDER BY sequence"
        ).fetchall()
        for row in rows:
            location = f"index_audit[{row['sequence']}]"
            if row["previous_hash"] != previous:
                errors.append(f"{location}: previous_hash mismatch")
            expected = sha256_value(
                {
                    "timestamp": row["timestamp"],
                    "event_type": row["event_type"],
                    "payload_hash": row["payload_hash"],
                    "previous_hash": row["previous_hash"],
                }
            )
            if row["event_hash"] != expected:
                errors.append(f"{location}: event_hash mismatch")
            previous = row["event_hash"]
        if anchors:
            try:
                anchored_count = int(anchors["audit_event_count"])
            except (KeyError, ValueError):
                errors.append("index_audit: audit_event_count anchor is invalid")
            else:
                if anchored_count != len(rows):
                    errors.append("index_audit: event count does not match audit anchor")
            if anchors.get("audit_head_hash") != (previous or ""):
                errors.append("index_audit: head hash does not match audit anchor")
    return errors


def records_from_document(data: dict[str, Any]) -> list[dict[str, Any]]:
    return project_index_records(
        data,
        normalize_text=normalize_text,
        canonical_json=canonical_json,
        tag_aliases=TAG_ALIASES,
    )


def iter_records_from_document(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return iter_index_records(
        data,
        normalize_text=normalize_text,
        canonical_json=canonical_json,
        tag_aliases=TAG_ALIASES,
    )


class HNSWIndexManager:
    def __init__(
        self,
        path: str | Path,
        dimension: int,
        model_fingerprint: str,
        backend: str | None = None,
    ):
        self._np = None
        self._hnswlib = None
        self._usearch_index_type = None
        self.path = Path(path)
        self.dimension = dimension
        self.model_fingerprint = model_fingerprint
        errors = []
        selected = [backend] if backend else ["usearch", "hnswlib"]
        self.index = None
        self.backend = ""
        for candidate in selected:
            if candidate == "usearch":
                try:
                    import numpy as np  # type: ignore
                    from usearch.index import Index  # type: ignore

                    self._np = np
                    self._usearch_index_type = Index
                    self.index = Index(
                        ndim=dimension,
                        metric="cos",
                        connectivity=16,
                        expansion_add=200,
                        expansion_search=HNSW_EXPANSION_SEARCH,
                    )
                    self.backend = "usearch"
                    break
                except ImportError as exc:
                    errors.append(str(exc))
            elif candidate == "hnswlib":
                try:
                    import hnswlib  # type: ignore
                    import numpy as np  # type: ignore

                    self._hnswlib = hnswlib
                    self._np = np
                    self.index = hnswlib.Index(space="cosine", dim=dimension)
                    self.backend = "hnswlib"
                    break
                except ImportError as exc:
                    errors.append(str(exc))
            else:
                raise ComponentUnavailable(f"unsupported HNSW backend: {candidate}")
        if self.index is None:
            raise ComponentUnavailable(
                "USearch or hnswlib with numpy is required for semantic indexing"
            )
        self.loaded = False

    def load_or_create(self, max_elements: int) -> None:
        if self.backend == "usearch":
            if self.path.is_file():
                self.index.load(str(self.path))
                self.loaded = True
            return
        if self.path.is_file():
            self.index.load_index(str(self.path), max_elements=max_elements)
            self.loaded = True
        else:
            self.index.init_index(
                max_elements=max(1000, max_elements),
                ef_construction=200,
                M=16,
                allow_replace_deleted=True,
            )
        self.index.set_ef(HNSW_EXPANSION_SEARCH)

    def ensure_capacity(self, needed: int) -> None:
        if self.backend == "usearch":
            return
        current = self.index.get_max_elements()
        if needed > current:
            self.index.resize_index(max(needed, int(current * 1.5)))

    def add(self, labels: list[int], vectors: list[list[float]]) -> None:
        if not labels:
            return
        array = self._np.asarray(vectors, dtype=self._np.float32)
        if array.ndim != 2 or array.shape[1] != self.dimension:
            raise ValueError("embedding dimension mismatch")
        if not self._np.isfinite(array).all():
            raise ValueError("embedding contains NaN or infinity")
        norms = self._np.linalg.norm(array, axis=1, keepdims=True)
        if (norms == 0).any():
            raise ValueError("embedding contains a zero vector")
        array = array / norms
        if self.backend == "usearch":
            self.index.add(
                self._np.asarray(labels, dtype=self._np.uint64),
                array,
            )
        else:
            self.index.add_items(array, self._np.asarray(labels, dtype=self._np.int64))

    def mark_deleted(self, label: int) -> None:
        try:
            if self.backend == "usearch":
                self.index.remove(label)
            else:
                self.index.mark_deleted(label)
        except (RuntimeError, KeyError):
            pass

    def search(self, vector: list[float], count: int) -> list[tuple[int, float]]:
        if self.backend == "usearch":
            matches = self.index.search(
                self._np.asarray(vector, dtype=self._np.float32),
                count=count,
            )
            return [
                (int(label), max(0.0, 1.0 - float(distance)))
                for label, distance in zip(matches.keys, matches.distances)
            ]
        array = self._np.asarray([vector], dtype=self._np.float32)
        labels, distances = self.index.knn_query(array, k=count)
        return [
            (int(label), max(0.0, 1.0 - float(distance)))
            for label, distance in zip(labels[0], distances[0])
            if int(label) >= 0
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + f".{uuid.uuid4().hex}.tmp")
        if self.backend == "usearch":
            self.index.save(str(temp_path))
        else:
            self.index.save_index(str(temp_path))
        os.replace(temp_path, self.path)


def _upsert_records(connection: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    sql = """
        INSERT INTO records(
            record_id,organization_id,analysis_id,paper_id,class_id,student_ref,student_name,
            attempt_id,question_id,subject,grade,curriculum_version,question_type,
            question_text,answer_text,reference_answer,error_text,review_status,
            score,max_score,event_date,evidence_json,source_refs_json,content_text,
            deleted,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(record_id) DO UPDATE SET
            organization_id=excluded.organization_id,
            analysis_id=excluded.analysis_id,
            paper_id=excluded.paper_id,
            class_id=excluded.class_id,
            student_ref=excluded.student_ref,
            student_name=excluded.student_name,
            attempt_id=excluded.attempt_id,
            question_id=excluded.question_id,
            subject=excluded.subject,
            grade=excluded.grade,
            curriculum_version=excluded.curriculum_version,
            question_type=excluded.question_type,
            question_text=excluded.question_text,
            answer_text=excluded.answer_text,
            reference_answer=excluded.reference_answer,
            error_text=excluded.error_text,
            review_status=excluded.review_status,
            score=excluded.score,
            max_score=excluded.max_score,
            event_date=excluded.event_date,
            evidence_json=excluded.evidence_json,
            source_refs_json=excluded.source_refs_json,
            content_text=excluded.content_text,
            deleted=0,
            updated_at=excluded.updated_at
    """
    now = utc_now()
    record_rows = [
        (
                record["record_id"],
                record["organization_id"],
                record["analysis_id"],
                record["paper_id"],
                record["class_id"],
                record["student_ref"],
                record["student_name"],
                record["attempt_id"],
                record["question_id"],
                record["subject"],
                record["grade"],
                record["curriculum_version"],
                record["question_type"],
                record["question_text"],
                record["answer_text"],
                record["reference_answer"],
                record["error_text"],
                record["review_status"],
                record["score"],
                record["max_score"],
                record["event_date"],
                record["evidence_json"],
                record["source_refs_json"],
                record["content_text"],
                0,
                now,
        )
        for record in records
    ]
    connection.executemany(sql, record_rows)
    id_rows = [(record["record_id"],) for record in records]
    connection.executemany("DELETE FROM records_fts WHERE record_id=?", id_rows)
    connection.executemany(
        "INSERT INTO records_fts(record_id,content) VALUES(?,?)",
        [
            (
                record["record_id"],
                f"{fts_document(record['content_text'])} {' '.join(_record_scope_tokens(record))}",
            )
            for record in records
        ],
    )
    connection.executemany("DELETE FROM record_tags WHERE record_id=?", id_rows)
    connection.executemany(
        "INSERT OR REPLACE INTO record_tags(record_id,dimension,name,confidence) VALUES(?,?,?,?)",
        [
            (
                record["record_id"],
                tag["dimension"],
                tag["name"],
                tag.get("confidence"),
            )
            for record in records
            for tag in record["tags"]
        ],
    )


def _update_vectors(
    connection: sqlite3.Connection,
    records: SpillableJsonRows,
    vector_path: str | Path,
    provider: EmbeddingProvider | None,
    reset: bool,
    batch_size: int = 512,
) -> list[str]:
    degraded: list[str] = []
    fingerprint = provider.model_fingerprint if provider else None
    dimension = provider.dimension if provider else None
    prepared_count = records.count if provider else 0
    if provider is None:
        for batch in records.iter_batches(batch_size):
            for record in batch:
                vector = record.get("embedding")
                record_fingerprint = record.get("embedding_model_fingerprint")
                if vector is None or not record_fingerprint:
                    continue
                if fingerprint is None:
                    fingerprint = record_fingerprint
                    dimension = len(vector)
                if record_fingerprint != fingerprint or len(vector) != dimension:
                    raise ValueError("precomputed embeddings use inconsistent models or dimensions")
                prepared_count += 1
    if not prepared_count:
        return ["semantic_index_not_updated"]
    assert fingerprint is not None and dimension is not None

    stored_fingerprint = connection.execute(
        "SELECT value FROM metadata WHERE key='vector_model_fingerprint'"
    ).fetchone()
    if stored_fingerprint and stored_fingerprint["value"] != fingerprint and not reset:
        raise ValueError("embedding model changed; run index rebuild")
    backend_row = connection.execute(
        "SELECT value FROM metadata WHERE key='vector_backend'"
    ).fetchone()
    manager = HNSWIndexManager(
        vector_path,
        dimension,
        fingerprint,
        backend=None if reset else backend_row["value"] if backend_row else None,
    )
    if reset:
        Path(vector_path).unlink(missing_ok=True)
        connection.execute("DELETE FROM vector_labels")
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('vector_next_label','0') "
            "ON CONFLICT(key) DO UPDATE SET value='0'"
        )
    count = connection.execute("SELECT COUNT(*) AS n FROM vector_labels").fetchone()["n"]
    manager.load_or_create(max(1000, count + prepared_count * 2))
    manager.ensure_capacity(count + prepared_count * 2 + 100)
    next_row = connection.execute(
        "SELECT value FROM metadata WHERE key='vector_next_label'"
    ).fetchone()
    next_label = int(next_row["value"]) if next_row else 0

    for batch in records.iter_batches(batch_size):
        if provider:
            vectors = provider.encode([record["content_text"] for record in batch])
            if len(vectors) != len(batch):
                raise ValueError("embedding provider returned an unexpected vector count")
            prepared = list(zip(batch, vectors))
        else:
            prepared = []
            for record in batch:
                vector = record.get("embedding")
                record_fingerprint = record.get("embedding_model_fingerprint")
                if vector is not None and record_fingerprint:
                    prepared.append((record, [float(value) for value in vector]))
        labels: list[int] = []
        vectors_to_add: list[list[float]] = []
        for record, vector in prepared:
            if provider:
                vector = [float(value) for value in vector]
            old = connection.execute(
                "SELECT label FROM vector_labels WHERE record_id=?", (record["record_id"],)
            ).fetchone()
            if old:
                manager.mark_deleted(int(old["label"]))
            label = next_label
            next_label += 1
            connection.execute(
                "INSERT INTO vector_labels(record_id,label,model_fingerprint,deleted) VALUES(?,?,?,0) "
                "ON CONFLICT(record_id) DO UPDATE SET label=excluded.label,"
                "model_fingerprint=excluded.model_fingerprint,deleted=0",
                (record["record_id"], label, fingerprint),
            )
            labels.append(label)
            vectors_to_add.append(vector)
        manager.add(labels, vectors_to_add)
    manager.save()
    metadata = {
        "vector_model_fingerprint": fingerprint,
        "vector_dimension": str(dimension),
        "vector_next_label": str(next_label),
        "vector_model_id": getattr(provider, "model_id", "precomputed"),
        "vector_license_id": getattr(provider, "license_id", "provided-with-input"),
        "vector_backend": manager.backend,
    }
    connection.executemany(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        metadata.items(),
    )
    return degraded


def _index_document_in_place(
    data: dict[str, Any],
    database_path: str | Path,
    mode: str = "update",
    embedding_provider: EmbeddingProvider | None = None,
    vector_path: str | Path | None = None,
    require_semantic: bool = False,
    memory_threshold_mb: int | None = None,
    spill_directory: str | Path | None = None,
) -> dict[str, Any]:
    if mode not in {"build", "update", "rebuild"}:
        raise ValueError("mode must be build, update or rebuild")
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    vector_path = Path(vector_path) if vector_path else database_path.with_suffix(".hnsw")
    reset = mode == "rebuild"
    threshold_bytes = resolve_memory_threshold_bytes(memory_threshold_mb)
    spill_path = resolve_spill_directory(spill_directory, preferred=database_path.parent)
    with SpillableJsonRows(threshold_bytes, spill_path) as records:
        for record in iter_records_from_document(data):
            records.append(record)
        spilled_to_disk = records.spilled_to_disk
        with connect_database(database_path) as connection:
            initialize_database(connection, data["organization_id"], reset=reset)
            tokenizer_row = connection.execute(
                "SELECT value FROM metadata WHERE key='fts_tokenizer_version'"
            ).fetchone()
            existing_count = connection.execute(
                "SELECT COUNT(*) AS n FROM records"
            ).fetchone()["n"]
            if (
                not reset
                and existing_count
                and (
                    tokenizer_row is None
                    or tokenizer_row["value"] != FTS_TOKENIZER_VERSION
                )
            ):
                raise ValueError("FTS tokenizer version changed; run index rebuild")
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('fts_tokenizer_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (FTS_TOKENIZER_VERSION,),
            )
            for batch in records.iter_batches():
                _upsert_records(connection, batch)
            try:
                degraded = _update_vectors(
                    connection,
                    records,
                    vector_path,
                    embedding_provider,
                    reset=reset,
                )
            except ComponentUnavailable as exc:
                if require_semantic:
                    raise
                degraded = [f"semantic_index:{exc}"]
            if require_semantic and degraded:
                raise ComponentUnavailable(
                    "semantic indexing was required but no usable embedding/index component was available"
                )
            index_version = f"idx-{uuid.uuid4().hex[:16]}"
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('index_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (index_version,),
            )
            _database_audit(
                connection,
                f"index.{mode}",
                {
                    "analysis_id": data["analysis_id"],
                    "records": records.count,
                    "index_version": index_version,
                    "degraded_components": degraded,
                    "intermediate_spilled_to_disk": spilled_to_disk,
                    "memory_threshold_bytes": threshold_bytes,
                },
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "database": str(database_path),
        "vector_index": str(vector_path),
        "index_version": index_version,
        "records_indexed": records.count,
        "degraded_components": degraded,
        "memory": {
            "threshold_bytes": threshold_bytes,
            "intermediate_spilled_to_disk": spilled_to_disk,
            "spill_directory": str(spill_path),
        },
    }


def _cleanup_rebuild_files(path: Path) -> None:
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
    ):
        candidate.unlink(missing_ok=True)


def index_document(
    data: dict[str, Any],
    database_path: str | Path,
    mode: str = "update",
    embedding_provider: EmbeddingProvider | None = None,
    vector_path: str | Path | None = None,
    require_semantic: bool = False,
    memory_threshold_mb: int | None = None,
    spill_directory: str | Path | None = None,
) -> dict[str, Any]:
    if mode != "rebuild":
        return _index_document_in_place(
            data,
            database_path,
            mode=mode,
            embedding_provider=embedding_provider,
            vector_path=vector_path,
            require_semantic=require_semantic,
            memory_threshold_mb=memory_threshold_mb,
            spill_directory=spill_directory,
        )

    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    vector_path = Path(vector_path) if vector_path else database_path.with_suffix(".hnsw")
    existing_owner = _read_database_organization(database_path)
    if existing_owner is not None and existing_owner != data["organization_id"]:
        raise ValueError(
            f"database belongs to organization {existing_owner}, not {data['organization_id']}"
        )

    token = uuid.uuid4().hex
    temporary_database = database_path.with_name(f".{database_path.name}.{token}.rebuild")
    temporary_vector = vector_path.with_name(f".{vector_path.name}.{token}.rebuild")
    try:
        result = _index_document_in_place(
            data,
            temporary_database,
            mode="rebuild",
            embedding_provider=embedding_provider,
            vector_path=temporary_vector,
            require_semantic=require_semantic,
            memory_threshold_mb=memory_threshold_mb,
            spill_directory=spill_directory,
        )
        if temporary_vector.is_file():
            vector_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary_vector, vector_path)
        else:
            vector_path.unlink(missing_ok=True)
        os.replace(temporary_database, database_path)
        result["database"] = str(database_path)
        result["vector_index"] = str(vector_path)
        result["rebuild_strategy"] = "same-volume-temporary-then-atomic-replace"
        return result
    finally:
        _cleanup_rebuild_files(temporary_database)
        _cleanup_rebuild_files(temporary_vector)


def _filter_clause(filters: dict[str, Any], alias: str = "r") -> tuple[str, list[Any]]:
    clauses = [f"{alias}.deleted=0"]
    params: list[Any] = []
    unsupported = set(filters) - ALLOWED_EXACT_FILTERS - set(TAG_FILTERS) - {
        "organization_id",
        "score_min",
        "score_max",
        "date_from",
        "date_to",
    }
    if unsupported:
        raise ValueError(f"unsupported filters: {', '.join(sorted(unsupported))}")
    for key in sorted(ALLOWED_EXACT_FILTERS):
        if key not in filters or filters[key] is None:
            continue
        values = filters[key] if isinstance(filters[key], list) else [filters[key]]
        if len(values) > MAX_FILTER_VALUES:
            raise ValueError(
                f"filter {key} exceeds {MAX_FILTER_VALUES} values"
            )
        if not values:
            clauses.append("1=0")
            continue
        placeholders = ",".join("?" for _ in values)
        clauses.append(f"{alias}.{key} IN ({placeholders})")
        params.extend(values)
    for key, operator in (("score_min", ">="), ("score_max", "<=")):
        if filters.get(key) is not None:
            clauses.append(f"{alias}.score {operator} ?")
            params.append(filters[key])
    for key, operator in (("date_from", ">="), ("date_to", "<=")):
        if filters.get(key) is not None:
            clauses.append(f"{alias}.event_date {operator} ?")
            params.append(filters[key])
    for key, dimension in TAG_FILTERS.items():
        values = filters.get(key)
        if values is None:
            continue
        values = values if isinstance(values, list) else [values]
        if len(values) > MAX_FILTER_VALUES:
            raise ValueError(
                f"filter {key} exceeds {MAX_FILTER_VALUES} values"
            )
        for value in values:
            clauses.append(
                f"EXISTS (SELECT 1 FROM record_tags rt WHERE rt.record_id={alias}.record_id "
                "AND rt.dimension=? AND rt.name=?)"
            )
            params.extend([dimension, value])
    return " AND ".join(clauses), params


def _lexical_ranks(
    connection: sqlite3.Connection,
    query: str,
    filters: dict[str, Any],
    limit: int,
) -> list[str]:
    match_query = _fts_query(query)
    if not match_query:
        return []
    scope_query = _scope_query(filters)
    if scope_query:
        match_query = f"({match_query}) AND {scope_query}"
    where, params = _filter_clause(filters)
    window = max(1000, min(5000, limit * 10))
    rows = connection.execute(
        "WITH candidates AS MATERIALIZED ("
        "SELECT r.record_id,bm25(records_fts) AS rank "
        "FROM records_fts JOIN records r ON r.record_id=records_fts.record_id "
        f"WHERE records_fts MATCH ? AND {where} LIMIT ?"
        ") SELECT record_id FROM candidates ORDER BY rank,record_id LIMIT ?",
        [match_query, *params, window, limit],
    ).fetchall()
    return [row["record_id"] for row in rows]


def _ngram_set(text: str) -> set[str]:
    normalized = normalize_text(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def _tag_ranks(
    connection: sqlite3.Connection,
    query: str,
    filters: dict[str, Any],
    limit: int,
) -> list[str]:
    query_grams = _ngram_set(query)
    if not query_grams:
        return []
    names = connection.execute(
        "SELECT DISTINCT dimension,name FROM record_tags"
    ).fetchall()
    matching = []
    for row in names:
        alias = TAG_ALIASES.get(row["name"], "")
        name_grams = _ngram_set(f"{row['name']} {alias}")
        union = query_grams | name_grams
        score = len(query_grams & name_grams) / len(union) if union else 0.0
        if normalize_text(row["name"]) in normalize_text(query) or (
            alias and normalize_text(alias) in normalize_text(query)
        ):
            score += 1.0
        if score > 0:
            matching.append((score, row["dimension"], row["name"]))
    matching.sort(key=lambda item: (-item[0], item[1], item[2]))
    if not matching:
        return []
    where, params = _filter_clause(filters)
    rank_by_record: dict[str, float] = {}
    for score, dimension, name in matching[:20]:
        rows = connection.execute(
            "SELECT r.record_id FROM record_tags rt "
            "JOIN records r ON r.record_id=rt.record_id "
            f"WHERE rt.dimension=? AND rt.name=? AND {where} LIMIT ?",
            [dimension, name, *params, limit],
        ).fetchall()
        for row in rows:
            rank_by_record[row["record_id"]] = max(
                rank_by_record.get(row["record_id"], 0.0), score
            )
    return [
        record_id
        for record_id, _ in sorted(
            rank_by_record.items(), key=lambda item: (-item[1], item[0])
        )[:limit]
    ]


def _semantic_ranks(
    connection: sqlite3.Connection,
    query: str,
    filters: dict[str, Any],
    limit: int,
    vector_path: str | Path,
    provider: EmbeddingProvider,
) -> list[str]:
    fingerprint_row = connection.execute(
        "SELECT value FROM metadata WHERE key='vector_model_fingerprint'"
    ).fetchone()
    dimension_row = connection.execute(
        "SELECT value FROM metadata WHERE key='vector_dimension'"
    ).fetchone()
    if not fingerprint_row or not dimension_row or not Path(vector_path).is_file():
        raise ComponentUnavailable("semantic index is unavailable")
    if fingerprint_row["value"] != provider.model_fingerprint:
        raise ComponentUnavailable("query model fingerprint does not match the semantic index")
    dimension = int(dimension_row["value"])
    if dimension != provider.dimension:
        raise ComponentUnavailable("query model dimension does not match the semantic index")
    count = connection.execute(
        "SELECT COUNT(*) AS n FROM vector_labels WHERE deleted=0"
    ).fetchone()["n"]
    if not count:
        return []
    backend_row = connection.execute(
        "SELECT value FROM metadata WHERE key='vector_backend'"
    ).fetchone()
    manager = HNSWIndexManager(
        vector_path,
        dimension,
        provider.model_fingerprint,
        backend=backend_row["value"] if backend_row else None,
    )
    manager.load_or_create(count + 100)
    vector = provider.encode([query])[0]
    candidates = manager.search(vector, min(count, max(limit * 10, limit)))
    labels = [label for label, _ in candidates]
    if not labels:
        return []
    label_to_rank = {label: index for index, label in enumerate(labels)}
    placeholders = ",".join("?" for _ in labels)
    where, params = _filter_clause(filters)
    rows = connection.execute(
        "SELECT vl.label,r.record_id FROM vector_labels vl "
        "JOIN records r ON r.record_id=vl.record_id "
        f"WHERE vl.label IN ({placeholders}) AND vl.deleted=0 AND {where}",
        [*labels, *params],
    ).fetchall()
    rows = sorted(rows, key=lambda row: label_to_rank[row["label"]])
    return [row["record_id"] for row in rows[:limit]]


def search_index(
    database_path: str | Path,
    query: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 20,
    candidate_limit: int = 200,
    embedding_provider: EmbeddingProvider | None = None,
    vector_path: str | Path | None = None,
    require_semantic: bool = False,
) -> dict[str, Any]:
    if not 1 <= top_k <= 100:
        raise ValueError("top_k must be between 1 and 100")
    if not 1 <= candidate_limit <= MAX_CANDIDATE_LIMIT:
        raise ValueError(
            f"candidate_limit must be between 1 and {MAX_CANDIDATE_LIMIT}"
        )
    if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be a string of at most {MAX_QUERY_LENGTH} characters")
    filters = filters or {}
    database_path = Path(database_path)
    vector_path = Path(vector_path) if vector_path else database_path.with_suffix(".hnsw")
    degraded: list[str] = []
    with connect_database(database_path) as connection:
        org_row = connection.execute(
            "SELECT value FROM metadata WHERE key='organization_id'"
        ).fetchone()
        if not org_row:
            raise ValueError("database is not initialized")
        if filters.get("organization_id") and filters["organization_id"] != org_row["value"]:
            raise ValueError("organization filter does not match the database")
        index_row = connection.execute(
            "SELECT value FROM metadata WHERE key='index_version'"
        ).fetchone()
        lexical = _lexical_ranks(connection, query, filters, candidate_limit)
        tag = _tag_ranks(connection, query, filters, candidate_limit)
        semantic: list[str] = []
        if embedding_provider is None:
            degraded.append("semantic_embedding_provider")
        else:
            try:
                semantic = _semantic_ranks(
                    connection,
                    query,
                    filters,
                    candidate_limit,
                    vector_path,
                    embedding_provider,
                )
            except ComponentUnavailable as exc:
                degraded.append(f"semantic_index:{exc}")
        if require_semantic and not semantic:
            raise ComponentUnavailable("semantic retrieval was required but is unavailable")

        ranks = {"lexical": lexical, "semantic": semantic, "tag": tag}
        fused: dict[str, float] = {}
        components: dict[str, dict[str, Any]] = {}
        for component, record_ids in ranks.items():
            if not record_ids:
                continue
            weight = RETRIEVAL_WEIGHTS[component]
            for rank, record_id in enumerate(record_ids, start=1):
                score = weight / (RRF_K + rank)
                fused[record_id] = fused.get(record_id, 0.0) + score
                components.setdefault(record_id, {})[component] = {
                    "rank": rank,
                    "rrf_score": round(score, 8),
                }
        ordered = [
            record_id
            for record_id, _ in sorted(
                fused.items(), key=lambda item: (-item[1], item[0])
            )[:top_k]
        ]
        if not ordered and not query:
            where, params = _filter_clause(filters)
            rows = connection.execute(
                f"SELECT record_id FROM records r WHERE {where} ORDER BY record_id LIMIT ?",
                [*params, top_k],
            ).fetchall()
            ordered = [row["record_id"] for row in rows]
            fused = {record_id: 0.0 for record_id in ordered}
        if ordered:
            placeholders = ",".join("?" for _ in ordered)
            rows = connection.execute(
                f"SELECT * FROM records WHERE record_id IN ({placeholders})",
                ordered,
            ).fetchall()
            by_id = {row["record_id"]: row for row in rows}
        else:
            by_id = {}
        query_normalized = normalize_text(query)
        results = []
        for record_id in ordered:
            row = by_id.get(record_id)
            if not row:
                continue
            source_text = row["question_text"] or row["error_text"] or row["answer_text"] or ""
            snippet = source_text[:240]
            if query_normalized:
                position = normalize_text(source_text).find(query_normalized)
                if position >= 0:
                    snippet = source_text[max(0, position - 60) : position + len(query) + 120]
            results.append(
                {
                    "record_id": record_id,
                    "analysis_id": row["analysis_id"],
                    "paper_id": row["paper_id"],
                    "class_id": row["class_id"],
                    "student_ref": row["student_ref"],
                    "student_name": row["student_name"],
                    "attempt_id": row["attempt_id"],
                    "question_id": row["question_id"],
                    "subject": row["subject"],
                    "grade": row["grade"],
                    "curriculum_version": row["curriculum_version"],
                    "question_type": row["question_type"],
                    "review_status": row["review_status"],
                    "score": row["score"],
                    "max_score": row["max_score"],
                    "fused_score": round(fused.get(record_id, 0.0), 8),
                    "components": components.get(record_id, {}),
                    "snippet": snippet,
                    "evidence": json.loads(row["evidence_json"]),
                    "source_refs": json.loads(row["source_refs_json"]),
                }
            )
        if degraded:
            _database_audit(
                connection,
                "search.degraded",
                {
                    "query_hash": sha256_value(query),
                    "filters_hash": sha256_value(filters),
                    "degraded_components": degraded,
                    "index_version": index_row["value"] if index_row else None,
                },
            )
    return {
        "organization_id": org_row["value"],
        "query": query,
        "filters": filters,
        "top_k": top_k,
        "index_version": index_row["value"] if index_row else None,
        "degraded_components": degraded,
        "retrieval_profile": {
            "lexical": "bounded_bm25",
            "lexical_candidate_window": max(
                1000, min(5000, candidate_limit * 10)
            ),
            "rrf_k": RRF_K,
            "weights": RETRIEVAL_WEIGHTS,
        },
        "results": results,
    }


def purge_records(
    database_path: str | Path,
    student_ref: str | None = None,
    attempt_id: str | None = None,
    vector_path: str | Path | None = None,
) -> dict[str, Any]:
    if bool(student_ref) == bool(attempt_id):
        raise ValueError("provide exactly one of student_ref or attempt_id")
    database_path = Path(database_path)
    vector_path = Path(vector_path) if vector_path else database_path.with_suffix(".hnsw")
    field, value = ("student_ref", student_ref) if student_ref else ("attempt_id", attempt_id)
    with connect_database(database_path) as connection:
        rows = connection.execute(
            f"SELECT record_id FROM records WHERE {field}=?", (value,)
        ).fetchall()
        record_ids = [row["record_id"] for row in rows]
        if not record_ids:
            return {"purged": 0, "vector_rebuild_required": False}
        labels = connection.execute(
            f"SELECT label FROM vector_labels WHERE record_id IN ({','.join('?' for _ in record_ids)})",
            record_ids,
        ).fetchall()
        for record_id in record_ids:
            connection.execute("DELETE FROM records_fts WHERE record_id=?", (record_id,))
            connection.execute("DELETE FROM records WHERE record_id=?", (record_id,))
        if labels and vector_path.is_file():
            fingerprint = connection.execute(
                "SELECT value FROM metadata WHERE key='vector_model_fingerprint'"
            ).fetchone()
            dimension = connection.execute(
                "SELECT value FROM metadata WHERE key='vector_dimension'"
            ).fetchone()
            if fingerprint and dimension:
                try:
                    backend = connection.execute(
                        "SELECT value FROM metadata WHERE key='vector_backend'"
                    ).fetchone()
                    manager = HNSWIndexManager(
                        vector_path,
                        int(dimension["value"]),
                        fingerprint["value"],
                        backend=backend["value"] if backend else None,
                    )
                    next_label = connection.execute(
                        "SELECT value FROM metadata WHERE key='vector_next_label'"
                    ).fetchone()
                    manager.load_or_create(max(1000, int(next_label["value"]) + 100 if next_label else 1000))
                    for label in labels:
                        manager.mark_deleted(int(label["label"]))
                    manager.save()
                except ComponentUnavailable:
                    pass
        _database_audit(
            connection,
            "index.purge",
            {
                "selector": field,
                "selector_hash": sha256_value(value),
                "records": len(record_ids),
            },
        )
    return {
        "purged": len(record_ids),
        "vector_rebuild_required": bool(labels),
    }


def benchmark(
    records: int = 10000,
    queries: int = 25,
    database_path: str | Path | None = None,
    include_semantic: bool = False,
) -> dict[str, Any]:
    if records < 1 or queries < 1:
        raise ValueError("records and queries must be positive")
    owned_temp = None
    if database_path is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="exam-error-benchmark-")
        database_path = Path(owned_temp.name) / "benchmark.sqlite3"
    database_path = Path(database_path)
    with connect_database(database_path) as connection:
        initialize_database(connection, "benchmark-org", reset=True)
        batch = []
        now = utc_now()
        base_records = min(records, 10000)
        for index in range(base_records):
            question_id = f"q{index % 1000}"
            record = {
                "record_id": f"rec-{index:012d}",
                "organization_id": "benchmark-org",
                "analysis_id": f"analysis-{index // 100}",
                "paper_id": f"paper-{index // 1000}",
                "class_id": f"class-{index % 20}",
                "student_ref": f"student-{index % 5000}",
                "student_name": f"Student {index % 5000}",
                "attempt_id": f"attempt-{index}",
                "question_id": question_id,
                "subject": "math",
                "grade": "grade-8",
                "curriculum_version": "benchmark",
                "question_type": "numeric",
                "question_text": f"第{index}题 二次方程 函数 计算 符号",
                "answer_text": str(index % 17),
                "reference_answer": str(index % 19),
                "error_text": "符号错误 计算错误" if index % 3 else "概念混淆",
                "review_status": "needs_review" if index % 7 == 0 else "auto_confirmed",
                "score": float(index % 5),
                "max_score": 5.0,
                "event_date": now,
                "evidence_json": "[]",
                "source_refs_json": "[]",
                "content_text": f"第{index}题 二次方程 函数 计算 符号错误",
                "tags": [
                    {"dimension": "knowledge", "name": "math/algebra/equation/quadratic", "confidence": 1.0},
                    {"dimension": "error", "name": "sign-error", "confidence": 0.9},
                ],
            }
            batch.append(record)
            if len(batch) >= 2000:
                _upsert_records(connection, batch)
                batch = []
        if batch:
            _upsert_records(connection, batch)
        for offset in range(base_records, records, base_records):
            copy_count = min(base_records, records - offset)
            prefix = f"dup{offset:012d}-"
            connection.execute(
                """
                INSERT INTO records(
                    record_id,organization_id,analysis_id,paper_id,class_id,student_ref,student_name,
                    attempt_id,question_id,subject,grade,curriculum_version,question_type,
                    question_text,answer_text,reference_answer,error_text,review_status,
                    score,max_score,event_date,evidence_json,source_refs_json,content_text,
                    deleted,updated_at
                )
                SELECT
                    ? || record_id,organization_id,analysis_id,paper_id,class_id,student_ref,student_name,
                    ? || attempt_id,question_id,subject,grade,curriculum_version,question_type,
                    question_text,answer_text,reference_answer,error_text,review_status,
                    score,max_score,event_date,evidence_json,source_refs_json,content_text,
                    deleted,updated_at
                FROM records
                WHERE record_id GLOB 'rec-*'
                ORDER BY record_id
                LIMIT ?
                """,
                (prefix, prefix, copy_count),
            )
            connection.execute(
                """
                INSERT INTO records_fts(record_id,content)
                SELECT ? || record_id,content
                FROM records_fts
                WHERE record_id GLOB 'rec-*'
                ORDER BY record_id
                LIMIT ?
                """,
                (prefix, copy_count),
            )
            connection.execute(
                """
                INSERT INTO record_tags(record_id,dimension,name,confidence)
                SELECT ? || rt.record_id,rt.dimension,rt.name,rt.confidence
                FROM record_tags rt
                WHERE rt.record_id IN (
                    SELECT record_id FROM records
                    WHERE record_id GLOB 'rec-*'
                    ORDER BY record_id
                    LIMIT ?
                )
                """,
                (prefix, copy_count),
            )
        connection.commit()
        latencies = []
        for index in range(queries):
            started = time.perf_counter()
            _lexical_ranks(
                connection,
                "二次方程 符号错误",
                {"class_id": f"class-{index % 20}"},
                200,
            )
            latencies.append((time.perf_counter() - started) * 1000)
    ordered = sorted(latencies)
    percentile_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    result = {
        "records": records,
        "queries": queries,
        "database": str(database_path),
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "lexical_filter_latency_ms": {
            "min": round(min(latencies), 3),
            "median": round(statistics.median(latencies), 3),
            "p95": round(ordered[percentile_index], 3),
            "max": round(max(latencies), 3),
        },
        "target": {
            "records": 1000000,
            "lexical_p95_ms": 500,
            "hybrid_p95_ms": 1000,
            "hnsw_recall_at_20": 0.90,
        },
        "target_evaluated": records >= 1000000,
        "hybrid_latency_ms": {
            "evaluated": False,
            "reason": "use --include-semantic with a USearch/hnswlib runtime",
        },
        "semantic_benchmark": {
            "evaluated": False,
            "reason": "use --include-semantic with a USearch/hnswlib runtime",
        },
    }
    if include_semantic:
        try:
            import numpy as np  # type: ignore

            vector_count = min(records, 1000000)
            dimension = 128
            rng = np.random.default_rng(20260729)
            vectors = rng.normal(size=(vector_count, dimension)).astype(np.float32)
            vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
            with tempfile.TemporaryDirectory(prefix="exam-hnsw-benchmark-") as vector_temp:
                vector_file = Path(vector_temp) / "benchmark.hnsw"
                manager = HNSWIndexManager(
                    vector_file,
                    dimension,
                    "benchmark:random-v1",
                )
                manager.load_or_create(vector_count + 100)
                manager.add(list(range(vector_count)), vectors)
                manager.save()
                recalls = []
                semantic_latencies = []
                query_count = min(20, vector_count)
                top_n = min(20, vector_count)
                for index in range(query_count):
                    query_vector = vectors[index]
                    exact_scores = vectors @ query_vector
                    exact = set(
                        np.argpartition(exact_scores, -top_n)[-top_n:].tolist()
                    )
                    started = time.perf_counter()
                    approximate = {
                        label
                        for label, _ in manager.search(query_vector.tolist(), top_n)
                    }
                    semantic_latencies.append((time.perf_counter() - started) * 1000)
                    recalls.append(len(exact & approximate) / top_n)
                backend = manager.backend
                with connect_database(database_path) as connection:
                    connection.execute("DELETE FROM vector_labels")
                    connection.execute(
                        "INSERT INTO vector_labels("
                        "record_id,label,model_fingerprint,deleted"
                        ") "
                        "SELECT record_id,ROW_NUMBER() OVER (ORDER BY record_id)-1,"
                        "?,0 FROM records ORDER BY record_id LIMIT ?",
                        ("benchmark:random-v1", vector_count),
                    )
                    connection.executemany(
                        "INSERT INTO metadata(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        [
                            ("vector_model_fingerprint", "benchmark:random-v1"),
                            ("vector_dimension", str(dimension)),
                            ("vector_backend", backend),
                            ("vector_model_id", "benchmark-random"),
                            ("vector_license_id", "synthetic"),
                        ],
                    )

                class BenchmarkProvider:
                    model_id = "benchmark-random"
                    model_fingerprint = "benchmark:random-v1"
                    license_id = "synthetic"
                    dimension = 128

                    def __init__(self, matrix):
                        self.matrix = matrix
                        self.query_index = 0

                    def encode(self, texts: list[str]) -> list[list[float]]:
                        return [
                            self.matrix[self.query_index].tolist()
                            for _ in texts
                        ]

                provider = BenchmarkProvider(vectors)
                hybrid_latencies = []
                for index in range(min(queries, query_count)):
                    provider.query_index = index
                    started = time.perf_counter()
                    search_index(
                        database_path,
                        "二次方程 符号错误",
                        filters={"class_id": f"class-{index % 20}"},
                        top_k=20,
                        candidate_limit=200,
                        embedding_provider=provider,
                        vector_path=vector_file,
                        require_semantic=True,
                    )
                    hybrid_latencies.append(
                        (time.perf_counter() - started) * 1000
                    )
                del manager
            semantic_ordered = sorted(semantic_latencies)
            semantic_p95_index = max(
                0, math.ceil(len(semantic_ordered) * 0.95) - 1
            )
            result["semantic_benchmark"] = {
                "evaluated": True,
                "backend": backend,
                "vectors": vector_count,
                "exact_baseline": "full_matrix_cosine",
                "dimension": dimension,
                "queries": query_count,
                "expansion_search": HNSW_EXPANSION_SEARCH,
                "recall_at_20": round(sum(recalls) / len(recalls), 4),
                "search_p95_ms": round(
                    semantic_ordered[semantic_p95_index], 3
                ),
                "target_recall_at_20": 0.90,
            }
            hybrid_ordered = sorted(hybrid_latencies)
            hybrid_p95_index = max(
                0, math.ceil(len(hybrid_ordered) * 0.95) - 1
            )
            result["hybrid_latency_ms"] = {
                "evaluated": True,
                "top_k": 20,
                "min": round(min(hybrid_latencies), 3),
                "median": round(statistics.median(hybrid_latencies), 3),
                "p95": round(hybrid_ordered[hybrid_p95_index], 3),
                "max": round(max(hybrid_latencies), 3),
            }
        except (ImportError, ComponentUnavailable, OSError, ValueError) as exc:
            result["semantic_benchmark"] = {
                "evaluated": False,
                "reason": str(exc),
            }
            result["hybrid_latency_ms"] = {
                "evaluated": False,
                "reason": str(exc),
            }
    result["acceptance"] = {
        "lexical_p95_pass": (
            result["lexical_filter_latency_ms"]["p95"]
            <= result["target"]["lexical_p95_ms"]
            if result["target_evaluated"]
            else None
        ),
        "hybrid_p95_pass": (
            result["hybrid_latency_ms"]["p95"]
            <= result["target"]["hybrid_p95_ms"]
            if result["target_evaluated"]
            and result["hybrid_latency_ms"].get("evaluated")
            else None
        ),
        "hnsw_recall_pass": (
            result["semantic_benchmark"]["recall_at_20"]
            >= result["target"]["hnsw_recall_at_20"]
            if result["target_evaluated"]
            and result["semantic_benchmark"].get("evaluated")
            else None
        ),
    }
    if owned_temp:
        owned_temp.cleanup()
        result["database"] = "temporary-cleaned"
    return result
