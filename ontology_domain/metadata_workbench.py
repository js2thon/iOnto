"""可编辑、可组合的源元数据工作台。

能力：
1. 持有多份 SourceMetadata（来自不同文件批 / PG schema / 手工）
2. 支持表级、列级字段编辑与关系候选调整
3. 合并多份元数据为统一视图，再交给 tbox_generator
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import json

from .metadata_extractor import (
    ColumnMetadata,
    SourceMetadata,
    TableMetadata,
    extract_source_metadata,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MetadataBundle:
    """一份可命名的元数据快照（一次导入或一次手工编辑结果）。"""

    id: str
    name: str
    metadata: SourceMetadata
    enabled: bool = True
    notes: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MetadataBundle":
        meta_raw = data.get("metadata") or {}
        tables: Dict[str, TableMetadata] = {}
        for tname, tdata in (meta_raw.get("tables") or {}).items():
            cols = [
                ColumnMetadata(**{k: v for k, v in c.items() if k in ColumnMetadata.__dataclass_fields__})
                for c in (tdata.get("columns") or [])
            ]
            tables[tname] = TableMetadata(
                name=tdata.get("name", tname),
                row_count=int(tdata.get("row_count") or 0),
                column_count=int(tdata.get("column_count") or len(cols)),
                columns=cols,
                primary_key_candidates=list(tdata.get("primary_key_candidates") or []),
                suggested_primary_key=tdata.get("suggested_primary_key"),
                foreign_key_candidates=list(tdata.get("foreign_key_candidates") or []),
                description=str(tdata.get("description") or ""),
            )
        meta = SourceMetadata(
            tables=tables,
            relationship_candidates=list(meta_raw.get("relationship_candidates") or []),
            extracted_at=str(meta_raw.get("extracted_at") or ""),
            source_kind=str(meta_raw.get("source_kind") or "dataframe"),
        )
        return cls(
            id=str(data.get("id") or f"bundle:{_now()}"),
            name=str(data.get("name") or "unnamed"),
            metadata=meta,
            enabled=bool(data.get("enabled", True)),
            notes=str(data.get("notes") or ""),
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
        )


class MetadataWorkbench:
    """管理多 bundle，支持编辑与合并。"""

    def __init__(self) -> None:
        self.bundles: Dict[str, MetadataBundle] = {}
        self.active_edits: Optional[SourceMetadata] = None  # 合并后的可编辑工作副本

    # ---- bundle management ----
    def add_from_dataframes(
        self,
        data: Dict[str, Any],
        *,
        name: str = "import",
        source_kind: str = "dataframe",
        bundle_id: Optional[str] = None,
    ) -> MetadataBundle:
        meta = extract_source_metadata(data, source_kind=source_kind)
        bid = bundle_id or f"bundle:{len(self.bundles)+1}:{_now()}"
        bundle = MetadataBundle(id=bid, name=name, metadata=meta)
        self.bundles[bid] = bundle
        self.rebuild_working_copy()
        return bundle

    def add_bundle(self, bundle: MetadataBundle) -> None:
        self.bundles[bundle.id] = bundle
        self.rebuild_working_copy()

    def remove_bundle(self, bundle_id: str) -> None:
        self.bundles.pop(bundle_id, None)
        self.rebuild_working_copy()

    def set_enabled(self, bundle_id: str, enabled: bool) -> None:
        if bundle_id in self.bundles:
            self.bundles[bundle_id].enabled = enabled
            self.bundles[bundle_id].updated_at = _now()
            self.rebuild_working_copy()

    def list_bundles(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": b.id,
                "name": b.name,
                "enabled": b.enabled,
                "tables": len(b.metadata.tables),
                "relationships": len(b.metadata.relationship_candidates),
                "source_kind": b.metadata.source_kind,
                "notes": b.notes,
            }
            for b in self.bundles.values()
        ]

    # ---- merge ----
    def merge_enabled(self) -> SourceMetadata:
        """合并所有 enabled bundle。同名表：后写覆盖列定义，关系候选去重合并。"""
        tables: Dict[str, TableMetadata] = {}
        rels: List[Dict[str, Any]] = []
        seen_rel: set = set()
        kinds: List[str] = []

        for b in self.bundles.values():
            if not b.enabled:
                continue
            kinds.append(b.metadata.source_kind)
            for tname, tmeta in b.metadata.tables.items():
                if tname not in tables:
                    tables[tname] = deepcopy(tmeta)
                else:
                    # merge columns by name (incoming overrides type/null stats if present)
                    existing = {c.name: c for c in tables[tname].columns}
                    for col in tmeta.columns:
                        existing[col.name] = deepcopy(col)
                    tables[tname].columns = list(existing.values())
                    tables[tname].column_count = len(tables[tname].columns)
                    tables[tname].row_count = max(tables[tname].row_count, tmeta.row_count)
                    # PK: prefer non-empty suggested
                    if tmeta.suggested_primary_key and not tables[tname].suggested_primary_key:
                        tables[tname].suggested_primary_key = tmeta.suggested_primary_key
                    # union pk candidates
                    pkset = list(
                        dict.fromkeys(
                            tables[tname].primary_key_candidates + tmeta.primary_key_candidates
                        )
                    )
                    tables[tname].primary_key_candidates = pkset
                    if tmeta.description and not tables[tname].description:
                        tables[tname].description = tmeta.description
            for rel in b.metadata.relationship_candidates:
                key = (
                    rel.get("source_table"),
                    rel.get("target_table"),
                    rel.get("source_column"),
                    rel.get("target_column"),
                )
                if key in seen_rel:
                    continue
                seen_rel.add(key)
                rels.append(dict(rel))

        rels.sort(key=lambda r: (-float(r.get("confidence") or 0), r.get("name") or ""))
        return SourceMetadata(
            tables=tables,
            relationship_candidates=rels,
            extracted_at=_now(),
            source_kind="+".join(sorted(set(kinds))) if kinds else "merged",
        )

    def rebuild_working_copy(self) -> SourceMetadata:
        self.active_edits = self.merge_enabled()
        return self.active_edits

    def get_working_copy(self) -> SourceMetadata:
        if self.active_edits is None:
            return self.rebuild_working_copy()
        return self.active_edits

    # ---- edits on working copy ----
    def update_table(
        self,
        table_name: str,
        *,
        suggested_primary_key: Optional[str] = None,
        description: Optional[str] = None,
        rename_to: Optional[str] = None,
    ) -> None:
        meta = self.get_working_copy()
        if table_name not in meta.tables:
            raise KeyError(f"表不存在: {table_name}")
        t = meta.tables[table_name]
        if suggested_primary_key is not None:
            t.suggested_primary_key = suggested_primary_key or None
            if suggested_primary_key and suggested_primary_key not in t.primary_key_candidates:
                t.primary_key_candidates.append(suggested_primary_key)
        if description is not None:
            t.description = description
        if rename_to and rename_to != table_name:
            new_name = rename_to.strip()
            if not new_name:
                raise ValueError("新表名不能为空")
            if new_name in meta.tables:
                raise ValueError(f"目标表名已存在: {new_name}")
            t.name = new_name
            meta.tables[new_name] = t
            del meta.tables[table_name]
            # fix relationships
            for rel in meta.relationship_candidates:
                if rel.get("source_table") == table_name:
                    rel["source_table"] = new_name
                if rel.get("target_table") == table_name:
                    rel["target_table"] = new_name

    def update_column(
        self,
        table_name: str,
        column_name: str,
        *,
        logical_type: Optional[str] = None,
        nullable: Optional[bool] = None,
        suspected_pk: Optional[bool] = None,
        suspected_fk: Optional[bool] = None,
        rename_to: Optional[str] = None,
    ) -> None:
        meta = self.get_working_copy()
        t = meta.tables.get(table_name)
        if not t:
            raise KeyError(f"表不存在: {table_name}")
        col = next((c for c in t.columns if c.name == column_name), None)
        if not col:
            raise KeyError(f"列不存在: {table_name}.{column_name}")
        if logical_type is not None:
            allowed = {
                "string",
                "integer",
                "number",
                "boolean",
                "date",
                "datetime",
                "enum",
                "object_ref",
                "json",
            }
            if logical_type not in allowed:
                raise ValueError(f"不支持的逻辑类型: {logical_type}")
            col.logical_type = logical_type
        if nullable is not None:
            col.nullable = nullable
        if suspected_pk is not None:
            col.suspected_pk = suspected_pk
            if suspected_pk and column_name not in t.primary_key_candidates:
                t.primary_key_candidates.append(column_name)
            if not suspected_pk and column_name in t.primary_key_candidates:
                t.primary_key_candidates = [c for c in t.primary_key_candidates if c != column_name]
        if suspected_fk is not None:
            col.suspected_fk = suspected_fk
        if rename_to and rename_to != column_name:
            new_name = rename_to.strip()
            if any(c.name == new_name for c in t.columns):
                raise ValueError(f"列名已存在: {new_name}")
            old = column_name
            col.name = new_name
            t.primary_key_candidates = [
                new_name if c == old else c for c in t.primary_key_candidates
            ]
            if t.suggested_primary_key == old:
                t.suggested_primary_key = new_name
            for rel in meta.relationship_candidates:
                if rel.get("source_table") == table_name and rel.get("source_column") == old:
                    rel["source_column"] = new_name
                if rel.get("target_table") == table_name and rel.get("target_column") == old:
                    rel["target_column"] = new_name

    def add_relationship_candidate(
        self,
        source_table: str,
        target_table: str,
        source_column: str,
        target_column: str,
        *,
        confidence: float = 0.8,
        name: Optional[str] = None,
    ) -> None:
        meta = self.get_working_copy()
        if source_table not in meta.tables or target_table not in meta.tables:
            raise KeyError("源表或目标表不存在")
        rel = {
            "name": name or f"{source_table}_to_{target_table}_via_{source_column}",
            "source_table": source_table,
            "target_table": target_table,
            "source_column": source_column,
            "target_column": target_column,
            "confidence": float(confidence),
            "value_coverage": None,
            "detection": "manual",
        }
        meta.relationship_candidates.append(rel)
        meta.relationship_candidates.sort(
            key=lambda r: (-float(r.get("confidence") or 0), r.get("name") or "")
        )

    def remove_relationship_candidate(self, index: int) -> None:
        meta = self.get_working_copy()
        if index < 0 or index >= len(meta.relationship_candidates):
            raise IndexError("关系候选索引越界")
        meta.relationship_candidates.pop(index)

    def set_relationship_confidence(self, index: int, confidence: float) -> None:
        meta = self.get_working_copy()
        meta.relationship_candidates[index]["confidence"] = float(confidence)

    def exclude_table(self, table_name: str) -> None:
        meta = self.get_working_copy()
        meta.tables.pop(table_name, None)
        meta.relationship_candidates = [
            r
            for r in meta.relationship_candidates
            if r.get("source_table") != table_name and r.get("target_table") != table_name
        ]

    # ---- serialization ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "bundles": {k: v.to_dict() for k, v in self.bundles.items()},
            "active_edits": self.active_edits.to_dict() if self.active_edits else None,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MetadataWorkbench":
        wb = cls()
        for bid, bdata in (data.get("bundles") or {}).items():
            wb.bundles[bid] = MetadataBundle.from_dict(bdata)
        if data.get("active_edits"):
            # rebuild active from stored shape via a temporary bundle roundtrip
            tmp = MetadataBundle.from_dict(
                {
                    "id": "working",
                    "name": "working",
                    "metadata": data["active_edits"],
                }
            )
            wb.active_edits = tmp.metadata
        else:
            wb.rebuild_working_copy()
        return wb
