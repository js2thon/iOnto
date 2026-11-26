"""Streamlit UI for the V5.1 mapping workbench."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from .mapping_workbench import (
    build_property_contract,
    build_relationship_contract,
    execute_and_persist,
    legacy_ontology_to_schema,
    load_latest_schema,
    next_editable_version,
    preview_mapping,
    set_contract_review,
    source_fingerprint,
)
from .sqlite_repository import RepositoryError, SQLiteOntologyRepository


def _frame_columns(frame: Any) -> List[str]:
    return [str(column) for column in getattr(frame, "columns", [])]


def _primary_key_for(legacy_ontology: Any, table: str) -> List[str]:
    info = (getattr(legacy_ontology, "object_types", {}) or {}).get(table) or {}
    field = info.get("primary_key")
    return [str(field)] if field else []


def _initialize_schema(legacy_ontology: Any, raw_data: Dict[str, Any], repository: Any) -> Any:
    fingerprint = source_fingerprint(raw_data)
    current = st.session_state.get("v5_mapping_schema")
    previous_fingerprint = st.session_state.get("v5_mapping_source_fingerprint")
    if current is not None:
        if previous_fingerprint and previous_fingerprint != fingerprint:
            st.session_state.v5_mapping_source_changed = True
        st.session_state.v5_mapping_source_fingerprint = fingerprint
        return current
    current = load_latest_schema(legacy_ontology, repository)
    current.metadata.setdefault("source_schema_fingerprint", fingerprint)
    st.session_state.v5_mapping_schema = current
    st.session_state.v5_mapping_source_fingerprint = fingerprint
    st.session_state.v5_mapping_source_changed = (
        current.metadata.get("source_schema_fingerprint") not in (None, fingerprint)
    )
    return current


def render_mapping_workbench() -> None:
    st.subheader("🧩 V5.1 映射工作台")
    st.caption("源字段 → 本体属性 / 关系 Join → 预览模拟 → 人工审核 → 确定性执行 → 发布")

    legacy_ontology = st.session_state.get("ontology_v2")
    raw_data = st.session_state.get("raw_data_v2") or {}
    if legacy_ontology is None or not raw_data:
        st.info("请先在「数据源」加载 CSV / Excel / PostgreSQL 数据，并在「本体建模」生成业务对象本体。")
        return

    repository = SQLiteOntologyRepository()
    schema = _initialize_schema(legacy_ontology, raw_data, repository)
    fingerprint = source_fingerprint(raw_data)
    st.session_state.v5_mapping_schema = schema

    if schema.validate():
        st.error("V5 Schema 校验未通过，暂不能配置映射。")
        st.code("\n".join(schema.validate()))
        return

    metric_cols = st.columns(4)
    metric_cols[0].metric("数据集", len(raw_data))
    metric_cols[1].metric("业务对象", len(schema.object_types))
    metric_cols[2].metric("属性", len(schema.properties))
    metric_cols[3].metric("关系", len(schema.relationships))
    st.caption(f"数据源结构指纹：{fingerprint[:24]}… · SQLite：ontology_workspace/v5_ontology.db")
    if st.session_state.get("v5_mapping_source_changed"):
        st.warning("检测到数据源结构发生变化。已保留已保存的 V5 Schema 和映射契约；请逐项检查源字段与 Join Key 后再执行。")

    tab_property, tab_relationship, tab_review, tab_runs = st.tabs([
        "字段映射", "关系 Join", "审核与发布", "运行与血缘",
    ])

    with tab_property:
        st.markdown("**业务对象属性映射**")
        object_ids = list(schema.object_types)
        object_id = st.selectbox(
            "目标业务对象",
            object_ids,
            format_func=lambda value: f"{schema.object_types[value].label} ({schema.object_types[value].name})",
            key="v51_property_object",
        )
        obj = schema.object_types[object_id]
        preferred_table = obj.name if obj.name in raw_data else list(raw_data)[0]
        tables = list(raw_data)
        table_index = tables.index(preferred_table) if preferred_table in tables else 0
        source_table = st.selectbox("源数据集", tables, index=table_index, key="v51_property_table")
        columns = _frame_columns(raw_data[source_table])
        if not columns:
            st.warning("所选数据集没有可用字段。")
        else:
            with st.form("v51_property_mapping_form"):
                selections = {}
                transforms = {}
                target_types = {}
                st.caption("同名字段仅作为界面默认建议；保存后仍是待审核契约，不会自动批准。")
                for prop_id in obj.property_ids:
                    prop = schema.properties[prop_id]
                    existing = schema.mapping_contracts.get(f"map:property:{prop_id}")
                    current_field = (
                        (existing.source_ref or {}).get("field")
                        if existing and existing.source_ref.get("table") == source_table else None
                    )
                    default_field = current_field or (prop.name if prop.name in columns else "(不映射)")
                    options = ["(不映射)"] + columns
                    index = options.index(default_field) if default_field in options else 0
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        selections[prop_id] = st.selectbox(
                            f"{prop.label} · {prop.data_type}",
                            options,
                            index=index,
                            key=f"v51_field_{object_id}_{source_table}_{prop_id}",
                        )
                    with c2:
                        transforms[prop_id] = st.selectbox(
                            "转换",
                            ["无", "去除首尾空格", "转小写", "转大写", "转字符串", "转整数", "转小数", "转布尔"],
                            key=f"v51_transform_{object_id}_{prop_id}",
                        )
                    with st.container():
                        target_types[prop_id] = st.selectbox(
                            f"{prop.label} 目标数据类型",
                            ["string", "integer", "decimal", "number", "boolean", "date", "datetime", "enum", "json"],
                            index=["string", "integer", "decimal", "number", "boolean", "date", "datetime", "enum", "json"].index(prop.data_type) if prop.data_type in ["string", "integer", "decimal", "number", "boolean", "date", "datetime", "enum", "json"] else 0,
                            key=f"v51_type_{object_id}_{prop_id}",
                        )
                submitted = st.form_submit_button("保存为待审核映射")
            if submitted:
                try:
                    editable = next_editable_version(schema, repository)
                    for prop_id, source_field in selections.items():
                        if source_field == "(不映射)":
                            continue
                        transform_name = transforms[prop_id]
                        operations = {
                            "去除首尾空格": {"op": "trim"},
                            "转小写": {"op": "lower"},
                            "转字符串": {"op": "to_string"},
                            "转大写": {"op": "upper"},
                            "转整数": {"op": "to_integer"},
                            "转小数": {"op": "to_decimal"},
                            "转布尔": {"op": "to_boolean"},
                        }
                        operation = operations.get(transform_name)
                        editable.properties[prop_id].data_type = target_types[prop_id]
                        contract = build_property_contract(
                            editable,
                            object_type_id=object_id,
                            property_id=prop_id,
                            source_table=source_table,
                            source_field=source_field,
                            record_key_fields=_primary_key_for(legacy_ontology, source_table),
                            mapping_kind="transform" if operation else "direct",
                            transform={"operations": [operation]} if operation else None,
                            source_schema_hash=fingerprint,
                        )
                        editable.mapping_contracts[contract.id] = contract
                    repository.save_schema(editable)
                    st.session_state.v5_mapping_schema = editable
                    st.success("映射草稿已保存，状态为 pending/draft，尚未参与正式执行。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"保存映射失败：{exc}")

    with tab_relationship:
        st.markdown("**业务对象关系映射**")
        if not schema.relationships:
            st.info("当前本体没有关系定义。请先在本体建模中确认业务对象关系。")
        else:
            relation_ids = list(schema.relationships)
            relation_id = st.selectbox(
                "目标关系",
                relation_ids,
                format_func=lambda value: (
                    f"{schema.object_types[schema.relationships[value].source_object_type_id].label}"
                    f" —{schema.relationships[value].label}→ "
                    f"{schema.object_types[schema.relationships[value].target_object_type_id].label}"
                ),
                key="v51_relation_select",
            )
            relation = schema.relationships[relation_id]
            source_obj = schema.object_types[relation.source_object_type_id]
            target_obj = schema.object_types[relation.target_object_type_id]
            source_table = source_obj.name if source_obj.name in raw_data else list(raw_data)[0]
            target_table = target_obj.name if target_obj.name in raw_data else list(raw_data)[0]
            source_table = st.selectbox("关系源数据集", list(raw_data), index=list(raw_data).index(source_table), key="v51_join_source_table")
            target_table = st.selectbox("关系目标数据集", list(raw_data), index=list(raw_data).index(target_table), key="v51_join_target_table")
            source_columns = _frame_columns(raw_data[source_table])
            target_columns = _frame_columns(raw_data[target_table])
            old_source = relation.metadata.get("legacy_source_property")
            old_target = relation.metadata.get("legacy_target_property")
            existing = schema.mapping_contracts.get(f"map:relationship:{relation_id}")
            old_keys = existing.join_keys if existing else []
            pair_count = st.number_input("Join Key 字段对数", min_value=1, max_value=5, value=max(1, len(old_keys)), step=1, key="v51_join_pair_count")
            join_keys = []
            for index in range(int(pair_count)):
                previous = old_keys[index] if index < len(old_keys) else {}
                source_default = previous.get("source_field") or (old_source if index == 0 else None)
                target_default = previous.get("target_field") or (old_target if index == 0 else None)
                c1, c2 = st.columns(2)
                with c1:
                    source_options = ["(选择字段)"] + source_columns
                    source_index = source_options.index(source_default) if source_default in source_options else 0
                    source_field = st.selectbox(f"源 Join 字段 {index + 1}", source_options, index=source_index, key=f"v51_join_src_{relation_id}_{index}")
                with c2:
                    target_options = ["(选择字段)"] + target_columns
                    target_index = target_options.index(target_default) if target_default in target_options else 0
                    target_field = st.selectbox(f"目标 Join 字段 {index + 1}", target_options, index=target_index, key=f"v51_join_tgt_{relation_id}_{index}")
                if source_field != "(选择字段)" and target_field != "(选择字段)":
                    join_keys.append({"source_field": source_field, "target_field": target_field})
            if st.button("保存关系 Join 草稿", key="v51_save_join"):
                try:
                    if len(join_keys) != int(pair_count):
                        raise ValueError("每一组 Join Key 都必须选择源字段和目标字段")
                    editable = next_editable_version(schema, repository)
                    contract = build_relationship_contract(
                        editable,
                        relationship_id=relation_id,
                        source_table=source_table,
                        target_table=target_table,
                        join_keys=join_keys,
                        source_record_key_fields=_primary_key_for(legacy_ontology, source_table),
                        target_record_key_fields=_primary_key_for(legacy_ontology, target_table),
                        source_schema_hash=fingerprint,
                    )
                    editable.mapping_contracts[contract.id] = contract
                    repository.save_schema(editable)
                    st.session_state.v5_mapping_schema = editable
                    st.success("关系 Join 草稿已保存，等待人工审核。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"保存 Join 失败：{exc}")

    with tab_review:
        st.markdown("**映射契约清单**")
        contracts = list(schema.mapping_contracts.values())
        if not contracts:
            st.info("尚未配置映射。请先完成字段映射或关系 Join。")
        else:
            st.dataframe(pd.DataFrame([
                {
                    "契约": item.id,
                    "映射": item.name,
                    "类型": item.mapping_kind,
                    "审核": item.review_status,
                    "状态": item.status,
                    "版本": item.version,
                }
                for item in contracts
            ]), use_container_width=True, hide_index=True)
            contract_ids = [item.id for item in contracts]
            selected_ids = st.multiselect("选择要审核的映射契约", contract_ids, default=[], key="v51_review_contracts")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("批准选中契约", disabled=not selected_ids, key="v51_approve"):
                    try:
                        editable = next_editable_version(schema, repository)
                        set_contract_review(editable, selected_ids, approved=True)
                        repository.save_schema(editable)
                        st.session_state.v5_mapping_schema = editable
                        st.success(f"已批准 {len(selected_ids)} 个契约。")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"批准失败：{exc}")
            with c2:
                if st.button("驳回选中契约", disabled=not selected_ids, key="v51_reject"):
                    try:
                        editable = next_editable_version(schema, repository)
                        set_contract_review(editable, selected_ids, approved=False)
                        repository.save_schema(editable)
                        st.session_state.v5_mapping_schema = editable
                        st.warning(f"已驳回 {len(selected_ids)} 个契约。")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"驳回失败：{exc}")

            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🧪 模拟预览（不改变审核状态）", key="v51_preview"):
                    try:
                        preview = preview_mapping(schema, raw_data)
                        st.session_state.v5_mapping_preview = preview
                    except Exception as exc:
                        st.error(f"预览失败：{exc}")
            with c2:
                approved = [item for item in contracts if item.review_status == "approved" and item.status in {"active", "released"}]
                if st.button("▶ 执行已批准映射并持久化", type="primary", disabled=not approved, key="v51_execute"):
                    try:
                        editable = next_editable_version(schema, repository)
                        outcome = execute_and_persist(editable, raw_data, repository)
                        st.session_state.v5_mapping_schema = editable
                        st.session_state.v5_mapping_last_run = outcome
                        st.success(f"运行已保存：{outcome['persisted']['run_id']}（{outcome['persisted']['instance_count']} 个实例）")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"执行失败：{exc}")

            preview = st.session_state.get("v5_mapping_preview")
            if preview:
                st.markdown("**最近一次模拟结果**")
                p1, p2, p3 = st.columns(3)
                p1.metric("实例数", preview.get("instance_count", 0))
                p2.metric("映射行数", preview.get("mapped_rows", 0))
                p3.metric("错误数", len(preview.get("errors", [])) + len(preview.get("blocked_contracts", [])))
                if preview.get("blocked_contracts"):
                    st.warning("阻断契约")
                    st.dataframe(pd.DataFrame(preview["blocked_contracts"]), use_container_width=True)
                if preview.get("errors"):
                    st.error("数据质量错误")
                    st.dataframe(pd.DataFrame(preview["errors"]), use_container_width=True)

        if st.button("📦 发布当前本体版本", key="v51_publish"):
            try:
                repository.save_schema(schema)
                release = repository.publish_release(schema)
                st.success(f"发布完成：{release['release_id']} · {release['checksum']}")
                st.session_state.v5_mapping_last_release = release
            except (RepositoryError, ValueError) as exc:
                st.error(f"发布门禁未通过：{exc}")

    with tab_runs:
        st.markdown("**最近运行**")
        try:
            runs = repository.list_mapping_runs(schema.ontology_id, schema.version, limit=30)
            if runs:
                st.dataframe(pd.DataFrame(runs), use_container_width=True, hide_index=True)
            else:
                st.info("当前本体版本尚无映射运行记录。")
        except Exception as exc:
            st.error(f"读取运行历史失败：{exc}")
        last_run = st.session_state.get("v5_mapping_last_run")
        if last_run:
            result = last_run["result"]
            st.markdown("**最近一次执行结果**")
            st.json({
                "success": result.get("success"),
                "run_id": result.get("run_id"),
                "instance_count": result.get("instance_count"),
                "mapped_rows": result.get("mapped_rows"),
                "errors": len(result.get("errors", [])),
                "blocked_contracts": len(result.get("blocked_contracts", [])),
            })
            if result.get("errors"):
                st.dataframe(pd.DataFrame(result["errors"]), use_container_width=True)
            if result.get("instances"):
                instance_ids = list(result["instances"])
                selected_instance = st.selectbox("查看实例血缘", instance_ids, key="v51_lineage_instance")
                instance = result["instances"][selected_instance]
                st.json({
                    "instance_id": instance.id,
                    "properties": instance.properties,
                    "field_lineage": [
                        {"property_id": item.property_id, "source": item.source.__dict__, "source_field": item.source_field,
                         "mapping_contract_id": item.mapping_contract_id, "source_value_hash": item.source_value_hash}
                        for item in instance.lineage.fields
                    ],
                    "relationship_lineage": [
                        {"relationship_id": item.relationship_id, "source": item.source.__dict__, "target": item.target.__dict__,
                         "mapping_contract_id": item.mapping_contract_id}
                        for item in instance.lineage.relationships
                    ],
                })
