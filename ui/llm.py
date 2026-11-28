"""LLM client and ontology tool-calling helpers."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from openai import OpenAI

from ontology_domain.legacy_runtime import Ontology, json_safe
def _make_llm_client(api_key: str, base_url: str):
    """统一创建 LLM 客户端：限制单次超时与重试次数，避免界面长时间无响应。"""
    endpoint = base_url.strip().rstrip("/") if base_url else None
    if endpoint and endpoint.endswith("/chat/completions"):
        endpoint = endpoint[:-len("/chat/completions")]
    return OpenAI(
        api_key=api_key,
        base_url=endpoint,
        timeout=90.0,
        max_retries=1,
    )


def ontology_schema_for_llm(ontology: Ontology) -> str:
    """仅序列化 Schema + 计数，不含实例，供 Tool Calling 系统提示使用。"""
    payload = {
        "object_types": {
            name: {
                "primary_key": info["primary_key"],
                "properties": info["properties"],
                "description": info.get("description", ""),
                "count": len(ontology.objects.get(name, {})),
            }
            for name, info in ontology.object_types.items()
        },
        "link_types": ontology.link_types,
        "graph_stats": {
            "nodes": ontology.links.number_of_nodes(),
            "edges": ontology.links.number_of_edges(),
        },
        "node_id_format": "Type:primary_key，例如 Employee:1001",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def execute_ontology_tool(
    ontology: Ontology,
    name: str,
    arguments: Dict[str, Any],
) -> Any:
    """执行本体查询工具，返回可 JSON 序列化的结果。"""
    try:
        if name == "search_objects":
            keyword = str(arguments.get("keyword") or "").strip()
            type_name = arguments.get("type_name")
            limit = int(arguments.get("limit") or 20)
            limit = max(1, min(limit, 50))
            if not keyword:
                return {"error": "keyword 不能为空", "results": []}

            types = (
                [str(type_name)]
                if type_name and str(type_name) in ontology.object_types
                else list(ontology.object_types.keys())
            )
            kw = keyword.lower()
            results = []
            for t in types:
                for pk, obj in ontology.objects.get(t, {}).items():
                    blob = " ".join(
                        str(v).lower() for v in obj.values() if v is not None
                    )
                    # 主键精确优先
                    if str(pk).lower() == kw or kw in blob:
                        results.append({
                            "type": t,
                            "primary_key": pk,
                            "node_id": make_node_id(t, pk),
                            "data": obj,
                        })
                        if len(results) >= limit:
                            return {"count": len(results), "results": results}
            return {"count": len(results), "results": results}

        if name == "get_object":
            type_name = str(arguments.get("type_name") or "")
            pk = str(arguments.get("primary_key") or "")
            if type_name not in ontology.object_types:
                return {
                    "error": f"未知类型 {type_name}",
                    "available_types": list(ontology.object_types.keys()),
                }
            obj = ontology.objects.get(type_name, {}).get(pk)
            if obj is None:
                # 尝试大小写不敏感匹配主键
                found = None
                for k, v in ontology.objects.get(type_name, {}).items():
                    if k.lower() == pk.lower():
                        found = (k, v)
                        break
                if found is None:
                    return {"error": f"未找到 {type_name}:{pk}"}
                pk, obj = found
            return {
                "type": type_name,
                "primary_key": pk,
                "node_id": make_node_id(type_name, pk),
                "data": obj,
            }

        if name == "get_neighbors":
            node_id = arguments.get("node_id")
            if not node_id:
                t = arguments.get("type_name")
                pk = arguments.get("primary_key")
                if t and pk is not None:
                    node_id = make_node_id(str(t), pk)
            node_id = str(node_id or "")
            limit = int(arguments.get("limit") or 30)
            limit = max(1, min(limit, 100))
            if node_id not in ontology.links:
                return {"error": f"节点不存在: {node_id}"}

            neighbors = []
            for pred in ontology.links.predecessors(node_id):
                edge_data = ontology.links.get_edge_data(pred, node_id) or {}
                relations = [
                    str(d.get("relation_type") or k)
                    for k, d in edge_data.items()
                ]
                neighbors.append({
                    "neighbor": pred,
                    "type": ontology.links.nodes[pred].get("type"),
                    "direction": "incoming",
                    "relations": relations,
                })
            for succ in ontology.links.successors(node_id):
                edge_data = ontology.links.get_edge_data(node_id, succ) or {}
                relations = [
                    str(d.get("relation_type") or k)
                    for k, d in edge_data.items()
                ]
                neighbors.append({
                    "neighbor": succ,
                    "type": ontology.links.nodes[succ].get("type"),
                    "direction": "outgoing",
                    "relations": relations,
                })
            return {
                "node_id": node_id,
                "neighbor_count": len(neighbors),
                "neighbors": neighbors[:limit],
            }

        if name == "find_path":
            source = str(arguments.get("source_node") or "")
            target = str(arguments.get("target_node") or "")
            path = find_shortest_path(ontology, source, target)
            if not path:
                return {
                    "found": False,
                    "source": source,
                    "target": target,
                    "message": "未找到路径",
                }
            steps = []
            for left, right in zip(path[:-1], path[1:]):
                edge_data = ontology.links.get_edge_data(left, right) or {}
                if not edge_data:
                    # 无向回退时可能边方向相反
                    edge_data = ontology.links.get_edge_data(right, left) or {}
                relations = [
                    str(d.get("relation_type") or k)
                    for k, d in edge_data.items()
                ]
                steps.append({
                    "from": left,
                    "to": right,
                    "relations": relations,
                })
            return {
                "found": True,
                "length": len(path) - 1,
                "path": path,
                "steps": steps,
            }

        if name == "count_objects":
            type_name = arguments.get("type_name")
            prop = arguments.get("property_name")
            prop_val = arguments.get("property_value")
            if type_name:
                type_name = str(type_name)
                if type_name not in ontology.object_types:
                    return {
                        "error": f"未知类型 {type_name}",
                        "available_types": list(ontology.object_types.keys()),
                    }
                objects = ontology.objects.get(type_name, {})
                if prop is not None and prop_val is not None:
                    pv = str(prop_val).lower()
                    matched = [
                        o for o in objects.values()
                        if str(o.get(str(prop), "")).lower() == pv
                    ]
                    return {
                        "type": type_name,
                        "filter": {str(prop): prop_val},
                        "count": len(matched),
                    }
                return {"type": type_name, "count": len(objects)}
            return {
                "counts": {
                    t: len(ontology.objects.get(t, {}))
                    for t in ontology.object_types
                },
                "total": ontology.links.number_of_nodes(),
            }

        if name == "list_link_types":
            return {
                "link_types": [
                    {"name": n, **info}
                    for n, info in ontology.link_types.items()
                ]
            }

        return {"error": f"未知工具: {name}"}
    except Exception as exc:
        return {"error": f"工具执行失败: {exc}"}


def _tool_result_json(data: Any, max_chars: int = 12000) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[: max_chars - 80] + '..."truncated":true}'
    return text


def _friendly_llm_error(exc: Exception) -> str:
    """将常见 API 错误转成可读提示。"""
    msg = str(exc)
    lower = msg.lower()
    if any(
        k in lower
        for k in (
            "context_length", "maximum context", "too many tokens",
            "token limit", "context window", "payload too large",
            "request too large",
        )
    ):
        return (
            "调用失败：上下文过长。\n"
            "建议使用「工具调用（推荐）」模式，仅按需查询实体，不要传全量对象。"
            f"\n原始错误：{msg}"
        )
    if "timeout" in lower or "timed out" in lower:
        return (
            "调用失败：请求超时。\n"
            "请检查 Base URL / 网络，或改用「工具调用」减少单次上下文。"
            f"\n原始错误：{msg}"
        )
    if any(
        k in lower
        for k in ("tool", "function calling", "tools are not supported",
                  "does not support", "unknown parameter")
    ):
        return (
            "调用失败：当前模型或接口可能不支持 Tool Calling。\n"
            "请在侧边栏切换为「智能检索」降级模式，或更换支持 function calling 的模型。"
            f"\n原始错误：{msg}"
        )
    return f"调用失败：{msg}"


def _system_prompt_for_tools(ontology: Ontology) -> str:
    schema = ontology_schema_for_llm(ontology)
    return (
        "你是企业本体问答助手。你只能依据工具返回的真实数据回答，禁止编造实体或关系。\n"
        "可用工具：search_objects、get_object、get_neighbors、find_path、"
        "count_objects、list_link_types。\n"
        "节点 ID 格式为 Type:primary_key（例如 Employee:1001）。\n"
        "回答流程建议：先 search_objects 或 get_object 定位实体，"
        "再 get_neighbors / find_path 查关系；统计类问题用 count_objects。\n"
        "若工具结果不足，请说明还缺什么，不要猜测。\n"
        "最终用简洁中文回答用户。\n\n"
        f"当前本体 Schema（无实例数据）：\n{schema}"
    )


def _build_llm_messages_legacy(
    question: str,
    ontology: Ontology,
    max_objects: Optional[int],
    context_mode: str = "retrieve",
) -> List[Dict[str, str]]:
    """降级模式：把检索/抽样实例直接塞进 prompt。"""
    context = ontology.to_context_for_llm(
        max_objects=max_objects,
        question=question,
        mode=context_mode,
        max_chars=60000,
    )
    system_prompt = (
        "你是企业本体问答助手。请严格依据给出的本体结构和业务数据回答，"
        "不要编造不存在的实体、属性或关系。若数据不足，请明确说明。"
        "\n\n当前本体数据（检索/抽样，可能非全量）：\n" + context
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]


def ask_llm_with_tools(
    question: str,
    ontology: Ontology,
    api_key: str,
    base_url: str,
    model: str,
    max_rounds: int = 6,
):
    """
    Tool Calling 代理：多轮调用本体查询工具后给出最终回答。
    yield ("status", 文本) / ("token", 文本) / ("error", 文本) /
          ("tool", {"name", "args", "result"})。
    tool 事件用于证据溯源与问题测评（result 截断存储）。
    """
    if not api_key:
        yield ("error", "请先配置 API Key。")
        return
    if OpenAI is None:
        yield ("error", "缺少 openai 依赖，请执行：pip install openai")
        return

    try:
        client = _make_llm_client(api_key, base_url)
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _system_prompt_for_tools(ontology)},
            {"role": "user", "content": question},
        ]

        for round_idx in range(max_rounds):
            yield ("status", f"思考中（第 {round_idx + 1}/{max_rounds} 轮）…")
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=ONTOLOGY_TOOLS,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=1500,
            )
            choice = response.choices[0]
            message = choice.message
            tool_calls = getattr(message, "tool_calls", None) or []

            # 无工具调用 → 最终回答
            if not tool_calls:
                content = (message.content or "").strip()
                if content:
                    yield ("token", content)
                else:
                    yield ("token", "模型没有返回文本。")
                return

            # 记录 assistant 工具调用消息
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in tool_calls:
                fn_name = tc.function.name
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args) if raw_args else {}
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                    result = {"error": f"工具参数不是合法 JSON: {raw_args[:200]}"}
                else:
                    yield (
                        "status",
                        f"调用工具 `{fn_name}`"
                        + (f"（{json.dumps(args, ensure_ascii=False)[:80]}）" if args else ""),
                    )
                    result = execute_ontology_tool(ontology, fn_name, args)

                # 结构化 tool 事件：证据溯源与问题测评消费
                yield (
                    "tool",
                    {
                        "name": fn_name,
                        "args": args,
                        "result": _tool_result_json(result, max_chars=2000),
                    },
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _tool_result_json(result),
                })

        # 超出轮次：强制再要一次纯文本答案
        yield ("status", "工具轮次已用尽，正在汇总答案…")
        messages.append({
            "role": "user",
            "content": "请根据已获取的工具结果给出最终中文回答，不要再调用工具。",
        })
        final = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=1500,
        )
        content = (final.choices[0].message.content or "").strip()
        yield ("token", content or "未能在限定轮次内完成回答，请缩小问题范围后重试。")
    except Exception as exc:
        yield ("error", _friendly_llm_error(exc))


def ask_llm(
    question: str,
    ontology: Ontology,
    api_key: str,
    base_url: str,
    model: str,
    max_objects: Optional[int] = None,
    context_mode: str = "retrieve",
) -> str:
    if not api_key:
        return "请先配置 API Key。"
    if OpenAI is None:
        return "缺少 openai 依赖，请执行：pip install openai"

    try:
        client = _make_llm_client(api_key, base_url)
        response = client.chat.completions.create(
            model=model,
            messages=_build_llm_messages_legacy(
                question, ontology, max_objects, context_mode=context_mode
            ),
            temperature=0.2,
            max_tokens=1500,
        )
        return response.choices[0].message.content or "模型没有返回文本。"
    except Exception as exc:
        return _friendly_llm_error(exc)


def ask_llm_stream(
    question: str,
    ontology: Ontology,
    api_key: str,
    base_url: str,
    model: str,
    max_objects: Optional[int] = None,
    context_mode: str = "retrieve",
):
    """降级模式流式回答（无 Tool Calling）。"""
    if not api_key:
        yield "请先配置 API Key。"
        return
    if OpenAI is None:
        yield "缺少 openai 依赖，请执行：pip install openai"
        return

    try:
        client = _make_llm_client(api_key, base_url)
        stream = client.chat.completions.create(
            model=model,
            messages=_build_llm_messages_legacy(
                question, ontology, max_objects, context_mode=context_mode
            ),
            temperature=0.2,
            max_tokens=1500,
            stream=True,
        )
        got_text = False
        for chunk in stream:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content or ""
            if text:
                got_text = True
                yield text
        if not got_text:
            yield "模型没有返回文本。"
    except Exception as exc:
        yield _friendly_llm_error(exc)


# ---------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------
from ui.data_io import (
    build_pg_engine,
    list_pg_schemas,
    list_pg_tables,
    load_pg_tables,
    read_uploaded_file,
    test_pg_connection,
)

