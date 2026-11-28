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
def json_safe(value: Any) -> Any:
    """将 Pandas / NumPy 类型转换为可 JSON 序列化类型。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def safe_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    return [
        {str(k): json_safe(v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]


def make_node_id(type_name: str, pk: Any) -> str:
    return f"{type_name}:{pk}"


# ---------------------------------------------------------------------
# 本体数据模型
# ---------------------------------------------------------------------
class Ontology:
    def __init__(self) -> None:
        self.object_types: Dict[str, Dict[str, Any]] = {}
        self.objects: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.links = nx.MultiDiGraph()
        self.link_types: Dict[str, Dict[str, Any]] = {}
        self.data_mapping: Dict[str, Dict[str, Any]] = {}
        self.actions_log: List[Dict[str, Any]] = []

    def add_object_type(
        self,
        name: str,
        primary_key: str,
        properties: List[str],
        description: str = "",
    ) -> None:
        self.object_types[name] = {
            "primary_key": primary_key,
            "properties": list(properties),
            "description": description,
        }
        self.objects.setdefault(name, {})

    def add_objects(self, type_name: str, records: List[Dict[str, Any]]) -> None:
        if type_name not in self.object_types:
            raise ValueError(f"ObjectType '{type_name}' 不存在")
        pk_field = self.object_types[type_name]["primary_key"]

        for row_number, record in enumerate(records, start=1):
            clean = {str(k): json_safe(v) for k, v in record.items()}
            pk_value = clean.get(pk_field)
            if pk_value is None or str(pk_value).strip() == "":
                raise ValueError(
                    f"对象类型 {type_name} 第 {row_number} 行主键 {pk_field} 为空；拒绝静默丢弃记录"
                )

            key = str(pk_value)
            if key in self.objects[type_name]:
                raise ValueError(
                    f"对象类型 {type_name} 存在重复主键 {key}；拒绝覆盖已有实例"
                )
            self.objects[type_name][key] = clean
            self.links.add_node(
                make_node_id(type_name, key),
                type=type_name,
                pk=key,
                label=f"{type_name}:{key}",
                **{str(k): str(v) for k, v in clean.items() if v is not None},
            )

    def add_link_type(
        self,
        name: str,
        source_type: str,
        target_type: str,
        description: str = "",
        source_property: str = "",
        target_property: str = "",
    ) -> None:
        self.link_types[name] = {
            "source": source_type,
            "target": target_type,
            "description": description,
            "source_property": source_property,
            "target_property": target_property,
        }

    def add_links(
        self,
        link_name: str,
        pairs: List[Tuple[str, str]],
    ) -> None:
        if link_name not in self.link_types:
            return

        info = self.link_types[link_name]
        src_type, tgt_type = info["source"], info["target"]

        for src_pk, tgt_pk in pairs:
            src_node = make_node_id(src_type, src_pk)
            tgt_node = make_node_id(tgt_type, tgt_pk)
            if src_node in self.links and tgt_node in self.links:
                self.links.add_edge(
                    src_node,
                    tgt_node,
                    key=link_name,
                    label=link_name,
                    relation_type=link_name,
                    source_type=src_type,
                    target_type=tgt_type,
                    source_pk=str(src_pk),
                    target_pk=str(tgt_pk),
                    description=info.get("description", ""),
                )

    def create_object(self, type_name: str, data: Dict[str, Any]) -> Optional[str]:
        if type_name not in self.object_types:
            return None

        pk_field = self.object_types[type_name]["primary_key"]
        clean = {str(k): json_safe(v) for k, v in data.items()}
        if clean.get(pk_field) is None or str(clean.get(pk_field)).strip() == "":
            raise ValueError(f"创建 {type_name} 失败：必须显式提供有效主键 {pk_field}")

        key = str(clean[pk_field])
        if key in self.objects[type_name]:
            raise ValueError(f"主键 {key} 已存在")

        self.objects[type_name][key] = clean
        self.links.add_node(
            make_node_id(type_name, key),
            type=type_name,
            pk=key,
            label=f"{type_name}:{key}",
            **{str(k): str(v) for k, v in clean.items() if v is not None},
        )
        self.actions_log.append({
            "action": "create", "type": type_name, "pk": key,
            "data": clean, "timestamp": datetime.now().isoformat(),
        })
        return key

    def update_object(
        self, type_name: str, primary_key: str, updates: Dict[str, Any]
    ) -> bool:
        key = str(primary_key)
        if type_name not in self.objects or key not in self.objects[type_name]:
            return False

        pk_field = self.object_types[type_name]["primary_key"]
        if pk_field in updates and str(updates[pk_field]) != key:
            raise ValueError("不允许通过更新操作修改主键")

        clean_updates = {k: json_safe(v) for k, v in updates.items()}
        self.objects[type_name][key].update(clean_updates)
        node_id = make_node_id(type_name, key)

        if node_id in self.links:
            for k, v in clean_updates.items():
                self.links.nodes[node_id][k] = str(v) if v is not None else None

        self.actions_log.append({
            "action": "update", "type": type_name, "pk": key,
            "updates": clean_updates, "timestamp": datetime.now().isoformat(),
        })
        return True

    def delete_object(self, type_name: str, primary_key: str) -> bool:
        key = str(primary_key)
        if type_name not in self.objects or key not in self.objects[type_name]:
            return False

        del self.objects[type_name][key]
        node_id = make_node_id(type_name, key)
        if node_id in self.links:
            self.links.remove_node(node_id)

        self.actions_log.append({
            "action": "delete", "type": type_name, "pk": key,
            "timestamp": datetime.now().isoformat(),
        })
        return True

    def summary(self) -> str:
        lines = ["### 本体摘要"]
        lines.append(f"- **Object Types**：{list(self.object_types.keys())}")
        for type_name, info in self.object_types.items():
            count = len(self.objects.get(type_name, {}))
            lines.append(
                f"  - `{type_name}`：**{count}** 个对象；"
                f"主键：`{info['primary_key']}`；"
                f"属性数：{len(info['properties'])}"
            )
        lines.append(f"- **Link Types**：{list(self.link_types.keys())}")
        lines.append(f"- **实体节点数**：{self.links.number_of_nodes()}")
        lines.append(f"- **关系边数**：{self.links.number_of_edges()}")
        lines.append(f"- **Action 次数**：{len(self.actions_log)}")
        return "\n".join(lines)

    def _extract_question_tokens(self, question: str) -> List[str]:
        """从问题中提取可用于匹配实体的关键词（数字 ID、英文词、中文词）。"""
        text = (question or "").strip().lower()
        if not text:
            return []

        tokens: List[str] = []
        # 连续数字 / 字母数字 ID（如 1001、emp_1001）
        tokens.extend(re.findall(r"[a-z0-9][a-z0-9_\-]{1,}", text))
        # 中文连续词（2 字以上）
        tokens.extend(re.findall(r"[\u4e00-\u9fff]{2,}", text))
        # 去重并过滤过短 token
        seen = set()
        result = []
        for t in tokens:
            t = t.strip()
            if len(t) < 2 or t in seen:
                continue
            # 过滤常见停用词（中英）
            if t in {
                "哪个", "哪些", "什么", "多少", "如何", "怎么", "是否",
                "属于", "相关", "关系", "查询", "显示", "列出", "所有",
                "the", "and", "or", "for", "with", "from", "that", "this",
                "what", "which", "where", "when", "how", "many", "list",
            }:
                continue
            seen.add(t)
            result.append(t)
        return result

    def retrieve_relevant_objects(
        self,
        question: str,
        max_per_type: int = 30,
        max_total: int = 120,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        按问题关键词检索相关实例，并补充其一跳邻居。
        无命中时回退为每种类型少量样本，保证问答仍可用。
        """
        tokens = self._extract_question_tokens(question)
        selected: Dict[str, Dict[str, Dict[str, Any]]] = {
            t: {} for t in self.object_types
        }
        matched_node_ids: List[str] = []

        def _record_matches(obj: Dict[str, Any], tokens: List[str]) -> bool:
            if not tokens:
                return False
            blob = " ".join(str(v).lower() for v in obj.values() if v is not None)
            return any(tok in blob for tok in tokens)

        # 1) 关键词命中
        if tokens:
            for type_name, objects in self.objects.items():
                for pk, obj in objects.items():
                    if _record_matches(obj, tokens):
                        selected[type_name][pk] = obj
                        matched_node_ids.append(make_node_id(type_name, pk))
                        if len(selected[type_name]) >= max_per_type:
                            break

        # 2) 一跳邻居扩展（关系路径问答更完整）
        if matched_node_ids and self.links.number_of_edges() > 0:
            neighbor_ids = set()
            for nid in matched_node_ids[:80]:
                if nid not in self.links:
                    continue
                neighbor_ids.update(self.links.predecessors(nid))
                neighbor_ids.update(self.links.successors(nid))
            for nid in neighbor_ids:
                attrs = self.links.nodes.get(nid) or {}
                t = attrs.get("type")
                pk = attrs.get("pk")
                if not t or pk is None or t not in selected:
                    continue
                if pk in selected[t]:
                    continue
                obj = self.objects.get(t, {}).get(str(pk))
                if obj is None:
                    continue
                if len(selected[t]) >= max_per_type:
                    continue
                selected[t][str(pk)] = obj

        # 3) 无命中则回退抽样
        total = sum(len(v) for v in selected.values())
        if total == 0:
            for type_name, objects in self.objects.items():
                for pk, obj in list(objects.items())[: min(10, max_per_type)]:
                    selected[type_name][pk] = obj

        # 4) 全局上限裁剪（保持类型尽量均衡）
        total = sum(len(v) for v in selected.values())
        if total > max_total:
            # 按类型轮询截断
            result: Dict[str, List[Dict[str, Any]]] = {t: [] for t in selected}
            keys = {t: list(objs.keys()) for t, objs in selected.items()}
            while sum(len(v) for v in result.values()) < max_total:
                progressed = False
                for t in list(keys.keys()):
                    if not keys[t]:
                        continue
                    pk = keys[t].pop(0)
                    result[t].append(selected[t][pk])
                    progressed = True
                    if sum(len(v) for v in result.values()) >= max_total:
                        break
                if not progressed:
                    break
            return result

        return {
            t: list(objs.values())
            for t, objs in selected.items()
            if objs
        }

    def to_context_for_llm(
        self,
        max_objects: Optional[int] = None,
        question: Optional[str] = None,
        mode: str = "retrieve",
        max_chars: int = 60000,
    ) -> str:
        """
        构建 LLM 上下文。

        mode:
          - retrieve: 按问题检索相关对象（推荐，默认）
          - sample:   每种类型最多 max_objects 条
          - full:     尽量全量，但仍受 max_chars 硬限制

        max_chars 为序列化后字符串的软上限，防止超出模型上下文。
        """
        context: Dict[str, Any] = {
            "object_types": {},
            "link_types": self.link_types,
            "sample_objects": {},
            "recent_actions": self.actions_log[-5:],
            "context_mode": mode,
            "note": "仅包含与问题相关或抽样的实例；完整数据见本体工作台。",
        }

        for type_name, info in self.object_types.items():
            count = len(self.objects.get(type_name, {}))
            context["object_types"][type_name] = {
                "primary_key": info["primary_key"],
                "properties": info["properties"],
                "description": info.get("description", ""),
                "count": count,
            }

        if mode == "retrieve" and question:
            context["sample_objects"] = self.retrieve_relevant_objects(
                question,
                max_per_type=max_objects or 30,
                max_total=(max_objects or 30) * max(1, len(self.object_types)),
            )
            context["context_mode"] = "retrieve"
        else:
            limit = max_objects
            if mode == "full":
                # 全量模式仍设安全上限，避免一次序列化百万行
                limit = None
            for type_name in self.object_types:
                records = list(self.objects.get(type_name, {}).values())
                if limit is not None:
                    records = records[:limit]
                context["sample_objects"][type_name] = records

        # 紧凑 JSON，并按字符预算逐步收缩实例
        def _dump(ctx: Dict[str, Any]) -> str:
            return json.dumps(ctx, ensure_ascii=False, separators=(",", ":"), default=str)

        payload = _dump(context)
        if len(payload) <= max_chars:
            return payload

        # 超限：按类型均匀裁剪 sample_objects
        samples = context.get("sample_objects") or {}
        if samples:
            # 每轮减半
            for _ in range(8):
                for t in list(samples.keys()):
                    arr = samples[t]
                    if not isinstance(arr, list) or len(arr) <= 1:
                        continue
                    samples[t] = arr[: max(1, len(arr) // 2)]
                context["sample_objects"] = samples
                context["note"] = (
                    "上下文已按大小自动裁剪；若答案不完整，请缩小问题范围或提高检索上限。"
                )
                payload = _dump(context)
                if len(payload) <= max_chars:
                    return payload

            # 仍超限：只保留 schema
            context["sample_objects"] = {}
            context["note"] = (
                "实例数据过大已全部省略，仅保留类型与关系定义。"
                "请改用「智能检索」或减少数据量后重试。"
            )
            payload = _dump(context)

        return payload

    def export_json(self, compact: bool = False) -> str:
        """导出完整本体 JSON。

        compact=True 时使用紧凑分隔符（版本快照落盘用），
        默认 indent=2 便于人工阅读（下载按钮用）。
        边导出完整属性（含 key=关系名），保证 from_dict 可无损重建。
        """
        data = {
            "format_version": 2,
            "object_types": self.object_types,
            "objects": self.objects,
            "link_types": self.link_types,
            "links": [
                {
                    "source": u,
                    "target": v,
                    "key": str(k),
                    **{str(attr): json_safe(val) for attr, val in d.items()},
                }
                for u, v, k, d in self.links.edges(keys=True, data=True)
            ],
            "data_mapping": self.data_mapping,
            "actions_log": self.actions_log,
            "exported_at": datetime.now().isoformat(),
        }
        if compact:
            return json.dumps(
                data, ensure_ascii=False, separators=(",", ":"), default=str
            )
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Ontology":
        """从 export_json 格式的字典重建本体（版本加载 / 回滚 / 刷新恢复）。

        重建顺序：先注册类型并导入实例（先节点），再注册关系类型，
        最后显式以 key=关系名 加边——若不传 key，networkx 会分配整数键，
        get_neighbors / find_path / 关系分析中的关系名会静默劣化。
        兼容旧格式（links 只有 type 无 key）：用 type 作 key，属性从 link_types 推导。
        """
        ontology = cls()

        for type_name, info in (data.get("object_types") or {}).items():
            ontology.add_object_type(
                str(type_name),
                str(info.get("primary_key", "")),
                [str(p) for p in info.get("properties", [])],
                str(info.get("description", "")),
            )

        for type_name, objects in (data.get("objects") or {}).items():
            if type_name in ontology.object_types:
                ontology.add_objects(type_name, list(objects.values()))

        for link_name, info in (data.get("link_types") or {}).items():
            ontology.add_link_type(
                str(link_name),
                str(info.get("source", "")),
                str(info.get("target", "")),
                str(info.get("description", "")),
                str(info.get("source_property", "")),
                str(info.get("target_property", "")),
            )

        for edge in data.get("links") or []:
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source not in ontology.links or target not in ontology.links:
                # 两端节点必须已存在，避免产生无 type 属性的幽灵节点
                continue

            key = edge.get("key")
            attrs = {
                str(k): v for k, v in edge.items()
                if k not in ("source", "target", "key")
            }
            if key is None:
                # 旧格式降级：type 即关系名，缺失属性从 link_types 推导
                key = str(edge.get("type") or edge.get("label") or "")
                info = ontology.link_types.get(str(key), {})
                src_type = ontology.links.nodes[source].get("type", "")
                tgt_type = ontology.links.nodes[target].get("type", "")
                attrs.setdefault("label", key)
                attrs.setdefault("relation_type", key)
                attrs.setdefault("source_type", src_type)
                attrs.setdefault("target_type", tgt_type)
                attrs.setdefault("description", info.get("description", ""))

            ontology.links.add_edge(source, target, key=str(key), **attrs)

        ontology.data_mapping = {
            str(k): dict(v) for k, v in (data.get("data_mapping") or {}).items()
        }
        ontology.actions_log = list(data.get("actions_log") or [])
        return ontology

    def snapshot_dict(self) -> Dict[str, Any]:
        """导出为字典（快照落盘 / hash 计算用，不含时间戳字段）。"""
        data = json.loads(self.export_json(compact=True))
        data.pop("exported_at", None)
        return data

    def export_owl(self, format: str = "xml") -> str:
        """导出 RDF/OWL。

        format:
          - "xml"    : 传统 RDF/XML（向后兼容）
          - "turtle" / "ttl" : 扁平 Turtle
          - "trig"   : TriG（保留命名图，推荐给 Ontosphere）
        """
        from ontology_domain.rdf_export import (
            export_ontology_to_dataset,
            serialize_owl_xml,
            serialize_turtle_flat,
            serialize_trig,
        )

        ds = export_ontology_to_dataset(
            self, include_instances=True, include_mapping=True
        )
        fmt = (format or "xml").lower()
        if fmt == "trig":
            return serialize_trig(ds)
        if fmt in ("turtle", "ttl"):
            return serialize_turtle_flat(ds)
        return serialize_owl_xml(ds)

    def export_for_ontosphere(
        self, *, include_instances: bool = True, include_mapping: bool = True
    ) -> Tuple[str, str]:
        """一键导出供 Ontosphere 使用，返回 (trig_content, filename)。"""
        from ontology_domain.rdf_export import export_for_ontosphere

        return export_for_ontosphere(
            self,
            include_instances=include_instances,
            include_mapping=include_mapping,
        )


# ---------------------------------------------------------------------
# 自动建模
# ---------------------------------------------------------------------
class AutoOntologyBuilder:
    def __init__(self) -> None:
        self.ontology = Ontology()

    @staticmethod
    def infer_primary_key(df: pd.DataFrame, table_name: str) -> str:
        if len(df.columns) == 0:
            raise ValueError(f"表 {table_name} 没有可用列")

        for col in df.columns:
            col_lower = str(col).lower()
            if (
                col_lower in ("id", "pk")
                or col_lower.endswith("_id")
                or col_lower == f"{table_name.lower()}_id"
            ):
                if df[col].is_unique and df[col].notna().all():
                    return str(col)

        for col in df.columns:
            if df[col].is_unique and df[col].notna().all():
                return str(col)

        # No reliable single-column identity was found. The UI may let a user
        # choose a key explicitly; the builder must never guess the first column.
        return ""

    def from_dataframes(
        self,
        data: Dict[str, pd.DataFrame],
        primary_keys: Optional[Dict[str, str]] = None,
        manual_links: Optional[List[Dict[str, Any]]] = None,
    ) -> Ontology:
        primary_keys = primary_keys or {}
        manual_links = manual_links or []

        normalized_data: Dict[str, pd.DataFrame] = {}
        actual_pks: Dict[str, str] = {}

        for table_name, original_df in data.items():
            df = original_df.copy()
            df.columns = [str(col).strip() for col in df.columns]
            df = df.loc[:, [c for c in df.columns if c != ""]]

            if df.empty and len(df.columns) == 0:
                continue

            pk = primary_keys.get(table_name) or self.infer_primary_key(df, table_name)
            if not pk or pk not in df.columns:
                raise ValueError(
                    f"数据表 {table_name} 无法自动识别可靠主键，请在本体建模配置中人工选择主键"
                )
            if df[pk].isna().any() or df[pk].astype(str).str.strip().eq("").any():
                raise ValueError(f"数据表 {table_name} 主键列 {pk} 存在空值")
            if df[pk].astype(str).duplicated().any():
                raise ValueError(f"数据表 {table_name} 主键列 {pk} 存在重复值")

            actual_pks[table_name] = pk
            normalized_data[table_name] = df

            self.ontology.add_object_type(
                table_name,
                pk,
                list(df.columns),
                f"来自数据源：{table_name}",
            )
            self.ontology.add_objects(table_name, safe_records(df))
            self.ontology.data_mapping[table_name] = {
                "source_name": table_name,
                "primary_key": pk,
                "columns": list(df.columns),
                "row_count": len(df),
                "loaded_count": len(self.ontology.objects[table_name]),
            }

        self._auto_detect_links(normalized_data, actual_pks)

        for link in manual_links:
            self._add_manual_link(normalized_data, link)

        return self.ontology

    def _auto_detect_links(
        self,
        data: Dict[str, pd.DataFrame],
        primary_keys: Dict[str, str],
    ) -> None:
        for source_table, source_df in data.items():
            source_pk = primary_keys[source_table]

            for target_table, target_df in data.items():
                if source_table == target_table:
                    continue

                target_pk = primary_keys[target_table]
                target_columns = {str(c).lower(): str(c) for c in target_df.columns}

                candidate_columns = []
                for col in source_df.columns:
                    col_name = str(col)
                    lower = col_name.lower()
                    if col_name == source_pk:
                        continue
                    if (
                        lower == target_pk.lower()
                        or lower == f"{target_table.lower()}_id"
                        or lower.endswith(f"_{target_table.lower()}_id")
                    ):
                        candidate_columns.append(col_name)

                for source_col in candidate_columns:
                    relation_name = f"{source_table}_to_{target_table}"
                    if relation_name not in self.ontology.link_types:
                        self.ontology.add_link_type(
                            relation_name,
                            source_table,
                            target_table,
                            f"自动识别：{source_table}.{source_col} → "
                            f"{target_table}.{target_pk}",
                            source_property=source_col,
                            target_property=target_pk,
                        )

                    pairs = [
                        (str(row[source_pk]), str(row[source_col]))
                        for _, row in source_df.iterrows()
                        if pd.notna(row[source_pk]) and pd.notna(row[source_col])
                    ]
                    self.ontology.add_links(relation_name, pairs)

    def _add_manual_link(
        self,
        data: Dict[str, pd.DataFrame],
        link: Dict[str, Any],
    ) -> None:
        source = link["source"]
        target = link["target"]
        source_col = link["source_col"]
        relation_name = link["name"]

        if source not in data or target not in data:
            raise ValueError(f"关系 {relation_name} 的源表或目标表不存在")
        if source_col not in data[source].columns:
            raise ValueError(f"源关联列 {source}.{source_col} 不存在")

        source_pk = self.ontology.object_types[source]["primary_key"]
        target_pk = self.ontology.object_types[target]["primary_key"]

        self.ontology.add_link_type(
            relation_name,
            source,
            target,
            link.get("description", "手动指定"),
            source_property=source_col,
            target_property=target_pk,
        )
        pairs = [
            (str(row[source_pk]), str(row[source_col]))
            for _, row in data[source].iterrows()
            if pd.notna(row.get(source_pk)) and pd.notna(row.get(source_col))
        ]
        self.ontology.add_links(relation_name, pairs)


# ---------------------------------------------------------------------
# 图谱可视化
# ---------------------------------------------------------------------
TYPE_COLORS = [
    "#1f1f1f", "#363636", "#4d4d4d", "#646464",
    "#7a7a7a", "#919191", "#a8a8a8", "#bfbfbf",
]


def font_color_for(background: str) -> str:
    """按背景亮度选择黑/白字体色，保证灰阶节点可读。"""
    value = background.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return "#1a1a1a" if (r * 299 + g * 587 + b * 114) / 1000 > 150 else "#ffffff"



def pyvis_html(net):
    """
    生成 PyVis HTML。
    直接生成 HTML 字符串，避免 Windows GBK 默认编码导致 UnicodeEncodeError。
    """
    try:
        # 直接生成 HTML，不调用 save_graph()
        html = net.generate_html()

        if not html:
            raise RuntimeError("PyVis 未能生成 HTML 内容")

        return html

    except Exception as e:
        raise RuntimeError(f"图谱 HTML 生成失败: {str(e)}") from e


def create_schema_graph(ontology: Ontology) -> str:
    net = Network(
        height="720px", width="100%", directed=True,
        bgcolor="#f5f5f5", font_color="#1f1f1f",
        cdn_resources="in_line",
    )
    net.barnes_hut(gravity=-5000, central_gravity=0.3, spring_length=180)

    for index, (type_name, info) in enumerate(ontology.object_types.items()):
        props = info.get("properties", [])
        description = html.escape(str(info.get("description", "")))
        title = (
            f"<b>实体类型：</b>{html.escape(type_name)}<br>"
            f"<b>主键：</b>{html.escape(str(info['primary_key']))}<br>"
            f"<b>属性数量：</b>{len(props)}<br>"
            f"<b>描述：</b>{description}<br>"
            f"<b>属性：</b>{html.escape(', '.join(props))}"
        )
        node_bg = TYPE_COLORS[index % len(TYPE_COLORS)]
        net.add_node(
            f"CLASS:{type_name}",
            label=type_name,
            title=title,
            shape="box",
            color=node_bg,
            size=32,
            font={"color": font_color_for(node_bg), "size": 17},
        )

    for relation_name, info in ontology.link_types.items():
        source, target = info["source"], info["target"]
        if source not in ontology.object_types or target not in ontology.object_types:
            continue
        net.add_edge(
            f"CLASS:{source}",
            f"CLASS:{target}",
            label=relation_name,
            title=html.escape(str(info.get("description", relation_name))),
            arrows="to",
            color="#4d4d4d",
            width=3,
            font={"color": "#4d4d4d", "size": 13, "strokeWidth": 2,
                  "strokeColor": "#f5f5f5"},
        )

    for type_name, info in ontology.object_types.items():
        class_id = f"CLASS:{type_name}"
        for prop in info.get("properties", []):
            prop_id = f"PROP:{type_name}:{prop}"
            net.add_node(
                prop_id,
                label=prop,
                title=f"属性：{html.escape(prop)}<br>所属类型："
                      f"{html.escape(type_name)}",
                shape="ellipse",
                color="#d9d9d9",
                size=13,
                font={"color": "#1f1f1f", "size": 11},
            )
            net.add_edge(
                class_id, prop_id, label="hasProperty",
                color="#a8a8a8", arrows="to", dashes=True,
                font={"color": "#7a7a7a", "size": 9},
            )

    net.set_options("""
    {
      "interaction": {"hover": true, "navigationButtons": true,
        "keyboard": true, "tooltipDelay": 100},
      "physics": {"enabled": true,
        "stabilization": {"enabled": true, "iterations": 400}},
      "edges": {"smooth": {"enabled": true, "type": "dynamic"}}
    }
    """)
    return pyvis_html(net)


def create_business_object_graph(ontology: Ontology) -> str:
    """
    业务对象关系图：AntV X6 图形引擎 + Dagre 自动分层布局。

    数据来自本体摘要同源信息：
    - 节点 = 实体类型（名称 + 实例数 / 主键 / 属性数）
    - 边 = 关系类型（关系名标签 + 箭头指向目标实体）

    生成自包含 HTML（CDN 引入 X6 与 dagre），经
    st.components.v1.html 嵌入，与 pyvis 图谱同模式。
    """
    # ---- 数据提取（与 summary() 同源） ----
    nodes = []
    for index, (type_name, info) in enumerate(ontology.object_types.items()):
        nodes.append({
            "id": type_name,
            "label": type_name,
            "count": len(ontology.objects.get(type_name, {})),
            "primary_key": str(info.get("primary_key", "")),
            "prop_count": len(info.get("properties", [])),
            "description": str(info.get("description", "")),
            "color": TYPE_COLORS[index % len(TYPE_COLORS)],
        })

    edges = []
    for relation_name, info in ontology.link_types.items():
        source, target = info["source"], info["target"]
        if source not in ontology.object_types or target not in ontology.object_types:
            continue
        edges.append({
            "source": source,
            "target": target,
            "label": relation_name,
            "description": str(info.get("description", relation_name)),
        })

    graph_data = json.dumps(
        {"nodes": nodes, "edges": edges},
        ensure_ascii=False, default=str,
    )

    # ---- 自包含 HTML（X6 + Dagre，CDN 引入） ----
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; background: #f5f5f5;
    font-family: "Source Sans Pro", "PingFang SC", "Microsoft YaHei", sans-serif; }}
  #toolbar {{ display: flex; gap: 8px; padding: 8px 12px;
    background: #ffffff; border-bottom: 1px solid #e0e0e0; }}
  #toolbar button {{ background: #ffffff; color: #1f1f1f;
    border: 1px solid #d6d6d6; border-radius: 6px; padding: 4px 12px;
    font-size: 13px; font-weight: 600; cursor: pointer; }}
  #toolbar button:hover {{ background: #ececec; border-color: #bfbfbf; }}
  #bo-graph {{ width: 100%; height: 660px; }}
  /* X6 HTML 节点卡片样式 */
  .bo-card {{ width: 180px; border-radius: 8px; overflow: hidden;
    box-shadow: 0 2px 6px rgba(0,0,0,0.18); cursor: move; }}
  .bo-card-head {{ padding: 8px 12px; font-size: 15px; font-weight: 700;
    text-align: center; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }}
  .bo-card-body {{ background: #ffffff; padding: 6px 12px 8px;
    font-size: 12px; color: #3d3d3d; line-height: 1.7; }}
  .bo-card-body .row {{ display: flex; justify-content: space-between; }}
  .bo-card-body .k {{ color: #7a7a7a; }}
  .bo-card-body .v {{ font-weight: 600; color: #1f1f1f;
    max-width: 110px; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }}
  /* tooltip */
  #bo-tip {{ position: fixed; display: none; z-index: 999;
    background: #2b2b2b; color: #ffffff; padding: 8px 12px;
    border-radius: 6px; font-size: 12px; line-height: 1.6;
    max-width: 320px; pointer-events: none; }}
</style>
</head>
<body>
<div id="toolbar">
  <button id="btn-fit">⤢ 适应画布</button>
  <button id="btn-layout">↻ 重新布局</button>
  <button id="btn-png">⬇ 导出 PNG</button>
  <span style="margin-left:auto; font-size:12px; color:#7a7a7a;
    align-self:center;">拖拽节点 · 滚轮缩放 · 空白处平移</span>
</div>
<div id="bo-graph"></div>
<div id="bo-tip"></div>

<script src="https://cdn.jsdelivr.net/npm/@antv/x6@2.18.1/dist/index.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js"></script>
<script>
const DATA = {graph_data};

// ---- Dagre 自动分层布局（左到右） ----
function computeLayout() {{
  const g = new dagre.graphlib.Graph();
  g.setGraph({{ rankdir: "LR", nodesep: 60, ranksep: 110,
    marginx: 40, marginy: 40 }});
  g.setDefaultEdgeLabel(() => ({{}}));
  DATA.nodes.forEach(n => g.setNode(n.id, {{ width: 180, height: 108 }}));
  DATA.edges.forEach(e => g.setEdge(e.source, e.target));
  dagre.layout(g);
  return g;
}}

// ---- X6 画布 ----
// 注意：Streamlit iframe 加载时容器 CSS 尺寸可能尚未生效（clientWidth/Height 为 0），
// X6 会把 0 尺寸写成 inline style 导致画布永久不可见。
// 因此构造时显式指定 width/height，并在初始化后强制恢复容器尺寸。
const boContainer = document.getElementById("bo-graph");
boContainer.style.width = "100%";
boContainer.style.height = "660px";
const graph = new X6.Graph({{
  container: boContainer,
  width: boContainer.clientWidth || 900,
  height: 660,
  grid: {{ size: 10, visible: true,
    args: {{ color: "#e8e8e8", thickness: 1 }} }},
  interacting: true,
  panning: true,
  mousewheel: {{ enabled: true, modifiers: [], minScale: 0.2, maxScale: 4 }},
  connecting: {{ snap: false }},
}});
// iframe 尺寸就绪后重设画布尺寸（覆盖 X6 写入的 0x0 inline style）
setTimeout(() => {{
  const w = boContainer.clientWidth || 900;
  graph.resize(w, 660);
}}, 100);

// ---- 节点：HTML 卡片（实体名 + 统计） ----
function fontColor(bg) {{
  const v = bg.replace("#", "");
  const r = parseInt(v.substr(0, 2), 16),
        g = parseInt(v.substr(2, 2), 16),
        b = parseInt(v.substr(4, 2), 16);
  return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? "#1a1a1a" : "#ffffff";
}}

function esc(s) {{
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}}

// X6 2.x UMD 的 html shape 必须先经 HTML.register 注册
// （renderHTMLComponent 从 shapeMaps[shape] 读取 html，节点级 html 属性不生效）
X6.Shape.HTML.register({{
  shape: "bo-card",
  width: 180,
  height: 108,
  html: (cell) => {{
    const n = cell.getData();
    const div = document.createElement("div");
    div.className = "bo-card";
    div.innerHTML =
      '<div class="bo-card-head" style="background:' + n.color +
      ';color:' + fontColor(n.color) + ';">' + esc(n.label) + "</div>" +
      '<div class="bo-card-body">' +
      '<div class="row"><span class="k">实例数</span>' +
      '<span class="v">' + n.count + "</span></div>" +
      '<div class="row"><span class="k">主键</span>' +
      '<span class="v" title="' + esc(n.primary_key) + '">' +
      esc(n.primary_key) + "</span></div>" +
      '<div class="row"><span class="k">属性数</span>' +
      '<span class="v">' + n.prop_count + "</span></div>" +
      "</div>";
    return div;
  }},
}});

const layout = computeLayout();
DATA.nodes.forEach(n => {{
  const pos = layout.node(n.id);
  graph.addNode({{
    id: n.id,
    shape: "bo-card",
    x: pos.x - 90,
    y: pos.y - 54,
    width: 180,
    height: 108,
    data: n,
  }});
}});

// ---- 边：正交路由 + 关系名标签 + 箭头 ----
DATA.edges.forEach(e => {{
  graph.addEdge({{
    source: {{ cell: e.source }},
    target: {{ cell: e.target }},
    shape: "edge",
    attrs: {{
      line: {{ stroke: "#4d4d4d", strokeWidth: 2,
        targetMarker: {{ name: "block", size: 8 }} }},
    }},
    router: {{ name: "orth" }},
    connector: {{ name: "rounded" }},
    labels: [{{
      attrs: {{ label: {{ text: e.label, fill: "#4d4d4d",
        fontSize: 12, fontWeight: 600,
        textWrap: {{ width: 140, height: 32, ellipsis: true }} }},
        rect: {{ fill: "#f5f5f5", stroke: "#d6d6d6", strokeWidth: 1,
          rx: 4, ry: 4 }} }},
      position: {{ distance: 0.5, offset: 0 }},
    }}],
    data: e,
  }});
}});

// ---- tooltip（hover 显示完整描述） ----
const tip = document.getElementById("bo-tip");
graph.on("node:mouseenter", ({{ node }}) => {{
  const d = node.getData();
  tip.innerHTML = "<b>" + esc(d.label) + "</b><br>" +
    "实例数：" + d.count + " · 主键：" + esc(d.primary_key) +
    " · 属性数：" + d.prop_count +
    (d.description ? "<br>" + esc(d.description) : "");
  tip.style.display = "block";
}});
graph.on("node:mousemove", ({{ e }}) => {{
  tip.style.left = e.clientX + 14 + "px";
  tip.style.top = e.clientY + 14 + "px";
}});
graph.on("node:mouseleave", () => {{ tip.style.display = "none"; }});
graph.on("edge:mouseenter", ({{ edge }}) => {{
  const d = edge.getData();
  tip.innerHTML = "<b>" + esc(d.label) + "</b><br>" + esc(d.description);
  tip.style.display = "block";
}});
graph.on("edge:mouseleave", () => {{ tip.style.display = "none"; }});

// ---- 工具栏 ----
document.getElementById("btn-fit").addEventListener("click", () => {{
  graph.zoomToFit({{ padding: 40, maxScale: 2 }});
}});
document.getElementById("btn-layout").addEventListener("click", () => {{
  const g2 = computeLayout();
  DATA.nodes.forEach(n => {{
    const pos = g2.node(n.id);
    const cell = graph.getCellById(n.id);
    if (cell) {{
      cell.position(pos.x - 90, pos.y - 54, {{ transition: {{
        duration: 300, timing: "linear" }} }});
    }}
  }});
}});
document.getElementById("btn-png").addEventListener("click", () => {{
  graph.toPNG((dataUri) => {{
    const a = document.createElement("a");
    a.href = dataUri;
    a.download = "business_object_graph.png";
    a.click();
  }}, {{ padding: 20, backgroundColor: "#f5f5f5" }});
}});

// 初始适应画布
setTimeout(() => graph.zoomToFit({{ padding: 40, maxScale: 1.5 }}), 200);
</script>
</body>
</html>"""


def create_instance_graph(
    ontology: Ontology,
    selected_types: List[str],
    max_nodes: int = 500,
    search_text: str = "",
    focus_node: Optional[str] = None,
    hops: int = 1,
    physics: bool = True,
) -> Tuple[str, int, int, bool]:
    """构建实例子图；返回 HTML、节点数、边数、是否发生节点截断。"""
    graph = ontology.links
    allowed_types = set(selected_types)
    candidates: List[str] = []

    search_text = search_text.strip().lower()
    for node_id, attrs in graph.nodes(data=True):
        node_type = attrs.get("type")
        if node_type not in allowed_types:
            continue
        if search_text:
            searchable = " ".join(
                [node_id] + [str(v) for v in attrs.values()]
            ).lower()
            if search_text not in searchable:
                continue
        candidates.append(node_id)

    truncated = len(candidates) > max_nodes
    chosen = set(candidates[:max_nodes])

    # 若指定焦点节点，则以焦点为中心展开一跳或多跳邻居
    if focus_node and focus_node in graph:
        neighborhood = {focus_node}
        frontier = {focus_node}
        for _ in range(max(1, hops)):
            next_frontier = set()
            for node in frontier:
                next_frontier.update(graph.predecessors(node))
                next_frontier.update(graph.successors(node))
            next_frontier -= neighborhood
            neighborhood.update(next_frontier)
            frontier = next_frontier

        neighborhood = {
            n for n in neighborhood
            if graph.nodes[n].get("type") in allowed_types
        }
        chosen = neighborhood
        if len(chosen) > max_nodes:
            chosen = set(list(chosen)[:max_nodes])
            truncated = True

    net = Network(
        height="740px", width="100%", directed=True,
        notebook=False, bgcolor="#f5f5f5", font_color="#1f1f1f",
        cdn_resources="in_line",
    )

    if physics:
        net.barnes_hut(
            gravity=-4500, central_gravity=0.25,
            spring_length=150, spring_strength=0.04, damping=0.12,
        )
    else:
        net.toggle_physics(False)

    type_color = {
        t: TYPE_COLORS[i % len(TYPE_COLORS)]
        for i, t in enumerate(ontology.object_types)
    }

    for node_id in chosen:
        attrs = graph.nodes[node_id]
        node_type = attrs.get("type", "Unknown")
        obj = ontology.objects.get(node_type, {}).get(attrs.get("pk"), {})
        attr_html = "<br>".join(
            f"<b>{html.escape(str(k))}</b>: "
            f"{html.escape(str(v))}"
            for k, v in obj.items() if v is not None
        )
        title = (
            f"<b>类型：</b>{html.escape(str(node_type))}<br>"
            f"<b>节点：</b>{html.escape(node_id)}<br>{attr_html}"
        )
        node_bg = type_color.get(node_type, "#919191")
        net.add_node(
            node_id,
            label=f"{node_type}\\n{attrs.get('pk', '')}",
            title=title,
            shape="dot",
            size=24,
            color={
                "background": node_bg,
                "border": "#1f1f1f",
                "highlight": {"background": "#1f1f1f", "border": "#1f1f1f"},
            },
            borderWidth=2,
            font={"color": font_color_for(node_bg), "size": 13,
                  "strokeWidth": 3, "strokeColor": "#f5f5f5"},
        )

    for source, target, key, attrs in graph.edges(keys=True, data=True):
        if source not in chosen or target not in chosen:
            continue
        relation = attrs.get("relation_type", key)
        net.add_edge(
            source, target,
            label=str(relation),
            title=(
                f"<b>关系：</b>{html.escape(str(relation))}<br>"
                f"<b>起点：</b>{html.escape(source)}<br>"
                f"<b>终点：</b>{html.escape(target)}<br>"
                f"<b>描述：</b>{html.escape(str(attrs.get('description', '')))}"
            ),
            arrows="to",
            color={"color": "#919191", "highlight": "#1f1f1f",
                   "hover": "#4d4d4d"},
            width=2,
            font={"color": "#4d4d4d", "size": 11,
                  "strokeWidth": 2, "strokeColor": "#f5f5f5"},
        )

    options = """
    {
      "interaction": {"hover": true, "navigationButtons": true,
        "keyboard": true, "multiselect": true, "tooltipDelay": 100,
        "hideEdgesOnDrag": true},
      "nodes": {"shadow": true},
      "edges": {"selectionWidth": 3, "hoverWidth": 2,
        "smooth": {"enabled": true, "type": "dynamic"}},
      "physics": {"enabled": PHYSICS,
        "stabilization": {"enabled": true, "iterations": 350}}
    }
    """.replace("PHYSICS", "true" if physics else "false")
    net.set_options(options)

    return pyvis_html(net), len(chosen), net.num_edges(), truncated


# ---------------------------------------------------------------------
# 关系分析与质量检查
# ---------------------------------------------------------------------
def find_shortest_path(
    ontology: Ontology, source: str, target: str
) -> Optional[List[str]]:
    graph = nx.DiGraph(ontology.links)
    try:
        return nx.shortest_path(graph, source=source, target=target)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        # 业务探索时也尝试无向路径
        try:
            return nx.shortest_path(graph.to_undirected(), source, target)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None


def quality_report(ontology: Ontology) -> Dict[str, Any]:
    graph = ontology.links
    isolated = list(nx.isolates(graph))
    duplicate_edges = []
    edge_seen = set()

    for source, target, key in graph.edges(keys=True):
        signature = (source, target, key)
        if signature in edge_seen:
            duplicate_edges.append(signature)
        edge_seen.add(signature)

    dangling: List[Dict[str, str]] = []
    for relation_name, info in ontology.link_types.items():
        source_type = info["source"]
        target_type = info["target"]
        source_col = info.get("source_property")
        if not source_col or source_type not in ontology.objects:
            continue

        target_keys = set(ontology.objects.get(target_type, {}).keys())
        for source_pk, obj in ontology.objects[source_type].items():
            value = obj.get(source_col)
            if value is not None and str(value) not in target_keys:
                dangling.append({
                    "relation": relation_name,
                    "source": f"{source_type}:{source_pk}",
                    "target_value": str(value),
                })

    invalid_relation_types = [
        name for name, info in ontology.link_types.items()
        if info["source"] not in ontology.object_types
        or info["target"] not in ontology.object_types
    ]

    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "isolated_nodes": isolated,
        "dangling_references": dangling,
        "invalid_relation_types": invalid_relation_types,
        "self_loops": list(nx.selfloop_edges(graph, keys=True)),
        "duplicate_edges": duplicate_edges,
    }


# ---------------------------------------------------------------------
# LLM 问答（Tool Calling 优先 + 检索/抽样降级）
# ---------------------------------------------------------------------
ONTOLOGY_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_objects",
            "description": (
                "按关键词搜索实体实例。可限定类型。"
                "用于查找员工、部门、订单等业务对象。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，如主键、姓名、编码",
                    },
                    "type_name": {
                        "type": "string",
                        "description": "可选，限定实体类型名称",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回条数，默认 20，最大 50",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_object",
            "description": "按类型与主键精确获取单个实体的全部属性。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type_name": {
                        "type": "string",
                        "description": "实体类型名称",
                    },
                    "primary_key": {
                        "type": "string",
                        "description": "主键值",
                    },
                },
                "required": ["type_name", "primary_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_neighbors",
            "description": (
                "查询实体的直接关联邻居（入边与出边）。"
                "node_id 格式为 Type:pk，例如 Employee:1001。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "节点 ID，格式 Type:pk",
                    },
                    "type_name": {
                        "type": "string",
                        "description": "若未给 node_id，可用类型+主键",
                    },
                    "primary_key": {
                        "type": "string",
                        "description": "若未给 node_id，可用类型+主键",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回邻居数，默认 30",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_path",
            "description": (
                "查询两个实体之间的最短关系路径。"
                "节点 ID 格式 Type:pk。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_node": {
                        "type": "string",
                        "description": "起点节点 ID，如 Employee:1001",
                    },
                    "target_node": {
                        "type": "string",
                        "description": "终点节点 ID，如 Department:D1",
                    },
                },
                "required": ["source_node", "target_node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_objects",
            "description": (
                "统计实体数量。可按类型统计，或按属性值过滤后计数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type_name": {
                        "type": "string",
                        "description": "实体类型；省略则返回各类型计数",
                    },
                    "property_name": {
                        "type": "string",
                        "description": "可选过滤属性名",
                    },
                    "property_value": {
                        "type": "string",
                        "description": "可选过滤属性值（字符串匹配）",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_link_types",
            "description": "列出本体中已定义的关系类型及其源/目标实体类型。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


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
def read_uploaded_file(uploaded_file: Any) -> pd.DataFrame:
    filename = uploaded_file.name.lower()
    uploaded_file.seek(0)
    if filename.endswith(".csv"):
        try:
            return pd.read_csv(uploaded_file)
        except UnicodeDecodeError:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, encoding="gb18030")
    if filename.endswith((".xlsx", ".xls")):
        return pd.read_excel(uploaded_file)
    raise ValueError(f"不支持的文件格式：{uploaded_file.name}")


# ---------------------------------------------------------------------
# PostgreSQL 数据源
# ---------------------------------------------------------------------
def _pg_dependency_error() -> Optional[str]:
    """检查 SQLAlchemy / psycopg2 是否可用。"""
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        return (
            "缺少 sqlalchemy 依赖，请执行：pip install sqlalchemy psycopg2-binary"
        )
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return (
            "缺少 psycopg2 依赖，请执行：pip install psycopg2-binary"
        )
    return None


def build_pg_engine(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    connect_timeout: int = 8,
):
    """创建 SQLAlchemy Engine（PostgreSQL）。"""
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus

    host = (host or "").strip() or "localhost"
    database = (database or "").strip()
    user = (user or "").strip()
    if not database:
        raise ValueError("数据库名不能为空")
    if not user:
        raise ValueError("用户名不能为空")

    url = (
        f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password or '')}"
        f"@{host}:{int(port)}/{quote_plus(database)}"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": int(connect_timeout)},
    )


def list_pg_schemas(engine) -> List[str]:
    """列出可用 schema（排除系统 schema）。"""
    from sqlalchemy import text

    sql = text(
        """
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND schema_name NOT LIKE 'pg_temp_%'
          AND schema_name NOT LIKE 'pg_toast_temp_%'
        ORDER BY schema_name
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [str(r[0]) for r in rows]


def list_pg_tables(engine, schema: str = "public") -> List[Dict[str, Any]]:
    """列出指定 schema 下的基表（含估算行数，若可用）。"""
    from sqlalchemy import text

    schema = (schema or "public").strip() or "public"
    sql = text(
        """
        SELECT
            t.table_name,
            COALESCE(s.n_live_tup, 0)::bigint AS approx_rows
        FROM information_schema.tables t
        LEFT JOIN pg_stat_user_tables s
          ON s.schemaname = t.table_schema
         AND s.relname = t.table_name
        WHERE t.table_schema = :schema
          AND t.table_type = 'BASE TABLE'
        ORDER BY t.table_name
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"schema": schema}).fetchall()
    return [
        {"table": str(r[0]), "approx_rows": int(r[1] or 0)}
        for r in rows
    ]


def load_pg_tables(
    engine,
    schema: str,
    table_names: List[str],
    row_limit: Optional[int] = None,
) -> Dict[str, pd.DataFrame]:
    """按表名加载为 DataFrame；表名作为实体类型名。

    row_limit 为 None 或 <=0 时全量加载；否则每表最多加载 row_limit 行。
    使用双引号安全引用标识符，避免 SQL 注入与大小写问题。
    """
    from sqlalchemy import text

    schema = (schema or "public").strip() or "public"
    result: Dict[str, pd.DataFrame] = {}

    def _quote_ident(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    for table in table_names:
        table = str(table).strip()
        if not table:
            continue
        qualified = f"{_quote_ident(schema)}.{_quote_ident(table)}"
        if row_limit is not None and int(row_limit) > 0:
            sql = text(f"SELECT * FROM {qualified} LIMIT :limit")
            params = {"limit": int(row_limit)}
        else:
            sql = text(f"SELECT * FROM {qualified}")
            params = {}
        df = pd.read_sql(sql, engine, params=params)
        # 实体类型名使用表名（不含 schema），与 CSV 文件名逻辑一致
        result[table] = df
    return result


def test_pg_connection(engine) -> str:
    """执行简单查询验证连接，返回服务端版本摘要。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar()
    return str(version or "connected")


# ---------------------------------------------------------------------
# LLM 配置持久化
# ---------------------------------------------------------------------
LLM_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "llm_config.json"
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

# ---- 全局 session_state 初始化（延续 v2 key，避免破坏现有交互） ----
if "ontology_v2" not in st.session_state:
    st.session_state.ontology_v2 = None
if "raw_data_v2" not in st.session_state:
    st.session_state.raw_data_v2 = {}
if "messages_v2" not in st.session_state:
    st.session_state.messages_v2 = []
if "pk_config" not in st.session_state:
    st.session_state.pk_config = {}
if "manual_links_config" not in st.session_state:
    st.session_state.manual_links_config = []
if "restored_version_id" not in st.session_state:
    st.session_state.restored_version_id = None

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
# 页面 1：Pipeline 总览
# ---------------------------------------------------------------------
def render_overview() -> None:
    ontology = st.session_state.ontology_v2

    st.subheader("Pipeline 总览")
    st.caption("数据源 → 本体建模 → 智能问答 → 问题测评 → 版本管理")

    # 五阶段状态流
    stages = [
        ("📥 数据源", bool(st.session_state.raw_data_v2) or ontology is not None),
        ("🏗️ 本体建模", ontology is not None),
        ("💬 智能问答", ontology is not None and bool(api_key)),
        ("🧪 问题测评", len(list_eval_runs()) > 0),
        ("🗂️ 版本管理", len(list_versions()) > 0),
    ]
    cols = st.columns(5)
    for col, (name, ready) in zip(cols, stages):
        col.metric(name, "✅ 就绪" if ready else "⬜ 待完成")

    # 当前版本卡片 + dirty 检测
    pointer = read_current_pointer()
    if pointer and pointer.get("version_id"):
        st.subheader("当前版本")
        version_id = str(pointer["version_id"])
        dirty = False
        if ontology is not None:
            dirty = _ontology_hash(ontology) != pointer.get("ontology_hash")
        st.info(
            f"**{version_id}**"
            + ("　⚠️ 有未保存修改（Action 修改后未创建新版本）" if dirty else "")
        )
    if st.session_state.restored_version_id:
        st.success(
            f"已从版本 {st.session_state.restored_version_id} 自动恢复"
            "（刷新后自动重建）。"
        )
        st.session_state.restored_version_id = None

    # 本体统计
    if ontology is not None:
        st.subheader("本体统计")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("实体类型", len(ontology.object_types))
        m2.metric("实体实例", ontology.links.number_of_nodes())
        m3.metric("关系类型", len(ontology.link_types))
        m4.metric("关系实例", ontology.links.number_of_edges())

    # 最近一次测评指标
    runs = list_eval_runs()
    if runs:
        latest = runs[0]
        metrics = latest.get("metrics") or {}
        st.subheader("最近测评")
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("运行", latest["run_id"])
        e2.metric(
            "答案准确率",
            f"{metrics.get('accuracy', 0):.0%}"
            if metrics.get("total") else "—",
        )
        e3.metric(
            "执行成功率",
            f"{metrics.get('execution_rate', 0):.0%}"
            if metrics.get("total") else "—",
        )
        e4.metric(
            "证据覆盖率",
            f"{metrics.get('evidence_rate', 0):.0%}"
            if metrics.get("total") else "—",
        )
    else:
        st.info("尚无测评运行。请到「问题测评」页创建问题集并运行测评。")


# ---------------------------------------------------------------------
# 页面 2：数据源
# ---------------------------------------------------------------------
def _build_ontology_from_raw(
    raw_data: Dict[str, pd.DataFrame],
    primary_keys: Optional[Dict[str, str]] = None,
    manual_links: Optional[List[Dict[str, Any]]] = None,
) -> "Ontology":
    """从原始表数据构建本体（自动推断主键 + 自动识别外键 + 可选手动关系）。"""
    if not raw_data:
        raise ValueError("没有可建模的数据表")

    # 过滤无列空表，避免主键推断失败
    cleaned: Dict[str, pd.DataFrame] = {}
    for name, df in raw_data.items():
        if df is None:
            continue
        if not isinstance(df, pd.DataFrame):
            continue
        if len(df.columns) == 0:
            continue
        cleaned[str(name)] = df

    if not cleaned:
        raise ValueError("所有数据表均无可用列，无法建模")

    pks: Dict[str, str] = dict(primary_keys or {})
    for table_name, df in cleaned.items():
        if table_name not in pks or pks[table_name] not in df.columns:
            pks[table_name] = AutoOntologyBuilder.infer_primary_key(df, table_name)

    builder = AutoOntologyBuilder()
    return builder.from_dataframes(cleaned, pks, manual_links or [])


def _commit_raw_data(
    preview_data: Dict[str, pd.DataFrame],
    auto_build: bool = True,
    source_kind: str = "file",
) -> Optional[str]:
    """将预览数据写入会话；默认自动生成本体。

    返回错误信息；成功返回 None。
    """
    st.session_state.raw_data_v2 = preview_data or {}
    st.session_state.source_kinds_v2 = {str(name): source_kind for name in (preview_data or {})}
    st.session_state.pop("v5_mapping_preview", None)
    st.session_state.pop("v5_mapping_last_run", None)
    st.session_state.messages_v2 = []
    st.session_state.manual_links_config = []
    st.session_state.pk_config = {}

    if not auto_build:
        st.session_state.ontology_v2 = None
        return None

    try:
        ontology = _build_ontology_from_raw(preview_data)
        st.session_state.ontology_v2 = ontology
        # 把推断出的主键写回配置，便于后续在建模页调整
        for type_name, info in ontology.object_types.items():
            st.session_state.pk_config[type_name] = info["primary_key"]
        return None
    except Exception as exc:
        st.session_state.ontology_v2 = None
        return str(exc)


def render_data_source() -> None:
    st.subheader("📥 数据源管理")
    st.caption(
        "支持 CSV / Excel 多文件导入，或连接 PostgreSQL 选择数据表后自动本体建模。"
    )

    tab_file, tab_pg = st.tabs(["📄 文件上传", "🐘 PostgreSQL"])

    # ---- 文件上传 ----
    with tab_file:
        uploaded_files = st.file_uploader(
            "支持 CSV / Excel，可多选",
            type=["csv", "xlsx", "xls"],
            accept_multiple_files=True,
        )

        if uploaded_files:
            with st.expander("已上传文件预览", expanded=True):
                preview_data: Dict[str, pd.DataFrame] = {}
                load_errors = []
                for file in uploaded_files:
                    table_name = os.path.splitext(file.name)[0]
                    try:
                        preview_data[table_name] = read_uploaded_file(file)
                        st.write(
                            f"**{file.name}**：{len(preview_data[table_name])} 行 × "
                            f"{len(preview_data[table_name].columns)} 列"
                        )
                        st.dataframe(
                            preview_data[table_name].head(5),
                            use_container_width=True,
                        )
                    except Exception as exc:
                        load_errors.append(f"{file.name}：{exc}")

                if load_errors:
                    for error in load_errors:
                        st.error(error)

                if st.button(
                    "读取文件并自动生成本体",
                    type="primary",
                    key="ds_commit_files",
                ):
                    err = _commit_raw_data(preview_data, auto_build=True)
                    if err:
                        st.error(f"自动建模失败：{err}")
                    else:
                        st.success(
                            "已加载数据并自动生成本体，请到「本体建模」查看结果。"
                        )
                        st.rerun()

    # ---- PostgreSQL ----
    with tab_pg:
        dep_error = _pg_dependency_error()
        if dep_error:
            st.warning(dep_error)
        else:
            st.markdown("**连接参数**")
            pg_cfg = st.session_state.setdefault(
                "pg_config",
                {
                    "host": "localhost",
                    "port": 5432,
                    "database": "",
                    "user": "",
                    "password": "",
                    "schema": "public",
                },
            )
            c1, c2, c3 = st.columns(3)
            with c1:
                pg_host = st.text_input(
                    "Host", value=str(pg_cfg.get("host") or "localhost"),
                    key="pg_host",
                )
                pg_user = st.text_input(
                    "用户名", value=str(pg_cfg.get("user") or ""),
                    key="pg_user",
                )
            with c2:
                pg_port = st.number_input(
                    "Port", min_value=1, max_value=65535,
                    value=int(pg_cfg.get("port") or 5432), step=1,
                    key="pg_port",
                )
                pg_password = st.text_input(
                    "密码", value=str(pg_cfg.get("password") or ""),
                    type="password", key="pg_password",
                )
            with c3:
                pg_database = st.text_input(
                    "数据库", value=str(pg_cfg.get("database") or ""),
                    key="pg_database",
                )
                pg_schema = st.text_input(
                    "Schema",
                    value=str(pg_cfg.get("schema") or "public"),
                    key="pg_schema",
                    help="默认为 public；连接成功后也可从下拉列表切换。",
                )

            st.session_state.pg_config = {
                "host": pg_host,
                "port": int(pg_port),
                "database": pg_database,
                "user": pg_user,
                "password": pg_password,
                "schema": pg_schema,
            }

            btn_c1, btn_c2, _ = st.columns([1, 1, 2])
            with btn_c1:
                test_clicked = st.button("🔌 测试连接", key="pg_test_conn")
            with btn_c2:
                list_clicked = st.button(
                    "📋 加载表列表", type="primary", key="pg_list_tables"
                )

            if test_clicked or list_clicked:
                try:
                    engine = build_pg_engine(
                        host=pg_host,
                        port=int(pg_port),
                        database=pg_database,
                        user=pg_user,
                        password=pg_password,
                    )
                    version = test_pg_connection(engine)
                    st.session_state.pg_engine_ok = True
                    st.session_state.pg_last_error = None
                    if test_clicked:
                        st.success(f"连接成功。{version[:120]}")
                    if list_clicked:
                        schemas = list_pg_schemas(engine)
                        st.session_state.pg_schemas = schemas
                        active_schema = (
                            pg_schema if pg_schema in schemas
                            else (schemas[0] if schemas else "public")
                        )
                        tables = list_pg_tables(engine, active_schema)
                        st.session_state.pg_tables = tables
                        st.session_state.pg_active_schema = active_schema
                        st.success(
                            f"Schema `{active_schema}` 下共 {len(tables)} 张基表。"
                        )
                except Exception as exc:
                    st.session_state.pg_engine_ok = False
                    st.session_state.pg_last_error = str(exc)
                    st.session_state.pg_tables = []
                    st.error(f"连接或查询失败：{exc}")

            tables_meta: List[Dict[str, Any]] = list(
                st.session_state.get("pg_tables") or []
            )
            schemas_cached: List[str] = list(
                st.session_state.get("pg_schemas") or []
            )
            if tables_meta or schemas_cached:
                st.markdown("**选择数据表**")
                if schemas_cached:
                    default_schema = st.session_state.get(
                        "pg_active_schema", pg_schema
                    )
                    schema_index = (
                        schemas_cached.index(default_schema)
                        if default_schema in schemas_cached
                        else 0
                    )
                    selected_schema = st.selectbox(
                        "Schema",
                        schemas_cached,
                        index=schema_index,
                        key="pg_schema_select",
                    )
                    if selected_schema != st.session_state.get("pg_active_schema"):
                        try:
                            engine = build_pg_engine(
                                host=pg_host,
                                port=int(pg_port),
                                database=pg_database,
                                user=pg_user,
                                password=pg_password,
                            )
                            tables_meta = list_pg_tables(engine, selected_schema)
                            st.session_state.pg_tables = tables_meta
                            st.session_state.pg_active_schema = selected_schema
                            st.session_state.pg_config["schema"] = selected_schema
                        except Exception as exc:
                            st.error(f"切换 Schema 失败：{exc}")
                            tables_meta = []

                table_names = [t["table"] for t in tables_meta]
                if table_names:
                    st.dataframe(
                        pd.DataFrame(tables_meta).rename(
                            columns={
                                "table": "表名",
                                "approx_rows": "约行数 (pg_stat)",
                            }
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )
                    selected_tables = st.multiselect(
                        "要导入的表（将作为实体类型）",
                        options=table_names,
                        default=[],
                        key="pg_selected_tables",
                        help="可多选；表名将作为本体中的 Object Type 名称。",
                    )
                    row_limit = st.number_input(
                        "每表最大行数（0 = 全量）",
                        min_value=0,
                        max_value=5_000_000,
                        value=50_000,
                        step=1000,
                        key="pg_row_limit",
                        help=(
                            "大数据表建议先限行验证建模；确认后再全量导入。"
                            "全量导入可能占用较多内存。"
                        ),
                    )

                    if selected_tables and st.button(
                        "⬇️ 加载选中表并自动生成本体",
                        type="primary",
                        key="pg_load_tables",
                    ):
                        try:
                            with st.spinner(
                                f"正在从 PostgreSQL 加载 {len(selected_tables)} 张表并建模…"
                            ):
                                engine = build_pg_engine(
                                    host=pg_host,
                                    port=int(pg_port),
                                    database=pg_database,
                                    user=pg_user,
                                    password=pg_password,
                                )
                                active_schema = st.session_state.get(
                                    "pg_active_schema", pg_schema
                                )
                                limit = (
                                    int(row_limit) if int(row_limit) > 0 else None
                                )
                                preview_data = load_pg_tables(
                                    engine,
                                    schema=str(active_schema),
                                    table_names=selected_tables,
                                    row_limit=limit,
                                )
                            if not preview_data:
                                st.warning("未加载到任何数据，请检查表选择。")
                            else:
                                with st.expander("加载结果预览", expanded=True):
                                    for name, df in preview_data.items():
                                        st.write(
                                            f"**{name}**：{len(df)} 行 × "
                                            f"{len(df.columns)} 列"
                                        )
                                        st.dataframe(
                                            df.head(5),
                                            use_container_width=True,
                                        )
                                err = _commit_raw_data(
                                    preview_data, auto_build=True, source_kind="postgresql"
                                )
                                if err:
                                    st.error(f"数据已加载，但自动建模失败：{err}")
                                    st.info(
                                        "请到「本体建模」页手动配置主键后重新生成。"
                                    )
                                else:
                                    ont = st.session_state.ontology_v2
                                    st.success(
                                        f"已加载 {len(preview_data)} 张表并自动生成本体："
                                        f"{len(ont.object_types)} 个类型 · "
                                        f"{ont.links.number_of_nodes()} 个实例 · "
                                        f"{ont.links.number_of_edges()} 条关系。"
                                        "请到「本体建模」查看详情。"
                                    )
                                st.rerun()
                        except Exception as exc:
                            st.error(f"加载表失败：{exc}")
                else:
                    st.info("当前 Schema 下没有基表。")

    if st.session_state.ontology_v2 is not None:
        ont = st.session_state.ontology_v2
        st.success(
            f"当前本体已就绪：{len(ont.object_types)} 个实体类型 · "
            f"{ont.links.number_of_nodes()} 个实例 · "
            f"{len(ont.link_types)} 种关系 · "
            f"{ont.links.number_of_edges()} 条关系边。"
            "请到「本体建模」查看图谱与详情。"
        )
    elif st.session_state.raw_data_v2:
        st.warning(
            f"已读取 {len(st.session_state.raw_data_v2)} 个数据表，"
            "但尚未生成有效本体。请到「本体建模」配置主键后重新生成。"
        )


# ---------------------------------------------------------------------
# 页面 3：本体建模（建模配置 + 生成后的 7 个子 tab）
# ---------------------------------------------------------------------
def _render_modeling_config(
    raw_data: Dict[str, pd.DataFrame],
    *,
    expanded: bool = True,
) -> None:
    """建模配置：主键网格 + 手动关系（配置存普通 key，防跨页丢失）。"""
    with st.expander("⚙️ 数据建模配置（主键 / 手动关系）", expanded=expanded):
        # 原始表摘要，避免“像没加载成功”
        st.markdown("**已加载数据表**")
        summary_rows = []
        for table_name, df in raw_data.items():
            summary_rows.append({
                "表名": table_name,
                "行数": int(len(df)),
                "列数": int(len(df.columns)),
                "列": ", ".join(str(c) for c in list(df.columns)[:12])
                + ("…" if len(df.columns) > 12 else ""),
            })
        if summary_rows:
            st.dataframe(
                pd.DataFrame(summary_rows),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.warning("原始数据为空。")

        st.markdown("**主键配置**")
        usable = {
            name: df for name, df in raw_data.items()
            if isinstance(df, pd.DataFrame) and len(df.columns) > 0
        }
        if not usable:
            st.error("没有包含列的数据表，无法配置主键。")
            return

        pk_columns = st.columns(min(3, max(1, len(usable))))
        for index, (table_name, df) in enumerate(usable.items()):
            with pk_columns[index % len(pk_columns)]:
                inferred = AutoOntologyBuilder.infer_primary_key(df, table_name)
                options = [str(c) for c in df.columns]
                saved = st.session_state.pk_config.get(table_name)
                if saved is not None:
                    saved = str(saved)
                default_index = (
                    options.index(saved) if saved in options
                    else options.index(str(inferred)) if str(inferred) in options
                    else 0
                )
                selected = st.selectbox(
                    f"{table_name} 主键",
                    options=options,
                    index=default_index,
                    key=f"v2_pk_{table_name}",
                )
                st.session_state.pk_config[table_name] = selected

        st.markdown("**手动关系配置（可选）**")
        relation_count = st.number_input(
            "手动关系数量", min_value=0, max_value=20, value=0, step=1,
            key="v2_manual_rel_count",
        )
        table_names = list(usable.keys())
        manual_links: List[Dict[str, Any]] = []

        for index in range(int(relation_count)):
            with st.expander(f"手动关系 {index + 1}", expanded=False):
                c1, c2 = st.columns(2)
                with c1:
                    relation_name = st.text_input(
                        "关系名称", value=f"manual_relation_{index + 1}",
                        key=f"v2_rel_name_{index}",
                    )
                    source_table = st.selectbox(
                        "源实体类型", table_names, key=f"v2_rel_src_{index}"
                    )
                    source_col = st.selectbox(
                        "源表关联列",
                        [str(c) for c in usable[source_table].columns],
                        key=f"v2_rel_col_{index}",
                    )
                with c2:
                    target_table = st.selectbox(
                        "目标实体类型", table_names, key=f"v2_rel_tgt_{index}"
                    )
                    relation_desc = st.text_input(
                        "关系描述", value="手动指定",
                        key=f"v2_rel_desc_{index}",
                    )
                manual_links.append({
                    "name": relation_name.strip() or f"manual_relation_{index + 1}",
                    "source": source_table,
                    "target": target_table,
                    "source_col": source_col,
                    "description": relation_desc,
                })
        st.session_state.manual_links_config = manual_links

        if st.button("🚀 确认并生成本体", type="primary", key="v2_build_ontology"):
            try:
                with st.spinner("正在构建本体与识别关系..."):
                    ontology = _build_ontology_from_raw(
                        usable,
                        primary_keys=dict(st.session_state.pk_config),
                        manual_links=manual_links,
                    )
                    st.session_state.ontology_v2 = ontology
                st.success(
                    f"本体生成成功：{len(ontology.object_types)} 类型 · "
                    f"{ontology.links.number_of_nodes()} 实例 · "
                    f"{ontology.links.number_of_edges()} 关系。"
                )
                st.rerun()
            except Exception as exc:
                st.exception(exc)


def render_modeling() -> None:
    ontology = st.session_state.ontology_v2
    raw_data = st.session_state.raw_data_v2 or {}

    # 有原始数据时始终可配置/重建；无本体时默认展开配置
    if raw_data:
        _render_modeling_config(raw_data, expanded=(ontology is None))

    if ontology is None:
        if raw_data:
            st.info(
                "请在上方确认主键（及可选手动关系）后点击「确认并生成本体」。"
                "从数据源加载时会尝试自动建模；若自动建模失败，可在此手动生成。"
            )
        else:
            st.info(
                "请先到「数据源」页上传 CSV / Excel，或连接 PostgreSQL 加载数据表。"
            )
        return

    # ---- 已有本体：展示结果 ----
    st.subheader("本体建模结果")
    st.caption(
        "以下为当前会话中的本体摘要、图谱与质量检测。"
        "若需调整主键或补充手动关系，可在上方配置区修改后重新生成。"
    )

    tabs = st.tabs([
        "📊 概览", "🔗 业务对象关系图", "🏗️ Schema 图谱", "🕸️ 实例图谱",
        "🎨 高级可视化", "🔎 关系分析", "🧪 质量检测", "⚡ Action", "📤 导出",
    ])

    # 概览
    with tabs[0]:
        st.markdown(ontology.summary())
        metric_cols = st.columns(4)
        metric_cols[0].metric("实体类型", len(ontology.object_types))
        metric_cols[1].metric("实体实例", ontology.links.number_of_nodes())
        metric_cols[2].metric("关系类型", len(ontology.link_types))
        metric_cols[3].metric("关系实例", ontology.links.number_of_edges())

        for type_name in ontology.object_types:
            objects = ontology.objects.get(type_name, {})
            with st.expander(f"{type_name}（{len(objects)} 条）"):
                if objects:
                    st.dataframe(
                        pd.DataFrame(list(objects.values())),
                        use_container_width=True,
                    )
                else:
                    st.info("当前类型没有有效实例。")

        if ontology.link_types:
            st.subheader("关系定义")
            st.dataframe(
                pd.DataFrame([
                    {"关系": name, **info}
                    for name, info in ontology.link_types.items()
                ]),
                use_container_width=True,
            )

    # 业务对象关系图（AntV X6 + Dagre）
    with tabs[1]:
        st.caption(
            "业务对象关系图：实体（含实例数 / 主键 / 属性数）与关系，"
            "Dagre 自动分层布局，支持拖拽 / 缩放 / 平移 / 导出 PNG。"
        )
        if ontology.object_types:
            st.components.v1.html(
                create_business_object_graph(ontology),
                height=740,
                scrolling=True,
            )
        else:
            st.warning("暂无本体结构。")

    # Schema 图谱
    with tabs[2]:
        st.info(
            "展示实体类型 Class、属性 Property，以及实体类型之间的 "
            "Object Property。"
        )
        if ontology.object_types:
            st.components.v1.html(
                create_schema_graph(ontology),
                height=740,
                scrolling=True,
            )
        else:
            st.warning("暂无本体结构。")

    # 实例图谱
    with tabs[3]:
        st.caption("按实体类型、关键词和节点数量筛选业务实例关系图。")
        all_types = list(ontology.object_types.keys())
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            selected_types = st.multiselect(
                "实体类型过滤",
                all_types,
                default=all_types,
                key="v2_graph_types",
            )
        with c2:
            max_nodes = st.number_input(
                "最大节点数", min_value=10, max_value=10000,
                value=500, step=50,
            )
        with c3:
            physics = st.checkbox("动态布局", value=True)

        search_text = st.text_input(
            "实体搜索（支持主键或属性关键词）",
            key="v2_graph_search",
        )

        focus_candidates = [
            node for node, attrs in ontology.links.nodes(data=True)
            if attrs.get("type") in selected_types
        ]
        focus_options = ["不指定焦点"] + focus_candidates
        focus_node = st.selectbox(
            "焦点实体（可选，展开邻居）",
            focus_options,
            key="v2_focus_node",
        )
        hops = st.radio(
            "邻居展开层数", [1, 2, 3], horizontal=True, index=0
        )

        if selected_types:
            graph_html, shown_nodes, shown_edges, truncated = create_instance_graph(
                ontology,
                selected_types=selected_types,
                max_nodes=int(max_nodes),
                search_text=search_text,
                focus_node=None if focus_node == "不指定焦点" else focus_node,
                hops=int(hops),
                physics=physics,
            )
            m1, m2, m3 = st.columns(3)
            m1.metric("当前显示节点", shown_nodes)
            m2.metric("当前显示关系", shown_edges)
            m3.metric("全量节点", ontology.links.number_of_nodes())

            if truncated:
                st.warning(
                    "当前图谱节点超过显示上限，已截断部分节点。"
                    "可提高最大节点数或缩小筛选范围。"
                )
            st.components.v1.html(graph_html, height=760, scrolling=True)
        else:
            st.warning("请至少选择一种实体类型。")

    # 高级可视化（Ontosphere 交互 + 统一控制台）
    with tabs[4]:
        st.subheader("高级可视化交互")
        st.caption(
            "本地图谱（业务对象 / Schema / 实例）适合快速浏览；"
            "Ontosphere 提供专业画布、完整 OWL 2 DL 推理与 SHACL 验证。"
        )

        viz_mode = st.radio(
            "可视化模式",
            ["本地交互增强", "结构编辑回写 V5", "Ontosphere 专业画布"],
            horizontal=True,
            key="v2_viz_mode",
        )

        if viz_mode == "结构编辑回写 V5":
            from ontology_domain.canvas_edit import CanvasEditService

            if "v5_schema" not in st.session_state:
                st.session_state.v5_schema = None
            if "canvas_edit_log" not in st.session_state:
                st.session_state.canvas_edit_log = []

            service = CanvasEditService(
                ontology,
                schema=st.session_state.v5_schema,
                auto_v5=True,
            )
            st.session_state.v5_schema = service.get_v5_schema()

            st.markdown("#### 白名单结构编辑（同步 Legacy 运行时 + V5 Schema）")
            st.caption(
                "仅允许 ObjectType / Property / Relationship 的增删改。"
                "删除有依赖时需勾选「强制」。变更立即反映到图谱与 V5 模型。"
            )

            summary = service.schema_summary()
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("V5 ObjectTypes", summary.get("object_types", 0))
            m2.metric("V5 Properties", summary.get("properties", 0))
            m3.metric("V5 Relationships", summary.get("relationships", 0))
            m4.metric("V5 version", summary.get("version", "-"))
            if summary.get("validation_errors"):
                st.warning("V5 校验问题：" + "; ".join(summary["validation_errors"][:5]))

            edit_kind = st.selectbox(
                "编辑对象",
                ["ObjectType", "Property", "Relationship"],
                key="v2_edit_kind",
            )
            action = st.radio(
                "操作", ["新增", "更新", "删除"], horizontal=True, key="v2_edit_action"
            )
            force = st.checkbox("强制删除（级联清理依赖）", value=False, key="v2_edit_force")
            result_box = st.empty()

            if edit_kind == "ObjectType":
                if action == "新增":
                    n = st.text_input("类型名称", key="v2_ot_name")
                    pk = st.text_input("主键字段", value="id", key="v2_ot_pk")
                    props_raw = st.text_input(
                        "属性列表（逗号分隔）", value="id,name", key="v2_ot_props"
                    )
                    desc = st.text_area("描述", key="v2_ot_desc", height=60)
                    if st.button("执行新增 ObjectType", type="primary", key="v2_ot_add"):
                        props = [p.strip() for p in props_raw.split(",") if p.strip()]
                        r = service.add_object_type(
                            n, primary_key=pk or "id", properties=props, description=desc
                        )
                        st.session_state.v5_schema = service.get_v5_schema()
                        st.session_state.canvas_edit_log = service.get_edit_log()
                        (result_box.success if r.ok else result_box.error)(r.message)
                        if r.ok:
                            st.rerun()
                elif action == "更新":
                    names = list(ontology.object_types.keys())
                    if not names:
                        st.info("暂无类型")
                    else:
                        n = st.selectbox("选择类型", names, key="v2_ot_upd_name")
                        label = st.text_input("显示标签", value=n, key="v2_ot_upd_label")
                        desc = st.text_area(
                            "描述",
                            value=str(ontology.object_types[n].get("description") or ""),
                            key="v2_ot_upd_desc",
                            height=60,
                        )
                        if st.button("执行更新 ObjectType", type="primary", key="v2_ot_upd"):
                            r = service.update_object_type(n, label=label, description=desc)
                            st.session_state.v5_schema = service.get_v5_schema()
                            st.session_state.canvas_edit_log = service.get_edit_log()
                            (result_box.success if r.ok else result_box.error)(r.message)
                            if r.ok:
                                st.rerun()
                else:
                    names = list(ontology.object_types.keys())
                    if not names:
                        st.info("暂无类型")
                    else:
                        n = st.selectbox("选择要删除的类型", names, key="v2_ot_del_name")
                        if st.button("执行删除 ObjectType", type="primary", key="v2_ot_del"):
                            r = service.delete_object_type(n, force=force)
                            st.session_state.v5_schema = service.get_v5_schema()
                            st.session_state.canvas_edit_log = service.get_edit_log()
                            (result_box.success if r.ok else result_box.error)(r.message)
                            for w in r.warnings:
                                st.warning(w)
                            if r.ok:
                                st.rerun()

            elif edit_kind == "Property":
                type_names = list(ontology.object_types.keys())
                if not type_names:
                    st.info("请先创建 ObjectType")
                elif action == "新增":
                    tn = st.selectbox("所属类型", type_names, key="v2_prop_type")
                    pn = st.text_input("属性名", key="v2_prop_name")
                    dt = st.selectbox(
                        "数据类型",
                        ["string", "integer", "number", "boolean", "date", "datetime", "json"],
                        key="v2_prop_dt",
                    )
                    req = st.checkbox("必填", value=False, key="v2_prop_req")
                    if st.button("执行新增属性", type="primary", key="v2_prop_add"):
                        r = service.add_property(tn, pn, data_type=dt, required=req)
                        st.session_state.v5_schema = service.get_v5_schema()
                        st.session_state.canvas_edit_log = service.get_edit_log()
                        (result_box.success if r.ok else result_box.error)(r.message)
                        if r.ok:
                            st.rerun()
                elif action == "删除":
                    tn = st.selectbox("所属类型", type_names, key="v2_prop_del_type")
                    props = list(ontology.object_types[tn].get("properties") or [])
                    if not props:
                        st.info("该类型暂无属性")
                    else:
                        pn = st.selectbox("属性", props, key="v2_prop_del_name")
                        if st.button("执行删除属性", type="primary", key="v2_prop_del"):
                            r = service.delete_property(tn, pn, force=force)
                            st.session_state.v5_schema = service.get_v5_schema()
                            st.session_state.canvas_edit_log = service.get_edit_log()
                            (result_box.success if r.ok else result_box.error)(r.message)
                            if r.ok:
                                st.rerun()
                else:
                    st.info("属性更新请使用「删除后重建」或后续字段编辑器。")

            else:
                type_names = list(ontology.object_types.keys())
                if action == "新增":
                    if not type_names:
                        st.info("请先创建 ObjectType")
                    else:
                        rn = st.text_input("关系名称", key="v2_rel_name")
                        src = st.selectbox("源类型", type_names, key="v2_rel_src")
                        tgt = st.selectbox("目标类型", type_names, key="v2_rel_tgt")
                        desc = st.text_input("描述", key="v2_rel_desc")
                        if st.button("执行新增关系", type="primary", key="v2_rel_add"):
                            r = service.add_relationship(rn, src, tgt, description=desc)
                            st.session_state.v5_schema = service.get_v5_schema()
                            st.session_state.canvas_edit_log = service.get_edit_log()
                            (result_box.success if r.ok else result_box.error)(r.message)
                            if r.ok:
                                st.rerun()
                elif action == "删除":
                    rels = list(ontology.link_types.keys())
                    if not rels:
                        st.info("暂无关系")
                    else:
                        rn = st.selectbox("关系", rels, key="v2_rel_del")
                        if st.button("执行删除关系", type="primary", key="v2_rel_del_btn"):
                            r = service.delete_relationship(rn, force=force)
                            st.session_state.v5_schema = service.get_v5_schema()
                            st.session_state.canvas_edit_log = service.get_edit_log()
                            (result_box.success if r.ok else result_box.error)(r.message)
                            if r.ok:
                                st.rerun()
                else:
                    st.info("关系更新请先删除再新增。")

            st.markdown("---")
            st.markdown("#### 当前结构预览")
            gcol1, gcol2 = st.columns(2)
            with gcol1:
                if ontology.object_types:
                    st.components.v1.html(
                        create_business_object_graph(ontology), height=520, scrolling=True
                    )
            with gcol2:
                st.markdown("**Legacy ObjectTypes**")
                st.json({
                    k: {
                        "pk": v.get("primary_key"),
                        "properties": v.get("properties"),
                        "description": v.get("description"),
                    }
                    for k, v in ontology.object_types.items()
                })
                st.markdown("**Legacy Relationships**")
                st.json(ontology.link_types)

            if st.session_state.get("canvas_edit_log"):
                with st.expander("编辑审计日志", expanded=False):
                    st.json(st.session_state.canvas_edit_log[-20:])

            if st.session_state.get("v5_schema") is not None:
                with st.expander("V5 Schema JSON（可复制用于持久化）", expanded=False):
                    st.code(st.session_state.v5_schema.to_json(), language="json")
                    st.download_button(
                        "下载 V5 Schema JSON",
                        data=st.session_state.v5_schema.to_json(),
                        file_name="ionto_v5_schema.json",
                        mime="application/json",
                        key="v2_v5_schema_dl",
                    )

            # ---- 1) V5 持久化 + 3) 推理门禁发布 ----
            st.markdown("---")
            st.markdown("#### V5 持久化与发布门禁")
            from ontology_domain.sqlite_repository import (
                RepositoryError,
                SQLiteOntologyRepository,
            )
            from ontology_domain.reasoning_gate import publish_with_gate, run_reasoning_gate

            db_path = st.text_input(
                "SQLite 路径",
                value=st.session_state.get("v5_db_path", "ontology_workspace/v5_ontology.db"),
                key="v2_v5_db_path",
            )
            st.session_state.v5_db_path = db_path

            pc1, pc2, pc3 = st.columns(3)
            with pc1:
                if st.button("保存 V5 Schema 到 SQLite", type="primary", key="v2_save_v5"):
                    try:
                        if st.session_state.v5_schema is None:
                            st.error("当前无 V5 Schema")
                        else:
                            repo = SQLiteOntologyRepository(db_path)
                            info = repo.save_schema(st.session_state.v5_schema)
                            st.success(
                                f"已保存 {info['ontology_id']}@v{info['version']} "
                                f"checksum={info['checksum'][:18]}…"
                            )
                    except RepositoryError as exc:
                        st.error(str(exc))
                    except Exception as exc:
                        st.error(f"保存失败：{exc}")
            with pc2:
                if st.button("运行推理/质量门禁", key="v2_run_gate"):
                    report = run_reasoning_gate(
                        st.session_state.v5_schema, ontology=ontology
                    )
                    st.session_state.v5_gate_report = report.to_dict()
                    if report.passed:
                        st.success("门禁通过")
                    else:
                        st.error("门禁未通过（存在 block/error）")
            with pc3:
                force_pub = st.checkbox("强制发布（跳过门禁）", value=False, key="v2_force_pub")
                if st.button("门禁通过后发布 Release", key="v2_publish"):
                    try:
                        if st.session_state.v5_schema is None:
                            st.error("当前无 V5 Schema")
                        else:
                            repo = SQLiteOntologyRepository(db_path)
                            out = publish_with_gate(
                                repo,
                                st.session_state.v5_schema,
                                ontology=ontology,
                                force=force_pub,
                            )
                            st.session_state.v5_gate_report = out.get("gate")
                            if out.get("published"):
                                st.success(out.get("message", "发布成功"))
                                st.json(out.get("release"))
                            else:
                                st.error(out.get("message", "发布失败"))
                    except RepositoryError as exc:
                        st.error(str(exc))
                    except Exception as exc:
                        st.error(f"发布失败：{exc}")

            # 加载已有版本
            with st.expander("从 SQLite 加载已有 Schema 版本", expanded=False):
                load_oid = st.text_input(
                    "ontology_id",
                    value=(
                        st.session_state.v5_schema.ontology_id
                        if st.session_state.get("v5_schema")
                        else ""
                    ),
                    key="v2_load_oid",
                )
                load_ver = st.number_input("version", min_value=1, value=1, key="v2_load_ver")
                if st.button("加载 Schema", key="v2_load_schema_btn"):
                    try:
                        repo = SQLiteOntologyRepository(db_path)
                        loaded = repo.load_schema(load_oid, int(load_ver))
                        st.session_state.v5_schema = loaded
                        st.success(f"已加载 {load_oid}@v{load_ver}")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

            if st.session_state.get("v5_gate_report"):
                with st.expander("最近门禁报告", expanded=True):
                    gr = st.session_state.v5_gate_report
                    st.write("通过" if gr.get("passed") else "未通过")
                    st.json(gr)

        elif viz_mode == "本地交互增强":
            # ---- 2) 选中节点 → 侧栏编辑 ----
            type_names = list(ontology.object_types.keys())
            selected_type = st.selectbox(
                "选中实体类型（模拟画布选中）",
                ["—"] + type_names,
                key="v2_canvas_selected_type",
            )
            left, right = st.columns([2, 1])
            with left:
                local_view = st.selectbox(
                    "视图",
                    ["业务对象关系图", "Schema 图谱", "实例图谱（当前筛选）"],
                    key="v2_local_view",
                )
                if local_view == "业务对象关系图" and ontology.object_types:
                    st.components.v1.html(
                        create_business_object_graph(ontology),
                        height=660,
                        scrolling=True,
                    )
                elif local_view == "Schema 图谱" and ontology.object_types:
                    st.components.v1.html(
                        create_schema_graph(ontology),
                        height=660,
                        scrolling=True,
                    )
                else:
                    all_types = list(ontology.object_types.keys())
                    sel = st.multiselect(
                        "实例类型",
                        all_types,
                        default=all_types[: min(5, len(all_types))],
                        key="v2_adv_inst_types",
                    )
                    if sel:
                        ghtml, n, e, trunc = create_instance_graph(
                            ontology,
                            selected_types=sel,
                            max_nodes=300,
                            search_text="",
                            focus_node=None,
                            hops=1,
                            physics=True,
                        )
                        st.caption(f"节点 {n} · 边 {e}" + (" · 已截断" if trunc else ""))
                        st.components.v1.html(ghtml, height=620, scrolling=True)
                    else:
                        st.info("请选择至少一个实体类型。")

            with right:
                st.markdown("#### 交互说明")
                st.markdown(
                    """
- **拖拽**：移动节点
- **滚轮**：缩放
- **空白拖动**：平移画布
- **悬停**：查看属性详情
- **上方选中类型**：打开侧栏编辑并回写 V5
                    """
                )
                st.markdown("#### 结构摘要")
                st.metric("实体类型", len(ontology.object_types))
                st.metric("关系类型", len(ontology.link_types))
                st.metric("实例节点", ontology.links.number_of_nodes())
                st.metric("关系边", ontology.links.number_of_edges())

                focus_type = selected_type if selected_type and selected_type != "—" else None
                if focus_type:
                    from ontology_domain.canvas_edit import CanvasEditService

                    info = ontology.object_types[focus_type]
                    st.markdown(f"#### 选中：`{focus_type}`")
                    st.write(f"**主键**：`{info.get('primary_key')}`")
                    st.write(f"**属性**：{', '.join(info.get('properties', []))}")
                    cnt = len(ontology.objects.get(focus_type, {}))
                    st.write(f"**实例数**：{cnt}")

                    new_desc = st.text_area(
                        "描述",
                        value=str(info.get("description") or ""),
                        key=f"v2_side_desc_{focus_type}",
                        height=70,
                    )
                    new_prop = st.text_input("新增属性名", key=f"v2_side_prop_{focus_type}")
                    if st.button("保存描述到 V5", key=f"v2_side_save_{focus_type}"):
                        svc = CanvasEditService(
                            ontology,
                            schema=st.session_state.get("v5_schema"),
                            auto_v5=True,
                        )
                        r = svc.update_object_type(focus_type, description=new_desc)
                        st.session_state.v5_schema = svc.get_v5_schema()
                        (st.success if r.ok else st.error)(r.message)
                        if r.ok:
                            st.rerun()
                    if new_prop and st.button("添加属性并回写 V5", key=f"v2_side_addp_{focus_type}"):
                        svc = CanvasEditService(
                            ontology,
                            schema=st.session_state.get("v5_schema"),
                            auto_v5=True,
                        )
                        r = svc.add_property(focus_type, new_prop.strip())
                        st.session_state.v5_schema = svc.get_v5_schema()
                        (st.success if r.ok else st.error)(r.message)
                        if r.ok:
                            st.rerun()
                    if st.button("删除该类型（强制）", key=f"v2_side_del_{focus_type}"):
                        svc = CanvasEditService(
                            ontology,
                            schema=st.session_state.get("v5_schema"),
                            auto_v5=True,
                        )
                        r = svc.delete_object_type(focus_type, force=True)
                        st.session_state.v5_schema = svc.get_v5_schema()
                        (st.success if r.ok else st.error)(r.message)
                        if r.ok:
                            st.rerun()
                else:
                    st.info("在上方选择实体类型以打开侧栏编辑。")

        else:
            # Ontosphere 专业画布
            st.markdown(
                """
**Ontosphere** 提供：
- Reactodia 专业知识图画布（拖拽编辑、多布局、聚类）
- 完整 **OWL 2 DL** 推理（Konclude WASM）
- 推理后 **SHACL** 验证与修复建议
- TBox / ABox 切换、命名空间管理、MCP Agent 工具
                """
            )
            c1, c2, c3 = st.columns([2, 1, 1])
            with c1:
                try:
                    trig_content, trig_name = ontology.export_for_ontosphere()
                    st.download_button(
                        "① 下载 TriG（含命名图）",
                        data=trig_content,
                        file_name=trig_name,
                        mime="application/trig",
                        use_container_width=True,
                        type="primary",
                        key="v2_adv_trig_dl",
                    )
                except Exception as exc:
                    st.error(f"TriG 导出失败：{exc}")
            with c2:
                try:
                    ttl = ontology.export_owl(format="turtle")
                    st.download_button(
                        "下载 Turtle",
                        data=ttl,
                        file_name="ionto_schema.ttl",
                        mime="text/turtle",
                        use_container_width=True,
                        key="v2_adv_ttl_dl",
                    )
                except Exception as exc:
                    st.error(str(exc))
            with c3:
                st.link_button(
                    "② 打开 Ontosphere",
                    "https://thhanke.github.io/ontosphere/",
                    use_container_width=True,
                )

            st.info(
                "操作步骤：下载 TriG → 打开 Ontosphere → 将文件拖入页面 "
                "（或使用 Load RDF）→ 即可交互编辑、运行推理与验证。"
            )

            # 内嵌 Ontosphere（只读入口，数据需用户自行加载）
            with st.expander("在页面内嵌 Ontosphere（需自行加载导出的文件）", expanded=False):
                st.components.v1.iframe(
                    "https://thhanke.github.io/ontosphere/",
                    height=720,
                    scrolling=True,
                )

            st.markdown("#### 导出内容说明")
            st.markdown(
                """
| 命名图 | 内容 |
|--------|------|
| `urn:ionto:schema:` | 类、数据属性、对象属性（TBox） |
| `urn:ionto:data:` | 实例与关系断言（ABox） |
| `urn:ionto:mapping:` | 源表映射元数据 |
                """
            )

    # 关系分析
    with tabs[5]:
        st.subheader("实体关联路径查询")
        node_options = list(ontology.links.nodes)
        if len(node_options) >= 2:
            c1, c2 = st.columns(2)
            with c1:
                source_node = st.selectbox(
                    "起点实体", node_options, key="v2_path_source"
                )
            with c2:
                target_node = st.selectbox(
                    "终点实体", node_options, index=1,
                    key="v2_path_target",
                )

            if st.button("查询最短关系路径"):
                path = find_shortest_path(
                    ontology, source_node, target_node
                )
                if path:
                    st.success(f"找到路径，共 {len(path) - 1} 条关系边。")
                    st.code("  →  ".join(path))
                    path_edges = []
                    for left, right in zip(path[:-1], path[1:]):
                        edge_data = ontology.links.get_edge_data(left, right) or {}
                        relations = [
                            str(data.get("relation_type", key))
                            for key, data in edge_data.items()
                        ]
                        path_edges.append({
                            "起点": left,
                            "关系": ", ".join(relations),
                            "终点": right,
                        })
                    st.dataframe(pd.DataFrame(path_edges), use_container_width=True)
                else:
                    st.warning("两个实体之间没有找到关系路径。")
        else:
            st.info("至少需要两个实体节点才能进行路径查询。")

        st.subheader("实体邻居查询")
        if node_options:
            selected_node = st.selectbox(
                "选择实体", node_options, key="v2_neighbor_node"
            )
            neighbors = set(ontology.links.predecessors(selected_node))
            neighbors.update(ontology.links.successors(selected_node))
            st.write(f"直接关联实体：{len(neighbors)} 个")
            if neighbors:
                st.dataframe(
                    pd.DataFrame([
                        {
                            "实体": neighbor,
                            "类型": ontology.links.nodes[neighbor].get("type"),
                            "方向": (
                                "指向当前实体"
                                if ontology.links.has_edge(neighbor, selected_node)
                                else "由当前实体指向"
                            ),
                        }
                        for neighbor in sorted(neighbors)
                    ]),
                    use_container_width=True,
                )

    # 质量检测
    with tabs[6]:
        st.subheader("本体关系质量检查")
        report = quality_report(ontology)
        q1, q2, q3, q4 = st.columns(4)
        q1.metric("实体节点", report["node_count"])
        q2.metric("关系边", report["edge_count"])
        q3.metric("孤立节点", len(report["isolated_nodes"]))
        q4.metric("悬空引用", len(report["dangling_references"]))

        if report["dangling_references"]:
            st.warning("发现悬空引用：源对象引用的目标主键不存在。")
            st.dataframe(
                pd.DataFrame(report["dangling_references"]),
                use_container_width=True,
            )
        else:
            st.success("未发现悬空引用。")

        if report["isolated_nodes"]:
            with st.expander("查看孤立节点"):
                st.write(report["isolated_nodes"][:500])
        else:
            st.success("未发现孤立节点。")

        if report["invalid_relation_types"]:
            st.error(f"无效关系定义：{report['invalid_relation_types']}")
        if report["self_loops"]:
            st.warning(f"自环关系数量：{len(report['self_loops'])}")
            st.write(report["self_loops"][:100])
        if report["duplicate_edges"]:
            st.warning(f"重复关系数量：{len(report['duplicate_edges'])}")
        st.caption(
            "说明：当前质量检测基于已导入的数据和关系定义；"
            "尚未实现完整 OWL 推理、SHACL 约束验证。"
        )

    # Action
    with tabs[7]:
        st.subheader("业务对象 Action")
        action_type = st.radio(
            "操作类型", ["更新对象", "新建对象", "删除对象"],
            horizontal=True, key="v2_action_type",
        )
        type_options = list(ontology.object_types.keys())

        if not type_options:
            st.info("暂无实体类型。")
        else:
            selected_type = st.selectbox(
                "实体类型", type_options, key="v2_action_entity_type"
            )
            pk_field = ontology.object_types[selected_type]["primary_key"]
            current_objects = ontology.objects.get(selected_type, {})

            if action_type == "更新对象":
                if not current_objects:
                    st.info("该类型暂无对象。")
                else:
                    selected_pk = st.selectbox(
                        "选择对象主键", list(current_objects.keys()),
                        key="v2_update_pk",
                    )
                    current = current_objects[selected_pk]
                    st.json(current)
                    updates = {}
                    for prop in ontology.object_types[selected_type]["properties"]:
                        if prop == pk_field:
                            continue
                        old_value = current.get(prop)
                        new_value = st.text_input(
                            prop,
                            value="" if old_value is None else str(old_value),
                            key=f"v2_update_{selected_type}_{selected_pk}_{prop}",
                        )
                        if new_value != ("" if old_value is None else str(old_value)):
                            updates[prop] = new_value

                    if st.button("执行更新", key="v2_do_update") and updates:
                        try:
                            ontology.update_object(
                                selected_type, selected_pk, updates
                            )
                            st.success("更新成功。")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))

            elif action_type == "新建对象":
                new_data = {}
                for prop in ontology.object_types[selected_type]["properties"]:
                    default_value = ""
                    new_data[prop] = st.text_input(
                        prop, value=default_value,
                        key=f"v2_create_{selected_type}_{prop}",
                    )
                if st.button("创建对象", key="v2_do_create"):
                    try:
                        new_pk = ontology.create_object(selected_type, new_data)
                        st.success(f"创建成功，主键：{new_pk}")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

            elif action_type == "删除对象":
                if not current_objects:
                    st.info("该类型暂无对象。")
                else:
                    selected_pk = st.selectbox(
                        "选择要删除的对象", list(current_objects.keys()),
                        key="v2_delete_pk",
                    )
                    st.warning("删除对象会同时删除图谱中与该对象关联的边。")
                    if st.button(
                        "确认删除", type="primary", key="v2_do_delete"
                    ):
                        ontology.delete_object(selected_type, selected_pk)
                        st.success("对象已删除。")
                        st.rerun()

        if ontology.actions_log:
            st.subheader("最近 Action 日志")
            st.json(ontology.actions_log[-10:])

    # 导出
    with tabs[8]:
        st.subheader("本体数据导出")
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button(
                "下载 JSON",
                data=ontology.export_json(),
                file_name="ontology_v4.json",
                mime="application/json",
                use_container_width=True,
            )
        with col2:
            try:
                owl_data = ontology.export_owl(format="xml")
                st.download_button(
                    "下载 OWL (RDF/XML)",
                    data=owl_data,
                    file_name="ontology_v4.owl",
                    mime="application/rdf+xml",
                    use_container_width=True,
                )
            except Exception as exc:
                st.error(f"OWL 导出失败：{exc}")
        with col3:
            try:
                ttl_data = ontology.export_owl(format="turtle")
                st.download_button(
                    "下载 Turtle",
                    data=ttl_data,
                    file_name="ontology_v4.ttl",
                    mime="text/turtle",
                    use_container_width=True,
                )
            except Exception as exc:
                st.error(f"Turtle 导出失败：{exc}")

        st.markdown("---")
        st.subheader("Ontosphere 集成（可视化 + 推理）")
        st.caption(
            "导出 TriG（保留命名图）后，可直接在 Ontosphere 中加载，"
            "进行专业画布交互、完整 OWL 2 DL 推理与 SHACL 验证。"
        )
        c1, c2 = st.columns([2, 1])
        with c1:
            try:
                trig_content, trig_name = ontology.export_for_ontosphere()
                st.download_button(
                    "下载 TriG（推荐给 Ontosphere）",
                    data=trig_content,
                    file_name=trig_name,
                    mime="application/trig",
                    use_container_width=True,
                    type="primary",
                )
            except Exception as exc:
                st.error(f"TriG 导出失败：{exc}")
        with c2:
            st.link_button(
                "打开 Ontosphere",
                "https://thhanke.github.io/ontosphere/",
                use_container_width=True,
            )
        st.info(
            "使用步骤：1) 下载 .trig 文件  2) 打开 Ontosphere  "
            "3) 将文件拖入页面或通过「Load RDF」加载  4) 可直接运行推理与验证。"
        )


# ---------------------------------------------------------------------
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
