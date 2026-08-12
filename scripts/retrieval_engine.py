#!/usr/bin/env python3
"""SQLite FTS5 and tag retrieval for exam-error records.

The retrieval layer intentionally has no model, embedding, or vector-index
dependency.  It is therefore usable in the packaged core runtime.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from exam_error_app.retrieval_projection import iter_index_records, project_index_records
from exam_error_core import canonical_json, normalize_text, sha256_value, utc_now
from resource_limits import SpillableJsonRows, resolve_memory_threshold_bytes, resolve_spill_directory


RRF_K = 60
FTS_TOKENIZER_VERSION = "2"
RETRIEVAL_WEIGHTS = {"lexical": 0.70, "tag": 0.30}
ALLOWED_EXACT_FILTERS = {"class_id", "student_ref", "student_name", "subject", "grade", "curriculum_version", "paper_id", "question_id", "question_type", "review_status"}
TAG_FILTERS = {"knowledge_tags": "knowledge", "cognitive_tags": "cognitive", "error_tags": "error"}
MAX_QUERY_LENGTH = 4_096
MAX_CANDIDATE_LIMIT = 5_000
MAX_FILTER_VALUES = 100
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
TAG_ALIASES = {"concept-missing": "概念缺失", "concept-confusion": "概念混淆", "theorem-misuse": "定理误用", "formula-misuse": "公式误用", "calculation-error": "计算错误", "sign-error": "符号错误", "unanswered": "未作答"}


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def fts_tokens(text: Any) -> list[str]:
    result: list[str] = []
    for token in TOKEN_PATTERN.findall(normalize_text(text).casefold()):
        if CJK_PATTERN.fullmatch(token):
            for width in (2, 3):
                result.extend(token[index:index + width] for index in range(max(0, len(token) - width + 1)))
        elif token:
            result.append(token)
    return result


def fts_document(text: Any) -> str:
    return " ".join(fts_tokens(text))


def _fts_query(text: str) -> str:
    tokens = fts_tokens(text)
    return " AND ".join(f'"{token}"' for token in tokens[:128])


def connect_database(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path), timeout=30, factory=ManagedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _read_database_organization(path: Path) -> str | None:
    if not path.is_file():
        return None
    with closing(sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone():
            raise ValueError("existing database is not an analyze-exam-errors index")
        row = connection.execute("SELECT value FROM metadata WHERE key='organization_id'").fetchone()
        if not row:
            raise ValueError("existing index database has no organization owner")
        return str(row[0])


def initialize_database(connection: sqlite3.Connection, organization_id: str, reset: bool = False) -> None:
    if reset:
        owner = connection.execute("SELECT value FROM metadata WHERE key='organization_id'").fetchone() if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone() else None
        if owner and owner["value"] != organization_id:
            raise ValueError(f"database belongs to organization {owner['value']}, not {organization_id}")
        connection.executescript("DROP TABLE IF EXISTS records_fts; DROP TABLE IF EXISTS record_tags; DROP TABLE IF EXISTS records; DROP TABLE IF EXISTS index_audit; DROP TABLE IF EXISTS metadata;")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (
          record_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, analysis_id TEXT NOT NULL,
          paper_id TEXT NOT NULL, class_id TEXT, student_ref TEXT NOT NULL, student_name TEXT,
          attempt_id TEXT NOT NULL, question_id TEXT NOT NULL, subject TEXT NOT NULL, grade TEXT,
          curriculum_version TEXT, question_type TEXT NOT NULL, question_text TEXT, answer_text TEXT,
          reference_answer TEXT, error_text TEXT, review_status TEXT NOT NULL, score REAL,
          max_score REAL NOT NULL, event_date TEXT, evidence_json TEXT NOT NULL,
          source_refs_json TEXT NOT NULL, content_text TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_records_scope ON records(paper_id,class_id,student_ref,question_id);
        CREATE INDEX IF NOT EXISTS idx_records_student_name ON records(student_name);
        CREATE TABLE IF NOT EXISTS record_tags (
          record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
          dimension TEXT NOT NULL, name TEXT NOT NULL, confidence REAL,
          PRIMARY KEY(record_id,dimension,name));
        CREATE INDEX IF NOT EXISTS idx_record_tags_lookup ON record_tags(dimension,name,record_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(record_id UNINDEXED,content,tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS index_audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT,timestamp TEXT NOT NULL,event_type TEXT NOT NULL,payload_hash TEXT NOT NULL,previous_hash TEXT,event_hash TEXT NOT NULL);
    """)
    owner = connection.execute("SELECT value FROM metadata WHERE key='organization_id'").fetchone()
    if owner and owner["value"] != organization_id:
        raise ValueError(f"database belongs to organization {owner['value']}, not {organization_id}")
    connection.execute("INSERT INTO metadata(key,value) VALUES('organization_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (organization_id,))
    for key, value in (("audit_event_count", "0"), ("audit_head_hash", "")):
        connection.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING", (key, value))


def _database_audit(connection: sqlite3.Connection, event_type: str, payload: Any) -> None:
    anchors = {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM metadata WHERE key IN ('audit_event_count','audit_head_hash')")}
    previous_row = connection.execute("SELECT event_hash FROM index_audit ORDER BY sequence DESC LIMIT 1").fetchone()
    previous = previous_row["event_hash"] if previous_row else None
    count = connection.execute("SELECT COUNT(*) AS n FROM index_audit").fetchone()["n"]
    if anchors != {"audit_event_count": str(count), "audit_head_hash": previous or ""}:
        raise ValueError("database audit chain does not match its anchor")
    timestamp = utc_now()
    payload_hash = sha256_value(payload)
    event_hash = sha256_value({"timestamp": timestamp, "event_type": event_type, "payload_hash": payload_hash, "previous_hash": previous})
    connection.execute("INSERT INTO index_audit(timestamp,event_type,payload_hash,previous_hash,event_hash) VALUES(?,?,?,?,?)", (timestamp,event_type,payload_hash,previous,event_hash))
    connection.executemany("UPDATE metadata SET value=? WHERE key=?", ((str(count + 1), "audit_event_count"), (event_hash, "audit_head_hash")))


def verify_database_audit(path: str | Path) -> list[str]:
    errors: list[str] = []
    with connect_database(path) as connection:
        anchors = {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM metadata WHERE key IN ('audit_event_count','audit_head_hash')")}
        if set(anchors) != {"audit_event_count", "audit_head_hash"}:
            return ["index_audit: audit anchor is missing or incomplete"]
        previous = None
        rows = connection.execute("SELECT * FROM index_audit ORDER BY sequence").fetchall()
        for row in rows:
            expected = sha256_value({"timestamp": row["timestamp"], "event_type": row["event_type"], "payload_hash": row["payload_hash"], "previous_hash": row["previous_hash"]})
            if row["previous_hash"] != previous or row["event_hash"] != expected:
                errors.append(f"index_audit[{row['sequence']}]: audit chain mismatch")
            previous = row["event_hash"]
        if anchors["audit_event_count"] != str(len(rows)) or anchors["audit_head_hash"] != (previous or ""):
            errors.append("index_audit: audit anchor does not match audit chain")
    return errors


def records_from_document(data: dict[str, Any]) -> list[dict[str, Any]]:
    return project_index_records(data, normalize_text=normalize_text, canonical_json=canonical_json, tag_aliases=TAG_ALIASES)


def iter_records_from_document(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return iter_index_records(data, normalize_text=normalize_text, canonical_json=canonical_json, tag_aliases=TAG_ALIASES)


def _upsert_records(connection: sqlite3.Connection, records: list[dict[str, Any]]) -> None:
    columns = "record_id,organization_id,analysis_id,paper_id,class_id,student_ref,student_name,attempt_id,question_id,subject,grade,curriculum_version,question_type,question_text,answer_text,reference_answer,error_text,review_status,score,max_score,event_date,evidence_json,source_refs_json,content_text,deleted,updated_at"
    placeholders = ",".join("?" for _ in range(26))
    updates = ",".join(f"{name}=excluded.{name}" for name in columns.split(",")[1:])
    for record in records:
        row = [record.get(name) for name in columns.split(",")[:-2]] + [0, utc_now()]
        connection.execute(f"INSERT INTO records({columns}) VALUES({placeholders}) ON CONFLICT(record_id) DO UPDATE SET {updates}", row)
        connection.execute("DELETE FROM record_tags WHERE record_id=?", (record["record_id"],))
        connection.execute("DELETE FROM records_fts WHERE record_id=?", (record["record_id"],))
        connection.execute("INSERT INTO records_fts(record_id,content) VALUES(?,?)", (record["record_id"], fts_document(record["content_text"])))
        connection.executemany("INSERT INTO record_tags(record_id,dimension,name,confidence) VALUES(?,?,?,?)", [(record["record_id"], tag["dimension"], tag["name"], tag.get("confidence")) for tag in record["tags"]])


def _index_document_in_place(data: dict[str, Any], database_path: str | Path, mode: str, memory_threshold_mb: int | None, spill_directory: str | Path | None) -> dict[str, Any]:
    if mode not in {"build", "update", "rebuild"}:
        raise ValueError("mode must be build, update or rebuild")
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    threshold = resolve_memory_threshold_bytes(memory_threshold_mb)
    spill_path = resolve_spill_directory(spill_directory, preferred=database_path.parent)
    with SpillableJsonRows(threshold, spill_path) as records:
        for record in iter_records_from_document(data): records.append(record)
        with connect_database(database_path) as connection:
            initialize_database(connection, data["organization_id"], reset=mode == "rebuild")
            connection.execute("INSERT INTO metadata(key,value) VALUES('fts_tokenizer_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (FTS_TOKENIZER_VERSION,))
            for batch in records.iter_batches(): _upsert_records(connection, batch)
            connection.execute("INSERT INTO metadata(key,value) VALUES('index_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(uuid.uuid4()),))
            _database_audit(connection, "index.write", {"mode": mode, "records": records.count})
            connection.commit()
    return {"database": str(database_path), "records_indexed": records.count, "degraded_components": [], "memory": {"threshold_bytes": threshold, "intermediate_spilled_to_disk": records.spilled_to_disk, "spill_directory": str(spill_path)}}


def index_document(data: dict[str, Any], database_path: str | Path, mode: str = "update", memory_threshold_mb: int | None = None, spill_directory: str | Path | None = None) -> dict[str, Any]:
    path = Path(database_path)
    if mode != "rebuild":
        return _index_document_in_place(data, path, mode, memory_threshold_mb, spill_directory)
    owner = _read_database_organization(path)
    if owner is not None and owner != data["organization_id"]:
        raise ValueError(f"database belongs to organization {owner}, not {data['organization_id']}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.rebuild")
    try:
        result = _index_document_in_place(data, temporary, "rebuild", memory_threshold_mb, spill_directory)
        temporary.replace(path)
        result.update(database=str(path), rebuild_strategy="same-volume-temporary-then-atomic-replace")
        return result
    finally:
        for candidate in (temporary, Path(str(temporary) + "-wal"), Path(str(temporary) + "-shm")):
            candidate.unlink(missing_ok=True)


def _filter_clause(filters: dict[str, Any], alias: str = "r") -> tuple[str, list[Any]]:
    unsupported = set(filters) - ALLOWED_EXACT_FILTERS - set(TAG_FILTERS) - {"organization_id", "score_min", "score_max", "date_from", "date_to"}
    if unsupported: raise ValueError(f"unsupported filters: {', '.join(sorted(unsupported))}")
    clauses, params = [f"{alias}.deleted=0"], []
    for key in sorted(ALLOWED_EXACT_FILTERS):
        if filters.get(key) is not None:
            values = filters[key] if isinstance(filters[key], list) else [filters[key]]
            if len(values) > MAX_FILTER_VALUES: raise ValueError(f"filter {key} exceeds {MAX_FILTER_VALUES} values")
            clauses.append(f"{alias}.{key} IN ({','.join('?' for _ in values)})" if values else "1=0"); params.extend(values)
    for key, operator in (("score_min", ">="), ("score_max", "<="), ("date_from", ">="), ("date_to", "<=")):
        if filters.get(key) is not None:
            column = "score" if key.startswith("score") else "event_date"; clauses.append(f"{alias}.{column} {operator} ?"); params.append(filters[key])
    for key, dimension in TAG_FILTERS.items():
        for value in ([] if filters.get(key) is None else filters[key] if isinstance(filters[key], list) else [filters[key]]):
            clauses.append(f"EXISTS (SELECT 1 FROM record_tags rt WHERE rt.record_id={alias}.record_id AND rt.dimension=? AND rt.name=?)"); params.extend((dimension, value))
    return " AND ".join(clauses), params


def _lexical_ranks(connection: sqlite3.Connection, query: str, filters: dict[str, Any], limit: int) -> list[str]:
    match = _fts_query(query)
    if not match: return []
    where, params = _filter_clause(filters)
    rows = connection.execute(f"SELECT r.record_id,bm25(records_fts) rank FROM records_fts JOIN records r ON r.record_id=records_fts.record_id WHERE records_fts MATCH ? AND {where} ORDER BY rank,r.record_id LIMIT ?", (match, *params, limit)).fetchall()
    return [row["record_id"] for row in rows]


def _tag_ranks(connection: sqlite3.Connection, query: str, filters: dict[str, Any], limit: int) -> list[str]:
    normalized = normalize_text(query)
    if not normalized: return []
    where, params = _filter_clause(filters)
    rows = connection.execute(f"SELECT DISTINCT r.record_id FROM record_tags rt JOIN records r ON r.record_id=rt.record_id WHERE {where} AND (instr(?,rt.name)>0 OR instr(?,COALESCE(rt.name,''))>0) LIMIT ?", (*params, normalized, normalized, limit)).fetchall()
    return [row["record_id"] for row in rows]


def search_index(database_path: str | Path, query: str, filters: dict[str, Any] | None = None, top_k: int = 20, candidate_limit: int = 200) -> dict[str, Any]:
    if not 1 <= top_k <= 100: raise ValueError("top_k must be between 1 and 100")
    if not 1 <= candidate_limit <= MAX_CANDIDATE_LIMIT: raise ValueError(f"candidate_limit must be between 1 and {MAX_CANDIDATE_LIMIT}")
    if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH: raise ValueError(f"query must be a string of at most {MAX_QUERY_LENGTH} characters")
    filters = filters or {}
    with connect_database(database_path) as connection:
        org = connection.execute("SELECT value FROM metadata WHERE key='organization_id'").fetchone()
        if not org: raise ValueError("database is not initialized")
        if filters.get("organization_id") and filters["organization_id"] != org["value"]: raise ValueError("organization filter does not match the database")
        index = connection.execute("SELECT value FROM metadata WHERE key='index_version'").fetchone()
        ranks = {"lexical": _lexical_ranks(connection, query, filters, candidate_limit), "tag": _tag_ranks(connection, query, filters, candidate_limit)}
        fused, components = {}, {}
        for component, ids in ranks.items():
            for rank, record_id in enumerate(ids, 1):
                score = RETRIEVAL_WEIGHTS[component] / (RRF_K + rank); fused[record_id] = fused.get(record_id, 0) + score; components.setdefault(record_id, {})[component] = {"rank": rank, "rrf_score": round(score, 8)}
        ordered = [key for key, _ in sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:top_k]]
        if not ordered and not query:
            where, params = _filter_clause(filters); ordered = [row["record_id"] for row in connection.execute(f"SELECT record_id FROM records r WHERE {where} ORDER BY record_id LIMIT ?", (*params, top_k))]; fused = {key: 0 for key in ordered}
        rows = connection.execute(f"SELECT * FROM records WHERE record_id IN ({','.join('?' for _ in ordered)})", ordered).fetchall() if ordered else []
        by_id = {row["record_id"]: row for row in rows}
        results = [{"record_id": key, "analysis_id": row["analysis_id"], "paper_id": row["paper_id"], "class_id": row["class_id"], "student_ref": row["student_ref"], "student_name": row["student_name"], "attempt_id": row["attempt_id"], "question_id": row["question_id"], "subject": row["subject"], "grade": row["grade"], "curriculum_version": row["curriculum_version"], "question_type": row["question_type"], "review_status": row["review_status"], "score": row["score"], "max_score": row["max_score"], "fused_score": round(fused[key], 8), "components": components.get(key, {}), "snippet": (row["question_text"] or row["error_text"] or row["answer_text"] or "")[:240], "evidence": json.loads(row["evidence_json"]), "source_refs": json.loads(row["source_refs_json"])} for key in ordered if (row := by_id.get(key))]
    return {"organization_id": org["value"], "query": query, "filters": filters, "top_k": top_k, "index_version": index["value"] if index else None, "degraded_components": [], "retrieval_profile": {"lexical": "bounded_bm25", "rrf_k": RRF_K, "weights": RETRIEVAL_WEIGHTS}, "results": results}


def purge_records(database_path: str | Path, student_ref: str | None = None, attempt_id: str | None = None) -> dict[str, Any]:
    if bool(student_ref) == bool(attempt_id): raise ValueError("provide exactly one of student_ref or attempt_id")
    field, value = ("student_ref", student_ref) if student_ref else ("attempt_id", attempt_id)
    with connect_database(database_path) as connection:
        ids = [row["record_id"] for row in connection.execute(f"SELECT record_id FROM records WHERE {field}=?", (value,))]
        for record_id in ids: connection.execute("DELETE FROM records_fts WHERE record_id=?", (record_id,)); connection.execute("DELETE FROM records WHERE record_id=?", (record_id,))
        if ids: _database_audit(connection, "index.purge", {"selector": field, "selector_hash": sha256_value(value), "records": len(ids)}); connection.commit()
    return {"purged": len(ids)}


def benchmark(records: int = 10_000, queries: int = 25, database_path: str | Path | None = None) -> dict[str, Any]:
    if records < 1 or queries < 1:
        raise ValueError("records and queries must be positive")
    owned_temp = None
    if database_path is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="exam-error-benchmark-")
        database_path = Path(owned_temp.name) / "benchmark.sqlite3"
    path = Path(database_path)
    try:
        with connect_database(path) as connection:
            connection.execute("DROP TABLE IF EXISTS benchmark_fts")
            connection.execute("CREATE VIRTUAL TABLE benchmark_fts USING fts5(content)")
            connection.executemany(
                "INSERT INTO benchmark_fts(content) VALUES(?)",
                [(fts_document(f"数学 二次方程 计算错误 样本{i % 100}"),) for i in range(records)],
            )
            connection.commit()
            timings = []
            for index in range(queries):
                started = time.perf_counter()
                connection.execute(
                    "SELECT rowid FROM benchmark_fts WHERE benchmark_fts MATCH ? LIMIT 20",
                    (_fts_query("二次方程 计算错误 样本" + str(index % 100)),),
                ).fetchall()
                timings.append((time.perf_counter() - started) * 1000)
        ordered = sorted(timings)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return {"records": records, "queries": queries, "retrieval": "fts5_and_tags", "latency_ms": {"p50": round(statistics.median(timings), 3), "p95": round(p95, 3)}, "target": {"p95_ms": 250}, "database": str(path)}
    finally:
        if owned_temp:
            owned_temp.cleanup()
