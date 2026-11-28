"""V5 为唯一结构真相源，Legacy Ontology 为运行时投影。

约定：
- 结构（ObjectType / Property / Relationship / Identity）以 OntologySchema 为准
- Legacy Ontology.object_types / link_types 由 V5 投影生成或刷新
- Legacy Ontology.objects / links 仍承载实例图，供问答与图谱使用（ABox 尚未完全迁 V5）
- 所有结构编辑应：改 V5 → project_structure_to_legacy →（可选）persist

本模块不依赖 Streamlit，便于单测。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .legacy_adapter import legacy_snapshot_to_v5
from .models import OntologySchema
from .tbox_generator import apply_tbox_to_legacy_ontology


def ensure_v5_schema(
    ontology: Any,
    schema: Optional[OntologySchema] = None,
    *,
    force_from_legacy: bool = False,
) -> OntologySchema:
    """返回可用的 V5 Schema。

    - 若 schema 已有且不 force：直接返回
    - 否则从 legacy snapshot 迁移生成
    """
    if schema is not None and not force_from_legacy:
        return schema
    snapshot = {
        "object_types": getattr(ontology, "object_types", {}) or {},
        "link_types": getattr(ontology, "link_types", {}) or {},
        "data_mapping": getattr(ontology, "data_mapping", {}) or {},
    }
    return legacy_snapshot_to_v5(snapshot)


def project_structure_to_legacy(schema: OntologySchema, ontology: Any) -> None:
    """把 V5 结构投影到 legacy Ontology（保留已有实例节点与边）。

    - 更新/补齐 object_types 与 link_types
    - 不删除仍存在于 V5 的类型上的实例
    - 移除 V5 中已不存在的 object_types / link_types（及其实例节点、关系边）
    """
    # 先按 TBox 应用（合并属性、补类型）
    apply_tbox_to_legacy_ontology(schema, ontology)

    v5_type_names = {ot.name for ot in schema.object_types.values()}
    v5_rel_names = {r.name for r in schema.relationships.values()}

    # 删除 V5 中已去掉的类型
    for tname in list(getattr(ontology, "object_types", {}).keys()):
        if tname not in v5_type_names:
            # drop instances
            for pk in list((ontology.objects.get(tname) or {}).keys()):
                node_id = f"{tname}:{pk}"
                if hasattr(ontology, "links") and node_id in ontology.links:
                    ontology.links.remove_node(node_id)
            ontology.objects.pop(tname, None)
            ontology.object_types.pop(tname, None)

    # 删除 V5 中已去掉的关系类型
    for rname in list(getattr(ontology, "link_types", {}).keys()):
        if rname not in v5_rel_names:
            if hasattr(ontology, "links"):
                for u, v, k, d in list(ontology.links.edges(keys=True, data=True)):
                    rel = d.get("relation_type") or d.get("label") or str(k)
                    if rel == rname:
                        ontology.links.remove_edge(u, v, k)
            ontology.link_types.pop(rname, None)

    # 对齐主键与属性列表（以 V5 为准）
    id_to_ot = {oid: ot for oid, ot in schema.object_types.items()}
    for ot in schema.object_types.values():
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
        info = ontology.object_types.setdefault(
            ot.name,
            {"primary_key": pk or "id", "properties": props, "description": ot.description},
        )
        info["description"] = ot.description
        info["properties"] = props
        if pk:
            info["primary_key"] = pk


def apply_structural_edit(
    ontology: Any,
    schema: Optional[OntologySchema],
    *,
    editor: Any,
) -> Tuple[OntologySchema, Any]:
    """在 CanvasEditService（或同类）完成编辑后，统一写回。

    editor 需提供 get_v5_schema()；若无 schema 则从 editor 取。
    返回 (canonical_schema, ontology)。
    """
    new_schema = editor.get_v5_schema() if hasattr(editor, "get_v5_schema") else schema
    if new_schema is None:
        new_schema = ensure_v5_schema(ontology, schema)
    project_structure_to_legacy(new_schema, ontology)
    return new_schema, ontology


def structure_diff(schema: OntologySchema, ontology: Any) -> Dict[str, Any]:
    """比较 V5 与 legacy 结构，找出不一致项（用于发布前告警）。"""
    legacy_types = set((getattr(ontology, "object_types", {}) or {}).keys())
    v5_types = {ot.name for ot in schema.object_types.values()}
    legacy_rels = set((getattr(ontology, "link_types", {}) or {}).keys())
    v5_rels = {r.name for r in schema.relationships.values()}

    issues: List[str] = []
    only_legacy_types = sorted(legacy_types - v5_types)
    only_v5_types = sorted(v5_types - legacy_types)
    only_legacy_rels = sorted(legacy_rels - v5_rels)
    only_v5_rels = sorted(v5_rels - legacy_rels)

    if only_legacy_types:
        issues.append(f"仅 legacy 有类型: {', '.join(only_legacy_types)}")
    if only_v5_types:
        issues.append(f"仅 V5 有类型: {', '.join(only_v5_types)}")
    if only_legacy_rels:
        issues.append(f"仅 legacy 有关系: {', '.join(only_legacy_rels)}")
    if only_v5_rels:
        issues.append(f"仅 V5 有关系: {', '.join(only_v5_rels)}")

    # property sets
    prop_mismatches = []
    for ot in schema.object_types.values():
        v5_props = {
            schema.properties[pid].name
            for pid in ot.property_ids
            if pid in schema.properties
        }
        leg = set((ontology.object_types.get(ot.name) or {}).get("properties") or [])
        if v5_props != leg:
            prop_mismatches.append(ot.name)
    if prop_mismatches:
        issues.append(f"属性集合不一致的类型: {', '.join(prop_mismatches[:20])}")

    return {
        "aligned": len(issues) == 0,
        "issues": issues,
        "only_legacy_types": only_legacy_types,
        "only_v5_types": only_v5_types,
        "only_legacy_rels": only_legacy_rels,
        "only_v5_rels": only_v5_rels,
        "prop_mismatches": prop_mismatches,
    }


def publish_precheck(schema: Optional[OntologySchema], ontology: Any) -> Dict[str, Any]:
    """发布前结构一致性预检。"""
    if schema is None:
        return {"ok": False, "message": "无 V5 Schema", "diff": None}
    diff = structure_diff(schema, ontology)
    return {
        "ok": diff["aligned"],
        "message": "结构一致" if diff["aligned"] else "V5 与 legacy 结构不一致",
        "diff": diff,
    }
