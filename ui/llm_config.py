"""Persist LLM API settings under ontology_workspace."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

def load_llm_config() -> Dict[str, str]:
    """从本地 llm_config.json 读取已保存的 LLM 配置。"""
    try:
        with open(LLM_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "api_key": str(data.get("api_key", "")),
            "base_url": str(data.get("base_url", "")),
            "model_name": str(data.get("model_name", "gpt-4o-mini")),
        }
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return {"api_key": "", "base_url": "", "model_name": "gpt-4o-mini"}


def save_llm_config(api_key: str, base_url: str, model_name: str) -> None:
    """将 LLM 配置保存到本地 llm_config.json。"""
    directory = os.path.dirname(LLM_CONFIG_FILE) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".llm-config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                {"api_key": api_key, "base_url": base_url, "model_name": model_name},
                f, ensure_ascii=False, indent=2,
            )
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates owner-only permissions (0600 on POSIX); keep the API
        # key out of partially written files and atomically replace the config.
        os.replace(tmp_path, LLM_CONFIG_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------
# 工作区持久化（版本快照 / 问题集 / 测评运行）
# ---------------------------------------------------------------------
WORKSPACE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ontology_workspace"
)
VERSIONS_DIR = os.path.join(WORKSPACE_DIR, "versions")
QUESTION_SETS_DIR = os.path.join(WORKSPACE_DIR, "question_sets")
EVAL_RUNS_DIR = os.path.join(WORKSPACE_DIR, "eval_runs")
CURRENT_POINTER_FILE = os.path.join(WORKSPACE_DIR, "current.json")

VERSION_STATUS_FLOW = ["draft", "validated", "released", "deprecated"]


