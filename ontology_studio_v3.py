#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iOnto V5.0
企业本体建模 Pipeline 工作台

功能：
- Pipeline 五大模块：数据源 → 本体建模 → 智能问答 → 问题测评 → 版本管理
- CSV / Excel 多文件导入
- PostgreSQL 数据源：连接配置、Schema 选表、按表加载后自动本体建模
- 主键选择与自动推断
- 自动识别外键关系 + 手动配置关系
- Schema 图谱：Class / Property / Object Property
- 实例图谱：类型过滤、节点数量控制、实体搜索
- 一跳邻居展开与最短路径查询
- 关系质量检查：悬空外键、孤立节点、重复关系等
- 对象 Action：创建、更新、删除
- LLM 问答（可选，支持 OpenAI 兼容接口）
  · LLM 仅负责问题理解与最终表达，不直接调用数据工具
  · 结构化查询计划 → 本体确定性执行 → 结果校验 → 证据链
  · 查询计划、执行结果、校验报告与证据链均可追溯
- 问题测评：CQ 问题集管理、自动测评、确定性比对、失败归因
- 版本管理：版本快照、差异比较、生命周期、回滚
- JSON / OWL 导出

安装：
pip install streamlit pandas networkx pyvis openai rdflib openpyxl sqlalchemy psycopg2-binary

运行：
streamlit run ontology_studio_v3.py
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import pandas as pd
import streamlit as st
from ontology_query_pipeline import execute_query_pipeline
from ontology_domain.mapping_workbench_ui import render_mapping_workbench
from pyvis.network import Network
from rdflib import Graph, Literal, Namespace, OWL, RDF, RDFS, URIRef

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# ---------------------------------------------------------------------
# 页面配置与主题
# ---------------------------------------------------------------------
st.set_page_config(
    page_title="iOnto V5.1",
    page_icon="🕸️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
.stApp { background-color: #f5f5f5; color: #1f1f1f; }
h1, h2, h3, h4 { color: #1a1a1a !important; }
p, li, span, label, .stMarkdown { color: #3d3d3d; }
section[data-testid="stSidebar"] {
    background-color: #ffffff; border-right: 1px solid #e0e0e0;
}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p { color: #2e2e2e !important; }
.stTextInput input, .stNumberInput input, .stTextArea textarea {
    background-color: #ffffff; color: #1f1f1f !important;
    border: 1px solid #d6d6d6; border-radius: 6px;
}
.stButton button, [data-testid="stBaseButton-secondary"],
[data-testid="stDownloadButton"] button {
    background: #ffffff !important; color: #1f1f1f !important;
    border: 1px solid #d6d6d6 !important; border-radius: 6px !important;
    font-weight: 600;
}
.stButton button:hover, [data-testid="stBaseButton-secondary"]:hover,
[data-testid="stDownloadButton"] button:hover {
    background: #ececec !important; border-color: #bfbfbf !important;
}
.stButton button[kind="primary"], [data-testid="stBaseButton-primary"] {
    background: #f0f0f0 !important; color: #1f1f1f !important;
    border: 1px solid #bfbfbf !important; border-radius: 6px !important;
    font-weight: 600;
}
.stButton button[kind="primary"]:hover,
[data-testid="stBaseButton-primary"]:hover {
    background: #e0e0e0 !important; border-color: #a8a8a8 !important;
}
.stTabs [data-baseweb="tab"] {
    background-color: #ececec !important; color: #525252 !important;
    border-radius: 6px 6px 0 0;
}
.stTabs [data-baseweb="tab"]:hover {
    background-color: #e0e0e0 !important; color: #3d3d3d !important;
}
.stTabs [data-baseweb="tab-highlight"] {
    background-color: #4d4d4d !important;
}
.stTabs [data-baseweb="tab-border"] {
    background-color: #4d4d4d !important;
}
.stTabs [aria-selected="true"] {
    background-color: #2b2b2b !important; color: #ffffff !important;
}
footer { visibility: hidden; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------
# Legacy 运行时（结构真相源为 V5，见 ontology_domain.runtime_bridge）
# ---------------------------------------------------------------------
from ontology_domain.legacy_runtime import (
    AutoOntologyBuilder,
    Ontology,
    json_safe,
    make_node_id,
    safe_records,
)


# 图谱可视化 / 关系分析（模块化）
# ---------------------------------------------------------------------
from ui.graphs import (
    TYPE_COLORS,
    create_business_object_graph,
    create_instance_graph,
    create_schema_graph,
    font_color_for,
    pyvis_html,
)
from ui.analysis import find_shortest_path, quality_report


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

def load_llm_config() -> Dict[str, str]:
    """从本地 llm_config.json 读取已保存的 LLM 配置。"""
    try:
        with open(LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "api_key": str(data.get("api_key", "")),
            "base_url": str(data.get("base_url", "")),
            "model_name": str(data.get("model_name", "gpt-4o-mini")),
        }
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return {"api_key": "", "base_url": "", "model_name": "gpt-4o-mini"}


def save_llm_config(api_key: str, base_url: str, model_name: str) -> None:
    """将 LLM 配置保存到本地 llm_config.json。"""
    directory = os.path.dirname(LLM_CONFIG_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".llm-config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"api_key": api_key, "base_url": base_url, "model_name": model_name},
                f, ensure_ascii=False, indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates owner-only permissions (0600 on POSIX); keep the API
        # key out of partially written files and atomically replace the config.
        os.replace(tmp_path, LLM_CONFIG_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------
# 工作区持久化（版本快照 / 问题集 / 测评运行）
# ---------------------------------------------------------------------
WORKSPACE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ontology_workspace"
)
VERSIONS_DIR = os.path.join(WORKSPACE_DIR, "versions")
QUESTION_SETS_DIR = os.path.join(WORKSPACE_DIR, "question_sets")
EVAL_RUNS_DIR = os.path.join(WORKSPACE_DIR, "eval_runs")
CURRENT_POINTER_FILE = os.path.join(WORKSPACE_DIR, "current.json")

VERSION_STATUS_FLOW = ["draft", "validated", "released", "deprecated"]


def _ensure_workspace() -> None:
    """确保工作区目录存在（幂等）。"""
    for path in (WORKSPACE_DIR, VERSIONS_DIR, QUESTION_SETS_DIR, EVAL_RUNS_DIR):
        os.makedirs(path, exist_ok=True)


def _atomic_write_json(path: str, data: Any, compact: bool = True) -> None:
    """原子写 JSON：先写临时文件再 os.replace，显式 UTF-8（Windows 默认 GBK）。"""
    _ensure_workspace()
    text = json.dumps(
        data, ensure_ascii=False,
        separators=(",", ":") if compact else None,
        default=str,
    )
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".iOnto-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _read_json(path: str) -> Optional[Any]:
    """读 JSON 文件；不存在或损坏时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _ontology_hash(ontology: Ontology) -> str:
    """本体内容 hash（不含时间戳），用于 dirty 检测。"""
    payload = json.dumps(
        ontology.snapshot_dict(), ensure_ascii=False,
        separators=(",", ":"), default=str, sort_keys=True,
    )
    import hashlib
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _verify_roundtrip(ontology: Ontology) -> Optional[str]:
    """round-trip 自检：export → from_dict → export，比较完整快照内容。

    返回错误描述；None 表示通过。save/load 前后均执行。
    """
    try:
        data = ontology.snapshot_dict()
        rebuilt = Ontology.from_dict(json.loads(json.dumps(data, default=str)))
        rebuilt_data = rebuilt.snapshot_dict()
        if data != rebuilt_data:
            return "快照 round-trip 后存在内容差异（Schema / 实例 / 关系 / 映射 / Action）"
        return None
    except Exception as exc:
        return f"round-trip 自检异常：{exc}"


# ---------------------------------------------------------------------
# 版本管理
# ---------------------------------------------------------------------
def _version_path(version_id: str) -> str:
    version_id = str(version_id or "")
    if (
        not version_id
        or version_id in {".", ".."}
        or os.path.basename(version_id) != version_id
        or any(sep in version_id for sep in ("/", chr(92)))
    ):
        raise ValueError("非法版本 ID")
    return os.path.join(VERSIONS_DIR, f"{version_id}.json")


def _new_version_id() -> str:
    """生成版本 ID：v{YYYYMMDD_HHMM}_{序号}，同分钟内递增。"""
    prefix = f"v{datetime.now().strftime('%Y%m%d_%H%M')}"
    existing = {v["id"] for v in list_versions()}
    seq = 1
    while f"{prefix}_{seq:02d}" in existing:
        seq += 1
    return f"{prefix}_{seq:02d}"


def list_versions() -> List[Dict[str, Any]]:
    """列出全部版本（按 ID 倒序 = 最新在前）。"""
    _ensure_workspace()
    versions: List[Dict[str, Any]] = []
    for filename in os.listdir(VERSIONS_DIR):
        if not filename.endswith(".json"):
            continue
        data = _read_json(os.path.join(VERSIONS_DIR, filename))
        if not isinstance(data, dict):
            continue
        meta = data.get("meta") or {}
        versions.append({
            "id": str(data.get("id") or filename[:-5]),
            "created_at": str(meta.get("created_at", "")),
            "status": str(meta.get("status", "draft")),
            "note": str(meta.get("note", "")),
            "stats": data.get("stats") or {},
        })
    versions.sort(key=lambda v: v["id"], reverse=True)
    return versions


def save_version(
    ontology: Ontology, note: str = "", status: str = "draft"
) -> Tuple[str, Optional[str]]:
    """保存当前本体为版本快照；返回 (版本 ID, 自检错误)。"""
    _ensure_workspace()
    if status not in VERSION_STATUS_FLOW:
        return "", f"非法版本状态：{status}"
    error = _verify_roundtrip(ontology)
    if error:
        return "", error

    version_id = _new_version_id()
    snapshot = ontology.snapshot_dict()
    snapshot["id"] = version_id
    snapshot["meta"] = {
        "created_at": datetime.now().isoformat(),
        "status": status,
        "note": note,
        "ontology_hash": _ontology_hash(ontology),
    }
    snapshot["stats"] = {
        "object_types": len(ontology.object_types),
        "objects": ontology.links.number_of_nodes(),
        "link_types": len(ontology.link_types),
        "links": ontology.links.number_of_edges(),
    }
    _atomic_write_json(_version_path(version_id), snapshot, compact=True)
    _update_current_pointer(version_id, _ontology_hash(ontology))
    return version_id, None


def load_version(version_id: str) -> Tuple[Optional[Ontology], Optional[str]]:
    """加载版本快照为 Ontology；返回 (本体, 自检错误)。"""
    data = _read_json(_version_path(version_id))
    if not isinstance(data, dict):
        return None, f"版本 {version_id} 不存在或已损坏"
    try:
        ontology = Ontology.from_dict(data)
    except Exception as exc:
        return None, f"版本 {version_id} 重建失败：{exc}"
    error = _verify_roundtrip(ontology)
    if error:
        return None, f"版本 {version_id} 自检失败：{error}"
    return ontology, None


def update_version_status(version_id: str, new_status: str) -> bool:
    """更新版本生命周期状态（draft → validated → released → deprecated）。"""
    path = _version_path(version_id)
    data = _read_json(path)
    if not isinstance(data, dict):
        return False
    if new_status not in VERSION_STATUS_FLOW:
        return False
    meta = data.setdefault("meta", {})
    old_status = str(meta.get("status", "draft"))
    allowed = {
        "draft": {"validated", "deprecated"},
        "validated": {"released", "deprecated"},
        "released": {"deprecated"},
        "deprecated": set(),
    }
    if new_status not in allowed.get(old_status, set()):
        return False
    meta["status"] = new_status
    _atomic_write_json(path, data, compact=True)
    return True


def _update_current_pointer(version_id: str, ontology_hash: str) -> None:
    _atomic_write_json(CURRENT_POINTER_FILE, {
        "version_id": version_id,
        "ontology_hash": ontology_hash,
        "updated_at": datetime.now().isoformat(),
    })


def read_current_pointer() -> Optional[Dict[str, Any]]:
    return _read_json(CURRENT_POINTER_FILE)


def diff_versions(version_id_a: str, version_id_b: str) -> Dict[str, Any]:
    """比较两个版本：Schema 层（类型/关系/映射）+ 实例计数层 + PK 级增删。"""
    data_a = _read_json(_version_path(version_id_a)) or {}
    data_b = _read_json(_version_path(version_id_b)) or {}

    def _schema_diff(
        key: str, fields: List[str]
    ) -> List[Dict[str, Any]]:
        items_a = data_a.get(key) or {}
        items_b = data_b.get(key) or {}
        rows: List[Dict[str, Any]] = []
        for name in sorted(set(items_a) | set(items_b)):
            info_a, info_b = items_a.get(name), items_b.get(name)
            if info_a is None:
                rows.append({"对象": name, "变更": "新增", "V1": "—", "V2": "存在"})
            elif info_b is None:
                rows.append({"对象": name, "变更": "删除", "V1": "存在", "V2": "—"})
            else:
                for field in fields:
                    va, vb = info_a.get(field), info_b.get(field)
                    if va != vb:
                        rows.append({
                            "对象": name,
                            "变更": f"修改 {field}",
                            "V1": str(va),
                            "V2": str(vb),
                        })
        return rows

    # 实例计数层：每类型对象数 / 每关系边数增量
    def _count_diff() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        objects_a = data_a.get("objects") or {}
        objects_b = data_b.get("objects") or {}
        object_rows = []
        pk_added, pk_removed = [], []
        total_objects = sum(len(v) for v in objects_b.values())
        for type_name in sorted(set(objects_a) | set(objects_b)):
            keys_a = set(objects_a.get(type_name, {}))
            keys_b = set(objects_b.get(type_name, {}))
            if len(keys_a) != len(keys_b) or keys_a != keys_b:
                object_rows.append({
                    "对象": type_name,
                    "变更": "实例变化",
                    "V1": f"{len(keys_a)} 个",
                    "V2": f"{len(keys_b)} 个",
                })
                if total_objects <= 2000:
                    pk_added.extend(
                        f"{type_name}:{k}" for k in sorted(keys_b - keys_a)
                    )
                    pk_removed.extend(
                        f"{type_name}:{k}" for k in sorted(keys_a - keys_b)
                    )
        links_a = data_a.get("links") or []
        links_b = data_b.get("links") or []
        edge_counts_a: Dict[str, int] = {}
        edge_counts_b: Dict[str, int] = {}
        for edge in links_a:
            key = str(edge.get("key") or edge.get("type") or "")
            edge_counts_a[key] = edge_counts_a.get(key, 0) + 1
        for edge in links_b:
            key = str(edge.get("key") or edge.get("type") or "")
            edge_counts_b[key] = edge_counts_b.get(key, 0) + 1
        link_rows = []
        for name in sorted(set(edge_counts_a) | set(edge_counts_b)):
            ca = edge_counts_a.get(name, 0)
            cb = edge_counts_b.get(name, 0)
            if ca != cb:
                link_rows.append({
                    "对象": name, "变更": "边数变化",
                    "V1": str(ca), "V2": str(cb),
                })
        return object_rows, link_rows, pk_added, pk_removed

    object_rows, link_rows, pk_added, pk_removed = _count_diff()
    return {
        "object_types": _schema_diff(
            "object_types", ["primary_key", "properties", "description"]
        ),
        "link_types": _schema_diff(
            "link_types",
            ["source", "target", "source_property", "target_property", "description"],
        ),
        "data_mapping": _schema_diff(
            "data_mapping", ["primary_key", "columns", "row_count"]
        ),
        "instance_counts": object_rows + link_rows,
        "pk_added": pk_added[:100],
        "pk_removed": pk_removed[:100],
        "pk_truncated": (
            len(pk_added) + len(pk_removed) > 100
        ),
    }


# ---------------------------------------------------------------------
# 问题集管理（CQ：Competency Question）
# ---------------------------------------------------------------------
QUESTION_CATEGORIES = ["basic", "relation", "aggregation", "complex", "boundary"]
DEFAULT_QUESTION_SET_FILE = os.path.join(QUESTION_SETS_DIR, "default.json")


def load_question_set() -> List[Dict[str, Any]]:
    """加载默认问题集；不存在时返回空列表。"""
    data = _read_json(DEFAULT_QUESTION_SET_FILE)
    if isinstance(data, dict):
        return list(data.get("questions") or [])
    return []


def save_question_set(questions: List[Dict[str, Any]]) -> None:
    """保存问题集；为缺失 question_id 的条目自动分配（Q001 递增）。"""
    existing_ids = {
        q.get("question_id") for q in questions if q.get("question_id")
    }
    max_num = 0
    for qid in existing_ids:
        match = re.match(r"^Q(\d+)$", str(qid))
        if match:
            max_num = max(max_num, int(match.group(1)))
    for question in questions:
        if not question.get("question_id"):
            max_num += 1
            question["question_id"] = f"Q{max_num:03d}"
    _atomic_write_json(DEFAULT_QUESTION_SET_FILE, {
        "questions": questions,
        "updated_at": datetime.now().isoformat(),
    })


def suggest_questions(ontology: Ontology, max_questions: int = 10) -> List[Dict[str, Any]]:
    """从本体确定性生成候选问题（期望答案由本体计算，零成本 ground truth）。

    模板：计数题（count_objects）、属性题（get_object）、邻居题（get_neighbors）。
    标记 provenance=auto 供人工复核。
    """
    candidates: List[Dict[str, Any]] = []

    # 1) 计数题：每类型一题
    for type_name in ontology.object_types:
        count = len(ontology.objects.get(type_name, {}))
        if count == 0:
            continue
        candidates.append({
            "question": f"本体中一共有多少个 {type_name}？",
            "category": "aggregation",
            "expected_answer": str(count),
            "expected_entities": [type_name],
            "expected_relations": [],
            "difficulty": "easy",
            "provenance": "auto",
        })

    # 2) 属性题：每类型取第一个对象
    for type_name, objects in ontology.objects.items():
        if not objects:
            continue
        pk, obj = next(iter(objects.items()))
        pk_field = ontology.object_types[type_name]["primary_key"]
        # 找一个非主键属性作为期望
        prop = next(
            (p for p in ontology.object_types[type_name]["properties"]
             if p != pk_field and obj.get(p) is not None),
            None,
        )
        if prop is None:
            continue
        candidates.append({
            "question": f"{type_name} {pk} 的 {prop} 是什么？",
            "category": "basic",
            "expected_answer": str(obj[prop]),
            "expected_entities": [f"{type_name}:{pk}"],
            "expected_relations": [],
            "difficulty": "easy",
            "provenance": "auto",
        })

    # 3) 邻居题：取有边的节点
    for node_id in list(ontology.links.nodes):
        if len(candidates) >= max_questions:
            break
        neighbors = list(ontology.links.predecessors(node_id))
        neighbors.extend(ontology.links.successors(node_id))
        if not neighbors:
            continue
        candidates.append({
            "question": f"{node_id} 直接关联的实体有哪些？",
            "category": "relation",
            "expected_answer": ", ".join(sorted(neighbors)[:10]),
            "expected_entities": [node_id] + sorted(neighbors)[:5],
            "expected_relations": [],
            "difficulty": "medium",
            "provenance": "auto",
        })

    return candidates[:max_questions]


# ---------------------------------------------------------------------
# 问题测评：确定性比对 + 失败归因 + 运行记录
# ---------------------------------------------------------------------
def _normalize_text(text: str) -> str:
    """答案文本归一化：小写、全角转半角、去标点/空白/markdown 符号。"""
    text = str(text or "").lower()
    # 全角转半角
    text = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in text
    )
    text = re.sub(r"[*_`#>\[\](){}|~]", " ", text)
    text = re.sub(r"[，。、；：？！,.;:?!\"'\s\-—–]", " ", text)
    return " ".join(text.split())


def _extract_numbers(text: str) -> List[float]:
    """抽取答案中的数值（处理千分位、百分比、单位）。"""
    text = str(text or "")
    # 去掉千分位逗号
    cleaned = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    numbers = []
    for match in re.finditer(r"-?\d+(?:\.\d+)?", cleaned):
        try:
            numbers.append(float(match.group()))
        except ValueError:
            continue
    return numbers


def compare_answer(
    question: Dict[str, Any],
    answer: str,
    trace: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """确定性比对：主指标为证据召回（expected 出现在工具结果中），辅以文本比对。

    返回 {"passed", "evidence_covered", "text_matched", "numbers_matched"}。
    """
    expected_answer = str(question.get("expected_answer") or "")
    expected_entities = [
        str(e) for e in (question.get("expected_entities") or [])
    ]
    expected_relations = [
        str(r) for r in (question.get("expected_relations") or [])
    ]

    # 主指标：证据召回——expected_entities/relations 出现在工具返回结果中
    tool_blob = " ".join(
        str(item.get("result") or "") for item in trace
    )
    evidence_covered = bool(trace) and all(
        expected in tool_blob for expected in expected_entities + expected_relations
    ) if (expected_entities or expected_relations) else bool(trace)

    # 辅助：文本归一化包含比对
    norm_answer = _normalize_text(answer)
    norm_expected = _normalize_text(expected_answer)
    text_matched = bool(norm_expected) and norm_expected in norm_answer

    # 数值比对：期望与答案中的数字集合按容差比较
    numbers_matched = None
    expected_numbers = _extract_numbers(expected_answer)
    if expected_numbers:
        answer_numbers = _extract_numbers(answer)
        numbers_matched = len(answer_numbers) > 0 and all(
            any(abs(a - e) <= max(0.01, abs(e) * 0.001) for a in answer_numbers)
            for e in expected_numbers
        )

    passed = bool(
        (numbers_matched is True)
        or (text_matched and evidence_covered)
        or (text_matched and not (expected_entities or expected_relations))
    )
    return {
        "passed": passed,
        "evidence_covered": evidence_covered,
        "text_matched": text_matched,
        "numbers_matched": numbers_matched,
    }


def attribute_failure(
    question: Dict[str, Any],
    answer: str,
    trace: List[Dict[str, Any]],
    comparison: Dict[str, Any],
    error_text: str,
    ontology: Ontology,
) -> str:
    """失败归因：有序决策树，一条结果只归一个码（可复现）。

    E00 未配置 LLM / E01 API 错误 / E02 超时 / E04 无查询证据阶段 /
    E05 工具结果未覆盖期望 / E06 覆盖但答案缺失 / E07 数值不匹配 /
    E08 文本不匹配 / E09 问题集漂移（期望实体不存在）/ E10 轮次耗尽
    """
    if not answer and not trace and not error_text:
        return "E00"
    if error_text:
        if "超时" in error_text or "timeout" in error_text.lower():
            return "E02"
        return "E01"
    # 问题集漂移：期望实体在当前版本不存在（不计模型失败率）
    for expected in question.get("expected_entities") or []:
        expected = str(expected)
        if ":" in expected:
            type_name, pk = expected.split(":", 1)
            if type_name not in ontology.objects or pk not in ontology.objects.get(type_name, {}):
                return "E09"
        elif expected not in ontology.object_types:
            return "E09"
    if not trace:
        return "E04"
    if not comparison.get("evidence_covered"):
        return "E05"
    if not answer.strip():
        return "E06"
    if comparison.get("numbers_matched") is False:
        return "E07"
    if not comparison.get("text_matched"):
        return "E08"
    return "E10"


def summarize_results(run: Dict[str, Any]) -> Dict[str, Any]:
    """汇总测评指标：答案准确率 / 执行成功率 / 证据覆盖率。"""
    results = run.get("results") or []
    total = len(results)
    if total == 0:
        return {"total": 0, "accuracy": 0.0, "execution_rate": 0.0,
                "evidence_rate": 0.0}
    passed = sum(1 for r in results if r.get("passed"))
    executed = sum(1 for r in results if r.get("error") is None)
    evidenced = sum(1 for r in results if r.get("evidence_covered"))
    return {
        "total": total,
        "accuracy": round(passed / total, 4),
        "execution_rate": round(executed / total, 4),
        "evidence_rate": round(evidenced / total, 4),
    }


def list_eval_runs() -> List[Dict[str, Any]]:
    """列出全部测评运行（按 run_id 倒序）。"""
    _ensure_workspace()
    runs: List[Dict[str, Any]] = []
    for filename in os.listdir(EVAL_RUNS_DIR):
        if not filename.endswith(".json"):
            continue
        data = _read_json(os.path.join(EVAL_RUNS_DIR, filename))
        if not isinstance(data, dict):
            continue
        runs.append({
            "run_id": str(data.get("run_id") or filename[:-5]),
            "version_id": str(data.get("version_id") or ""),
            "status": str(data.get("status") or "completed"),
            "started_at": str(data.get("started_at") or ""),
            "config": data.get("config") or {},
            "metrics": data.get("metrics") or {},
            "result_count": len(data.get("results") or []),
        })
    runs.sort(key=lambda r: r["run_id"], reverse=True)
    return runs


def load_eval_run(run_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(os.path.join(EVAL_RUNS_DIR, f"{run_id}.json"))


def save_eval_run(run: Dict[str, Any]) -> None:
    """保存测评运行（逐题追加时也整体落盘，支持断点续跑）。"""
    run["metrics"] = summarize_results(run)
    _atomic_write_json(
        os.path.join(EVAL_RUNS_DIR, f"{run['run_id']}.json"), run, compact=True
    )


# ---------------------------------------------------------------------
# Streamlit 主界面（st.navigation 多页 + 页面函数）
# ---------------------------------------------------------------------
st.title("🕸️ iOnto V5.1")
st.caption(
    "企业本体建模 Pipeline 工作台 · 数据源 → 本体建模 → 映射工作台 → 智能问答 → "
    "问题测评 → 版本管理 · CSV/Excel · Schema/实例图谱 · LLM 问答 · "
    "证据溯源 · 确定性测评 · 版本快照/回滚"
)

# ---- 全局 session_state（V5 Schema 为结构真相源，见 ui.session） ----
from ui.session import (
    after_structural_edit,
    ensure_canonical_schema,
    get_v5_schema,
    init_session,
    set_v5_schema,
    sync_legacy_from_v5,
)

init_session()

# ---- 刷新恢复：session 为空且 current.json 存在时自动重建本体 ----
if st.session_state.ontology_v2 is None:
    pointer = read_current_pointer()
    if pointer and pointer.get("version_id"):
        ontology_restored, restore_error = load_version(
            str(pointer["version_id"])
        )
        if ontology_restored is not None:
            st.session_state.ontology_v2 = ontology_restored
            st.session_state.restored_version_id = str(pointer["version_id"])
        elif restore_error:
            st.warning(f"自动恢复版本失败：{restore_error}")


# ---- 侧边栏：LLM 配置 + 问答模式（全局共享） ----
with st.sidebar:
    st.header("⚙️ LLM 配置（可选）")
    saved_llm_config = load_llm_config()
    api_key = st.text_input(
        "API Key", value=saved_llm_config["api_key"], type="password"
    )
    base_url = st.text_input(
        "Base URL",
        value=saved_llm_config["base_url"],
        placeholder="https://api.openai.com/v1",
    )
    model_name = st.text_input(
        "模型名称", value=saved_llm_config["model_name"]
    )
    if st.button("保存 LLM 配置"):
        try:
            save_llm_config(api_key, base_url, model_name)
            st.success("LLM 配置已保存，下次启动自动加载。")
        except OSError as exc:
            st.error(f"保存失败：{exc}")

    st.header("💬 问答模式")
    st.info(
        "固定使用确定性本体查询 Pipeline：LLM 解析问题，"
        "查询规划器生成计划，本体引擎执行，校验器验证，溯源引擎生成证据。"
    )

    st.divider()
    if st.button("🗑️ 重置当前本体"):
        st.session_state.ontology_v2 = None
        st.session_state.raw_data_v2 = {}
        st.session_state.messages_v2 = []
        st.session_state.pk_config = {}
        st.session_state.manual_links_config = []
        st.session_state.source_kinds_v2 = {}
        for state_key in (
            "v5_mapping_schema", "v5_mapping_source_fingerprint",
            "v5_mapping_source_changed", "v5_mapping_preview",
            "v5_mapping_last_run", "v5_mapping_last_release",
        ):
            st.session_state.pop(state_key, None)
        st.rerun()


# ---------------------------------------------------------------------
# 页面 1–3（模块化）
# ---------------------------------------------------------------------
from ui.pages.overview import render_overview
from ui.pages.data_source import render_data_source
from ui.pages.modeling import render_modeling


# 页面 4：智能问答（含证据溯源）
# ---------------------------------------------------------------------
def render_qa() -> None:
    ontology = st.session_state.ontology_v2

    if ontology is None:
        st.info("请先生成本体（「数据源」→「本体建模」），再进行智能问答。")
        return

    st.subheader("基于本体的智能问答")
    st.caption(
        "LLM 负责语义理解和最终表达；查询规划器生成结构化计划，"
        "本体执行引擎确定性查询，校验器验证结果，溯源引擎生成证据链。"
    )

    # 当前版本标签
    pointer = read_current_pointer()
    version_label = (
        f"（版本 {pointer['version_id']}）" if pointer and pointer.get("version_id") else ""
    )
    if version_label:
        st.caption(f"当前绑定{version_label}")

    # 评测运行中禁用问答输入，防止交错
    eval_state = st.session_state.get("eval_state")
    chat_disabled = bool(eval_state and eval_state.get("running"))

    for message in st.session_state.messages_v2:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            # 证据溯源：展示该条回答的工具调用轨迹
            trace = message.get("trace")
            if trace:
                with st.expander(
                    f"🔍 查询证据链（{len(trace)} 个处理阶段）", expanded=False
                ):
                    for i, item in enumerate(trace, 1):
                        st.markdown(
                            f"**{i}. `{item.get('name')}`**　"
                            f"`{json.dumps(item.get('args', {}), ensure_ascii=False)[:120]}`"
                        )
                        st.code(
                            str(item.get("result", ""))[:600], language="json"
                        )

    question = st.chat_input(
        "例如：员工 1001 属于哪个部门？",
        key="v2_chat_input",
        disabled=chat_disabled,
    )
    if chat_disabled:
        st.warning("问题测评运行中，问答暂不可用。")

    if question:
        st.session_state.messages_v2.append({
            "role": "user", "content": question
        })
        with st.chat_message("user"):
            st.markdown(question)

        # 所有问答统一走确定性本体查询 Pipeline；不再提供直接上下文问答旁路。
        with st.chat_message("assistant"):
            status_box = st.empty()
            answer_box = st.empty()
            answer_parts: List[str] = []
            structured_trace: List[Dict[str, Any]] = []
            status_box.caption("正在理解问题并生成结构化本体查询计划…")
            try:
                if not api_key:
                    raise RuntimeError("请先配置 LLM API Key。")
                if OpenAI is None:
                    raise RuntimeError("缺少 openai 依赖，请执行：pip install openai")
                client = _make_llm_client(api_key, base_url)
                pipeline_result = execute_query_pipeline(
                    client, model_name, question, ontology,
                    version_id=pointer.get("version_id") if pointer else None,
                )
                structured_trace = pipeline_result.get("trace", [])
                answer = pipeline_result.get("answer", "")
                validation = pipeline_result.get("validation", {})
                if not validation.get("valid", False):
                    answer_box.error(answer)
                else:
                    answer_box.markdown(answer)
                answer_parts.append(answer)
                status_box.caption(
                    "查询计划已生成 → 本体查询已执行 → "
                    + ("结果校验通过" if validation.get("valid") else "结果校验未通过")
                    + " → 证据链已生成"
                )
            except Exception as exc:
                answer = f"本体查询 Pipeline 执行失败：{_friendly_llm_error(exc)}"
                answer_parts.append(answer)
                answer_box.error(answer)
                structured_trace.append({
                    "name": "pipeline_error", "args": {},
                    "result": str(exc),
                })
            finally:
                status_box.empty()


        st.session_state.messages_v2.append({
            "role": "assistant",
            "content": answer,
            "trace": structured_trace,
            "version_id": pointer.get("version_id") if pointer else None,
        })


# ---------------------------------------------------------------------
# 页面 5：问题测评
# ---------------------------------------------------------------------
def _ensure_list(value: Any) -> List[str]:
    """data_editor 中列表列可能被编辑为字符串；统一转 List[str]。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,，;；]", text) if part.strip()]


def _eval_state_to_run(eval_state: Dict[str, Any], status: str) -> Dict[str, Any]:
    return {
        "run_id": eval_state["run_id"],
        "version_id": eval_state["version_id"],
        "status": status,
        "started_at": eval_state.get("started_at") or datetime.now().isoformat(),
        "config": eval_state["config"],
        "results": eval_state["results"],
    }


def _render_evaluation_running(eval_state: Dict[str, Any]) -> None:
    """评测运行中：每次 run 只消费一题，完成后落盘并 rerun 下一题。"""
    questions = eval_state["questions"]
    cursor = eval_state["cursor"]
    total = len(questions)

    if cursor >= total:
        # 全部完成
        run = _eval_state_to_run(eval_state, status="completed")
        save_eval_run(run)
        st.session_state.eval_state = None
        st.success(f"测评完成：{total} 题。结果已保存到 {run['run_id']}。")
        st.rerun()
        return

    question = questions[cursor]
    st.progress(cursor / total, text=f"进度 {cursor}/{total}")
    st.info(f"正在测评第 {cursor + 1} 题：{question.get('question', '')}")

    # 停止按钮（题间生效）
    if st.button("⏹️ 停止测评"):
        run = _eval_state_to_run(eval_state, status="stopped")
        save_eval_run(run)
        st.session_state.eval_state = None
        st.warning(f"测评已停止（完成 {cursor}/{total} 题），结果已保存。")
        st.rerun()
        return

    # 消费当前一题：复用在线问答的确定性 Pipeline
    trace: List[Dict[str, Any]] = []
    error_text = ""
    answer = ""
    started = datetime.now()
    try:
        if not api_key:
            raise RuntimeError("请先配置 LLM API Key。")
        if OpenAI is None:
            raise RuntimeError("缺少 openai 依赖，请执行：pip install openai")
        client = _make_llm_client(api_key, base_url)
        pipeline_result = execute_query_pipeline(
            client, model_name, str(question.get("question", "")),
            eval_state["eval_ontology"],
            version_id=eval_state.get("version_id"),
        )
        answer = pipeline_result.get("answer", "")
        trace = pipeline_result.get("trace", [])
        if not pipeline_result.get("validation", {}).get("valid", False):
            error_text = "确定性结果校验未通过"
    except Exception as exc:
        error_text = _friendly_llm_error(exc)
        answer = f"本体查询 Pipeline 执行失败：{error_text}"
    elapsed = (datetime.now() - started).total_seconds()

    comparison = compare_answer(question, answer, trace)
    failure_code = (
        "" if comparison["passed"]
        else attribute_failure(
            question, answer, trace, comparison, error_text,
            eval_state["eval_ontology"],
        )
    )
    eval_state["results"].append({
        "question_id": question.get("question_id", ""),
        "question": question.get("question", ""),
        "category": question.get("category", ""),
        "expected_answer": question.get("expected_answer", ""),
        "answer": answer[:2000],
        "passed": comparison["passed"],
        "evidence_covered": comparison["evidence_covered"],
        "text_matched": comparison["text_matched"],
        "numbers_matched": comparison["numbers_matched"],
        "failure_code": failure_code,
        "pipeline_stages": len(trace),
        "tool_calls": len(trace),  # legacy compatibility for historical evaluation reports
        "elapsed_seconds": round(elapsed, 1),
        "error": error_text or None,
    })
    eval_state["cursor"] = cursor + 1

    # 逐题落盘（断点续跑依据）
    save_eval_run(_eval_state_to_run(eval_state, status="running"))
    st.rerun()


def _render_evaluation_results() -> None:
    """结果与归因：最近一次运行 + 指标 + 回归对比（简版）。"""
    runs = list_eval_runs()
    if not runs:
        st.info("尚无测评运行。")
        return

    run_options = [r["run_id"] for r in runs]
    selected_run_id = st.selectbox("选择测评运行", run_options)
    run = load_eval_run(selected_run_id)
    if not run:
        st.error("加载测评运行失败。")
        return

    metrics = run.get("metrics") or summarize_results(run)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("题数", metrics.get("total", 0))
    m2.metric(
        "答案准确率",
        f"{metrics.get('accuracy', 0):.0%}" if metrics.get("total") else "—",
    )
    m3.metric(
        "执行成功率",
        f"{metrics.get('execution_rate', 0):.0%}" if metrics.get("total") else "—",
    )
    m4.metric(
        "证据覆盖率",
        f"{metrics.get('evidence_rate', 0):.0%}" if metrics.get("total") else "—",
    )
    st.caption(
        f"运行 {run.get('run_id')} · 版本 {run.get('version_id')} · "
        f"状态 {run.get('status')} · 模型 {(run.get('config') or {}).get('model', '')}"
    )

    results = run.get("results") or []
    if results:
        st.markdown("**逐题结果**")
        st.dataframe(
            pd.DataFrame([
                {
                    "question_id": r.get("question_id"),
                    "问题": r.get("question"),
                    "通过": "✅" if r.get("passed") else "❌",
                    "归因": r.get("failure_code", ""),
                    "Pipeline 阶段": r.get("pipeline_stages", r.get("tool_calls", 0)),
                    "耗时(s)": r.get("elapsed_seconds"),
                }
                for r in results
            ]),
            use_container_width=True,
        )

        # 失败归因分布
        failures = [r.get("failure_code") for r in results if not r.get("passed")]
        if failures:
            st.markdown("**失败归因分布**")
            st.bar_chart(pd.Series(failures).value_counts())

    # 回归对比（简版）：与上一次运行按 question_id join
    if len(runs) >= 2:
        prev_run = load_eval_run(run_options[1])
        if prev_run:
            prev_map = {
                r.get("question_id"): r.get("passed")
                for r in prev_run.get("results") or []
            }
            regressions, improvements = [], []
            for r in results:
                qid = r.get("question_id")
                if qid in prev_map:
                    if prev_map[qid] and not r.get("passed"):
                        regressions.append(qid)
                    elif not prev_map[qid] and r.get("passed"):
                        improvements.append(qid)
            if regressions or improvements:
                st.markdown("**与上次运行对比（回归检测）**")
                c1, c2 = st.columns(2)
                c1.metric("⚠️ 退化（上次通过→本次失败）", len(regressions))
                c2.metric("✅ 改善（上次失败→本次通过）", len(improvements))
                if regressions:
                    st.warning(f"退化问题：{', '.join(regressions)}")


def render_evaluation() -> None:
    ontology = st.session_state.ontology_v2

    st.subheader("🧪 问题测评（CQ 问题集 · 自动测评 · 失败归因）")
    st.caption(
        "确定性比对为主：数值精确比对 + 证据召回（期望实体/关系出现在证据链中）。"
        "评测使用与在线问答相同的结构化查询 Pipeline，且必须先保存版本（版本绑定评测）。"
    )

    if ontology is None:
        st.info("请先生成本体，再进行问题测评。")
        return

    # ---- 评测运行中：一题一 rerun 状态机 ----
    eval_state = st.session_state.get("eval_state")
    if eval_state and eval_state.get("running"):
        _render_evaluation_running(eval_state)
        return

    # ---- 问题集管理 ----
    questions = load_question_set()

    tab_qs, tab_run, tab_results = st.tabs([
        "📋 问题集", "▶️ 运行测评", "📊 结果与归因",
    ])

    with tab_qs:
        st.markdown("**问题集编辑**（保存时自动为新增行分配 question_id）")
        edited = st.data_editor(
            pd.DataFrame(questions) if questions else pd.DataFrame(
                columns=[
                    "question_id", "question", "category",
                    "expected_answer", "expected_entities",
                    "expected_relations", "difficulty", "enabled",
                ]
            ),
            use_container_width=True,
            num_rows="dynamic",
            key="v4_question_editor",
        )
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            if st.button("💾 保存问题集", type="primary"):
                rows = edited.to_dict(orient="records")
                for row in rows:
                    row["enabled"] = bool(row.get("enabled", True))
                    row["expected_entities"] = _ensure_list(
                        row.get("expected_entities")
                    )
                    row["expected_relations"] = _ensure_list(
                        row.get("expected_relations")
                    )
                save_question_set(rows)
                st.success(f"已保存 {len(rows)} 个问题。")
                st.rerun()
        with c2:
            if st.button("🤖 从本体自动生成候选问题"):
                candidates = suggest_questions(ontology, max_questions=10)
                merged = questions + candidates
                save_question_set(merged)
                st.success(
                    f"已生成 {len(candidates)} 个候选问题"
                    "（provenance=auto，期望答案由本体确定性计算）。"
                )
                st.rerun()
        with c3:
            st.caption(
                "自动生成：计数题 / 属性题 / 邻居题，期望答案从本体计算，"
                "零成本 ground truth，供人工复核后启用。"
            )

    with tab_run:
        enabled_questions = [q for q in questions if q.get("enabled", True)]
        st.write(f"启用的问题：**{len(enabled_questions)}** / {len(questions)}")

        if not enabled_questions:
            st.warning("问题集为空。请先在「问题集」tab 添加或自动生成问题。")
        elif not api_key:
            st.warning("请先在侧边栏配置 LLM（API Key）。")
        else:
            if st.button("▶️ 开始测评", type="primary"):
                # 强制先保存版本（版本绑定评测，保证可复现）
                version_id, save_error = save_version(
                    ontology, note="测评前自动保存"
                )
                if save_error:
                    st.error(f"版本保存失败，无法开始测评：{save_error}")
                else:
                    # 冻结副本：防止评测期间 Action 修改污染
                    frozen, freeze_error = load_version(version_id)
                    if frozen is None:
                        st.error(f"冻结副本失败：{freeze_error}")
                    else:
                        st.session_state.eval_state = {
                            "running": True,
                            "cursor": 0,
                            "questions": enabled_questions,
                            "results": [],
                            "run_id": f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                            "version_id": version_id,
                            "config": {
                                "model": model_name,
                                "base_url": base_url,
                                "mode": "tool_calling",
                                "max_rounds": 4,
                            },
                            "eval_ontology": frozen,
                        }
                        st.rerun()

    with tab_results:
        _render_evaluation_results()


# ---------------------------------------------------------------------
# 页面 6：版本管理
# ---------------------------------------------------------------------
def render_versions() -> None:
    ontology = st.session_state.ontology_v2

    st.subheader("🗂️ 版本管理")
    st.caption(
        "版本快照（Schema + 映射 + 实例 + 关系）· 生命周期 "
        "draft → validated → released → deprecated · 差异比较 · 回滚"
    )

    # ---- 从当前会话创建版本 ----
    if ontology is not None:
        with st.expander("➕ 从当前会话创建版本", expanded=True):
            note = st.text_input(
                "版本备注", value="", key="v4_version_note",
                placeholder="例如：新增 Department.manager 关系",
            )
            if st.button("📸 创建版本快照", type="primary"):
                version_id, error = save_version(ontology, note=note)
                if error:
                    st.error(f"创建失败（round-trip 自检未通过）：{error}")
                else:
                    st.success(f"版本 {version_id} 已创建。")
                    st.rerun()

    # ---- 版本列表 ----
    versions = list_versions()
    if not versions:
        st.info("尚无版本。生成本体后可在上方创建第一个版本。")
        return

    st.markdown(f"**版本列表**（共 {len(versions)} 个）")
    st.dataframe(
        pd.DataFrame([
            {
                "版本": v["id"],
                "时间": v["created_at"][:19].replace("T", " "),
                "状态": v["status"],
                "备注": v["note"],
                "类型": (v["stats"] or {}).get("object_types", 0),
                "实例": (v["stats"] or {}).get("objects", 0),
                "关系": (v["stats"] or {}).get("links", 0),
            }
            for v in versions
        ]),
        use_container_width=True,
    )

    # ---- 版本操作 ----
    version_ids = [v["id"] for v in versions]
    c1, c2 = st.columns(2)
    with c1:
        selected_id = st.selectbox("选择版本", version_ids, key="v4_version_select")
        selected = next((v for v in versions if v["id"] == selected_id), None)

        if selected:
            # 生命周期流转
            status = selected["status"]
            next_status = {
                "draft": "validated", "validated": "released",
                "released": "deprecated",
            }.get(status)
            bc1, bc2 = st.columns(2)
            with bc1:
                if next_status and st.button(f"➡️ 升级为 {next_status}"):
                    if update_version_status(selected_id, next_status):
                        st.success(f"已升级为 {next_status}。")
                        st.rerun()
            with bc2:
                if status != "deprecated" and st.button("⛔ 标记废弃"):
                    if update_version_status(selected_id, "deprecated"):
                        st.success("已标记废弃。")
                        st.rerun()

            # 回滚
            st.divider()
            st.warning(
                "回滚将用该版本替换当前本体；原始数据表会清空"
                "（防止建模页重新生成覆盖回滚结果），问答历史保留。"
            )
            if st.button("⏪ 回滚到此版本", type="primary"):
                loaded, error = load_version(selected_id)
                if loaded is None:
                    st.error(f"回滚失败：{error}")
                else:
                    st.session_state.ontology_v2 = loaded
                    st.session_state.raw_data_v2 = {}
                    _update_current_pointer(
                        selected_id, _ontology_hash(loaded)
                    )
                    st.success(f"已回滚到 {selected_id}。")
                    st.rerun()

    with c2:
        # ---- 差异比较 ----
        st.markdown("**差异比较**")
        if len(version_ids) >= 2:
            diff_a = st.selectbox(
                "版本 A（旧）", version_ids, index=1, key="v4_diff_a"
            )
            diff_b = st.selectbox(
                "版本 B（新）", version_ids, index=0, key="v4_diff_b"
            )
            if diff_a != diff_b and st.button("🔍 比较差异"):
                diff = diff_versions(diff_a, diff_b)
                sections = [
                    ("实体类型（Schema）", diff["object_types"]),
                    ("关系类型（Schema）", diff["link_types"]),
                    ("数据映射", diff["data_mapping"]),
                    ("实例与边数", diff["instance_counts"]),
                ]
                has_diff = False
                for title, rows in sections:
                    if rows:
                        has_diff = True
                        st.markdown(f"**{title}**")
                        st.dataframe(
                            pd.DataFrame(rows), use_container_width=True
                        )
                if diff["pk_added"] or diff["pk_removed"]:
                    has_diff = True
                    st.markdown("**实例 PK 级增删**")
                    ca, cb = st.columns(2)
                    with ca:
                        st.markdown(f"新增（{len(diff['pk_added'])}）")
                        st.code("\n".join(diff["pk_added"]) or "无")
                    with cb:
                        st.markdown(f"删除（{len(diff['pk_removed'])}）")
                        st.code("\n".join(diff["pk_removed"]) or "无")
                    if diff["pk_truncated"]:
                        st.caption("清单过长已截断至 100 条。")
                if not has_diff:
                    st.success("两个版本无差异。")
        else:
            st.info("至少需要两个版本才能比较差异。")


# ---------------------------------------------------------------------
# 页面导航
# ---------------------------------------------------------------------
pages = [
    st.Page(render_overview, title="🏠 Pipeline 总览", default=True),
    st.Page(render_data_source, title="📥 数据源"),
    st.Page(render_modeling, title="🏗️ 本体建模"),
    st.Page(render_mapping_workbench, title="🧩 映射工作台"),
    st.Page(render_qa, title="💬 智能问答"),
    st.Page(render_evaluation, title="🧪 问题测评"),
    st.Page(render_versions, title="🗂️ 版本管理"),
]

pg = st.navigation(pages)
pg.run()
