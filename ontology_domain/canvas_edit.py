"""Canvas structure editing with write-back to legacy Ontology + V5 OntologySchema.

白名单操作（仅结构层，不直接改 MappingContract / 血缘）：
- ObjectType: add / rename_label / update_description / delete
- Property: add / update / delete
- Relationship: add / update / delete

设计原则：
1. 所有变更必须经过显式确认（UI 侧）
2. 同时更新运行时 Ontology（图谱立即刷新）与 V5 Schema（持久化真相源）
3. 记录 edit log，便于审计与回滚
4. 删除有依赖检查（实例、关系引用）
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import re

from .legacy_adapter import legacy_snapshot_to_v5
from .models import (
    Identity,
    ObjectType,
    OntologySchema,
    Property,
    Relationship,
    SemanticTerms,
    new_id,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(name: str) -> str:
    s = re.sub(r"\s+", "_", str(name).strip())
    s = re.sub(r"[^\w\u4e00-\u9fff\-]", "", s)
    return s or "unnamed"


@dataclass
class EditResult:
    ok: bool
    message: str
    operation: str
    details: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CanvasEditLogEntry:
    timestamp: str
    operation: str
    payload: Dict[str, Any]
    result: str
    message: str


class CanvasEditService:
    """对 legacy Ontology + 可选 V5 OntologySchema 执行结构编辑。"""

    def __init__(
        self,
        ontology: Any,
        schema: Optional[OntologySchema] = None,
        *,
        auto_v5: bool = True,
    ) -> None:
        self.ontology = ontology
        self.schema = schema
        self.edit_log: List[CanvasEditLogEntry] = []
        if self.schema is None and auto_v5:
            try:
                snapshot = {
                    "object_types": getattr(ontology, "object_types", {}),
                    "link_types": getattr(ontology, "link_types", {}),
                    "data_mapping": getattr(ontology, "data_mapping", {}),
                }
                self.schema = legacy_snapshot_to_v5(snapshot)
            except Exception:
                self.schema = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _log(self, operation: str, payload: Dict[str, Any], result: EditResult) -> None:
        self.edit_log.append(
            CanvasEditLogEntry(
                timestamp=_utc_now(),
                operation=operation,
                payload=payload,
                result="ok" if result.ok else "error",
                message=result.message,
            )
        )

    def _find_object_type_id_by_name(self, name: str) -> Optional[str]:
        if not self.schema:
            return None
        for oid, ot in self.schema.object_types.items():
            if ot.name == name or ot.label == name:
                return oid
        return None

    def _find_property_id(self, type_name: str, prop_name: str) -> Optional[str]:
        if not self.schema:
            return None
        oid = self._find_object_type_id_by_name(type_name)
        if not oid:
            return None
        for pid in self.schema.object_types[oid].property_ids:
            p = self.schema.properties.get(pid)
            if p and p.name == prop_name:
                return pid
        return None

    def _find_relationship_id(self, rel_name: str) -> Optional[str]:
        if not self.schema:
            return None
        for rid, rel in self.schema.relationships.items():
            if rel.name == rel_name or rel.label == rel_name:
                return rid
        return None

    # ------------------------------------------------------------------
    # ObjectType
    # ------------------------------------------------------------------
    def add_object_type(
        self,
        name: str,
        *,
        primary_key: str = "id",
        properties: Optional[List[str]] = None,
        description: str = "",
        label: Optional[str] = None,
    ) -> EditResult:
        op = "add_object_type"
        payload = {"name": name, "primary_key": primary_key, "properties": properties, "description": description}
        name = str(name).strip()
        if not name:
            result = EditResult(False, "ObjectType 名称不能为空", op)
            self._log(op, payload, result)
            return result
        if name in self.ontology.object_types:
            result = EditResult(False, f"ObjectType '{name}' 已存在", op)
            self._log(op, payload, result)
            return result

        props = list(properties or [])
        if primary_key and primary_key not in props:
            props.insert(0, primary_key)

        # legacy
        self.ontology.add_object_type(name, primary_key, props, description)

        # v5
        if self.schema is not None:
            oid = new_id("object-type")
            prop_ids: List[str] = []
            for pname in props:
                pid = new_id("property")
                prop_ids.append(pid)
                self.schema.properties[pid] = Property(
                    id=pid,
                    name=pname,
                    label=pname,
                    domain_object_type_id=oid,
                    data_type="string",
                    required=(pname == primary_key),
                    nullable=(pname != primary_key),
                )
            identity_ids: List[str] = []
            if primary_key:
                iid = new_id("identity")
                pk_prop = next(
                    (pid for pid in prop_ids if self.schema.properties[pid].name == primary_key),
                    None,
                )
                if pk_prop:
                    identity_ids.append(iid)
                    self.schema.identities[iid] = Identity(
                        id=iid,
                        object_type_id=oid,
                        name=f"{name} primary key",
                        property_refs=[pk_prop],
                        identity_type="technical",
                    )
            self.schema.object_types[oid] = ObjectType(
                id=oid,
                name=name,
                label=label or name,
                description=description,
                property_ids=prop_ids,
                identity_ids=identity_ids,
                status="draft",
            )
            self.schema.version += 1

        result = EditResult(True, f"已新增 ObjectType '{name}'", op, {"name": name, "properties": props})
        self._log(op, payload, result)
        return result

    def update_object_type(
        self,
        name: str,
        *,
        label: Optional[str] = None,
        description: Optional[str] = None,
    ) -> EditResult:
        op = "update_object_type"
        payload = {"name": name, "label": label, "description": description}
        if name not in self.ontology.object_types:
            result = EditResult(False, f"ObjectType '{name}' 不存在", op)
            self._log(op, payload, result)
            return result

        info = self.ontology.object_types[name]
        if description is not None:
            info["description"] = description

        if self.schema is not None:
            oid = self._find_object_type_id_by_name(name)
            if oid:
                ot = self.schema.object_types[oid]
                if label is not None:
                    ot.label = label
                if description is not None:
                    ot.description = description
                self.schema.version += 1

        result = EditResult(True, f"已更新 ObjectType '{name}'", op, payload)
        self._log(op, payload, result)
        return result

    def delete_object_type(self, name: str, *, force: bool = False) -> EditResult:
        op = "delete_object_type"
        payload = {"name": name, "force": force}
        if name not in self.ontology.object_types:
            result = EditResult(False, f"ObjectType '{name}' 不存在", op)
            self._log(op, payload, result)
            return result

        warnings: List[str] = []
        inst_count = len(self.ontology.objects.get(name, {}))
        if inst_count and not force:
            result = EditResult(
                False,
                f"ObjectType '{name}' 仍有 {inst_count} 个实例，请先清空或使用 force=True",
                op,
                {"instance_count": inst_count},
            )
            self._log(op, payload, result)
            return result

        # 检查关系依赖
        rel_deps = [
            rn
            for rn, info in self.ontology.link_types.items()
            if info.get("source") == name or info.get("target") == name
        ]
        if rel_deps and not force:
            result = EditResult(
                False,
                f"ObjectType '{name}' 被关系引用：{', '.join(rel_deps)}。请先删除关系或 force=True",
                op,
                {"relationships": rel_deps},
            )
            self._log(op, payload, result)
            return result

        # legacy cleanup
        if force:
            for rn in list(rel_deps):
                self.delete_relationship(rn, force=True)
            # remove instance nodes
            for pk in list(self.ontology.objects.get(name, {}).keys()):
                node_id = f"{name}:{pk}"
                if node_id in self.ontology.links:
                    self.ontology.links.remove_node(node_id)
            self.ontology.objects.pop(name, None)
            warnings.append(f"已强制删除 {inst_count} 个实例与 {len(rel_deps)} 个关系")

        del self.ontology.object_types[name]

        if self.schema is not None:
            oid = self._find_object_type_id_by_name(name)
            if oid:
                ot = self.schema.object_types.pop(oid)
                for pid in list(ot.property_ids):
                    self.schema.properties.pop(pid, None)
                for iid in list(ot.identity_ids):
                    self.schema.identities.pop(iid, None)
                for rid in list(ot.relationship_ids):
                    self.schema.relationships.pop(rid, None)
                # clean reverse refs
                for other in self.schema.object_types.values():
                    other.relationship_ids = [r for r in other.relationship_ids if r in self.schema.relationships]
                self.schema.version += 1

        result = EditResult(True, f"已删除 ObjectType '{name}'", op, payload, warnings)
        self._log(op, payload, result)
        return result

    # ------------------------------------------------------------------
    # Property
    # ------------------------------------------------------------------
    def add_property(
        self,
        type_name: str,
        prop_name: str,
        *,
        data_type: str = "string",
        description: str = "",
        required: bool = False,
    ) -> EditResult:
        op = "add_property"
        payload = {
            "type_name": type_name,
            "prop_name": prop_name,
            "data_type": data_type,
            "description": description,
            "required": required,
        }
        if type_name not in self.ontology.object_types:
            result = EditResult(False, f"ObjectType '{type_name}' 不存在", op)
            self._log(op, payload, result)
            return result
        props = self.ontology.object_types[type_name].setdefault("properties", [])
        if prop_name in props:
            result = EditResult(False, f"属性 '{prop_name}' 已存在于 '{type_name}'", op)
            self._log(op, payload, result)
            return result

        props.append(prop_name)

        if self.schema is not None:
            oid = self._find_object_type_id_by_name(type_name)
            if oid:
                pid = new_id("property")
                self.schema.properties[pid] = Property(
                    id=pid,
                    name=prop_name,
                    label=prop_name,
                    domain_object_type_id=oid,
                    data_type=data_type if data_type in {
                        "string", "integer", "number", "boolean", "date", "datetime", "enum", "object_ref", "json"
                    } else "string",
                    description=description,
                    required=required,
                    nullable=not required,
                )
                self.schema.object_types[oid].property_ids.append(pid)
                self.schema.version += 1

        result = EditResult(True, f"已为 '{type_name}' 新增属性 '{prop_name}'", op, payload)
        self._log(op, payload, result)
        return result

    def delete_property(self, type_name: str, prop_name: str, *, force: bool = False) -> EditResult:
        op = "delete_property"
        payload = {"type_name": type_name, "prop_name": prop_name, "force": force}
        if type_name not in self.ontology.object_types:
            result = EditResult(False, f"ObjectType '{type_name}' 不存在", op)
            self._log(op, payload, result)
            return result
        info = self.ontology.object_types[type_name]
        props = info.get("properties", [])
        if prop_name not in props:
            result = EditResult(False, f"属性 '{prop_name}' 不存在", op)
            self._log(op, payload, result)
            return result
        if info.get("primary_key") == prop_name and not force:
            result = EditResult(False, "不能删除主键属性（除非 force=True）", op)
            self._log(op, payload, result)
            return result

        props.remove(prop_name)
        # scrub instance values
        for obj in self.ontology.objects.get(type_name, {}).values():
            obj.pop(prop_name, None)

        if self.schema is not None:
            pid = self._find_property_id(type_name, prop_name)
            oid = self._find_object_type_id_by_name(type_name)
            if pid:
                self.schema.properties.pop(pid, None)
            if oid:
                ot = self.schema.object_types[oid]
                ot.property_ids = [p for p in ot.property_ids if p != pid]
                # clean identities referencing this property
                for iid in list(ot.identity_ids):
                    ident = self.schema.identities.get(iid)
                    if ident and pid in ident.property_refs:
                        ident.property_refs = [p for p in ident.property_refs if p != pid]
                        if not ident.property_refs:
                            self.schema.identities.pop(iid, None)
                            ot.identity_ids = [i for i in ot.identity_ids if i != iid]
                self.schema.version += 1

        result = EditResult(True, f"已删除属性 '{type_name}.{prop_name}'", op, payload)
        self._log(op, payload, result)
        return result

    # ------------------------------------------------------------------
    # Relationship
    # ------------------------------------------------------------------
    def add_relationship(
        self,
        name: str,
        source_type: str,
        target_type: str,
        *,
        description: str = "",
        source_property: str = "",
        target_property: str = "",
    ) -> EditResult:
        op = "add_relationship"
        payload = {
            "name": name,
            "source_type": source_type,
            "target_type": target_type,
            "description": description,
        }
        name = str(name).strip()
        if not name:
            result = EditResult(False, "关系名称不能为空", op)
            self._log(op, payload, result)
            return result
        if name in self.ontology.link_types:
            result = EditResult(False, f"关系 '{name}' 已存在", op)
            self._log(op, payload, result)
            return result
        if source_type not in self.ontology.object_types:
            result = EditResult(False, f"源类型 '{source_type}' 不存在", op)
            self._log(op, payload, result)
            return result
        if target_type not in self.ontology.object_types:
            result = EditResult(False, f"目标类型 '{target_type}' 不存在", op)
            self._log(op, payload, result)
            return result

        self.ontology.add_link_type(
            name, source_type, target_type, description, source_property, target_property
        )

        if self.schema is not None:
            sid = self._find_object_type_id_by_name(source_type)
            tid = self._find_object_type_id_by_name(target_type)
            if sid and tid:
                rid = new_id("relationship")
                self.schema.relationships[rid] = Relationship(
                    id=rid,
                    name=name,
                    label=name,
                    source_object_type_id=sid,
                    target_object_type_id=tid,
                    description=description,
                    status="candidate",
                    metadata={
                        "legacy_source_property": source_property,
                        "legacy_target_property": target_property,
                        "source": "canvas_edit",
                    },
                )
                self.schema.object_types[sid].relationship_ids.append(rid)
                if tid != sid:
                    self.schema.object_types[tid].relationship_ids.append(rid)
                self.schema.version += 1

        result = EditResult(True, f"已新增关系 '{name}'：{source_type} → {target_type}", op, payload)
        self._log(op, payload, result)
        return result

    def delete_relationship(self, name: str, *, force: bool = False) -> EditResult:
        op = "delete_relationship"
        payload = {"name": name, "force": force}
        if name not in self.ontology.link_types:
            result = EditResult(False, f"关系 '{name}' 不存在", op)
            self._log(op, payload, result)
            return result

        # remove edges with this relation
        edges_to_remove = []
        for u, v, k, d in list(self.ontology.links.edges(keys=True, data=True)):
            rel = d.get("relation_type") or d.get("label") or str(k)
            if rel == name:
                edges_to_remove.append((u, v, k))
        if edges_to_remove and not force:
            result = EditResult(
                False,
                f"关系 '{name}' 仍有 {len(edges_to_remove)} 条实例边，请使用 force=True 一并删除",
                op,
                {"edge_count": len(edges_to_remove)},
            )
            self._log(op, payload, result)
            return result

        for u, v, k in edges_to_remove:
            self.ontology.links.remove_edge(u, v, k)
        del self.ontology.link_types[name]

        if self.schema is not None:
            rid = self._find_relationship_id(name)
            if rid:
                self.schema.relationships.pop(rid, None)
                for ot in self.schema.object_types.values():
                    ot.relationship_ids = [r for r in ot.relationship_ids if r != rid]
                self.schema.version += 1

        result = EditResult(
            True,
            f"已删除关系 '{name}'" + (f"（含 {len(edges_to_remove)} 条边）" if edges_to_remove else ""),
            op,
            payload,
        )
        self._log(op, payload, result)
        return result

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------
    def get_v5_schema(self) -> Optional[OntologySchema]:
        return self.schema

    def get_edit_log(self) -> List[Dict[str, Any]]:
        return [asdict(e) for e in self.edit_log]

    def schema_summary(self) -> Dict[str, Any]:
        if not self.schema:
            return {"v5": False}
        return {
            "v5": True,
            "ontology_id": self.schema.ontology_id,
            "version": self.schema.version,
            "object_types": len(self.schema.object_types),
            "properties": len(self.schema.properties),
            "relationships": len(self.schema.relationships),
            "identities": len(self.schema.identities),
            "validation_errors": self.schema.validate(),
        }
