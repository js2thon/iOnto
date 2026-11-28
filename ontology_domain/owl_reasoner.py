"""OWL reasoning facade for iOnto.

分层策略：
1. 始终可用：基于 V5 Schema + 导出 RDF 的结构/启发式一致性检查
2. 可选：RDFLib + OWL-RL（pip install owlrl）做 RDFS/OWL-RL 封闭
3. 外部：导出 TriG，交给 Ontosphere / Konclude WASM 做完整 OWL 2 DL

本模块不直接依赖浏览器 WASM；完整 DL 推理通过 Ontosphere 完成。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import OntologySchema


@dataclass
class ReasonerFinding:
    severity: str  # error | warning | info
    code: str
    message: str
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReasonerReport:
    engine: str
    consistent: bool
    findings: List[ReasonerFinding] = field(default_factory=list)
    inferred_triple_count: int = 0
    asserted_triple_count: int = 0
    profile: str = "structural"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "consistent": self.consistent,
            "findings": [f.to_dict() for f in self.findings],
            "inferred_triple_count": self.inferred_triple_count,
            "asserted_triple_count": self.asserted_triple_count,
            "profile": self.profile,
            "details": self.details,
        }


def _structural_checks(schema: OntologySchema, ontology: Any = None) -> Tuple[bool, List[ReasonerFinding]]:
    findings: List[ReasonerFinding] = []

    # disjoint-ish name clashes between class and property local names
    type_names = {ot.name for ot in schema.object_types.values()}
    prop_names = {p.name for p in schema.properties.values()}
    clash = type_names & prop_names
    if clash:
        findings.append(
            ReasonerFinding(
                "warning",
                "NAME_CLASH",
                f"类名与属性名冲突：{', '.join(sorted(clash)[:10])}",
            )
        )

    # relationship cycles of length 1 (self-loop) are fine; detect missing inverses soft
    for rel in schema.relationships.values():
        if rel.inverse_relationship_id:
            inv = schema.relationships.get(rel.inverse_relationship_id)
            if inv is None:
                findings.append(
                    ReasonerFinding(
                        "error",
                        "BROKEN_INVERSE",
                        f"关系 {rel.name} 声明的 inverse 不存在",
                        [rel.id],
                    )
                )

    # cardinality sanity already in validate(); double-check required vs nullable props
    for prop in schema.properties.values():
        if prop.required and prop.nullable:
            findings.append(
                ReasonerFinding(
                    "error",
                    "REQUIRED_NULLABLE",
                    f"属性 {prop.name} 同时 required 与 nullable",
                    [prop.id],
                )
            )

    # instance type coverage
    if ontology is not None:
        for tname, objs in (getattr(ontology, "objects", {}) or {}).items():
            if tname not in (getattr(ontology, "object_types", {}) or {}):
                findings.append(
                    ReasonerFinding(
                        "error",
                        "INSTANCE_UNKNOWN_TYPE",
                        f"实例集合引用未知类型 {tname}",
                    )
                )

    consistent = not any(f.severity == "error" for f in findings)
    return consistent, findings


def _try_owlrl(dataset_or_graph: Any) -> Tuple[Optional[int], List[ReasonerFinding], str]:
    """Run OWL-RL expansion if owlrl is installed. Returns (inferred_count, findings, engine)."""
    findings: List[ReasonerFinding] = []
    try:
        import owlrl  # type: ignore
        from rdflib import Graph
    except Exception:
        return None, [
            ReasonerFinding(
                "info",
                "OWLRL_UNAVAILABLE",
                "未安装 owlrl，跳过 OWL-RL 推理。可执行: pip install owlrl",
            )
        ], "none"

    try:
        g = Graph()
        # Dataset quads → flat graph
        if hasattr(dataset_or_graph, "quads"):
            for s, p, o, _c in dataset_or_graph.quads((None, None, None, None)):
                g.add((s, p, o))
        else:
            for t in dataset_or_graph:
                g.add(t)

        before = len(g)
        # Combined Closure (RDFS + OWL-RL)
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)
        after = len(g)
        inferred = max(0, after - before)
        return inferred, findings, "owlrl"
    except Exception as exc:
        findings.append(
            ReasonerFinding("warning", "OWLRL_FAILED", f"OWL-RL 推理失败：{exc}")
        )
        return None, findings, "owlrl-error"


def reason_over_ontology(
    ontology: Any,
    schema: Optional[OntologySchema] = None,
    *,
    include_instances: bool = True,
    try_owlrl: bool = True,
) -> ReasonerReport:
    """对当前本体运行推理检查，返回报告。"""
    findings: List[ReasonerFinding] = []
    engine = "structural"
    profile = "structural"
    inferred = 0
    asserted = 0
    details: Dict[str, Any] = {}

    # structural layer
    if schema is not None:
        ok, struct_findings = _structural_checks(schema, ontology)
        findings.extend(struct_findings)
        consistent = ok
    else:
        consistent = True
        findings.append(
            ReasonerFinding("info", "NO_V5_SCHEMA", "无 V5 Schema，仅基于 RDF 导出检查")
        )

    # RDF export + optional OWL-RL（rdflib / owlrl 为可选依赖）
    try:
        from .rdf_export import export_ontology_to_dataset

        ds = export_ontology_to_dataset(
            ontology,
            include_instances=include_instances,
            include_mapping=False,
        )
        asserted = sum(1 for _ in ds.quads((None, None, None, None)))
        details["asserted_quads"] = asserted

        if try_owlrl:
            inferred_count, rl_findings, rl_engine = _try_owlrl(ds)
            findings.extend(rl_findings)
            if inferred_count is not None:
                inferred = inferred_count
                engine = rl_engine
                profile = "owl-rl"
                details["owlrl_inferred"] = inferred
    except ImportError as exc:
        findings.append(
            ReasonerFinding(
                "info",
                "RDFLIB_UNAVAILABLE",
                f"rdflib 不可用，跳过 RDF/OWL-RL 层：{exc}",
            )
        )
    except Exception as exc:
        findings.append(
            ReasonerFinding("warning", "RDF_EXPORT_FAILED", f"RDF 导出/推理准备失败：{exc}")
        )

    # any structural error flips consistency
    if any(f.severity == "error" for f in findings):
        consistent = False

    details["trig_available"] = True
    return ReasonerReport(
        engine=engine,
        consistent=consistent,
        findings=findings,
        inferred_triple_count=inferred,
        asserted_triple_count=asserted,
        profile=profile,
        details=details,
    )


def export_for_external_dl_reasoner(ontology: Any) -> Tuple[str, str]:
    """导出 TriG，供 Ontosphere / Konclude 等外部 DL 推理器使用。"""
    from .rdf_export import export_ontology_to_dataset, serialize_trig

    ds = export_ontology_to_dataset(ontology, include_instances=True, include_mapping=True)
    return serialize_trig(ds), "application/trig"


def reason_and_enrich_gate(
    schema: Optional[OntologySchema],
    ontology: Any = None,
    *,
    try_owlrl: bool = True,
) -> Dict[str, Any]:
    """供 reasoning_gate / UI 调用的聚合入口。"""
    report = reason_over_ontology(schema=schema, ontology=ontology, try_owlrl=try_owlrl)
    return report.to_dict()
