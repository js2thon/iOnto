"""Shared ontology_workspace paths (project root)."""

from __future__ import annotations

import os

# Project root = parent of ui/
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORKSPACE_DIR = os.path.join(_ROOT, "ontology_workspace")
VERSIONS_DIR = os.path.join(WORKSPACE_DIR, "versions")
QUESTION_SETS_DIR = os.path.join(WORKSPACE_DIR, "question_sets")
EVAL_RUNS_DIR = os.path.join(WORKSPACE_DIR, "eval_runs")
CURRENT_POINTER_FILE = os.path.join(WORKSPACE_DIR, "current.json")
LLM_CONFIG_FILE = os.path.join(WORKSPACE_DIR, "llm_config.json")
DEFAULT_QUESTION_SET_FILE = os.path.join(QUESTION_SETS_DIR, "default.json")

VERSION_STATUS_FLOW = ["draft", "validated", "released", "deprecated"]
QUESTION_CATEGORIES = ["basic", "relation", "aggregation", "complex", "boundary"]
