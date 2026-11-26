"""Deterministic MappingContract execution and instance materialization.

Only approved/active contracts execute. Transformations use a small allow-list;
no dynamic Python, eval, SQL, shell or user-provided callable is executed.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .instances import FieldLineage, OntologyInstance, SourceLineage, SourceRecordRef
from .models import MappingContract, OntologySchema


class MappingExecutionError(ValueError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _value_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "是", "是的"}:
        return True
    if normalized in {"false", "0", "no", "n", "否", "不是"}:
        return False
    raise ValueError(f"无法转换为布尔值: {value!r}")


def apply_transform(value: Any, spec: Optional[Dict[str, Any]]) -> Any:
    """Apply allow-listed deterministic operations in declared order."""
    if not spec:
        return value
    if not isinstance(spec, dict):
        raise MappingExecutionError("transform 必须是 JSON 对象")
    forbidden = {"python", "code", "script", "eval", "expression"}
    if forbidden.intersection(spec):
        raise MappingExecutionError("transform 不允许执行代码或任意表达式")
    operations = spec.get("operations", [])
    if not isinstance(operations, list):
        raise MappingExecutionError("transform.operations 必须是数组")
    result = value
    for operation in operations:
        if not isinstance(operation, dict):
            raise MappingExecutionError("转换操作必须是对象")
        op = operation.get("op")
        arg = operation.get("value")
        if op == "trim":
            result = None if result is None else str(result).strip()
        elif op == "lower":
            result = None if result is None else str(result).lower()
        elif op == "upper":
            result = None if result is None else str(result).upper()
        elif op == "to_string":
            result = None if result is None else str(result)
        elif op == "to_integer":
            result = None if result in (None, "") else int(result)
        elif op == "to_decimal":
            result = None if result in (None, "") else str(Decimal(str(result)))
        elif op == "to_boolean":
            result = None if result in (None, "") else _to_bool(result)
        elif op == "replace":
            if result is not None:
                result = str(result).replace(str(operation.get("old", "")), str(operation.get("new", "")))
        elif op == "map_values":
            mapping = operation.get("mapping") or {}
            key = str(result)
            if key in mapping:
                result = mapping[key]
            elif "default" in operation:
                result = operation["default"]
            else:
                raise ValueError(f"枚举映射缺少值: {key}")
        elif op == "date_iso":
            if result not in (None, ""):
                result = date.fromisoformat(str(result)).isoformat()
        elif op == "datetime_iso":
            if result not in (None, ""):
                result = datetime.fromisoformat(str(result)).isoformat()
        elif op == "regex_replace":
            if result is not None:
                pattern = str(operation.get("pattern", ""))
                if len(pattern) > 256:
                    raise MappingExecutionError("正则表达式长度不能超过 256")
                result = re.sub(pattern, str(operation.get("replacement", "")), str(result))
        else:
            raise MappingExecutionError(f"不支持的转换操作: {op}")
    return result


def _contract_source_table(contract: MappingContract) -> str:
    return str(contract.source_ref.get("table") or contract.source_ref.get("dataset") or "")


def _source_id(contract: MappingContract) -> str:
    return str(contract.source_ref.get("source_id") or _contract_source_table(contract) or "source:unknown")


def _identity_for(schema: OntologySchema, object_type_id: str, values: Dict[str, Any]):
    obj = schema.object_types[object_type_id]
    for identity_id in obj.identity_ids:
        identity = schema.identities.get(identity_id)
        if identity and all(prop in values and values[prop] not in (None, "") for prop in identity.property_refs):
            return identity
    return None


def _instance_id(object_type_id: str, identity_values: Dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical(identity_values).encode("utf-8")).hexdigest()[:24]
    return f"{object_type_id}:{digest}"


def _validate_contracts(schema: OntologySchema) -> Tuple[List[MappingContract], List[Dict[str, Any]]]:
    errors = schema.validate()
    if errors:
        raise MappingExecutionError("本体 Schema 校验失败: " + "; ".join(errors))
    runnable: List[MappingContract] = []
    blocked: List[Dict[str, Any]] = []
    for contract in schema.mapping_contracts.values():
        try:
            contract.validate()
        except ValueError as exc:
            blocked.append({"contract_id": contract.id, "reason": str(exc)})
            continue
        if contract.review_status != "approved" or contract.status not in {"active", "released"}:
            blocked.append({
                "contract_id": contract.id,
                "reason": "映射契约未审核通过或未激活",
                "review_status": contract.review_status,
                "status": contract.status,
            })
            continue
        runnable.append(contract)
    return runnable, blocked


def execute_mapping(
    schema: OntologySchema,
    source_data: Dict[str, List[Dict[str, Any]]],
    *,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute approved property and relationship contracts against in-memory rows.

    source_data is keyed by source table/dataset name. Each row is a mapping of
    source column names to values. Failed rows are reported; they are never
    silently dropped or overwritten.
    """
    runnable, blocked = _validate_contracts(schema)
    run_id = run_id or "mapping-run:" + hashlib.sha256(
        _canonical({"ontology": schema.ontology_id, "version": schema.version, "contracts": [c.id for c in runnable]}).encode("utf-8")
    ).hexdigest()[:16]

    property_contracts: Dict[str, List[MappingContract]] = {}
    relationship_contracts: List[MappingContract] = []
    for contract in runnable:
        target = contract.target_ref
        if target.get("property_id"):
            property_contracts.setdefault(str(target.get("object_type_id") or ""), []).append(contract)
        elif target.get("relationship_id"):
            relationship_contracts.append(contract)
        else:
            blocked.append({"contract_id": contract.id, "reason": "target_ref 必须指定 property_id 或 relationship_id"})

    instances: Dict[str, OntologyInstance] = {}
    row_instance_ids: Dict[Tuple[str, int, str], str] = {}
    errors: List[Dict[str, Any]] = []
    mapped_rows = 0

    # Phase 1: map property values and materialize business object instances.
    for object_type_id, contracts in property_contracts.items():
        if object_type_id not in schema.object_types:
            for contract in contracts:
                errors.append({"contract_id": contract.id, "reason": f"未知 ObjectType: {object_type_id}"})
            continue
        by_table: Dict[str, List[MappingContract]] = {}
        for contract in contracts:
            table = _contract_source_table(contract)
            by_table.setdefault(table, []).append(contract)
        for table, table_contracts in by_table.items():
            rows = source_data.get(table)
            if rows is None:
                errors.append({"table": table, "object_type_id": object_type_id, "reason": "数据源中不存在映射契约指定的数据集"})
                continue
            seen_targets = set()
            for contract in table_contracts:
                property_id = str(contract.target_ref.get("property_id"))
                if property_id not in schema.properties:
                    errors.append({"contract_id": contract.id, "reason": f"目标属性不存在: {property_id}"})
                    continue
                prop = schema.properties[property_id]
                if prop.domain_object_type_id != object_type_id:
                    errors.append({"contract_id": contract.id, "reason": "目标属性不属于指定 ObjectType"})
                if property_id in seen_targets:
                    errors.append({"table": table, "property_id": property_id, "reason": "同一数据集存在重复目标属性映射"})
                seen_targets.add(property_id)

            for row_index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    errors.append({"table": table, "row": row_index, "reason": "源记录必须是对象"})
                    continue
                values: Dict[str, Any] = {}
                field_traces: List[FieldLineage] = []
                record_key: Dict[str, Any] = {}
                row_failed = False
                for contract in table_contracts:
                    property_id = str(contract.target_ref.get("property_id"))
                    field_name = str(contract.source_ref.get("field") or contract.source_ref.get("column") or "")
                    if not field_name:
                        errors.append({"contract_id": contract.id, "table": table, "row": row_index, "reason": "source_ref 缺少 field/column"})
                        row_failed = True
                        continue
                    if field_name not in row:
                        errors.append({"contract_id": contract.id, "table": table, "row": row_index, "reason": f"源字段不存在: {field_name}"})
                        row_failed = True
                        continue
                    raw_value = row.get(field_name)
                    try:
                        value = apply_transform(raw_value, contract.transform)
                        if value is None or value == "":
                            if contract.null_policy == "default":
                                value = contract.default_value
                            elif contract.null_policy == "reject":
                                raise ValueError("源值为空且 null_policy=reject")
                            elif contract.null_policy == "drop":
                                continue
                        values[property_id] = value
                        source = SourceRecordRef(
                            source_id=_source_id(contract), dataset=contract.source_ref.get("dataset"),
                            table=contract.source_ref.get("table") or table,
                            record_key={"row_number": row_index}, row_number=row_index,
                            source_version=contract.source_schema_hash,
                        )
                        field_traces.append(FieldLineage(
                            target_property_id=property_id, source=source,
                            source_field=field_name, source_value_hash=_value_hash(raw_value),
                            transform_ref=contract.id,
                            mapping_contract_id=contract.id,
                            mapping_contract_version=contract.version,
                            confidence=contract.confidence,
                            evidence=list(contract.evidence),
                        ))
                    except Exception as exc:
                        errors.append({"contract_id": contract.id, "table": table, "row": row_index, "field": field_name, "reason": str(exc)})
                        row_failed = True
                if row_failed:
                    continue
                identity = _identity_for(schema, object_type_id, values)
                if identity is None:
                    errors.append({"table": table, "row": row_index, "object_type_id": object_type_id, "reason": "无法从已映射属性解析有效业务身份"})
                    continue
                identity_values = {prop_id: values[prop_id] for prop_id in identity.property_refs}
                instance_id = _instance_id(object_type_id, identity_values)
                instance = OntologyInstance(
                    id=instance_id, object_type_id=object_type_id,
                    identity_values=identity_values, properties=values,
                    ontology_id=schema.ontology_id, ontology_version=schema.version,
                    mapping_versions={c.id: c.version for c in table_contracts},
                    lineage=SourceLineage(
                        sources=[SourceRecordRef(
                            source_id=str(next((_source_id(c) for c in table_contracts), table)),
                            dataset=table, table=table, record_key={"row_number": row_index},
                            row_number=row_index,
                        )],
                        fields=field_traces, mapping_run_id=run_id,
                    ),
                )
                try:
                    instance.validate(schema)
                except ValueError as exc:
                    errors.append({"table": table, "row": row_index, "instance_id": instance_id, "reason": str(exc)})
                    continue
                if instance_id in instances:
                    existing = instances[instance_id]
                    if existing.properties != instance.properties:
                        errors.append({"table": table, "row": row_index, "instance_id": instance_id, "reason": "同一身份生成冲突属性值，拒绝覆盖"})
                        continue
                    existing.lineage.sources.extend(instance.lineage.sources)
                    existing.lineage.fields.extend(instance.lineage.fields)
                else:
                    instances[instance_id] = instance
                row_instance_ids[(table, row_index, object_type_id)] = instance_id
                mapped_rows += 1

    # Phase 2: resolve relationship joins only after both endpoint instances exist.
    for contract in relationship_contracts:
        relationship_id = str(contract.target_ref.get("relationship_id"))
        relation = schema.relationships.get(relationship_id)
        if relation is None:
            errors.append({"contract_id": contract.id, "reason": f"目标关系不存在: {relationship_id}"})
            continue
        source_table = _contract_source_table(contract)
        target_table = str(contract.target_ref.get("target_table") or "")
        source_rows, target_rows = source_data.get(source_table), source_data.get(target_table)
        join_keys = contract.join_keys
        if not source_rows or not target_rows or not join_keys:
            errors.append({"contract_id": contract.id, "reason": "关系映射需要 source/target 数据集及 join_keys"})
            continue
        source_type_id = relation.source_object_type_id
        target_type_id = relation.target_object_type_id
        source_index = {idx: row_instance_ids.get((source_table, idx, source_type_id)) for idx in range(1, len(source_rows)+1)}
        target_index = {idx: row_instance_ids.get((target_table, idx, target_type_id)) for idx in range(1, len(target_rows)+1)}
        target_lookup: Dict[Tuple[Any, ...], List[str]] = {}
        for idx, row in enumerate(target_rows, start=1):
            key = tuple(row.get(k.get("target_field")) for k in join_keys)
            if all(v is not None for v in key) and target_index.get(idx):
                target_lookup.setdefault(key, []).append(target_index[idx])
        for idx, row in enumerate(source_rows, start=1):
            source_instance_id = source_index.get(idx)
            if not source_instance_id:
                continue
            key = tuple(row.get(k.get("source_field")) for k in join_keys)
            if not all(v is not None for v in key):
                continue
            targets = target_lookup.get(key, [])
            if not targets:
                continue
            instance = instances[source_instance_id]
            instance.relationship_refs.setdefault(relationship_id, [])
            for target_instance_id in targets:
                if target_instance_id not in instance.relationship_refs[relationship_id]:
                    instance.relationship_refs[relationship_id].append(target_instance_id)
        # record join contract version on participating source instances
        for instance in instances.values():
            if instance.object_type_id == source_type_id:
                instance.mapping_versions[contract.id] = contract.version

    for instance in instances.values():
        try:
            instance.validate(schema)
        except ValueError as exc:
            errors.append({"instance_id": instance.id, "reason": str(exc)})

    return {
        "run_id": run_id,
        "ontology_id": schema.ontology_id,
        "ontology_version": schema.version,
        "mapping_contract_versions": {c.id: c.version for c in runnable},
        "instances": instances,
        "instance_count": len(instances),
        "mapped_rows": mapped_rows,
        "blocked_contracts": blocked,
        "errors": errors,
        "success": not blocked and not errors,
    }
