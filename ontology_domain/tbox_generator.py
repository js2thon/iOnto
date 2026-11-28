"""基于 SourceMetadata 生成 V5 TBox（ObjectType / Property / Relationship / Identity）。

原则：
- 只生成结构（TBox），不加载实例（ABox）
- 主键 → Identity(technical)
- 外键候选 → Relationship(candidate)
- 列逻辑类型 → Property.data_type
- 输出可被 CanvasEdit / Mapping / Gate 直接消费
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .metadata_extractor import SourceMetadata, TableMetadata
from .models import (
    Identity,
    ObjectType,
    OntologySchema,
    Property,
    Relationship,
    SemanticTerms,
    new_id,
)


def _type_name_from_table(table: str) -> str:
    # 保留原表名作为 ObjectType.name，便于溯源；可在 UI 中改 label
    return str(table).strip()


def generate_tbox_from_workbench(
    workbench: Any,
    *,
    ontology_id: Optional[str] = None,
    include_relationships: bool = True,
    min_rel_confidence: float = 0.5,
    primary_key_overrides: Optional[Dict[str, str]] = None,
    table_allowlist: Optional[List[str]] = None,
) -> OntologySchema:
    """从 MetadataWorkbench 工作副本（多源合并 + 人工编辑）生成 TBox。"""
    meta = workbench.get_working_copy()
    if table_allowlist is not None:
        allow = set(table_allowlist)
        filtered_tables = {k: v for k, v in meta.tables.items() if k in allow}
        filtered_rels = [
            r
            for r in meta.relationship_candidates
            if r.get("source_table") in allow and r.get("target_table") in allow
        ]
        meta = SourceMetadata(
            tables=filtered_tables,
            relationship_candidates=filtered_rels,
            extracted_at=meta.extracted_at,
            source_kind=meta.source_kind,
        )
    return generate_tbox_from_metadata(
        meta,
        ontology_id=ontology_id,
        include_relationships=include_relationships,
        min_rel_confidence=min_rel_confidence,
        primary_key_overrides=primary_key_overrides,
    )


def generate_tbox_from_metadata(
    metadata: SourceMetadata,
    *,
    ontology_id: Optional[str] = None,
    include_relationships: bool = True,
    min_rel_confidence: float = 0.5,
    primary_key_overrides: Optional[Dict[str, str]] = None,
) -> OntologySchema:
    """从元数据生成 V5 OntologySchema（仅 TBox）。"""
    primary_key_overrides = primary_key_overrides or {}
    schema = OntologySchema(
        ontology_id=ontology_id or new_id("ontology"),
        version=1,
        metadata={
            "generated_from": "source_metadata",
            "source_kind": metadata.source_kind,
            "extracted_at": metadata.extracted_at,
            "tbox_only": True,
        },
    )

    type_id_by_name: Dict[str, str] = {}
    prop_id_by_key: Dict[Tuple[str, str], str] = {}

    for table_name, tmeta in metadata.tables.items():
        type_name = _type_name_from_table(table_name)
        oid = new_id("object-type")
        type_id_by_name[type_name] = oid

        pk = primary_key_overrides.get(table_name) or tmeta.suggested_primary_key
        prop_ids: List[str] = []
        identity_ids: List[str] = []

        for col in tmeta.columns:
            pid = new_id("property")
            prop_id_by_key[(type_name, col.name)] = pid
            prop_ids.append(pid)
            is_pk = bool(pk and col.name == pk)
            schema.properties[pid] = Property(
                id=pid,
                name=col.name,
                label=col.name,
                domain_object_type_id=oid,
                data_type=col.logical_type
                if col.logical_type
                in {
                    "string",
                    "integer",
                    "number",
                    "boolean",
                    "date",
                    "datetime",
                    "enum",
                    "object_ref",
                    "json",
                }
                else "string",
                description=_column_description(col),
                required=is_pk or (not col.nullable and col.null_ratio == 0),
                nullable=not is_pk and col.nullable,
                metadata={
                    "source_table": table_name,
                    "source_column": col.name,
                    "pandas_dtype": col.pandas_dtype,
                    "null_ratio": col.null_ratio,
                    "unique_ratio": col.unique_ratio,
                    "name_hints": col.name_hints,
                    "suspected_pk": col.suspected_pk,
                    "suspected_fk": col.suspected_fk,
                },
            )

        if pk and (type_name, pk) in prop_id_by_key:
            iid = new_id("identity")
            identity_ids.append(iid)
            schema.identities[iid] = Identity(
                id=iid,
                object_type_id=oid,
                name=f"{type_name} primary key",
                property_refs=[prop_id_by_key[(type_name, pk)]],
                identity_type="technical",
                metadata={"source": "metadata_pk", "column": pk},
            )

        schema.object_types[oid] = ObjectType(
            id=oid,
            name=type_name,
            label=type_name,
            description=tmeta.description or f"从数据表 {table_name} 生成",
            object_category=_guess_category(type_name, tmeta),
            semantic_terms=SemanticTerms(),
            property_ids=prop_ids,
            identity_ids=identity_ids,
            status="draft",
            metadata={
                "source_table": table_name,
                "row_count": tmeta.row_count,
                "suggested_primary_key": pk,
                "pk_candidates": tmeta.primary_key_candidates,
            },
        )

    if include_relationships:
        for rel in metadata.relationship_candidates:
            if float(rel.get("confidence") or 0) < min_rel_confidence:
                continue
            src = _type_name_from_table(rel["source_table"])
            tgt = _type_name_from_table(rel["target_table"])
            sid = type_id_by_name.get(src)
            tid = type_id_by_name.get(tgt)
            if not sid or not tid:
                continue
            rid = new_id("relationship")
            rname = str(rel.get("name") or f"{src}_to_{tgt}")
            schema.relationships[rid] = Relationship(
                id=rid,
                name=rname,
                label=rname,
                source_object_type_id=sid,
                target_object_type_id=tid,
                description=(
                    f"元数据推断：{rel['source_table']}.{rel['source_column']} → "
                    f"{rel['target_table']}.{rel['target_column']}"
                ),
                relationship_kind="association",
                confidence=float(rel.get("confidence") or 0),
                status="candidate",
                evidence=[
                    {
                        "type": "fk_detection",
                        "source_column": rel.get("source_column"),
                        "target_column": rel.get("target_column"),
                        "value_coverage": rel.get("value_coverage"),
                        "detection": rel.get("detection"),
                    }
                ],
                metadata={
                    "source_column": rel.get("source_column"),
                    "target_column": rel.get("target_column"),
                    "generated_from": "metadata",
                },
            )
            schema.object_types[sid].relationship_ids.append(rid)
            if tid != sid:
                schema.object_types[tid].relationship_ids.append(rid)

    errors = schema.validate()
    if errors:
        schema.metadata["validation_warnings"] = errors
    return schema


def apply_tbox_to_legacy_ontology(schema: OntologySchema, ontology: Any) -> None:
    """把生成的 TBox 写入 legacy Ontology 结构（仅类型/关系，不含实例）。"""
    # clear structure but keep instances if any? For TBox generation we typically reset types.
    # Caller decides; here we merge by name.
    id_to_name = {oid: ot.name for oid, ot in schema.object_types.items()}

    for oid, ot in schema.object_types.items():
        props = []
        for pid in ot.property_ids:
            p = schema.properties.get(pid)
            if p:
                props.append(p.name)
        pk = None
        for iid in ot.identity_ids:
            ident = schema.identities.get(iid)
            if ident and ident.property_refs:
                pref = schema.properties.get(ident.property_refs[0])
                if pref:
                    pk = pref.name
                    break
        if not pk and props:
            pk = props[0]
        if ot.name in ontology.object_types:
            # update description/properties
            info = ontology.object_types[ot.name]
            info["description"] = ot.description
            # merge properties
            existing = list(info.get("properties") or [])
            for p in props:
                if p not in existing:
                    existing.append(p)
            info["properties"] = existing
            if pk and not info.get("primary_key"):
                info["primary_key"] = pk
        else:
            ontology.add_object_type(
                ot.name,
                pk or "id",
                props,
                ot.description,
            )

    for rid, rel in schema.relationships.items():
        src = id_to_name.get(rel.source_object_type_id)
        tgt = id_to_name.get(rel.target_object_type_id)
        if not src or not tgt:
            continue
        if rel.name in ontology.link_types:
            continue
        ontology.add_link_type(
            rel.name,
            src,
            tgt,
            rel.description,
            source_property=str((rel.metadata or {}).get("source_column") or ""),
            target_property=str((rel.metadata or {}).get("target_column") or ""),
        )


def _column_description(col: Any) -> str:
    parts = [f"逻辑类型={col.logical_type}"]
    if col.suspected_pk:
        parts.append("疑似主键")
    if col.suspected_fk:
        parts.append("疑似外键")
    if col.null_ratio:
        parts.append(f"空值率={col.null_ratio:.1%}")
    return "；".join(parts)


def _guess_category(type_name: str, tmeta: TableMetadata) -> str:
    n = type_name.lower()
    if any(x in n for x in ("order", "transaction", "payment", "invoice", "event", "log")):
        return "transaction"
    if any(x in n for x in ("org", "dept", "department", "company", "team")):
        return "organization"
    if any(x in n for x in ("process", "workflow", "step")):
        return "process"
    if any(x in n for x in ("dict", "code", "ref", "type", "status", "category")):
        return "reference"
    if tmeta.row_count > 10000:
        return "transaction"
    return "master"


def metadata_to_tbox_summary(schema: OntologySchema) -> Dict[str, Any]:
    return {
        "ontology_id": schema.ontology_id,
        "version": schema.version,
        "object_types": len(schema.object_types),
        "properties": len(schema.properties),
        "relationships": len(schema.relationships),
        "identities": len(schema.identities),
        "types": [
            {
                "name": ot.name,
                "label": ot.label,
                "properties": len(ot.property_ids),
                "identities": len(ot.identity_ids),
                "category": ot.object_category,
            }
            for ot in schema.object_types.values()
        ],
        "relationships": [
            {
                "name": r.name,
                "confidence": r.confidence,
                "status": r.status,
            }
            for r in schema.relationships.values()
        ],
    }
