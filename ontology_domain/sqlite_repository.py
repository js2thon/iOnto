"""SQLite persistence for iOnto V5 schemas, mapping runs, instances and releases.

Uses only the Python standard library. Published release bundles are immutable;
working schemas and execution runs remain independently versioned records.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .models import OntologySchema


class RepositoryError(RuntimeError):
    """Raised when a persistence operation violates repository invariants."""


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteOntologyRepository:
    """A small local repository suitable for single-node desktop deployments.

    Each operation opens its own connection, so instances can be shared across
    Streamlit reruns/threads without sharing sqlite connection objects.
    """

    def __init__(self, database_path: str | Path = "ontology_workspace/v5_ontology.db", *, timeout: float = 10.0):
        self.database_path = str(database_path)
        self.timeout = timeout
        if self.database_path != ":memory:":
            Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS ontology_schemas (
                    ontology_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    schema_json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (ontology_id, version)
                );
                CREATE TABLE IF NOT EXISTS mapping_runs (
                    run_id TEXT PRIMARY KEY,
                    ontology_id TEXT NOT NULL,
                    ontology_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_checksum TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_mapping_runs_ontology
                    ON mapping_runs (ontology_id, ontology_version, created_at);
                CREATE TABLE IF NOT EXISTS ontology_instances (
                    ontology_id TEXT NOT NULL,
                    ontology_version INTEGER NOT NULL,
                    instance_id TEXT NOT NULL,
                    object_type_id TEXT NOT NULL,
                    instance_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (ontology_id, ontology_version, instance_id)
                );
                CREATE INDEX IF NOT EXISTS idx_instances_object_type
                    ON ontology_instances (ontology_id, ontology_version, object_type_id);
                CREATE TABLE IF NOT EXISTS instance_lineage (
                    ontology_id TEXT NOT NULL,
                    ontology_version INTEGER NOT NULL,
                    instance_id TEXT NOT NULL,
                    mapping_run_id TEXT NOT NULL,
                    lineage_json TEXT NOT NULL,
                    PRIMARY KEY (ontology_id, ontology_version, instance_id, mapping_run_id),
                    FOREIGN KEY (ontology_id, ontology_version, instance_id)
                        REFERENCES ontology_instances (ontology_id, ontology_version, instance_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (mapping_run_id) REFERENCES mapping_runs (run_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS release_bundles (
                    release_id TEXT PRIMARY KEY,
                    ontology_id TEXT NOT NULL,
                    ontology_version INTEGER NOT NULL,
                    bundle_json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (ontology_id, ontology_version)
                );
            """)

    def save_schema(self, schema: OntologySchema) -> Dict[str, Any]:
        errors = schema.validate()
        if errors:
            raise RepositoryError("Schema validation failed: " + "; ".join(errors))
        payload = schema.to_json(indent=None)
        checksum = _sha256(payload)
        now = _now()
        with self._connect() as db:
            released = db.execute(
                "SELECT 1 FROM release_bundles WHERE ontology_id=? AND ontology_version=?",
                (schema.ontology_id, schema.version),
            ).fetchone()
            if released:
                raise RepositoryError("This ontology version is already released and immutable")
            existing = db.execute(
                "SELECT schema_json FROM ontology_schemas WHERE ontology_id=? AND version=?",
                (schema.ontology_id, schema.version),
            ).fetchone()
            if existing and existing["schema_json"] != payload:
                runs = db.execute(
                    "SELECT 1 FROM mapping_runs WHERE ontology_id=? AND ontology_version=? LIMIT 1",
                    (schema.ontology_id, schema.version),
                ).fetchone()
                if runs:
                    raise RepositoryError(
                        "This ontology version already has mapping runs; create a new version instead of overwriting its Schema"
                    )
            db.execute("""
                INSERT INTO ontology_schemas
                    (ontology_id, version, schema_json, checksum, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ontology_id, version) DO UPDATE SET
                    schema_json=excluded.schema_json,
                    checksum=excluded.checksum,
                    updated_at=excluded.updated_at
            """, (schema.ontology_id, schema.version, payload, checksum, now, now))
        return {"ontology_id": schema.ontology_id, "version": schema.version, "checksum": checksum}

    def load_schema(self, ontology_id: str, version: int) -> OntologySchema:
        with self._connect() as db:
            row = db.execute(
                "SELECT schema_json, checksum FROM ontology_schemas WHERE ontology_id=? AND version=?",
                (ontology_id, version),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"Schema not found: {ontology_id}@{version}")
        if _sha256(row["schema_json"]) != row["checksum"]:
            raise RepositoryError("Stored schema checksum mismatch")
        return OntologySchema.from_json(row["schema_json"])

    def list_schema_versions(self, ontology_id: str) -> list[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT version, checksum, created_at, updated_at
                FROM ontology_schemas WHERE ontology_id=? ORDER BY version
            """, (ontology_id,)).fetchall()
        return [dict(row) for row in rows]

    def save_mapping_run(self, schema: OntologySchema, result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("ontology_id") != schema.ontology_id or result.get("ontology_version") != schema.version:
            raise RepositoryError("Mapping run ontology/version does not match supplied Schema")
        run_id = str(result.get("run_id") or "")
        if not run_id:
            raise RepositoryError("Mapping run must have a run_id")
        instances = result.get("instances") or {}
        if not isinstance(instances, dict):
            raise RepositoryError("Mapping run instances must be a dictionary")
        serial_instances = {}
        for instance_id, instance in instances.items():
            if instance.id != instance_id:
                raise RepositoryError("Instance dictionary key does not match instance.id")
            instance.validate(schema)
            serial_instances[instance_id] = _json_default(instance)
        summary = {key: value for key, value in result.items() if key != "instances"}
        summary["instances"] = serial_instances
        payload = _canonical_json(summary)
        checksum = _sha256(payload)
        status = "succeeded" if result.get("success") else "failed"
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                INSERT INTO mapping_runs
                    (run_id, ontology_id, ontology_version, status, result_json, result_checksum, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (run_id, schema.ontology_id, schema.version, status, payload, checksum, now))
            for instance_id, instance in instances.items():
                instance_json = _canonical_json(instance)
                db.execute("""
                    INSERT INTO ontology_instances
                        (ontology_id, ontology_version, instance_id, object_type_id, instance_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ontology_id, ontology_version, instance_id) DO UPDATE SET
                        object_type_id=excluded.object_type_id,
                        instance_json=excluded.instance_json,
                        created_at=excluded.created_at
                """, (schema.ontology_id, schema.version, instance_id, instance.object_type_id, instance_json, now))
                lineage_json = _canonical_json(instance.lineage)
                db.execute("""
                    INSERT OR REPLACE INTO instance_lineage
                        (ontology_id, ontology_version, instance_id, mapping_run_id, lineage_json)
                    VALUES (?, ?, ?, ?, ?)
                """, (schema.ontology_id, schema.version, instance_id, run_id, lineage_json))
        return {"run_id": run_id, "status": status, "instance_count": len(instances), "checksum": checksum}

    def load_mapping_run(self, run_id: str) -> Dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM mapping_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise RepositoryError(f"Mapping run not found: {run_id}")
        if _sha256(row["result_json"]) != row["result_checksum"]:
            raise RepositoryError("Stored mapping run checksum mismatch")
        result = json.loads(row["result_json"])
        result["status"] = row["status"]
        result["created_at"] = row["created_at"]
        result["checksum"] = row["result_checksum"]
        return result

    def list_instances(self, ontology_id: str, version: int, *, object_type_id: Optional[str] = None) -> list[Dict[str, Any]]:
        query = """
            SELECT instance_json FROM ontology_instances
            WHERE ontology_id=? AND ontology_version=?
        """
        params: list[Any] = [ontology_id, version]
        if object_type_id:
            query += " AND object_type_id=?"
            params.append(object_type_id)
        query += " ORDER BY object_type_id, instance_id"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [json.loads(row["instance_json"]) for row in rows]

    def get_lineage(self, ontology_id: str, version: int, instance_id: str) -> list[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT mapping_run_id, lineage_json FROM instance_lineage
                WHERE ontology_id=? AND ontology_version=? AND instance_id=?
                ORDER BY mapping_run_id
            """, (ontology_id, version, instance_id)).fetchall()
        return [{"mapping_run_id": row["mapping_run_id"], "lineage": json.loads(row["lineage_json"])} for row in rows]

    def publish_release(self, schema: OntologySchema, *, release_id: Optional[str] = None) -> Dict[str, Any]:
        errors = schema.validate()
        if errors:
            raise RepositoryError("Cannot publish invalid Schema: " + "; ".join(errors))
        pending = [
            contract.id for contract in schema.mapping_contracts.values()
            if contract.review_status != "approved" or contract.status not in {"active", "released"}
        ]
        if pending:
            raise RepositoryError("Cannot publish with unapproved/inactive MappingContracts: " + ", ".join(pending))
        release_id = release_id or f"{schema.ontology_id}@{schema.version}"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT release_id, checksum FROM release_bundles WHERE ontology_id=? AND ontology_version=?",
                (schema.ontology_id, schema.version),
            ).fetchone()
            if existing:
                return {"release_id": existing["release_id"], "checksum": existing["checksum"], "created": False}
            schema_row = db.execute(
                "SELECT schema_json, checksum FROM ontology_schemas WHERE ontology_id=? AND version=?",
                (schema.ontology_id, schema.version),
            ).fetchone()
            if schema_row is None:
                raise RepositoryError("Save the validated Schema before publishing")
            if _sha256(schema_row["schema_json"]) != schema_row["checksum"]:
                raise RepositoryError("Stored schema checksum mismatch")
            if schema.mapping_contracts:
                latest_run = db.execute("""
                    SELECT run_id, status FROM mapping_runs
                    WHERE ontology_id=? AND ontology_version=?
                    ORDER BY created_at DESC, run_id DESC LIMIT 1
                """, (schema.ontology_id, schema.version)).fetchone()
                if latest_run is None:
                    raise RepositoryError("Cannot publish mapped ontology before a mapping run succeeds")
                if latest_run["status"] != "succeeded":
                    raise RepositoryError(
                        f"Cannot publish: latest mapping run {latest_run['run_id']} is {latest_run['status']}"
                    )
            runs = db.execute("""
                SELECT run_id, status, result_checksum FROM mapping_runs
                WHERE ontology_id=? AND ontology_version=? ORDER BY created_at, run_id
            """, (schema.ontology_id, schema.version)).fetchall()
            instances = db.execute("""
                SELECT instance_id, instance_json FROM ontology_instances
                WHERE ontology_id=? AND ontology_version=? ORDER BY instance_id
            """, (schema.ontology_id, schema.version)).fetchall()
            bundle = {
                "bundle_version": 1,
                "release_id": release_id,
                "ontology_id": schema.ontology_id,
                "ontology_version": schema.version,
                "schema": json.loads(schema_row["schema_json"]),
                "mapping_runs": [dict(row) for row in runs],
                "instances": [json.loads(row["instance_json"]) for row in instances],
                "published_at": _now(),
            }
            bundle_json = _canonical_json(bundle)
            checksum = _sha256(bundle_json)
            db.execute("""
                INSERT INTO release_bundles
                    (release_id, ontology_id, ontology_version, bundle_json, checksum, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (release_id, schema.ontology_id, schema.version, bundle_json, checksum, bundle["published_at"]))
        return {"release_id": release_id, "checksum": checksum, "created": True}

    def load_release(self, release_id: str) -> Dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT bundle_json, checksum FROM release_bundles WHERE release_id=?", (release_id,)).fetchone()
        if row is None:
            raise RepositoryError(f"Release not found: {release_id}")
        if _sha256(row["bundle_json"]) != row["checksum"]:
            raise RepositoryError("Release bundle checksum mismatch")
        return json.loads(row["bundle_json"])

    def list_mapping_runs(self, ontology_id: str, version: int, *, limit: int = 20) -> list[Dict[str, Any]]:
        """List recent mapping runs for one immutable ontology version."""
        safe_limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute("""
                SELECT run_id, status, result_checksum, created_at
                FROM mapping_runs
                WHERE ontology_id=? AND ontology_version=?
                ORDER BY created_at DESC, run_id DESC LIMIT ?
            """, (ontology_id, version, safe_limit)).fetchall()
        return [dict(row) for row in rows]

    def list_releases(self, ontology_id: str) -> list[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT release_id, ontology_version, checksum, created_at
                FROM release_bundles WHERE ontology_id=? ORDER BY ontology_version
            """, (ontology_id,)).fetchall()
        return [dict(row) for row in rows]
