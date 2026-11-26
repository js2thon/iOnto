"""Deterministic MappingContract execution and instance materialization.

Only approved/active contracts execute. Transformations use a small allow-list;
no dynamic Python, eval, SQL, shell or user-provided callable is executed.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .instances import FieldLineage, OntologyInstance, RelationshipLineage, SourceLineage, SourceRecordRef
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
    if set(spec) - {"operations"}:
        raise MappingExecutionError("transform 仅允许 operations 字段")
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
        else:
            raise MappingExecutionError(f"不支持的转换操作: {op}")
    return result


def _contract_source_table(contract: MappingContract) -> str:
    return str(contract.source_ref.get("table") or contract.source_ref.get("dataset") or "")


def _source_id(contract: MappingContract) -> str:
    return str(contract.source_ref.get("source_id") or _contract_source_table(contract) or "source:unknown")


def _validate_property_value(prop: Any, value: Any) -> None:
    if value is None:
        if prop.required or not prop.nullable:
            raise ValueError(f"属性 {prop.name} 不允许为空")
        return
    kind = prop.data_type
    valid = True
    if kind == "string":
        valid = isinstance(value, str)
    elif kind == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind in {"decimal", "number"}:
        try:
            Decimal(str(value))
        except (InvalidOperation, ValueError):
            valid = False
    elif kind == "boolean":
        valid = isinstance(value, bool)
    elif kind == "date":
        try:
            date.fromisoformat(str(value))
        except ValueError:
            valid = False
    elif kind == "datetime":
        try:
            datetime.fromisoformat(str(value))
        except ValueError:
            valid = False
    elif kind == "enum":
        valid = value in prop.enum_values
    elif kind == "json":
        valid = isinstance(value, (dict, list))
    if not valid:
        raise ValueError(f"属性 {prop.name} 的值与声明类型 {kind} 不匹配")


def _identity_for(
    schema: OntologySchema, object_type_id: str, values: Dict[str, Any],
    identity_refs: Optional[List[str]] = None,
):
    obj = schema.object_types[object_type_id]
    candidate_ids = identity_refs or obj.identity_ids
    for identity_id in candidate_ids:
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
        if (
            str(contract.source_ref.get("source_kind") or "").lower() in {"database", "postgresql", "sql"}
            and not (contract.source_ref.get("record_key_fields") or contract.source_ref.get("primary_key_field"))
        ):
            blocked.append({
                "contract_id": contract.id,
                "reason": "数据库来源映射必须配置 record_key_fields/primary_key_field，不能用不稳定的结果行号作为记录身份",
            })
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
            if contract.mapping_kind not in {"direct", "transform"}:
                blocked.append({"contract_id": contract.id, "reason": f"属性映射暂不支持 mapping_kind={contract.mapping_kind}"})
                continue
            object_type_id = str(target.get("object_type_id") or "")
            property_id = str(target.get("property_id") or "")
            prop = schema.properties.get(property_id)
            if object_type_id not in schema.object_types:
                blocked.append({"contract_id": contract.id, "reason": f"未知 ObjectType: {object_type_id}"})
            elif prop is None:
                blocked.append({"contract_id": contract.id, "reason": f"目标属性不存在: {property_id}"})
            elif prop.domain_object_type_id != object_type_id:
                blocked.append({"contract_id": contract.id, "reason": "目标属性不属于指定 ObjectType"})
            else:
                property_contracts.setdefault(object_type_id, []).append(contract)
        elif target.get("relationship_id"):
            if contract.mapping_kind != "join":
                blocked.append({"contract_id": contract.id, "reason": "关系映射必须使用 mapping_kind=join"})
                continue
            relationship_contracts.append(contract)
        else:
            blocked.append({"contract_id": contract.id, "reason": "target_ref 必须指定 property_id 或 relationship_id"})

    if not property_contracts and not relationship_contracts and not blocked:
        blocked.append({"reason": "当前本体没有可执行的映射契约"})

    instances: Dict[str, OntologyInstance] = {}
    row_instance_ids: Dict[Tuple[str, int, str], str] = {}
    errors: List[Dict[str, Any]] = []
    mapped_rows = 0

    # Phase 1: map property values and materialize business object instances.
    for object_type_id, contracts in property_contracts.items():
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
                        _validate_property_value(schema.properties[property_id], value)
                        values[property_id] = value
                        source = SourceRecordRef(
                            source_id=_source_id(contract), dataset=contract.source_ref.get("dataset"),
                            table=contract.source_ref.get("table") or table,
                            record_key=_record_key(row, contract.source_ref, row_index), row_number=row_index,
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
                missing_required = [
                    prop.id for prop in schema.properties.values()
                    if prop.domain_object_type_id == object_type_id
                    and prop.required and prop.id not in values
                ]
                if missing_required:
                    errors.append({
                        "table": table, "row": row_index, "object_type_id": object_type_id,
                        "reason": "缺少必填属性: " + ", ".join(missing_required),
                    })
                    continue
                contract_identity_refs = list(dict.fromkeys(
                    identity_id for contract in table_contracts for identity_id in contract.identity_refs
                ))
                identity = _identity_for(
                    schema, object_type_id, values, contract_identity_refs or None
                )
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
                            dataset=table, table=table, record_key=_record_key(
                                rows[row_index - 1], table_contracts[0].source_ref, row_index
                            ),
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
        if source_rows is None or target_rows is None or not join_keys:
            errors.append({"contract_id": contract.id, "reason": "关系映射需要 source/target 数据集及 join_keys"})
            continue
        invalid_join_fields = []
        for join_key in join_keys:
            source_field = str(join_key.get("source_field") or "")
            target_field = str(join_key.get("target_field") or "")
            if not source_field or not target_field:
                invalid_join_fields.append("join key 缺少 source_field/target_field")
            elif source_rows and source_field not in source_rows[0]:
                invalid_join_fields.append(f"源数据集缺少关联字段 {source_field}")
            elif target_rows and target_field not in target_rows[0]:
                invalid_join_fields.append(f"目标数据集缺少关联字段 {target_field}")
        if invalid_join_fields:
            errors.append({"contract_id": contract.id, "reason": "; ".join(invalid_join_fields)})
            continue
        source_type_id = relation.source_object_type_id
        target_type_id = relation.target_object_type_id
        source_index = {idx: row_instance_ids.get((source_table, idx, source_type_id)) for idx in range(1, len(source_rows)+1)}
        target_index = {idx: row_instance_ids.get((target_table, idx, target_type_id)) for idx in range(1, len(target_rows)+1)}
        target_lookup: Dict[Tuple[Any, ...], List[str]] = {}
        target_row_by_instance: Dict[str, int] = {}
        for idx, row in enumerate(target_rows, start=1):
            key = tuple(row.get(k.get("target_field")) for k in join_keys)
            if all(v is not None for v in key) and target_index.get(idx):
                if target_index[idx] not in target_lookup.setdefault(key, []):
                    target_lookup[key].append(target_index[idx])
                target_row_by_instance[target_index[idx]] = idx
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
                target_row = target_row_by_instance[target_instance_id]
                instance.lineage.relationships.append(RelationshipLineage(
                    relationship_id=relationship_id,
                    source=SourceRecordRef(
                        source_id=_source_id(contract), dataset=source_table, table=source_table,
                        record_key=_record_key(row, contract.source_ref, idx), row_number=idx,
                    ),
                    target=SourceRecordRef(
                        source_id=str(contract.target_ref.get("source_id") or target_table),
                        dataset=target_table, table=target_table,
                        record_key=_record_key(
                            target_rows[target_row - 1], contract.target_ref, target_row
                        ), row_number=target_row,
                    ),
                    mapping_contract_id=contract.id,
                    mapping_contract_version=contract.version,
                    join_key_hash=_value_hash(key),
                    evidence=list(contract.evidence),
                ))
        # Enforce declared relationship endpoint cardinalities after join resolution.
        outgoing_counts: Dict[str, int] = {}
        incoming_counts: Dict[str, int] = {}
        for instance in instances.values():
            if instance.object_type_id == source_type_id:
                linked = instance.relationship_refs.get(relationship_id, [])
                outgoing_counts[instance.id] = len(linked)
                for target_instance_id in linked:
                    incoming_counts[target_instance_id] = incoming_counts.get(target_instance_id, 0) + 1
            if instance.object_type_id == target_type_id:
                incoming_counts[instance.id] = incoming_counts.get(instance.id, 0)
        for source_instance_id, count in outgoing_counts.items():
            if count < relation.source_min_cardinality or (
                relation.source_max_cardinality is not None and count > relation.source_max_cardinality
            ):
                errors.append({
                    "contract_id": contract.id, "instance_id": source_instance_id,
                    "reason": f"关系源端基数不满足约束: {count}",
                })
        for target_instance_id, count in incoming_counts.items():
            if count < relation.target_min_cardinality or (
                relation.target_max_cardinality is not None and count > relation.target_max_cardinality
            ):
                errors.append({
                    "contract_id": contract.id, "instance_id": target_instance_id,
                    "reason": f"关系目标端基数不满足约束: {count}",
                })

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
