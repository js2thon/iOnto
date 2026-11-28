"""Graph visualization helpers extracted from ontology_studio_v3."""
from __future__ import annotations

import html
import json
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx
from pyvis.network import Network

# Ontology is duck-typed (object_types, link_types, objects, links)

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


def create_schema_graph(ontology: Any) -> str:
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


def create_business_object_graph(ontology: Any) -> str:
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
    ontology: Any,
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
