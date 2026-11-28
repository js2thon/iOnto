"""Release reasoning / quality gate for iOnto V5.

在发布前执行结构化检查，并可选用轻量 RDF 一致性启发式。
完整 OWL 2 DL（Konclude）可后续通过 Ontosphere / WASM 服务接入。

门禁结果写入发布元数据，失败时默认阻止 publish。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .models import OntologySchema


@dataclass
class GateFinding:
    severity: str  # block | error | warning | info
    code: str
    message: str
    target_ref: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GateReport:
    passed: bool
    findings: List[GateFinding] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "findings": [f.to_dict() for f in self.findings],
            "stats": self.stats,
        }


def _check_schema_structure(schema: OntologySchema) -> List[GateFinding]:
    findings: List[GateFinding] = []
    errors = schema.validate()
    for err in errors:
        findings.append(GateFinding("block", "SCHEMA_VALIDATE", err))

    if not schema.object_types:
        findings.append(GateFinding("block", "EMPTY_SCHEMA", "Schema 中没有任何 ObjectType"))

    # dangling property domain
    for pid, prop in schema.properties.items():
        if prop.domain_object_type_id not in schema.object_types:
            findings.append(
                GateFinding(
                    "error",
                    "DANGLING_PROPERTY_DOMAIN",
                    f"Property {prop.name} domain 指向不存在的 ObjectType",
                    pid,
                )
            )

    # relationship endpoints
    for rid, rel in schema.relationships.items():
        if rel.source_object_type_id not in schema.object_types:
            findings.append(
                GateFinding("error", "DANGLING_REL_SOURCE", f"关系 {rel.name} 源类型缺失", rid)
            )
        if rel.target_object_type_id not in schema.object_types:
            findings.append(
                GateFinding("error", "DANGLING_REL_TARGET", f"关系 {rel.name} 目标类型缺失", rid)
            )

    # identity property refs
    for iid, ident in schema.identities.items():
        if ident.object_type_id not in schema.object_types:
            findings.append(
                GateFinding("error", "DANGLING_IDENTITY_TYPE", f"Identity {ident.name} 类型缺失", iid)
            )
        for pref in ident.property_refs:
            if pref not in schema.properties:
                findings.append(
                    GateFinding("error", "DANGLING_IDENTITY_PROP", f"Identity {ident.name} 属性缺失", iid)
                )

    # name uniqueness soft checks
    type_names = [ot.name for ot in schema.object_types.values()]
    if len(type_names) != len(set(type_names)):
        findings.append(GateFinding("warning", "DUP_TYPE_NAME", "存在重复的 ObjectType.name"))

    rel_names = [r.name for r in schema.relationships.values()]
    if len(rel_names) != len(set(rel_names)):
        findings.append(GateFinding("warning", "DUP_REL_NAME", "存在重复的 Relationship.name"))

    # orphan types (no properties)
    for oid, ot in schema.object_types.items():
        if not ot.property_ids:
            findings.append(
                GateFinding("warning", "TYPE_NO_PROPS", f"ObjectType {ot.name} 没有任何属性", oid)
            )
        if not ot.identity_ids:
            findings.append(
                GateFinding("info", "TYPE_NO_IDENTITY", f"ObjectType {ot.name} 未声明 Identity", oid)
            )

    return findings


def _check_legacy_runtime(ontology: Any) -> List[GateFinding]:
    """Optional checks against in-memory legacy Ontology graph."""
    findings: List[GateFinding] = []
    if ontology is None:
        return findings

    # isolated nodes
    links = getattr(ontology, "links", None)
    if links is not None and hasattr(links, "nodes"):
        isolated = [n for n in links.nodes if links.degree(n) == 0]
        if isolated:
            findings.append(
                GateFinding(
                    "warning",
                    "ISOLATED_INSTANCES",
                    f"存在 {len(isolated)} 个孤立实例节点（无入边/出边）",
                )
            )

    # link types without endpoints present
    for name, info in (getattr(ontology, "link_types", {}) or {}).items():
        src, tgt = info.get("source"), info.get("target")
        ots = getattr(ontology, "object_types", {}) or {}
        if src not in ots or tgt not in ots:
            findings.append(
                GateFinding(
                    "error",
                    "LEGACY_REL_ENDPOINT",
                    f"关系 {name} 的源/目标类型在 object_types 中不存在",
                )
            )
    return findings


def run_reasoning_gate(
    schema: Optional[OntologySchema],
    *,
    ontology: Any = None,
    require_identity: bool = False,
) -> GateReport:
    findings: List[GateFinding] = []
    stats: Dict[str, Any] = {}

    if schema is None:
        findings.append(GateFinding("block", "NO_SCHEMA", "没有可用的 V5 Schema，无法发布"))
        return GateReport(passed=False, findings=findings, stats=stats)

    findings.extend(_check_schema_structure(schema))
    findings.extend(_check_legacy_runtime(ontology))

    if require_identity:
        for oid, ot in schema.object_types.items():
            if not ot.identity_ids:
                findings.append(
                    GateFinding(
                        "block",
                        "IDENTITY_REQUIRED",
                        f"ObjectType {ot.name} 缺少 Identity（门禁要求）",
                        oid,
                    )
                )

    stats = {
        "object_types": len(schema.object_types),
        "properties": len(schema.properties),
        "relationships": len(schema.relationships),
        "identities": len(schema.identities),
        "constraints": len(schema.constraints),
        "schema_version": schema.version,
        "finding_counts": {
            "block": sum(1 for f in findings if f.severity == "block"),
            "error": sum(1 for f in findings if f.severity == "error"),
            "warning": sum(1 for f in findings if f.severity == "warning"),
            "info": sum(1 for f in findings if f.severity == "info"),
        },
    }

    blocks = [f for f in findings if f.severity in {"block", "error"}]
    return GateReport(passed=len(blocks) == 0, findings=findings, stats=stats)


def publish_with_gate(
    repo: Any,
    schema: OntologySchema,
    *,
    ontology: Any = None,
    release_id: Optional[str] = None,
    require_identity: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """运行门禁后发布。force=True 时仅记录警告仍发布（不建议生产使用）。"""
    report = run_reasoning_gate(schema, ontology=ontology, require_identity=require_identity)
    if not report.passed and not force:
        return {
            "published": False,
            "gate": report.to_dict(),
            "message": "推理/质量门禁未通过，已阻止发布",
        }

    # ensure schema saved first
    save_info = repo.save_schema(schema)
    release_info = repo.publish_release(schema, release_id=release_id)
    return {
        "published": True,
        "gate": report.to_dict(),
        "save": save_info,
        "release": release_info,
        "forced": bool(force and not report.passed),
        "message": "发布成功" + ("（强制跳过门禁）" if force and not report.passed else ""),
    }
