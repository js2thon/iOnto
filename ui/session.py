"""统一 session 状态：V5 Schema 为结构真相源。"""

from __future__ import annotations

from typing import Any, Optional

import streamlit as st

from ontology_domain.models import OntologySchema
from ontology_domain.runtime_bridge import (
    ensure_v5_schema,
    project_structure_to_legacy,
    structure_diff,
)

# Canonical keys
KEY_ONTOLOGY = "ontology_v2"
KEY_V5_SCHEMA = "v5_schema"
KEY_V5_TBOX = "v5_tbox_schema"  # 兼容旧 key，写入时同步到 KEY_V5_SCHEMA
KEY_RAW_DATA = "raw_data_v2"
KEY_META_WB = "metadata_workbench"
KEY_SOURCE_META = "source_metadata"
KEY_PK_CONFIG = "pk_config"
KEY_MANUAL_LINKS = "manual_links_config"
KEY_MESSAGES = "messages_v2"


def init_session() -> None:
    defaults = {
        KEY_ONTOLOGY: None,
        KEY_RAW_DATA: {},
        KEY_MESSAGES: [],
        KEY_PK_CONFIG: {},
        KEY_MANUAL_LINKS: [],
        "restored_version_id": None,
        KEY_V5_SCHEMA: None,
        KEY_V5_TBOX: None,
        KEY_META_WB: None,
        KEY_SOURCE_META: None,
        "canvas_edit_log": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def get_ontology() -> Any:
    return st.session_state.get(KEY_ONTOLOGY)


def set_ontology(ontology: Any) -> None:
    st.session_state[KEY_ONTOLOGY] = ontology


def get_v5_schema() -> Optional[OntologySchema]:
    schema = st.session_state.get(KEY_V5_SCHEMA)
    if schema is None:
        schema = st.session_state.get(KEY_V5_TBOX)
        if schema is not None:
            st.session_state[KEY_V5_SCHEMA] = schema
    return schema


def set_v5_schema(schema: OntologySchema, *, project: bool = True) -> None:
    """写入唯一 V5 真相源；可选投影到 legacy 结构。"""
    st.session_state[KEY_V5_SCHEMA] = schema
    st.session_state[KEY_V5_TBOX] = schema  # 兼容旧 UI
    if project:
        ontology = get_ontology()
        if ontology is not None:
            project_structure_to_legacy(schema, ontology)


def ensure_canonical_schema() -> Optional[OntologySchema]:
    """若有 ontology 但无 schema，从 legacy 生成并设为真相源。"""
    schema = get_v5_schema()
    ontology = get_ontology()
    if schema is not None:
        return schema
    if ontology is None:
        return None
    schema = ensure_v5_schema(ontology, None)
    set_v5_schema(schema, project=False)
    return schema


def sync_legacy_from_v5() -> Optional[dict]:
    """强制用 V5 刷新 legacy 结构，返回 diff 结果。"""
    schema = get_v5_schema()
    ontology = get_ontology()
    if schema is None or ontology is None:
        return None
    project_structure_to_legacy(schema, ontology)
    return structure_diff(schema, ontology)


def after_structural_edit(editor: Any) -> OntologySchema:
    """CanvasEditService 等编辑完成后调用。"""
    schema = editor.get_v5_schema()
    if schema is None:
        schema = ensure_canonical_schema()
    if schema is None:
        raise RuntimeError("结构编辑后仍无 V5 Schema")
    set_v5_schema(schema, project=True)
    if hasattr(editor, "get_edit_log"):
        st.session_state["canvas_edit_log"] = editor.get_edit_log()
    return schema
