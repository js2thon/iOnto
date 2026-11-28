"""Competency question evaluation page."""
from __future__ import annotations

from typing import Any, Dict, List
import re

import streamlit as st
from ontology_query_pipeline import execute_query_pipeline
from openai import OpenAI

from ui.evaluation_lib import (
    attribute_failure,
    compare_answer,
    list_eval_runs,
    load_eval_run,
    load_question_set,
    save_eval_run,
    save_question_set,
    suggest_questions,
    summarize_results,
)
from ui.llm import _friendly_llm_error, _make_llm_client, ask_llm
from ui.llm_config import load_llm_config

def _ensure_list(value: Any) -> List[str]:
    """data_editor 中列表列可能被编辑为字符串；统一转 List[str]。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,，;；]", text) if part.strip()]


def _eval_state_to_run(eval_state: Dict[str, Any], status: str) -> Dict[str, Any]:
    return {
        "run_id": eval_state["run_id"],
        "version_id": eval_state["version_id"],
        "status": status,
        "started_at": eval_state.get("started_at") or datetime.now().isoformat(),
        "config": eval_state["config"],
        "results": eval_state["results"],
    }


def _render_evaluation_running(eval_state: Dict[str, Any]) -> None:
    """评测运行中：每次 run 只消费一题，完成后落盘并 rerun 下一题。"""
    questions = eval_state["questions"]
    cursor = eval_state["cursor"]
    total = len(questions)

    if cursor >= total:
        # 全部完成
        run = _eval_state_to_run(eval_state, status="completed")
        save_eval_run(run)
        st.session_state.eval_state = None
        st.success(f"测评完成：{total} 题。结果已保存到 {run['run_id']}。")
        st.rerun()
        return

    question = questions[cursor]
    st.progress(cursor / total, text=f"进度 {cursor}/{total}")
    st.info(f"正在测评第 {cursor + 1} 题：{question.get('question', '')}")

    # 停止按钮（题间生效）
    if st.button("⏹️ 停止测评"):
        run = _eval_state_to_run(eval_state, status="stopped")
        save_eval_run(run)
        st.session_state.eval_state = None
        st.warning(f"测评已停止（完成 {cursor}/{total} 题），结果已保存。")
        st.rerun()
        return

    # 消费当前一题：复用在线问答的确定性 Pipeline
    trace: List[Dict[str, Any]] = []
    error_text = ""
    answer = ""
    started = datetime.now()
    try:
        if not api_key:
            raise RuntimeError("请先配置 LLM API Key。")
        if OpenAI is None:
            raise RuntimeError("缺少 openai 依赖，请执行：pip install openai")
        client = _make_llm_client(api_key, base_url)
        pipeline_result = execute_query_pipeline(
            client, model_name, str(question.get("question", "")),
            eval_state["eval_ontology"],
            version_id=eval_state.get("version_id"),
        )
        answer = pipeline_result.get("answer", "")
        trace = pipeline_result.get("trace", [])
        if not pipeline_result.get("validation", {}).get("valid", False):
            error_text = "确定性结果校验未通过"
    except Exception as exc:
        error_text = _friendly_llm_error(exc)
        answer = f"本体查询 Pipeline 执行失败：{error_text}"
    elapsed = (datetime.now() - started).total_seconds()

    comparison = compare_answer(question, answer, trace)
    failure_code = (
        "" if comparison["passed"]
        else attribute_failure(
            question, answer, trace, comparison, error_text,
            eval_state["eval_ontology"],
        )
    )
    eval_state["results"].append({
        "question_id": question.get("question_id", ""),
        "question": question.get("question", ""),
        "category": question.get("category", ""),
        "expected_answer": question.get("expected_answer", ""),
        "answer": answer[:2000],
        "passed": comparison["passed"],
        "evidence_covered": comparison["evidence_covered"],
        "text_matched": comparison["text_matched"],
        "numbers_matched": comparison["numbers_matched"],
        "failure_code": failure_code,
        "pipeline_stages": len(trace),
        "tool_calls": len(trace),  # legacy compatibility for historical evaluation reports
        "elapsed_seconds": round(elapsed, 1),
        "error": error_text or None,
    })
    eval_state["cursor"] = cursor + 1

    # 逐题落盘（断点续跑依据）
    save_eval_run(_eval_state_to_run(eval_state, status="running"))
    st.rerun()


def _render_evaluation_results() -> None:
    """结果与归因：最近一次运行 + 指标 + 回归对比（简版）。"""
    runs = list_eval_runs()
    if not runs:
        st.info("尚无测评运行。")
        return

    run_options = [r["run_id"] for r in runs]
    selected_run_id = st.selectbox("选择测评运行", run_options)
    run = load_eval_run(selected_run_id)
    if not run:
        st.error("加载测评运行失败。")
        return

    metrics = run.get("metrics") or summarize_results(run)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("题数", metrics.get("total", 0))
    m2.metric(
        "答案准确率",
        f"{metrics.get('accuracy', 0):.0%}" if metrics.get("total") else "—",
    )
    m3.metric(
        "执行成功率",
        f"{metrics.get('execution_rate', 0):.0%}" if metrics.get("total") else "—",
    )
    m4.metric(
        "证据覆盖率",
        f"{metrics.get('evidence_rate', 0):.0%}" if metrics.get("total") else "—",
    )
    st.caption(
        f"运行 {run.get('run_id')} · 版本 {run.get('version_id')} · "
        f"状态 {run.get('status')} · 模型 {(run.get('config') or {}).get('model', '')}"
    )

    results = run.get("results") or []
    if results:
        st.markdown("**逐题结果**")
        st.dataframe(
            pd.DataFrame([
                {
                    "question_id": r.get("question_id"),
                    "问题": r.get("question"),
                    "通过": "✅" if r.get("passed") else "❌",
                    "归因": r.get("failure_code", ""),
                    "Pipeline 阶段": r.get("pipeline_stages", r.get("tool_calls", 0)),
                    "耗时(s)": r.get("elapsed_seconds"),
                }
                for r in results
            ]),
            use_container_width=True,
        )

        # 失败归因分布
        failures = [r.get("failure_code") for r in results if not r.get("passed")]
        if failures:
            st.markdown("**失败归因分布**")
            st.bar_chart(pd.Series(failures).value_counts())

    # 回归对比（简版）：与上一次运行按 question_id join
    if len(runs) >= 2:
        prev_run = load_eval_run(run_options[1])
        if prev_run:
            prev_map = {
                r.get("question_id"): r.get("passed")
                for r in prev_run.get("results") or []
            }
            regressions, improvements = [], []
            for r in results:
                qid = r.get("question_id")
                if qid in prev_map:
                    if prev_map[qid] and not r.get("passed"):
                        regressions.append(qid)
                    elif not prev_map[qid] and r.get("passed"):
                        improvements.append(qid)
            if regressions or improvements:
                st.markdown("**与上次运行对比（回归检测）**")
                c1, c2 = st.columns(2)
                c1.metric("⚠️ 退化（上次通过→本次失败）", len(regressions))
                c2.metric("✅ 改善（上次失败→本次通过）", len(improvements))
                if regressions:
                    st.warning(f"退化问题：{', '.join(regressions)}")


def render_evaluation() -> None:
    ontology = st.session_state.ontology_v2

    st.subheader("🧪 问题测评（CQ 问题集 · 自动测评 · 失败归因）")
    st.caption(
        "确定性比对为主：数值精确比对 + 证据召回（期望实体/关系出现在证据链中）。"
        "评测使用与在线问答相同的结构化查询 Pipeline，且必须先保存版本（版本绑定评测）。"
    )

    if ontology is None:
        st.info("请先生成本体，再进行问题测评。")
        return

    # ---- 评测运行中：一题一 rerun 状态机 ----
    eval_state = st.session_state.get("eval_state")
    if eval_state and eval_state.get("running"):
        _render_evaluation_running(eval_state)
        return

    # ---- 问题集管理 ----
    questions = load_question_set()

    tab_qs, tab_run, tab_results = st.tabs([
        "📋 问题集", "▶️ 运行测评", "📊 结果与归因",
    ])

    with tab_qs:
        st.markdown("**问题集编辑**（保存时自动为新增行分配 question_id）")
        edited = st.data_editor(
            pd.DataFrame(questions) if questions else pd.DataFrame(
                columns=[
                    "question_id", "question", "category",
                    "expected_answer", "expected_entities",
                    "expected_relations", "difficulty", "enabled",
                ]
            ),
            use_container_width=True,
            num_rows="dynamic",
            key="v4_question_editor",
        )
        c1, c2, c3 = st.columns([1, 1, 2])
        with c1:
            if st.button("💾 保存问题集", type="primary"):
                rows = edited.to_dict(orient="records")
                for row in rows:
                    row["enabled"] = bool(row.get("enabled", True))
                    row["expected_entities"] = _ensure_list(
                        row.get("expected_entities")
                    )
                    row["expected_relations"] = _ensure_list(
                        row.get("expected_relations")
                    )
                save_question_set(rows)
                st.success(f"已保存 {len(rows)} 个问题。")
                st.rerun()
        with c2:
            if st.button("🤖 从本体自动生成候选问题"):
                candidates = suggest_questions(ontology, max_questions=10)
                merged = questions + candidates
                save_question_set(merged)
                st.success(
                    f"已生成 {len(candidates)} 个候选问题"
                    "（provenance=auto，期望答案由本体确定性计算）。"
                )
                st.rerun()
        with c3:
            st.caption(
                "自动生成：计数题 / 属性题 / 邻居题，期望答案从本体计算，"
                "零成本 ground truth，供人工复核后启用。"
            )

    with tab_run:
        enabled_questions = [q for q in questions if q.get("enabled", True)]
        st.write(f"启用的问题：**{len(enabled_questions)}** / {len(questions)}")

        if not enabled_questions:
            st.warning("问题集为空。请先在「问题集」tab 添加或自动生成问题。")
        elif not api_key:
            st.warning("请先在侧边栏配置 LLM（API Key）。")
        else:
            if st.button("▶️ 开始测评", type="primary"):
                # 强制先保存版本（版本绑定评测，保证可复现）
                version_id, save_error = save_version(
                    ontology, note="测评前自动保存"
                )
                if save_error:
                    st.error(f"版本保存失败，无法开始测评：{save_error}")
                else:
                    # 冻结副本：防止评测期间 Action 修改污染
                    frozen, freeze_error = load_version(version_id)
                    if frozen is None:
                        st.error(f"冻结副本失败：{freeze_error}")
                    else:
                        st.session_state.eval_state = {
                            "running": True,
                            "cursor": 0,
                            "questions": enabled_questions,
                            "results": [],
                            "run_id": f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                            "version_id": version_id,
                            "config": {
                                "model": model_name,
                                "base_url": base_url,
                                "mode": "tool_calling",
                                "max_rounds": 4,
                            },
                            "eval_ontology": frozen,
                        }
                        st.rerun()

    with tab_results:
        _render_evaluation_results()


# ---------------------------------------------------------------------
