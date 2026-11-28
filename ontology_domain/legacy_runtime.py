"""Legacy runtime Ontology + AutoOntologyBuilder (ABox graph host).

Structural truth is V5 OntologySchema; this module holds the NetworkX-backed
runtime used by graphs, QA, and version snapshots until ABox fully migrates.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import pandas as pd

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
