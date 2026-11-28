"""Competency-question evaluation helpers."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ontology_domain.legacy_runtime import Ontology
from ui.versioning import _atomic_write_json, _ensure_workspace, _read_json
from ui.workspace import (
    DEFAULT_QUESTION_SET_FILE,
    EVAL_RUNS_DIR,
    QUESTION_CATEGORIES,
)

def load_question_set() -> List[Dict[str, Any]]:
    """加载默认问题集；不存在时返回空列表。"""
    data = _read_json(DEFAULT_QUESTION_SET_FILE)
    if isinstance(data, dict):
        return list(data.get("questions") or [])
    return []


def save_question_set(questions: List[Dict[str, Any]]) -> None:
    """保存问题集；为缺失 question_id 的条目自动分配（Q001 递增）。"""
    existing_ids = {
        q.get("question_id") for q in questions if q.get("question_id")
    }
    max_num = 0
    for qid in existing_ids:
        match = re.match(r"^Q(\d+)$", str(qid))
        if match:
            max_num = max(max_num, int(match.group(1)))
    for question in questions:
        if not question.get("question_id"):
            max_num += 1
            question["question_id"] = f"Q{max_num:03d}"
    _atomic_write_json(DEFAULT_QUESTION_SET_FILE, {
        "questions": questions,
        "updated_at": datetime.now().isoformat(),
    })


def suggest_questions(ontology: Ontology, max_questions: int = 10) -> List[Dict[str, Any]]:
    """从本体确定性生成候选问题（期望答案由本体计算，零成本 ground truth）。

    模板：计数题（count_objects）、属性题（get_object）、邻居题（get_neighbors）。
    标记 provenance=auto 供人工复核。
    """
    candidates: List[Dict[str, Any]] = []

    # 1) 计数题：每类型一题
    for type_name in ontology.object_types:
        count = len(ontology.objects.get(type_name, {}))
        if count == 0:
            continue
        candidates.append({
            "question": f"本体中一共有多少个 {type_name}？",
            "category": "aggregation",
            "expected_answer": str(count),
            "expected_entities": [type_name],
            "expected_relations": [],
            "difficulty": "easy",
            "provenance": "auto",
        })

    # 2) 属性题：每类型取第一个对象
    for type_name, objects in ontology.objects.items():
        if not objects:
            continue
        pk, obj = next(iter(objects.items()))
        pk_field = ontology.object_types[type_name]["primary_key"]
        # 找一个非主键属性作为期望
        prop = next(
            (p for p in ontology.object_types[type_name]["properties"]
             if p != pk_field and obj.get(p) is not None),
            None,
        )
        if prop is None:
            continue
        candidates.append({
            "question": f"{type_name} {pk} 的 {prop} 是什么？",
            "category": "basic",
            "expected_answer": str(obj[prop]),
            "expected_entities": [f"{type_name}:{pk}"],
            "expected_relations": [],
            "difficulty": "easy",
            "provenance": "auto",
        })

    # 3) 邻居题：取有边的节点
    for node_id in list(ontology.links.nodes):
        if len(candidates) >= max_questions:
            break
        neighbors = list(ontology.links.predecessors(node_id))
        neighbors.extend(ontology.links.successors(node_id))
        if not neighbors:
            continue
        candidates.append({
            "question": f"{node_id} 直接关联的实体有哪些？",
            "category": "relation",
            "expected_answer": ", ".join(sorted(neighbors)[:10]),
            "expected_entities": [node_id] + sorted(neighbors)[:5],
            "expected_relations": [],
            "difficulty": "medium",
            "provenance": "auto",
        })

    return candidates[:max_questions]


# ---------------------------------------------------------------------
# 问题测评：确定性比对 + 失败归因 + 运行记录
# ---------------------------------------------------------------------
def _normalize_text(text: str) -> str:
    """答案文本归一化：小写、全角转半角、去标点/空白/markdown 符号。"""
    text = str(text or "").lower()
    # 全角转半角
    text = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch
        for ch in text
    )
    text = re.sub(r"[*_`#>\[\](){}|~]", " ", text)
    text = re.sub(r"[，。、；：？！,.;:?!\"'\s\-—–]", " ", text)
    return " ".join(text.split())


def _extract_numbers(text: str) -> List[float]:
    """抽取答案中的数值（处理千分位、百分比、单位）。"""
    text = str(text or "")
    # 去掉千分位逗号
    cleaned = re.sub(r"(\d),(\d{3})", r"\1\2", text)
    numbers = []
    for match in re.finditer(r"-?\d+(?:\.\d+)?", cleaned):
        try:
            numbers.append(float(match.group()))
        except ValueError:
            continue
    return numbers


def compare_answer(
    question: Dict[str, Any],
    answer: str,
    trace: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """确定性比对：主指标为证据召回（expected 出现在工具结果中），辅以文本比对。

    返回 {"passed", "evidence_covered", "text_matched", "numbers_matched"}。
    """
    expected_answer = str(question.get("expected_answer") or "")
    expected_entities = [
        str(e) for e in (question.get("expected_entities") or [])
    ]
    expected_relations = [
        str(r) for r in (question.get("expected_relations") or [])
    ]

    # 主指标：证据召回——expected_entities/relations 出现在工具返回结果中
    tool_blob = " ".join(
        str(item.get("result") or "") for item in trace
    )
    evidence_covered = bool(trace) and all(
        expected in tool_blob for expected in expected_entities + expected_relations
    ) if (expected_entities or expected_relations) else bool(trace)

    # 辅助：文本归一化包含比对
    norm_answer = _normalize_text(answer)
    norm_expected = _normalize_text(expected_answer)
    text_matched = bool(norm_expected) and norm_expected in norm_answer

    # 数值比对：期望与答案中的数字集合按容差比较
    numbers_matched = None
    expected_numbers = _extract_numbers(expected_answer)
    if expected_numbers:
        answer_numbers = _extract_numbers(answer)
        numbers_matched = len(answer_numbers) > 0 and all(
            any(abs(a - e) <= max(0.01, abs(e) * 0.001) for a in answer_numbers)
            for e in expected_numbers
        )

    passed = bool(
        (numbers_matched is True)
        or (text_matched and evidence_covered)
        or (text_matched and not (expected_entities or expected_relations))
    )
    return {
        "passed": passed,
        "evidence_covered": evidence_covered,
        "text_matched": text_matched,
        "numbers_matched": numbers_matched,
    }


def attribute_failure(
    question: Dict[str, Any],
    answer: str,
    trace: List[Dict[str, Any]],
    comparison: Dict[str, Any],
    error_text: str,
    ontology: Ontology,
) -> str:
    """失败归因：有序决策树，一条结果只归一个码（可复现）。

    E00 未配置 LLM / E01 API 错误 / E02 超时 / E04 无查询证据阶段 /
    E05 工具结果未覆盖期望 / E06 覆盖但答案缺失 / E07 数值不匹配 /
    E08 文本不匹配 / E09 问题集漂移（期望实体不存在）/ E10 轮次耗尽
    """
    if not answer and not trace and not error_text:
        return "E00"
    if error_text:
        if "超时" in error_text or "timeout" in error_text.lower():
            return "E02"
        return "E01"
    # 问题集漂移：期望实体在当前版本不存在（不计模型失败率）
    for expected in question.get("expected_entities") or []:
        expected = str(expected)
        if ":" in expected:
            type_name, pk = expected.split(":", 1)
            if type_name not in ontology.objects or pk not in ontology.objects.get(type_name, {}):
                return "E09"
        elif expected not in ontology.object_types:
            return "E09"
    if not trace:
        return "E04"
    if not comparison.get("evidence_covered"):
        return "E05"
    if not answer.strip():
        return "E06"
    if comparison.get("numbers_matched") is False:
        return "E07"
    if not comparison.get("text_matched"):
        return "E08"
    return "E10"


def summarize_results(run: Dict[str, Any]) -> Dict[str, Any]:
    """汇总测评指标：答案准确率 / 执行成功率 / 证据覆盖率。"""
    results = run.get("results") or []
    total = len(results)
    if total == 0:
        return {"total": 0, "accuracy": 0.0, "execution_rate": 0.0,
                "evidence_rate": 0.0}
    passed = sum(1 for r in results if r.get("passed"))
    executed = sum(1 for r in results if r.get("error") is None)
    evidenced = sum(1 for r in results if r.get("evidence_covered"))
    return {
        "total": total,
        "accuracy": round(passed / total, 4),
        "execution_rate": round(executed / total, 4),
        "evidence_rate": round(evidenced / total, 4),
    }


def list_eval_runs() -> List[Dict[str, Any]]:
    """列出全部测评运行（按 run_id 倒序）。"""
    _ensure_workspace()
    runs: List[Dict[str, Any]] = []
    for filename in os.listdir(EVAL_RUNS_DIR):
        if not filename.endswith(".json"):
            continue
        data = _read_json(os.path.join(EVAL_RUNS_DIR, filename))
        if not isinstance(data, dict):
            continue
        runs.append({
            "run_id": str(data.get("run_id") or filename[:-5]),
            "version_id": str(data.get("version_id") or ""),
            "status": str(data.get("status") or "completed"),
            "started_at": str(data.get("started_at") or ""),
            "config": data.get("config") or {},
            "metrics": data.get("metrics") or {},
            "result_count": len(data.get("results") or []),
        })
    runs.sort(key=lambda r: r["run_id"], reverse=True)
    return runs


def load_eval_run(run_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(os.path.join(EVAL_RUNS_DIR, f"{run_id}.json"))


def save_eval_run(run: Dict[str, Any]) -> None:
    """保存测评运行（逐题追加时也整体落盘，支持断点续跑）。"""
    run["metrics"] = summarize_results(run)
    _atomic_write_json(
        os.path.join(EVAL_RUNS_DIR, f"{run['run_id']}.json"), run, compact=True
    )


# ---------------------------------------------------------------------
# Streamlit 主界面（st.navigation 多页 + 页面函数）
# ---------------------------------------------------------------------
st.title("🕸️ iOnto V5.1")
st.caption(
    "企业本体建模 Pipeline 工作台 · 数据源 → 本体建模 → 映射工作台 → 智能问答 → "
    "问题测评 → 版本管理 · CSV/Excel · Schema/实例图谱 · LLM 问答 · "
    "证据溯源 · 确定性测评 · 版本快照/回滚"
)

# ---- 全局 session_state（V5 Schema 为结构真相源，见 ui.session） ----
from ui.session import (
    after_structural_edit,
    ensure_canonical_schema,
    get_v5_schema,
    init_session,
    set_v5_schema,
    sync_legacy_from_v5,
)

init_session()

# ---- 刷新恢复：session 为空且 current.json 存在时自动重建本体 ----
if st.session_state.ontology_v2 is None:
    pointer = read_current_pointer()
    if pointer and pointer.get("version_id"):
        ontology_restored, restore_error = load_version(
            str(pointer["version_id"])
        )
        if ontology_restored is not None:
            st.session_state.ontology_v2 = ontology_restored
            st.session_state.restored_version_id = str(pointer["version_id"])
        elif restore_error:
            st.warning(f"自动恢复版本失败：{restore_error}")


# ---- 侧边栏：LLM 配置 + 问答模式（全局共享） ----
with st.sidebar:
    st.header("⚙️ LLM 配置（可选）")
    saved_llm_config = load_llm_config()
    api_key = st.text_input(
        "API Key", value=saved_llm_config["api_key"], type="password"
    )
    base_url = st.text_input(
        "Base URL",
        value=saved_llm_config["base_url"],
        placeholder="https://api.openai.com/v1",
    )
    model_name = st.text_input(
        "模型名称", value=saved_llm_config["model_name"]
    )
    if st.button("保存 LLM 配置"):
        try:
            save_llm_config(api_key, base_url, model_name)
            st.success("LLM 配置已保存，下次启动自动加载。")
        except OSError as exc:
            st.error(f"保存失败：{exc}")

    st.header("💬 问答模式")
    st.info(
        "固定使用确定性本体查询 Pipeline：LLM 解析问题，"
        "查询规划器生成计划，本体引擎执行，校验器验证，溯源引擎生成证据。"
    )

    st.divider()
    if st.button("🗑️ 重置当前本体"):
        st.session_state.ontology_v2 = None
        st.session_state.raw_data_v2 = {}
        st.session_state.messages_v2 = []
        st.session_state.pk_config = {}
        st.session_state.manual_links_config = []
        st.session_state.source_kinds_v2 = {}
        for state_key in (
            "v5_mapping_schema", "v5_mapping_source_fingerprint",
            "v5_mapping_source_changed", "v5_mapping_preview",
            "v5_mapping_last_run", "v5_mapping_last_release",
        ):
            st.session_state.pop(state_key, None)
        st.rerun()


# ---------------------------------------------------------------------
