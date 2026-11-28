"""Path and quality analysis helpers."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import networkx as nx

def find_shortest_path(
    ontology: Any, source: str, target: str
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


def quality_report(ontology: Any) -> Dict[str, Any]:
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


