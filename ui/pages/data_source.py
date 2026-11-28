"""Data source page: file upload, PostgreSQL, commit raw data."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from ontology_domain.legacy_runtime import AutoOntologyBuilder, Ontology, safe_records
from ui.data_io import (
    build_pg_engine,
    list_pg_schemas,
    list_pg_tables,
    load_pg_tables,
    read_uploaded_file,
    test_pg_connection,
)

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
    """将预览数据写入会话；提取元数据；默认自动生成本体。

    返回错误信息；成功返回 None。
    """
    st.session_state.raw_data_v2 = preview_data or {}
    st.session_state.source_kinds_v2 = {str(name): source_kind for name in (preview_data or {})}
    st.session_state.pop("v5_mapping_preview", None)
    st.session_state.pop("v5_mapping_last_run", None)
    st.session_state.messages_v2 = []
    st.session_state.manual_links_config = []
    st.session_state.pk_config = {}
    st.session_state.source_metadata = None
    st.session_state.v5_tbox_schema = None

    # 元数据提取：写入可组合 MetadataWorkbench（不清空已有 bundle，支持多源叠加）
    if preview_data:
        try:
            from ontology_domain.metadata_workbench import MetadataWorkbench

            if st.session_state.get("metadata_workbench") is None:
                st.session_state.metadata_workbench = MetadataWorkbench()
            wb = st.session_state.metadata_workbench
            keys = list(preview_data.keys())
            bundle_name = f"{source_kind}:{','.join(keys[:3])}"
            if len(keys) > 3:
                bundle_name += "…"
            wb.add_from_dataframes(preview_data, name=bundle_name, source_kind=source_kind)
            meta = wb.get_working_copy()
            st.session_state.source_metadata = meta
            for tname, tmeta in meta.tables.items():
                if tmeta.suggested_primary_key:
                    st.session_state.pk_config[tname] = tmeta.suggested_primary_key
        except Exception as meta_exc:
            st.session_state.source_metadata_error = str(meta_exc)

    if not auto_build:
        st.session_state.ontology_v2 = None
        return None

    try:
        ontology = _build_ontology_from_raw(
            preview_data,
            primary_keys=dict(st.session_state.pk_config) or None,
        )
        st.session_state.ontology_v2 = ontology
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
