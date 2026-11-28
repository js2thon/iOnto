"""Smart QA page."""
from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st
from openai import OpenAI

from ontology_query_pipeline import execute_query_pipeline
from ui.llm import _friendly_llm_error, _make_llm_client
from ui.llm_config import load_llm_config
from ui.versioning import read_current_pointer

def render_qa() -> None:
    ontology = st.session_state.ontology_v2

    if ontology is None:
        st.info("请先生成本体（「数据源」→「本体建模」），再进行智能问答。")
        return

    st.subheader("基于本体的智能问答")
    st.caption(
        "LLM 负责语义理解和最终表达；查询规划器生成结构化计划，"
        "本体执行引擎确定性查询，校验器验证结果，溯源引擎生成证据链。"
    )

    # 当前版本标签
    pointer = read_current_pointer()
    version_label = (
        f"（版本 {pointer['version_id']}）" if pointer and pointer.get("version_id") else ""
    )
    if version_label:
        st.caption(f"当前绑定{version_label}")

    # 评测运行中禁用问答输入，防止交错
    eval_state = st.session_state.get("eval_state")
    chat_disabled = bool(eval_state and eval_state.get("running"))

    for message in st.session_state.messages_v2:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            # 证据溯源：展示该条回答的工具调用轨迹
            trace = message.get("trace")
            if trace:
                with st.expander(
                    f"🔍 查询证据链（{len(trace)} 个处理阶段）", expanded=False
                ):
                    for i, item in enumerate(trace, 1):
                        st.markdown(
                            f"**{i}. `{item.get('name')}`**　"
                            f"`{json.dumps(item.get('args', {}), ensure_ascii=False)[:120]}`"
                        )
                        st.code(
                            str(item.get("result", ""))[:600], language="json"
                        )

    question = st.chat_input(
        "例如：员工 1001 属于哪个部门？",
        key="v2_chat_input",
        disabled=chat_disabled,
    )
    if chat_disabled:
        st.warning("问题测评运行中，问答暂不可用。")

    if question:
        st.session_state.messages_v2.append({
            "role": "user", "content": question
        })
        with st.chat_message("user"):
            st.markdown(question)

        # 所有问答统一走确定性本体查询 Pipeline；不再提供直接上下文问答旁路。
        with st.chat_message("assistant"):
            status_box = st.empty()
            answer_box = st.empty()
            answer_parts: List[str] = []
            structured_trace: List[Dict[str, Any]] = []
            status_box.caption("正在理解问题并生成结构化本体查询计划…")
            try:
                if not api_key:
                    raise RuntimeError("请先配置 LLM API Key。")
                if OpenAI is None:
                    raise RuntimeError("缺少 openai 依赖，请执行：pip install openai")
                client = _make_llm_client(api_key, base_url)
                pipeline_result = execute_query_pipeline(
                    client, model_name, question, ontology,
                    version_id=pointer.get("version_id") if pointer else None,
                )
                structured_trace = pipeline_result.get("trace", [])
                answer = pipeline_result.get("answer", "")
                validation = pipeline_result.get("validation", {})
                if not validation.get("valid", False):
                    answer_box.error(answer)
                else:
                    answer_box.markdown(answer)
                answer_parts.append(answer)
                status_box.caption(
                    "查询计划已生成 → 本体查询已执行 → "
                    + ("结果校验通过" if validation.get("valid") else "结果校验未通过")
                    + " → 证据链已生成"
                )
            except Exception as exc:
                answer = f"本体查询 Pipeline 执行失败：{_friendly_llm_error(exc)}"
                answer_parts.append(answer)
                answer_box.error(answer)
                structured_trace.append({
                    "name": "pipeline_error", "args": {},
                    "result": str(exc),
                })
            finally:
                status_box.empty()


        st.session_state.messages_v2.append({
            "role": "assistant",
            "content": answer,
            "trace": structured_trace,
            "version_id": pointer.get("version_id") if pointer else None,
        })


# ---------------------------------------------------------------------
