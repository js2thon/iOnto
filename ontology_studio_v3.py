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

import streamlit as st

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
# Legacy / UI modules
from ontology_domain.legacy_runtime import Ontology
from ui.llm_config import load_llm_config, save_llm_config
from ui.session import init_session
from ui.versioning import load_version, read_current_pointer

from ui.pages.overview import render_overview
from ui.pages.data_source import render_data_source
from ui.pages.modeling import render_modeling
from ui.pages.qa import render_qa
from ui.pages.evaluation import render_evaluation
from ui.pages.versions import render_versions
from ontology_domain.mapping_workbench_ui import render_mapping_workbench

# ---- session ----
init_session()

# ---- restore ontology from current pointer ----
if st.session_state.ontology_v2 is None:
    pointer = read_current_pointer()
    if pointer and pointer.get("version_id"):
        ontology_restored, restore_error = load_version(str(pointer["version_id"]))
        if ontology_restored is not None:
            st.session_state.ontology_v2 = ontology_restored
            st.session_state.restored_version_id = str(pointer["version_id"])
        elif restore_error:
            st.warning(f"自动恢复版本失败：{restore_error}")

# ---- sidebar: LLM config ----
with st.sidebar:
    st.markdown("### LLM 配置")
    saved = load_llm_config()
    api_key = st.text_input("API Key", value=saved.get("api_key", ""), type="password")
    base_url = st.text_input("Base URL", value=saved.get("base_url", ""))
    model_name = st.text_input("Model", value=saved.get("model_name", "gpt-4o-mini"))
    if st.button("保存 LLM 配置"):
        save_llm_config(api_key, base_url, model_name)
        st.success("已保存")
    st.session_state["llm_api_key"] = api_key
    st.session_state["llm_base_url"] = base_url
    st.session_state["llm_model_name"] = model_name

# ---- navigation ----
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
