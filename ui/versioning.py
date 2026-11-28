"""Local version snapshots for legacy Ontology runtime."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ontology_domain.legacy_runtime import Ontology
from ui.workspace import (
    CURRENT_POINTER_FILE,
    EVAL_RUNS_DIR,
    QUESTION_SETS_DIR,
    VERSION_STATUS_FLOW,
    VERSIONS_DIR,
    WORKSPACE_DIR,
)

def _ensure_workspace() -> None:
    """确保工作区目录存在（幂等）。"""
    for path in (WORKSPACE_DIR, VERSIONS_DIR, QUESTION_SETS_DIR, EVAL_RUNS_DIR):
        os.makedirs(path, exist_ok=True)


def _atomic_write_json(path: str, data: Any, compact: bool = True) -> None:
    """原子写 JSON：先写临时文件再 os.replace，显式 UTF-8（Windows 默认 GBK）。"""
    _ensure_workspace()
    text = json.dumps(
        data, ensure_ascii=False,
        separators=(",", ":") if compact else None,
        default=str,
    )
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".iOnto-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _read_json(path: str) -> Optional[Any]:
    """读 JSON 文件；不存在或损坏时返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _ontology_hash(ontology: Ontology) -> str:
    """本体内容 hash（不含时间戳），用于 dirty 检测。"""
    payload = json.dumps(
        ontology.snapshot_dict(), ensure_ascii=False,
        separators=(",", ":"), default=str, sort_keys=True,
    )
    import hashlib
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _verify_roundtrip(ontology: Ontology) -> Optional[str]:
    """round-trip 自检：export → from_dict → export，比较完整快照内容。

    返回错误描述；None 表示通过。save/load 前后均执行。
    """
    try:
        data = ontology.snapshot_dict()
        rebuilt = Ontology.from_dict(json.loads(json.dumps(data, default=str)))
        rebuilt_data = rebuilt.snapshot_dict()
        if data != rebuilt_data:
            return "快照 round-trip 后存在内容差异（Schema / 实例 / 关系 / 映射 / Action）"
        return None
    except Exception as exc:
        return f"round-trip 自检异常：{exc}"


# ---------------------------------------------------------------------
# 版本管理
# ---------------------------------------------------------------------
def _version_path(version_id: str) -> str:
    version_id = str(version_id or "")
    if (
        not version_id
        or version_id in {".", ".."}
        or os.path.basename(version_id) != version_id
        or any(sep in version_id for sep in ("/", chr(92)))
    ):
        raise ValueError("非法版本 ID")
    return os.path.join(VERSIONS_DIR, f"{version_id}.json")


def _new_version_id() -> str:
    """生成版本 ID：v{YYYYMMDD_HHMM}_{序号}，同分钟内递增。"""
    prefix = f"v{datetime.now().strftime('%Y%m%d_%H%M')}"
    existing = {v["id"] for v in list_versions()}
    seq = 1
    while f"{prefix}_{seq:02d}" in existing:
        seq += 1
    return f"{prefix}_{seq:02d}"


def list_versions() -> List[Dict[str, Any]]:
    """列出全部版本（按 ID 倒序 = 最新在前）。"""
    _ensure_workspace()
    versions: List[Dict[str, Any]] = []
    for filename in os.listdir(VERSIONS_DIR):
        if not filename.endswith(".json"):
            continue
        data = _read_json(os.path.join(VERSIONS_DIR, filename))
        if not isinstance(data, dict):
            continue
        meta = data.get("meta") or {}
        versions.append({
            "id": str(data.get("id") or filename[:-5]),
            "created_at": str(meta.get("created_at", "")),
            "status": str(meta.get("status", "draft")),
            "note": str(meta.get("note", "")),
            "stats": data.get("stats") or {},
        })
    versions.sort(key=lambda v: v["id"], reverse=True)
    return versions


def save_version(
    ontology: Ontology, note: str = "", status: str = "draft"
) -> Tuple[str, Optional[str]]:
    """保存当前本体为版本快照；返回 (版本 ID, 自检错误)。"""
    _ensure_workspace()
    if status not in VERSION_STATUS_FLOW:
        return "", f"非法版本状态：{status}"
    error = _verify_roundtrip(ontology)
    if error:
        return "", error

    version_id = _new_version_id()
    snapshot = ontology.snapshot_dict()
    snapshot["id"] = version_id
    snapshot["meta"] = {
        "created_at": datetime.now().isoformat(),
        "status": status,
        "note": note,
        "ontology_hash": _ontology_hash(ontology),
    }
    snapshot["stats"] = {
        "object_types": len(ontology.object_types),
        "objects": ontology.links.number_of_nodes(),
        "link_types": len(ontology.link_types),
        "links": ontology.links.number_of_edges(),
    }
    _atomic_write_json(_version_path(version_id), snapshot, compact=True)
    _update_current_pointer(version_id, _ontology_hash(ontology))
    return version_id, None


def load_version(version_id: str) -> Tuple[Optional[Ontology], Optional[str]]:
    """加载版本快照为 Ontology；返回 (本体, 自检错误)。"""
    data = _read_json(_version_path(version_id))
    if not isinstance(data, dict):
        return None, f"版本 {version_id} 不存在或已损坏"
    try:
        ontology = Ontology.from_dict(data)
    except Exception as exc:
        return None, f"版本 {version_id} 重建失败：{exc}"
    error = _verify_roundtrip(ontology)
    if error:
        return None, f"版本 {version_id} 自检失败：{error}"
    return ontology, None


def update_version_status(version_id: str, new_status: str) -> bool:
    """更新版本生命周期状态（draft → validated → released → deprecated）。"""
    path = _version_path(version_id)
    data = _read_json(path)
    if not isinstance(data, dict):
        return False
    if new_status not in VERSION_STATUS_FLOW:
        return False
    meta = data.setdefault("meta", {})
    old_status = str(meta.get("status", "draft"))
    allowed = {
        "draft": {"validated", "deprecated"},
        "validated": {"released", "deprecated"},
        "released": {"deprecated"},
        "deprecated": set(),
    }
    if new_status not in allowed.get(old_status, set()):
        return False
    meta["status"] = new_status
    _atomic_write_json(path, data, compact=True)
    return True


def _update_current_pointer(version_id: str, ontology_hash: str) -> None:
    _atomic_write_json(CURRENT_POINTER_FILE, {
        "version_id": version_id,
        "ontology_hash": ontology_hash,
        "updated_at": datetime.now().isoformat(),
    })


def read_current_pointer() -> Optional[Dict[str, Any]]:
    return _read_json(CURRENT_POINTER_FILE)


def diff_versions(version_id_a: str, version_id_b: str) -> Dict[str, Any]:
    """比较两个版本：Schema 层（类型/关系/映射）+ 实例计数层 + PK 级增删。"""
    data_a = _read_json(_version_path(version_id_a)) or {}
    data_b = _read_json(_version_path(version_id_b)) or {}

    def _schema_diff(
        key: str, fields: List[str]
    ) -> List[Dict[str, Any]]:
        items_a = data_a.get(key) or {}
        items_b = data_b.get(key) or {}
        rows: List[Dict[str, Any]] = []
        for name in sorted(set(items_a) | set(items_b)):
            info_a, info_b = items_a.get(name), items_b.get(name)
            if info_a is None:
                rows.append({"对象": name, "变更": "新增", "V1": "—", "V2": "存在"})
            elif info_b is None:
                rows.append({"对象": name, "变更": "删除", "V1": "存在", "V2": "—"})
            else:
                for field in fields:
                    va, vb = info_a.get(field), info_b.get(field)
                    if va != vb:
                        rows.append({
                            "对象": name,
                            "变更": f"修改 {field}",
                            "V1": str(va),
                            "V2": str(vb),
                        })
        return rows

    # 实例计数层：每类型对象数 / 每关系边数增量
    def _count_diff() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        objects_a = data_a.get("objects") or {}
        objects_b = data_b.get("objects") or {}
        object_rows = []
        pk_added, pk_removed = [], []
        total_objects = sum(len(v) for v in objects_b.values())
        for type_name in sorted(set(objects_a) | set(objects_b)):
            keys_a = set(objects_a.get(type_name, {}))
            keys_b = set(objects_b.get(type_name, {}))
            if len(keys_a) != len(keys_b) or keys_a != keys_b:
                object_rows.append({
                    "对象": type_name,
                    "变更": "实例变化",
                    "V1": f"{len(keys_a)} 个",
                    "V2": f"{len(keys_b)} 个",
                })
                if total_objects <= 2000:
                    pk_added.extend(
                        f"{type_name}:{k}" for k in sorted(keys_b - keys_a)
                    )
                    pk_removed.extend(
                        f"{type_name}:{k}" for k in sorted(keys_a - keys_b)
                    )
        links_a = data_a.get("links") or []
        links_b = data_b.get("links") or []
        edge_counts_a: Dict[str, int] = {}
        edge_counts_b: Dict[str, int] = {}
        for edge in links_a:
            key = str(edge.get("key") or edge.get("type") or "")
            edge_counts_a[key] = edge_counts_a.get(key, 0) + 1
        for edge in links_b:
            key = str(edge.get("key") or edge.get("type") or "")
            edge_counts_b[key] = edge_counts_b.get(key, 0) + 1
        link_rows = []
        for name in sorted(set(edge_counts_a) | set(edge_counts_b)):
            ca = edge_counts_a.get(name, 0)
            cb = edge_counts_b.get(name, 0)
            if ca != cb:
                link_rows.append({
                    "对象": name, "变更": "边数变化",
                    "V1": str(ca), "V2": str(cb),
                })
        return object_rows, link_rows, pk_added, pk_removed

    object_rows, link_rows, pk_added, pk_removed = _count_diff()
    return {
        "object_types": _schema_diff(
            "object_types", ["primary_key", "properties", "description"]
        ),
        "link_types": _schema_diff(
            "link_types",
            ["source", "target", "source_property", "target_property", "description"],
        ),
        "data_mapping": _schema_diff(
            "data_mapping", ["primary_key", "columns", "row_count"]
        ),
        "instance_counts": object_rows + link_rows,
        "pk_added": pk_added[:100],
        "pk_removed": pk_removed[:100],
        "pk_truncated": (
            len(pk_added) + len(pk_removed) > 100
        ),
    }


# ---------------------------------------------------------------------
