"""Deterministic ontology query pipeline.

The LLM is limited to semantic interpretation and final wording. All data access,
query execution, validation and evidence assembly are deterministic Python code.
"""
from __future__ import annotations

import hashlib
import json
import operator
import re
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx


PLAN_VERSION = "1.0"
ALLOWED_INTENTS = {"lookup", "search", "count", "neighbors", "path"}
FILTER_OPERATORS = {
    "eq": operator.eq, "ne": operator.ne, "gt": operator.gt,
    "gte": operator.ge, "lt": operator.lt, "lte": operator.le,
}


class QueryPlanError(ValueError):
    """Raised when an LLM-proposed plan is invalid against the ontology."""


def ontology_schema(ontology: Any) -> Dict[str, Any]:
    return {
        "object_types": {
            name: {
                "primary_key": info.get("primary_key"),
                "properties": list(info.get("properties") or []),
                "description": info.get("description", ""),
            }
            for name, info in ontology.object_types.items()
        },
        "relations": {
            name: {
                "source": info.get("source"),
                "target": info.get("target"),
                "description": info.get("description", ""),
            }
            for name, info in ontology.link_types.items()
        },
    }


def _json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^\`\`\`(?:json)?\s*|\s*\`\`\`$", "", text, flags=re.I)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise QueryPlanError("模型返回的查询计划必须是 JSON 对象")
    return data


def interpret_question(client: Any, model: str, question: str, ontology: Any) -> Dict[str, Any]:
    """Ask the LLM for a plan only; it receives schema, never data-access tools."""
    schema = ontology_schema(ontology)
    system = (
        "你是本体查询意图解析器。你只能输出一个 JSON 查询计划，不得回答用户问题，"
        "不得调用工具，不得虚构本体类型、属性或关系。只能使用下列 intent："
        "lookup（按主键取对象）、search（关键词搜索）、count（计数）、"
        "neighbors（查对象关联对象）、path（查两个对象间路径）。"
        "过滤器仅支持 eq/ne/gt/gte/lt/lte/contains。"
        "JSON 字段：intent,target_type,primary_key,keyword,filters,source_type,"
        "source_pk,target_type_to,target_pk,relation_type,direction,limit。"
        "不确定时用 null；不要猜测主键。"
        "\n本体 Schema：\n" + json.dumps(schema, ensure_ascii=False)
    )
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": question}],
        temperature=0,
        max_tokens=700,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or "{}"
    return _validate_plan(_json_object(raw), ontology)


def _validate_plan(raw: Dict[str, Any], ontology: Any) -> Dict[str, Any]:
    intent = str(raw.get("intent") or "").strip().lower()
    if intent not in ALLOWED_INTENTS:
        raise QueryPlanError(f"不支持的查询意图：{intent or '未识别'}")
    plan = dict(raw)
    plan["plan_version"] = PLAN_VERSION
    plan["intent"] = intent
    plan["limit"] = max(1, min(int(raw.get("limit") or 50), 200))
    types = ontology.object_types
    if intent in {"lookup", "search", "count"}:
        t = str(raw.get("target_type") or "")
        if t and t not in types:
            raise QueryPlanError(f"本体中不存在对象类型：{t}")
        if intent == "lookup" and not t:
            raise QueryPlanError("按主键查询必须指定 target_type")
        if intent == "lookup" and raw.get("primary_key") in (None, ""):
            raise QueryPlanError("按主键查询缺少 primary_key")
        if intent == "search" and not str(raw.get("keyword") or "").strip():
            raise QueryPlanError("关键词搜索缺少 keyword")
        plan["target_type"] = t or None
        for item in raw.get("filters") or []:
            if not isinstance(item, dict):
                raise QueryPlanError("filters 中存在非法条件")
            prop = str(item.get("property") or "")
            if t and prop not in types[t].get("properties", []):
                raise QueryPlanError(f"属性 {t}.{prop} 不存在")
            if item.get("operator") not in set(FILTER_OPERATORS) | {"contains"}:
                raise QueryPlanError(f"不支持过滤操作符：{item.get('operator')}")
            if "value" not in item:
                raise QueryPlanError(f"过滤条件 {prop} 缺少 value")
    if intent in {"neighbors", "path"}:
        stype = str(raw.get("source_type") or "")
        spk = raw.get("source_pk")
        if stype not in types or spk in (None, ""):
            raise QueryPlanError("关系查询必须提供有效的 source_type 和 source_pk")
        if intent == "path":
            ttype = str(raw.get("target_type_to") or "")
            tpk = raw.get("target_pk")
            if ttype not in types or tpk in (None, ""):
                raise QueryPlanError("路径查询必须提供有效的 target_type_to 和 target_pk")
        relation = raw.get("relation_type")
        if relation and relation not in ontology.link_types:
            raise QueryPlanError(f"本体中不存在关系类型：{relation}")
        direction = str(raw.get("direction") or "out").lower()
        if direction not in {"out", "in", "both"}:
            raise QueryPlanError("direction 仅支持 out/in/both")
        plan["direction"] = direction
    plan["filters"] = raw.get("filters") or []
    return plan


def _matches(obj: Dict[str, Any], filters: List[Dict[str, Any]]) -> bool:
    for item in filters:
        actual, expected = obj.get(item["property"]), item["value"]
        op = item["operator"]
        if op == "contains":
            ok = str(expected).lower() in str(actual or "").lower()
        else:
            try:
                left, right = actual, expected
                if isinstance(left, (int, float)) and not isinstance(left, bool):
                    right = float(right)
                ok = FILTER_OPERATORS[op](left, right)
            except (TypeError, ValueError):
                ok = str(actual).lower() == str(expected).lower() if op == "eq" else False
        if not ok:
            return False
    return True


def _record(ontology: Any, type_name: str, pk: str) -> Optional[Dict[str, Any]]:
    obj = ontology.objects.get(type_name, {}).get(str(pk))
    if obj is None:
        return None
    return {"type": type_name, "primary_key": str(pk),
            "node_id": f"{type_name}:{pk}", "data": dict(obj)}


def execute_query_plan(ontology: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Execute only validated operations against the ontology's canonical object/edge store."""
    intent = plan["intent"]
    records: List[Dict[str, Any]] = []
    relation_evidence: List[Dict[str, Any]] = []
    if intent == "lookup":
        item = _record(ontology, plan["target_type"], plan["primary_key"])
        records = [item] if item and _matches(item["data"], plan["filters"]) else []
    elif intent in {"search", "count"}:
        types = [plan["target_type"]] if plan.get("target_type") else list(ontology.object_types)
        keyword = str(plan.get("keyword") or "").lower()
        for t in types:
            for pk, obj in ontology.objects.get(t, {}).items():
                if keyword and keyword not in " ".join(str(v).lower() for v in obj.values() if v is not None):
                    continue
                if _matches(obj, plan["filters"]):
                    records.append(_record(ontology, t, pk))
        if intent == "count":
            return {"intent": intent, "count": len(records),
                    "matched_object_ids": [x["node_id"] for x in records],
                    "records": records[:plan["limit"]], "truncated": len(records) > plan["limit"]}
    elif intent == "neighbors":
        source = _record(ontology, plan["source_type"], plan["source_pk"])
        if source is None:
            raise QueryPlanError("源对象不存在")
        node = source["node_id"]
        direction = plan["direction"]
        edges = []
        if direction in {"out", "both"}:
            edges.extend((node, n, d) for _, n, d in ontology.links.out_edges(node, data=True))
        if direction in {"in", "both"}:
            edges.extend((n, node, d) for n, _, d in ontology.links.in_edges(node, data=True))
        for left, right, data in edges:
            rel = str(data.get("relation_type") or "")
            if plan.get("relation_type") and rel != plan["relation_type"]:
                continue
            target_node = right if left == node else left
            target_data = ontology.links.nodes.get(target_node, {})
            t, pk = target_data.get("type"), target_data.get("pk")
            item = _record(ontology, str(t), str(pk)) if t is not None and pk is not None else None
            if item:
                records.append(item)
                relation_evidence.append({"from": left, "to": right, "relation": rel})
    elif intent == "path":
        start = f"{plan['source_type']}:{plan['source_pk']}"
        end = f"{plan['target_type_to']}:{plan['target_pk']}"
        if start not in ontology.links or end not in ontology.links:
            raise QueryPlanError("路径起点或终点不存在")
        graph = ontology.links
        if plan.get("relation_type"):
            graph = nx.DiGraph()
            for u, v, data in ontology.links.edges(data=True):
                if data.get("relation_type") == plan["relation_type"]:
                    graph.add_edge(u, v, **data)
        try:
            path = nx.shortest_path(graph, start, end)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            path = []
        for node in path:
            meta = ontology.links.nodes.get(node, {})
            item = _record(ontology, str(meta.get("type")), str(meta.get("pk")))
            if item:
                records.append(item)
        for left, right in zip(path, path[1:]):
            data = ontology.links.get_edge_data(left, right) or {}
            relation_evidence.append({
                "from": left, "to": right,
                "relations": [str(v.get("relation_type") or k) for k, v in data.items()],
            })
        return {"intent": intent, "found": bool(path), "length": max(0, len(path)-1),
                "path": path, "records": records, "relations": relation_evidence}
    return {"intent": intent, "count": len(records), "records": records[:plan["limit"]],
            "truncated": len(records) > plan["limit"], "relations": relation_evidence}


def validate_result(ontology: Any, plan: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Re-check returned object payloads against canonical storage and compute a stable digest."""
    errors: List[str] = []
    for item in result.get("records", []):
        t, pk = item.get("type"), str(item.get("primary_key"))
        canonical = ontology.objects.get(t, {}).get(pk)
        if canonical is None:
            errors.append(f"对象不存在：{t}:{pk}")
        elif canonical != item.get("data"):
            errors.append(f"对象内容与本体实例存储不一致：{t}:{pk}")
    if plan["intent"] == "count" and result.get("count") != len(result.get("matched_object_ids", [])):
        errors.append("计数值与命中对象 ID 数量不一致")
    if plan["intent"] == "lookup" and len(result.get("records", [])) > 1:
        errors.append("主键查询返回多条记录")
    canonical_json = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return {"valid": not errors, "errors": errors, "result_sha256": digest,
            "checked_records": len(result.get("records", []))}


def build_provenance(ontology: Any, plan: Dict[str, Any], result: Dict[str, Any],
                     validation: Dict[str, Any], version_id: Optional[str] = None) -> Dict[str, Any]:
    evidence = []
    for item in result.get("records", []):
        t = item["type"]
        evidence.append({
            "evidence_id": f"object:{item['node_id']}",
            "object_type": t, "primary_key": item["primary_key"],
            "object_id": item["node_id"], "record": item["data"],
            "source_mapping": ontology.data_mapping.get(t, {}),
        })
    return {
        "version_id": version_id, "plan_version": PLAN_VERSION,
        "query_plan": plan, "evidence": evidence,
        "relation_evidence": result.get("relations", []),
        "validation": validation,
        "result_sha256": validation["result_sha256"],
        "evidence_count": len(evidence),
    }


def generate_answer(client: Any, model: str, question: str,
                    result: Dict[str, Any], provenance: Dict[str, Any]) -> str:
    """LLM may phrase validated evidence; it cannot query or add unsupported facts."""
    payload = {"result": result, "evidence": provenance["evidence"],
               "relations": provenance["relation_evidence"],
               "validation": provenance["validation"]}
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content":
             "你是企业本体问答助手。只根据提供的已验证查询结果和证据回答。"
             "不得推测、补全或引入证据之外的事实。若结果为空，明确说本体数据中未查到。"
             "若校验未通过，不得给出事实性答案，只说明查询校验失败。回答简洁中文。"},
            {"role": "user", "content": "问题：" + question + "\n已验证数据："
             + json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        temperature=0,
        max_tokens=1200,
    )
    return (response.choices[0].message.content or "").strip() or "模型没有返回文本。"


def execute_query_pipeline(client: Any, model: str, question: str, ontology: Any,
                           version_id: Optional[str] = None) -> Dict[str, Any]:
    """Full pipeline: interpret -> plan validation -> execute -> validate -> provenance -> answer."""
    plan = interpret_question(client, model, question, ontology)
    result = execute_query_plan(ontology, plan)
    validation = validate_result(ontology, plan, result)
    provenance = build_provenance(ontology, plan, result, validation, version_id)
    if not validation["valid"]:
        answer = "查询结果未通过确定性校验，已阻止生成事实性答案。"
    else:
        answer = generate_answer(client, model, question, result, provenance)
    trace = [
        {"name": "query_plan", "args": {}, "result": json.dumps(plan, ensure_ascii=False, default=str)},
        {"name": "ontology_execute", "args": plan, "result": json.dumps(result, ensure_ascii=False, default=str)[:12000]},
        {"name": "deterministic_validation", "args": {}, "result": json.dumps(validation, ensure_ascii=False)},
        {"name": "provenance", "args": {}, "result": json.dumps(provenance, ensure_ascii=False, default=str)[:12000]},
    ]
    return {"answer": answer, "plan": plan, "result": result,
            "validation": validation, "provenance": provenance, "trace": trace}
