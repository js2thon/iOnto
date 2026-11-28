"""Version management page."""
from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from ui.versioning import (
    diff_versions,
    list_versions,
    load_version,
    save_version,
    update_version_status,
)

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
