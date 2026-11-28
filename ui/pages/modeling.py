"""Ontology modeling page: metadata workbench, TBox, graphs, export."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from ontology_domain.legacy_runtime import AutoOntologyBuilder, Ontology, safe_records
from ui.analysis import find_shortest_path, quality_report
from ui.graphs import (
    create_business_object_graph,
    create_instance_graph,
    create_schema_graph,
)
from ui.pages.data_source import _build_ontology_from_raw
from ui.session import (
    after_structural_edit,
    get_v5_schema,
    set_v5_schema,
    sync_legacy_from_v5,
)

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

        # ---- 元数据工作台：多源提取 / 编辑 / 组合 → TBox ----
        st.markdown("---")
        st.markdown("**① 元数据工作台（提取 · 编辑 · 多源组合 → TBox）**")
        st.caption(
            "可多次导入数据源叠加为多个元数据 Bundle；启用/停用后合并，"
            "再编辑表/列/关系，最后组合生成 V5 TBox。"
        )
        from ontology_domain.metadata_workbench import MetadataWorkbench

        if st.session_state.get("metadata_workbench") is None:
            st.session_state.metadata_workbench = MetadataWorkbench()
        wb: MetadataWorkbench = st.session_state.metadata_workbench

        c_ex1, c_ex2, c_ex3 = st.columns(3)
        with c_ex1:
            if usable and st.button("从当前数据提取并加入 Bundle", key="v2_wb_add_bundle"):
                try:
                    wb.add_from_dataframes(usable, name="manual_extract", source_kind="dataframe")
                    st.session_state.source_metadata = wb.get_working_copy()
                    st.success("已加入 Bundle 并合并工作副本")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
        with c_ex2:
            if st.button("重建合并工作副本", key="v2_wb_rebuild"):
                wb.rebuild_working_copy()
                st.session_state.source_metadata = wb.get_working_copy()
                st.rerun()
        with c_ex3:
            if st.button("清空全部 Bundle", key="v2_wb_clear"):
                st.session_state.metadata_workbench = MetadataWorkbench()
                st.session_state.source_metadata = None
                st.rerun()

        bundles = wb.list_bundles()
        if bundles:
            st.markdown("**元数据 Bundle 列表（可多选启用）**")
            for b in bundles:
                cols = st.columns([3, 1, 1, 1])
                cols[0].write(
                    f"`{b['name']}` · 表 {b['tables']} · 关系 {b['relationships']} · {b['source_kind']}"
                )
                en = cols[1].checkbox("启用", value=b["enabled"], key=f"v2_wb_en_{b['id']}")
                if en != b["enabled"]:
                    wb.set_enabled(b["id"], en)
                    st.session_state.source_metadata = wb.get_working_copy()
                    st.rerun()
                if cols[2].button("删除", key=f"v2_wb_del_{b['id']}"):
                    wb.remove_bundle(b["id"])
                    st.session_state.source_metadata = wb.get_working_copy()
                    st.rerun()
        else:
            st.info("尚无元数据 Bundle。加载数据源或点击「从当前数据提取并加入 Bundle」。")

        meta = wb.get_working_copy()
        st.session_state.source_metadata = meta

        if meta.tables:
            st.markdown("**合并后的工作副本**")
            meta_rows = []
            for tname, tmeta in meta.tables.items():
                meta_rows.append(
                    {
                        "表": tname,
                        "行数": tmeta.row_count,
                        "列数": tmeta.column_count,
                        "建议主键": tmeta.suggested_primary_key or "—",
                        "PK候选": ", ".join(tmeta.primary_key_candidates) or "—",
                        "FK候选数": len(tmeta.foreign_key_candidates),
                    }
                )
            st.dataframe(pd.DataFrame(meta_rows), use_container_width=True, hide_index=True)

            with st.expander("编辑表 / 列元数据", expanded=False):
                edit_table = st.selectbox(
                    "选择表", list(meta.tables.keys()), key="v2_wb_edit_table"
                )
                tmeta = meta.tables[edit_table]
                new_pk = st.selectbox(
                    "建议主键",
                    ["—"] + [c.name for c in tmeta.columns],
                    index=(
                        (["—"] + [c.name for c in tmeta.columns]).index(tmeta.suggested_primary_key)
                        if tmeta.suggested_primary_key
                        in [c.name for c in tmeta.columns]
                        else 0
                    ),
                    key="v2_wb_edit_pk",
                )
                new_desc = st.text_input(
                    "表描述", value=tmeta.description or "", key="v2_wb_edit_desc"
                )
                new_tname = st.text_input("重命名表（可选）", value=edit_table, key="v2_wb_edit_tname")
                if st.button("保存表级修改", key="v2_wb_save_table"):
                    try:
                        wb.update_table(
                            edit_table,
                            suggested_primary_key=None if new_pk == "—" else new_pk,
                            description=new_desc,
                            rename_to=new_tname if new_tname != edit_table else None,
                        )
                        if new_pk != "—":
                            st.session_state.pk_config[new_tname if new_tname != edit_table else edit_table] = new_pk
                        st.session_state.source_metadata = wb.get_working_copy()
                        st.success("表元数据已更新")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

                col_names = [c.name for c in tmeta.columns]
                edit_col = st.selectbox("选择列", col_names, key="v2_wb_edit_col")
                col_obj = next(c for c in tmeta.columns if c.name == edit_col)
                logical = st.selectbox(
                    "逻辑类型",
                    ["string", "integer", "number", "boolean", "date", "datetime", "json"],
                    index=["string", "integer", "number", "boolean", "date", "datetime", "json"].index(
                        col_obj.logical_type
                        if col_obj.logical_type
                        in {"string", "integer", "number", "boolean", "date", "datetime", "json"}
                        else "string"
                    ),
                    key="v2_wb_edit_ltype",
                )
                nullable = st.checkbox("可空", value=col_obj.nullable, key="v2_wb_edit_null")
                spk = st.checkbox("疑似主键", value=col_obj.suspected_pk, key="v2_wb_edit_spk")
                sfk = st.checkbox("疑似外键", value=col_obj.suspected_fk, key="v2_wb_edit_sfk")
                rename_col = st.text_input("重命名列（可选）", value=edit_col, key="v2_wb_edit_cname")
                if st.button("保存列级修改", key="v2_wb_save_col"):
                    try:
                        wb.update_column(
                            edit_table,
                            edit_col,
                            logical_type=logical,
                            nullable=nullable,
                            suspected_pk=spk,
                            suspected_fk=sfk,
                            rename_to=rename_col if rename_col != edit_col else None,
                        )
                        st.session_state.source_metadata = wb.get_working_copy()
                        st.success("列元数据已更新")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
                if st.button("从工作副本排除该表", key="v2_wb_exclude_table"):
                    wb.exclude_table(edit_table)
                    st.session_state.source_metadata = wb.get_working_copy()
                    st.rerun()

            with st.expander("关系候选编辑", expanded=False):
                if meta.relationship_candidates:
                    st.dataframe(
                        pd.DataFrame(meta.relationship_candidates),
                        use_container_width=True,
                        hide_index=True,
                    )
                    del_idx = st.number_input(
                        "删除关系候选索引",
                        min_value=0,
                        max_value=max(0, len(meta.relationship_candidates) - 1),
                        value=0,
                        key="v2_wb_rel_del_idx",
                    )
                    if st.button("删除该关系候选", key="v2_wb_rel_del"):
                        wb.remove_relationship_candidate(int(del_idx))
                        st.rerun()
                tnames = list(meta.tables.keys())
                if len(tnames) >= 1:
                    st.markdown("手动添加关系候选")
                    rs = st.selectbox("源表", tnames, key="v2_wb_rel_src")
                    rt = st.selectbox("目标表", tnames, key="v2_wb_rel_tgt")
                    rsc = st.selectbox(
                        "源列",
                        [c.name for c in meta.tables[rs].columns],
                        key="v2_wb_rel_scol",
                    )
                    rtc = st.selectbox(
                        "目标列",
                        [c.name for c in meta.tables[rt].columns],
                        key="v2_wb_rel_tcol",
                    )
                    rconf = st.slider("置信度", 0.0, 1.0, 0.8, 0.05, key="v2_wb_rel_conf")
                    if st.button("添加关系候选", key="v2_wb_rel_add"):
                        try:
                            wb.add_relationship_candidate(
                                rs, rt, rsc, rtc, confidence=float(rconf)
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))

            allow_tables = st.multiselect(
                "参与 TBox 生成的表（默认全部）",
                list(meta.tables.keys()),
                default=list(meta.tables.keys()),
                key="v2_wb_allow_tables",
            )
            min_conf = st.slider(
                "关系最小置信度",
                min_value=0.0,
                max_value=1.0,
                value=0.5,
                step=0.05,
                key="v2_tbox_min_conf",
            )
            if st.button(
                "组合当前元数据生成 TBox（仅结构）",
                type="primary",
                key="v2_gen_tbox",
            ):
                try:
                    from ontology_domain.tbox_generator import (
                        apply_tbox_to_legacy_ontology,
                        generate_tbox_from_workbench,
                        metadata_to_tbox_summary,
                    )

                    tbox = generate_tbox_from_workbench(
                        wb,
                        include_relationships=True,
                        min_rel_confidence=float(min_conf),
                        primary_key_overrides=dict(st.session_state.pk_config),
                        table_allowlist=allow_tables or None,
                    )
                    if st.session_state.ontology_v2 is None:
                        st.session_state.ontology_v2 = Ontology()
                    set_v5_schema(tbox, project=True)
                    summary = metadata_to_tbox_summary(tbox)
                    st.success(
                        f"TBox 已生成：{summary['object_types']} 类 · "
                        f"{summary['properties']} 属性 · "
                        f"{summary['relationships']} 关系 · "
                        f"{summary['identities']} Identity"
                    )
                    st.json(summary)
                except Exception as exc:
                    st.error(f"TBox 生成失败：{exc}")

            if st.session_state.get("v5_tbox_schema") is not None:
                st.download_button(
                    "下载 TBox V5 JSON",
                    data=st.session_state.v5_tbox_schema.to_json(),
                    file_name="ionto_tbox_v5.json",
                    mime="application/json",
                    key="v2_tbox_dl",
                )
            st.download_button(
                "下载元数据工作台 JSON",
                data=wb.to_json(),
                file_name="ionto_metadata_workbench.json",
                mime="application/json",
                key="v2_wb_dl",
            )

        st.markdown("---")
        st.markdown("**② 确认并生成完整本体（TBox + ABox 实例）**")

        if st.button("🚀 确认并生成本体", type="primary", key="v2_build_ontology"):
            try:
                with st.spinner("正在构建本体与识别关系..."):
                    ontology = _build_ontology_from_raw(
                        usable,
                        primary_keys=dict(st.session_state.pk_config),
                        manual_links=manual_links,
                    )
                    st.session_state.ontology_v2 = ontology
                    # 若已有 TBox，保留 V5 并标记已物化 ABox
                    schema = get_v5_schema()
                    if schema is not None:
                        schema.metadata["abox_materialized"] = True
                        set_v5_schema(schema, project=False)
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

            service = CanvasEditService(
                ontology,
                schema=get_v5_schema(),
                auto_v5=True,
            )
            after_structural_edit(service)

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
                        after_structural_edit(service)
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
                            after_structural_edit(service)
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
                            after_structural_edit(service)
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
                        after_structural_edit(service)
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
                            after_structural_edit(service)
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
                            after_structural_edit(service)
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
                            after_structural_edit(service)
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
            from ontology_domain.runtime_bridge import publish_precheck, structure_diff

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
                        st.session_state.v5_schema, ontology=ontology, run_owl=True
                    )
                    st.session_state.v5_gate_report = report.to_dict()
                    if report.passed:
                        st.success("门禁通过")
                    else:
                        st.error("门禁未通过（存在 block/error）")
                if st.button("仅运行 OWL 推理层", key="v2_run_owl"):
                    try:
                        from ontology_domain.owl_reasoner import reason_over_ontology

                        rr = reason_over_ontology(
                            ontology,
                            schema=st.session_state.v5_schema,
                            try_owlrl=True,
                        )
                        st.session_state.v5_owl_report = rr.to_dict()
                        if rr.consistent:
                            st.success(
                                f"一致 · engine={rr.engine} · "
                                f"断言 {rr.asserted_triple_count} · 推断 +{rr.inferred_triple_count}"
                            )
                        else:
                            st.error(f"不一致 · engine={rr.engine}")
                    except Exception as exc:
                        st.error(f"OWL 推理失败：{exc}")
            with pc3:
                force_pub = st.checkbox("强制发布（跳过门禁）", value=False, key="v2_force_pub")
                if st.button("门禁通过后发布 Release", key="v2_publish"):
                    try:
                        if st.session_state.v5_schema is None:
                            st.error("当前无 V5 Schema")
                        else:
                            repo = SQLiteOntologyRepository(db_path)
                            pre = publish_precheck(get_v5_schema(), ontology)
                            if not pre["ok"] and not force_pub:
                                st.error(pre["message"])
                                st.json(pre.get("diff"))
                                st.stop()
                            if not pre["ok"] and force_pub:
                                sync_legacy_from_v5()
                            out = publish_with_gate(
                                repo,
                                get_v5_schema(),
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
                        set_v5_schema(loaded, project=True)
                        st.success(f"已加载 {load_oid}@v{load_ver}")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

            if st.session_state.get("v5_gate_report"):
                with st.expander("最近门禁报告", expanded=True):
                    gr = st.session_state.v5_gate_report
                    st.write("通过" if gr.get("passed") else "未通过")
                    st.json(gr)
            if st.session_state.get("v5_owl_report"):
                with st.expander("OWL 推理报告", expanded=False):
                    st.json(st.session_state.v5_owl_report)

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
                            schema=get_v5_schema(),
                            auto_v5=True,
                        )
                        r = svc.update_object_type(focus_type, description=new_desc)
                        after_structural_edit(svc)
                        (st.success if r.ok else st.error)(r.message)
                        if r.ok:
                            st.rerun()
                    if new_prop and st.button("添加属性并回写 V5", key=f"v2_side_addp_{focus_type}"):
                        svc = CanvasEditService(
                            ontology,
                            schema=get_v5_schema(),
                            auto_v5=True,
                        )
                        r = svc.add_property(focus_type, new_prop.strip())
                        after_structural_edit(svc)
                        (st.success if r.ok else st.error)(r.message)
                        if r.ok:
                            st.rerun()
                    if st.button("删除该类型（强制）", key=f"v2_side_del_{focus_type}"):
                        svc = CanvasEditService(
                            ontology,
                            schema=get_v5_schema(),
                            auto_v5=True,
                        )
                        r = svc.delete_object_type(focus_type, force=True)
                        after_structural_edit(svc)
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
