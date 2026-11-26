"""V5.1 mapping-workbench services.

This module bridges the legacy in-memory data-source session and the V5 domain
model. It never infers a field mapping as an approved fact: generated contracts
remain draft/pending until a user explicitly approves them.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from .legacy_adapter import legacy_snapshot_to_v5
from .models import MappingContract, OntologySchema, new_id
from .mapping_engine import execute_mapping


def source_fingerprint(source_data: Dict[str, Any], source_kinds: Optional[Dict[str, str]] = None) -> str:
    inventory = {}
    for name, frame in sorted(source_data.items()):
        columns = list(getattr(frame, "columns", []))
        dtypes = {str(k): str(v) for k, v in getattr(frame, "dtypes", {}).items()}
        inventory[str(name)] = {"columns": [str(c) for c in columns], "dtypes": dtypes,
                                "source_kind": (source_kinds or {}).get(str(name), "file")}
    payload = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def legacy_ontology_to_schema(ontology: Any) -> OntologySchema:
    """Migrate the current legacy ontology structure to stable V5 identifiers."""
    if ontology is None:
        raise ValueError("当前尚未生成旧版本体")
    snapshot = {
        "schema_version": "legacy-session",
        "object_types": deepcopy(getattr(ontology, "object_types", {}) or {}),
        "link_types": deepcopy(getattr(ontology, "link_types", {}) or {}),
        "data_mapping": deepcopy(getattr(ontology, "data_mapping", {}) or {}),
    }
    schema = legacy_snapshot_to_v5(snapshot)
    schema.metadata["source"] = "legacy-session"
    return schema


def load_latest_schema(ontology: Any, repository: Any) -> OntologySchema:
    """Restore the latest persisted V5 schema for the deterministic legacy ontology ID."""
    base = legacy_ontology_to_schema(ontology)
    versions = repository.list_schema_versions(base.ontology_id)
    if not versions:
        return base
    latest_version = max(int(item["version"]) for item in versions)
    return repository.load_schema(base.ontology_id, latest_version)


def source_rows(source_data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Convert loaded dataframe-like sources to JSON-safe row dictionaries."""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for table, frame in source_data.items():
        if frame is None:
            continue
        if hasattr(frame, "to_dict"):
            rows = frame.to_dict(orient="records")
        else:
            rows = list(frame)
        clean_rows = []
        for row in rows:
            clean = {}
            for key, value in dict(row).items():
                try:
                    import pandas as pd
                    if pd.isna(value):
                        value = None
                except (ImportError, TypeError, ValueError):
                    pass
                if hasattr(value, "item"):
                    try:
                        value = value.item()
                    except Exception:
                        pass
                if hasattr(value, "isoformat") and not isinstance(value, (str, int, float, bool)):
                    try:
                        value = value.isoformat()
                    except Exception:
                        pass
                clean[str(key)] = value
            clean_rows.append(clean)
        result[str(table)] = clean_rows
    return result


def _contract_id(kind: str, target_id: str) -> str:
    return f"map:{kind}:{target_id}"


def build_property_contract(
    schema: OntologySchema,
    *,
    object_type_id: str,
    property_id: str,
    source_table: str,
    source_field: str,
    record_key_fields: Optional[List[str]] = None,
    mapping_kind: str = "direct",
    transform: Optional[Dict[str, Any]] = None,
    source_kind: str = "file",
    source_schema_hash: Optional[str] = None,
) -> MappingContract:
    obj = schema.object_types.get(object_type_id)
    prop = schema.properties.get(property_id)
    if obj is None or prop is None or prop.domain_object_type_id != object_type_id:
        raise ValueError("目标对象类型或属性无效")
    if not source_table.strip() or not source_field.strip():
        raise ValueError("源数据表和字段不能为空")
    if mapping_kind not in {"direct", "transform"}:
        raise ValueError("属性映射仅支持 direct 或 transform")
    contract_id = _contract_id("property", property_id)
    previous = schema.mapping_contracts.get(contract_id)
    return MappingContract(
        id=contract_id,
        name=f"{source_table}.{source_field} → {obj.label}.{prop.label}",
        source_ref={
            "source_id": f"dataset:{source_table}",
            "source_kind": source_kind,
            "table": source_table,
            "dataset": source_table,
            "field": source_field,
            "record_key_fields": list(record_key_fields or []),
        },
        target_ref={"object_type_id": object_type_id, "property_id": property_id},
        mapping_kind=mapping_kind,
        transform=deepcopy(transform),
        identity_refs=list(obj.identity_ids),
        review_status="pending",
        status="draft",
        version=(previous.version + 1) if previous else 1,
        source_schema_hash=source_schema_hash,
        evidence=[{"kind": "user_configured", "source_field": source_field}],
        metadata={"created_by": "mapping_workbench"},
    )


def build_relationship_contract(
    schema: OntologySchema,
    *,
    relationship_id: str,
    source_table: str,
    target_table: str,
    join_keys: List[Dict[str, str]],
    source_record_key_fields: Optional[List[str]] = None,
    target_record_key_fields: Optional[List[str]] = None,
    source_kind: str = "file",
    target_source_kind: Optional[str] = None,
    source_schema_hash: Optional[str] = None,
) -> MappingContract:
    relation = schema.relationships.get(relationship_id)
    if relation is None:
        raise ValueError("目标关系不存在")
    normalized = [
        {"source_field": str(item.get("source_field") or "").strip(),
         "target_field": str(item.get("target_field") or "").strip()}
        for item in join_keys
    ]
    if not normalized or any(not item["source_field"] or not item["target_field"] for item in normalized):
        raise ValueError("关系 Join Key 必须同时配置源字段和目标字段")
    contract_id = _contract_id("relationship", relationship_id)
    previous = schema.mapping_contracts.get(contract_id)
    source_obj = schema.object_types[relation.source_object_type_id]
    target_obj = schema.object_types[relation.target_object_type_id]
    return MappingContract(
        id=contract_id,
        name=f"{source_table} → {target_table} · {relation.label}",
        source_ref={
            "source_id": f"dataset:{source_table}",
            "source_kind": source_kind,
            "table": source_table,
            "dataset": source_table,
            "record_key_fields": list(source_record_key_fields or []),
        },
        target_ref={
            "relationship_id": relationship_id,
            "target_table": target_table,
            "source_id": f"dataset:{target_table}",
            "source_kind": target_source_kind or source_kind,
            "record_key_fields": list(target_record_key_fields or []),
            "source_object_type_id": source_obj.id,
            "target_object_type_id": target_obj.id,
        },
        mapping_kind="join",
        join_keys=normalized,
        review_status="pending",
        status="draft",
        version=(previous.version + 1) if previous else 1,
        source_schema_hash=source_schema_hash,
        evidence=[{"kind": "user_configured_join", "join_keys": normalized}],
        metadata={"created_by": "mapping_workbench"},
    )


def set_contract_review(
    schema: OntologySchema, contract_ids: Iterable[str], *, approved: bool
) -> None:
    for contract_id in contract_ids:
        contract = schema.mapping_contracts.get(contract_id)
        if contract is None:
            raise ValueError(f"映射契约不存在: {contract_id}")
        contract.review_status = "approved" if approved else "rejected"
        contract.status = "active" if approved else "draft"
        contract.version += 1


def preview_mapping(schema: OntologySchema, source_data: Dict[str, Any]) -> Dict[str, Any]:
    """Simulate all configured contracts without changing their review state."""
    candidate = deepcopy(schema)
    for contract in candidate.mapping_contracts.values():
        if contract.review_status != "rejected":
            contract.review_status = "approved"
            contract.status = "active"
    return execute_mapping(candidate, source_rows(source_data))


def execute_and_persist(schema: OntologySchema, source_data: Dict[str, Any], repository: Any) -> Dict[str, Any]:
    """Persist the exact schema snapshot and the deterministic mapping run."""
    repository.save_schema(schema)
    result = execute_mapping(schema, source_rows(source_data), run_id=new_id("mapping-run"))
    persisted = repository.save_mapping_run(schema, result)
    return {"result": result, "persisted": persisted}


def next_editable_version(schema: OntologySchema, repository: Any) -> OntologySchema:
    """Clone a released version before edits; never mutate a published snapshot."""
    releases = repository.list_releases(schema.ontology_id)
    has_release = any(int(item["ontology_version"]) == schema.version for item in releases)
    has_runs = bool(repository.list_mapping_runs(schema.ontology_id, schema.version, limit=1))
    if not has_release and not has_runs:
        return schema
    versions = repository.list_schema_versions(schema.ontology_id)
    next_version = max([int(item["version"]) for item in versions] + [schema.version]) + 1
    draft = deepcopy(schema)
    draft.version = next_version
    draft.metadata["parent_version"] = schema.version
    draft.metadata["branch_reason"] = "edit_after_release"
    return draft
