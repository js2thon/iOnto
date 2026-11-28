"""iOnto → RDF/OWL 导出（支持 TriG 命名图，便于 Ontosphere 加载与推理）

命名图约定：
- urn:ionto:schema:     本体结构（TBox：类、属性、关系）
- urn:ionto:data:       实例数据（ABox）
- urn:ionto:mapping:    MappingContract / 字段映射元数据
- urn:ionto:lineage:    血缘与 provenance（预留）

使用方式：
    from ontology_domain.rdf_export import export_ontology_to_dataset, serialize_trig
    ds = export_ontology_to_dataset(ontology)
    trig = serialize_trig(ds)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from rdflib import Dataset, Graph, Literal, Namespace, OWL, RDF, RDFS, URIRef, XSD
from rdflib.namespace import DCTERMS


# ---------------------------------------------------------------------------
# 命名空间
# ---------------------------------------------------------------------------
IONTO = Namespace("https://ionto.local/ontology/")
EX = Namespace("https://ionto.local/ex/")
SCHEMA_G = URIRef("urn:ionto:schema:")
DATA_G = URIRef("urn:ionto:data:")
MAPPING_G = URIRef("urn:ionto:mapping:")
LINEAGE_G = URIRef("urn:ionto:lineage:")


def _safe_local(name: str) -> str:
    """把名称转成相对安全的 IRI local part。"""
    return quote(str(name).replace(" ", "_").replace(":", "_"), safe="_-.")


def _iri(*parts: str) -> URIRef:
    return URIRef(str(EX) + "/".join(_safe_local(p) for p in parts if p is not None))


def export_ontology_to_dataset(
    ontology: Any,
    *,
    include_instances: bool = True,
    include_mapping: bool = True,
    base_iri: str = "https://ionto.local/ontology/",
) -> Dataset:
    """
    把 iOnto 本体（旧版 Ontology 或兼容对象）导出为带命名图的 Dataset。

    兼容 ontology_studio_v3.py 中的 Ontology 类属性：
    - object_types, objects, link_types, links, data_mapping
    """
    ds = Dataset()
    ds.bind("ionto", IONTO)
    ds.bind("ex", EX)
    ds.bind("owl", OWL)
    ds.bind("rdfs", RDFS)
    ds.bind("xsd", XSD)
    ds.bind("dcterms", DCTERMS)

    # ---------- 默认图：本体声明 ----------
    onto_uri = URIRef(base_iri.rstrip("/") + "/")
    ds.add((onto_uri, RDF.type, OWL.Ontology))
    ds.add((onto_uri, RDFS.label, Literal("iOnto Exported Ontology")))
    ds.add(
        (
            onto_uri,
            DCTERMS.created,
            Literal(datetime.now(timezone.utc).isoformat(), datatype=XSD.dateTime),
        )
    )
    ds.add((onto_uri, DCTERMS.creator, Literal("iOnto")))
    ds.add((onto_uri, RDFS.comment, Literal("Exported for Ontosphere visualization & OWL 2 DL reasoning")))

    object_types: Dict[str, Any] = getattr(ontology, "object_types", {}) or {}
    link_types: Dict[str, Any] = getattr(ontology, "link_types", {}) or {}
    objects: Dict[str, Any] = getattr(ontology, "objects", {}) or {}
    data_mapping: Dict[str, Any] = getattr(ontology, "data_mapping", {}) or {}
    links = getattr(ontology, "links", None)

    # ---------- Schema 命名图（TBox） ----------
    schema = ds.graph(SCHEMA_G)

    for type_name, info in object_types.items():
        cls = _iri("type", type_name)
        schema.add((cls, RDF.type, OWL.Class))
        schema.add((cls, RDFS.label, Literal(str(type_name))))
        if info.get("description"):
            schema.add((cls, RDFS.comment, Literal(str(info["description"]))))

        pk = info.get("primary_key")
        for prop in info.get("properties", []) or []:
            prop_uri = _iri("prop", type_name, prop)
            schema.add((prop_uri, RDF.type, OWL.DatatypeProperty))
            schema.add((prop_uri, RDFS.domain, cls))
            schema.add((prop_uri, RDFS.label, Literal(str(prop))))
            if pk and prop == pk:
                schema.add((prop_uri, IONTO.isPrimaryKey, Literal(True, datatype=XSD.boolean)))

    for rel_name, info in link_types.items():
        prop = _iri("rel", rel_name)
        schema.add((prop, RDF.type, OWL.ObjectProperty))
        schema.add((prop, RDFS.label, Literal(str(rel_name))))
        src = info.get("source")
        tgt = info.get("target")
        if src:
            schema.add((prop, RDFS.domain, _iri("type", src)))
        if tgt:
            schema.add((prop, RDFS.range, _iri("type", tgt)))
        if info.get("description"):
            schema.add((prop, RDFS.comment, Literal(str(info["description"]))))
        # 保留源/目标字段信息（便于后续反向映射）
        if info.get("source_property"):
            schema.add((prop, IONTO.sourceProperty, Literal(str(info["source_property"]))))
        if info.get("target_property"):
            schema.add((prop, IONTO.targetProperty, Literal(str(info["target_property"]))))

    # ---------- Data 命名图（ABox） ----------
    if include_instances:
        data = ds.graph(DATA_G)

        for type_name, instances in objects.items():
            cls = _iri("type", type_name)
            for pk, obj in (instances or {}).items():
                inst = _iri("inst", type_name, str(pk))
                data.add((inst, RDF.type, cls))
                data.add((inst, RDFS.label, Literal(f"{type_name}:{pk}")))
                for prop, value in (obj or {}).items():
                    if value is None:
                        continue
                    prop_uri = _iri("prop", type_name, prop)
                    data.add((inst, prop_uri, Literal(str(value))))

        # 关系实例（兼容 networkx MultiDiGraph）
        if links is not None and hasattr(links, "edges"):
            for edge in links.edges(data=True, keys=True):
                # networkx 可能返回 (u, v, key, data) 或 (u, v, data)
                if len(edge) == 4:
                    u, v, key, edge_data = edge
                else:
                    u, v, edge_data = edge
                    key = None

                rel_name = (
                    edge_data.get("relation_type")
                    or edge_data.get("label")
                    or (str(key) if key is not None else None)
                )
                if not rel_name:
                    continue

                def _node_to_iri(node_id: Any) -> URIRef:
                    s = str(node_id)
                    if ":" in s:
                        t, p = s.split(":", 1)
                        return _iri("inst", t, p)
                    return _iri("inst", "unknown", s)

                data.add((_node_to_iri(u), _iri("rel", rel_name), _node_to_iri(v)))

    # ---------- Mapping 命名图 ----------
    if include_mapping and data_mapping:
        mapping_g = ds.graph(MAPPING_G)
        for table_name, meta in data_mapping.items():
            table_uri = _iri("mapping", "table", table_name)
            mapping_g.add((table_uri, RDF.type, IONTO.SourceTable))
            mapping_g.add((table_uri, RDFS.label, Literal(str(table_name))))
            try:
                payload = json.dumps(meta, ensure_ascii=False, default=str)
            except Exception:
                payload = str(meta)
            mapping_g.add((table_uri, IONTO.mappingJson, Literal(payload)))

    return ds


def serialize_trig(ds: Dataset) -> str:
    """导出为 TriG（推荐给 Ontosphere，保留命名图）。"""
    return ds.serialize(format="trig")


def serialize_turtle_flat(ds: Dataset) -> str:
    """扁平 Turtle（所有三元组合并，兼容性更好）。"""
    g = Graph()
    for s, p, o, _ctx in ds.quads((None, None, None, None)):
        g.add((s, p, o))
    g.bind("ex", EX)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("ionto", IONTO)
    g.bind("xsd", XSD)
    g.bind("dcterms", DCTERMS)
    return g.serialize(format="turtle")


def serialize_owl_xml(ds: Dataset) -> str:
    """兼容旧版 export_owl 的 RDF/XML。"""
    g = Graph()
    for s, p, o, _ctx in ds.quads((None, None, None, None)):
        g.add((s, p, o))
    return g.serialize(format="xml")


def build_ontosphere_url(
    rdf_url: str,
    base: str = "https://thhanke.github.io/ontosphere/",
) -> str:
    """生成可直接在 Ontosphere 中打开的 URL（需要 rdf_url 公网可访问）。"""
    return f"{base.rstrip('/')}/?rdfUrl={quote(rdf_url, safe='')}"


def export_for_ontosphere(
    ontology: Any,
    *,
    include_instances: bool = True,
    include_mapping: bool = True,
) -> Tuple[str, str]:
    """
    一键导出供 Ontosphere 使用。

    返回:
        (trig_content, suggested_filename)
    """
    ds = export_ontology_to_dataset(
        ontology,
        include_instances=include_instances,
        include_mapping=include_mapping,
    )
    content = serialize_trig(ds)
    filename = f"ionto_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.trig"
    return content, filename
