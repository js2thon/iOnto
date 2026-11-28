"""Pipeline overview page."""
from __future__ import annotations

import streamlit as st

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
