"""V5 ABox runtime: instance index + legacy-compatible query view.

问答与图谱在迁移期仍可用「类名 + 属性名 + Type:pk」接口；
本模块以 OntologyInstance 为存储真相，并投影出 duck-type 的 query view。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
import json

import networkx as nx

from .instances import OntologyInstance
from .models import OntologySchema, new_id


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@dataclass
class ABoxStore:
    """Canonical in-memory ABox bound to one OntologySchema version."""

    schema: OntologySchema
    instances: Dict[str, OntologyInstance] = field(default_factory=dict)  # instance.id -> inst

    # name indexes
    type_name_by_id: Dict[str, str] = field(default_factory=dict)
    type_id_by_name: Dict[str, str] = field(default_factory=dict)
    prop_name_by_id: Dict[str, str] = field(default_factory=dict)
    prop_id_by_type_and_name: Dict[Tuple[str, str], str] = field(default_factory=dict)
    rel_name_by_id: Dict[str, str] = field(default_factory=dict)
    rel_id_by_name: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._rebuild_schema_index()

    def _rebuild_schema_index(self) -> None:
        self.type_name_by_id = {oid: ot.name for oid, ot in self.schema.object_types.items()}
        self.type_id_by_name = {ot.name: oid for oid, ot in self.schema.object_types.items()}
        self.prop_name_by_id = {pid: p.name for pid, p in self.schema.properties.items()}
        self.prop_id_by_type_and_name = {}
        for pid, p in self.schema.properties.items():
            tname = self.type_name_by_id.get(p.domain_object_type_id, "")
            self.prop_id_by_type_and_name[(tname, p.name)] = pid
        self.rel_name_by_id = {rid: r.name for rid, r in self.schema.relationships.items()}
        self.rel_id_by_name = {r.name: rid for rid, r in self.schema.relationships.items()}

    # ---- loaders ----
    @classmethod
    def from_legacy(cls, ontology: Any, schema: OntologySchema) -> "ABoxStore":
        """从 legacy Ontology.objects / links 物化 V5 实例（属性按名称→id 映射）。"""
        store = cls(schema=schema)
        type_id_by_name = store.type_id_by_name

        for tname, objs in (getattr(ontology, "objects", {}) or {}).items():
            oid = type_id_by_name.get(tname)
            if not oid:
                continue
            ot = schema.object_types[oid]
            pk_name = (getattr(ontology, "object_types", {}).get(tname) or {}).get("primary_key") or "id"
            for pk, row in (objs or {}).items():
                props: Dict[str, Any] = {}
                for col, val in (row or {}).items():
                    pid = store.prop_id_by_type_and_name.get((tname, str(col)))
                    if pid:
                        props[pid] = val
                identity = {}
                pk_pid = store.prop_id_by_type_and_name.get((tname, str(pk_name)))
                if pk_pid:
                    identity[pk_pid] = pk
                else:
                    identity["legacy_pk"] = pk
                iid = new_id("instance")
                store.instances[iid] = OntologyInstance(
                    id=iid,
                    object_type_id=oid,
                    identity_values=identity,
                    properties=props,
                    ontology_id=schema.ontology_id,
                    ontology_version=schema.version,
                    metadata={"legacy_type": tname, "legacy_pk": str(pk), "source": "legacy_projection"},
                )

        # relationship_refs from link_types + edges
        for u, v, _k, data in getattr(ontology, "links", nx.MultiDiGraph()).edges(keys=True, data=True):
            rel_name = str(data.get("relation_type") or data.get("label") or "")
            rid = store.rel_id_by_name.get(rel_name)
            if not rid:
                continue
            src_inst = store._find_by_legacy_node(str(u))
            tgt_inst = store._find_by_legacy_node(str(v))
            if not src_inst or not tgt_inst:
                continue
            src_inst.relationship_refs.setdefault(rid, [])
            if tgt_inst.id not in src_inst.relationship_refs[rid]:
                src_inst.relationship_refs[rid].append(tgt_inst.id)

        return store

    @classmethod
    def from_mapping_result(cls, schema: OntologySchema, result: Dict[str, Any]) -> "ABoxStore":
        """从 mapping_engine.execute_mapping 结果装载。"""
        store = cls(schema=schema)
        raw_instances = result.get("instances") or {}
        if isinstance(raw_instances, dict):
            values = raw_instances.values()
        else:
            values = raw_instances
        for item in values:
            if isinstance(item, OntologyInstance):
                inst = item
            elif isinstance(item, dict):
                # mapping may store dataclasses as objects already
                inst = OntologyInstance(
                    id=str(item.get("id") or new_id("instance")),
                    object_type_id=str(item["object_type_id"]),
                    identity_values=dict(item.get("identity_values") or {}),
                    properties=dict(item.get("properties") or {}),
                    relationship_refs={
                        str(k): list(v) for k, v in (item.get("relationship_refs") or {}).items()
                    },
                    ontology_id=item.get("ontology_id") or schema.ontology_id,
                    ontology_version=int(item.get("ontology_version") or schema.version),
                    metadata=dict(item.get("metadata") or {}),
                )
            else:
                continue
            store.instances[inst.id] = inst
        return store

    def _find_by_legacy_node(self, node_id: str) -> Optional[OntologyInstance]:
        # node_id format Type:pk
        if ":" not in node_id:
            return None
        tname, pk = node_id.split(":", 1)
        return self.find_by_type_and_pk(tname, pk)

    def find_by_type_and_pk(self, type_name: str, pk: str) -> Optional[OntologyInstance]:
        oid = self.type_id_by_name.get(type_name)
        if not oid:
            return None
        pk = str(pk)
        for inst in self.instances.values():
            if inst.object_type_id != oid:
                continue
            # identity values may be keyed by property id
            for val in inst.identity_values.values():
                if str(val) == pk:
                    return inst
            if str(inst.metadata.get("legacy_pk")) == pk:
                return inst
        return None

    def properties_by_name(self, inst: OntologyInstance) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for pid, val in inst.properties.items():
            name = self.prop_name_by_id.get(pid, pid)
            out[name] = val
        # include identity fields by name
        for pid, val in inst.identity_values.items():
            name = self.prop_name_by_id.get(pid, pid if pid != "legacy_pk" else "id")
            out.setdefault(name, val)
        return out

    def primary_key_value(self, inst: OntologyInstance) -> str:
        if inst.metadata.get("legacy_pk") is not None:
            return str(inst.metadata["legacy_pk"])
        if not inst.identity_values:
            return inst.id
        return str(next(iter(inst.identity_values.values())))

    def type_name(self, inst: OntologyInstance) -> str:
        return self.type_name_by_id.get(inst.object_type_id, inst.object_type_id)

    def to_query_view(self) -> "V5QueryView":
        return V5QueryView(self)

    def summary(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for inst in self.instances.values():
            name = self.type_name(inst)
            by_type[name] = by_type.get(name, 0) + 1
        return {
            "ontology_id": self.schema.ontology_id,
            "ontology_version": self.schema.version,
            "instance_count": len(self.instances),
            "by_type": by_type,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_ontology_id": self.schema.ontology_id,
            "schema_version": self.schema.version,
            "instances": [_jsonable(inst) for inst in self.instances.values()],
            "summary": self.summary(),
        }


class V5QueryView:
    """Duck-type legacy Ontology for ontology_query_pipeline.execute_query_plan."""

    def __init__(self, store: ABoxStore) -> None:
        self._store = store
        self.object_types: Dict[str, Dict[str, Any]] = {}
        self.objects: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.link_types: Dict[str, Dict[str, Any]] = {}
        self.links = nx.MultiDiGraph()
        self._materialize()

    def _materialize(self) -> None:
        store = self._store
        schema = store.schema
        for oid, ot in schema.object_types.items():
            props = []
            pk = "id"
            for pid in ot.property_ids:
                p = schema.properties.get(pid)
                if p:
                    props.append(p.name)
            for iid in ot.identity_ids:
                ident = schema.identities.get(iid)
                if ident and ident.property_refs:
                    pref = schema.properties.get(ident.property_refs[0])
                    if pref:
                        pk = pref.name
                        break
            self.object_types[ot.name] = {
                "primary_key": pk,
                "properties": props,
                "description": ot.description,
            }
            self.objects[ot.name] = {}

        for rid, rel in schema.relationships.items():
            src = store.type_name_by_id.get(rel.source_object_type_id, "")
            tgt = store.type_name_by_id.get(rel.target_object_type_id, "")
            self.link_types[rel.name] = {
                "source": src,
                "target": tgt,
                "description": rel.description,
            }

        # instances as objects[type][pk]
        id_to_node: Dict[str, str] = {}
        for inst in store.instances.values():
            tname = store.type_name(inst)
            pk = store.primary_key_value(inst)
            row = store.properties_by_name(inst)
            self.objects.setdefault(tname, {})[pk] = row
            node_id = f"{tname}:{pk}"
            id_to_node[inst.id] = node_id
            self.links.add_node(node_id, type=tname, pk=pk, label=str(pk))

        # edges from relationship_refs
        for inst in store.instances.values():
            src_node = id_to_node.get(inst.id)
            if not src_node:
                continue
            for rid, targets in inst.relationship_refs.items():
                rel_name = store.rel_name_by_id.get(rid, rid)
                for tid in targets:
                    tgt_node = id_to_node.get(tid)
                    if not tgt_node:
                        continue
                    self.links.add_edge(src_node, tgt_node, relation_type=rel_name, label=rel_name)
