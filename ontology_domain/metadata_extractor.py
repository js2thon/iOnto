"""从数据源提取结构化元数据，供 TBox 生成使用。

输入：pandas DataFrame 字典（CSV/Excel/PG 加载结果）
输出：SourceMetadata 报告（表级 + 列级统计、类型推断、候选主键/外键）

不修改业务数据，只做只读分析。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import re

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore


# ---------------------------------------------------------------------------
# 数据类型推断
# ---------------------------------------------------------------------------
_BOOL_TOKENS = {"true", "false", "0", "1", "yes", "no", "y", "n", "是", "否"}


def _infer_logical_type(series: "pd.Series") -> str:
    """推断逻辑类型：string/integer/number/boolean/date/datetime/json。"""
    non_null = series.dropna()
    if len(non_null) == 0:
        return "string"

    # pandas dtype hints
    dtype = str(series.dtype).lower()
    if "bool" in dtype:
        return "boolean"
    if "int" in dtype:
        return "integer"
    if "float" in dtype or "double" in dtype:
        return "number"
    if "datetime" in dtype or "timestamp" in dtype:
        return "datetime"
    if "date" in dtype:
        return "date"

    sample = non_null.astype(str).str.strip()
    sample = sample[sample != ""]
    if len(sample) == 0:
        return "string"

    # boolean
    lower = sample.str.lower()
    if lower.isin(_BOOL_TOKENS).mean() > 0.95:
        return "boolean"

    # integer
    try:
        as_num = pd.to_numeric(sample, errors="coerce")
        if as_num.notna().mean() > 0.95:
            if (as_num.dropna() % 1 == 0).all():
                return "integer"
            return "number"
    except Exception:
        pass

    # datetime / date
    try:
        parsed = pd.to_datetime(sample, errors="coerce", utc=False, format="mixed")
        if parsed.notna().mean() > 0.9:
            # if all times are midnight → date
            times = parsed.dropna()
            if len(times) and (times.dt.hour.fillna(0) == 0).all() and (times.dt.minute.fillna(0) == 0).all():
                return "date"
            return "datetime"
    except Exception:
        pass

    # json-ish
    if sample.str.match(r"^\s*[\{\[]").mean() > 0.8:
        return "json"

    return "string"


def _is_unique_non_null(series: "pd.Series") -> bool:
    non_null = series.dropna()
    if len(non_null) == 0:
        return False
    if non_null.astype(str).str.strip().eq("").any():
        return False
    return non_null.astype(str).is_unique


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ColumnMetadata:
    name: str
    logical_type: str
    pandas_dtype: str
    nullable: bool
    null_ratio: float
    unique_count: int
    unique_ratio: float
    is_unique: bool
    sample_values: List[Any] = field(default_factory=list)
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    avg_length: Optional[float] = None
    suspected_pk: bool = False
    suspected_fk: bool = False
    fk_candidates: List[Dict[str, str]] = field(default_factory=list)
    name_hints: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TableMetadata:
    name: str
    row_count: int
    column_count: int
    columns: List[ColumnMetadata]
    primary_key_candidates: List[str] = field(default_factory=list)
    suggested_primary_key: Optional[str] = None
    foreign_key_candidates: List[Dict[str, str]] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "columns": [c.to_dict() for c in self.columns],
            "primary_key_candidates": self.primary_key_candidates,
            "suggested_primary_key": self.suggested_primary_key,
            "foreign_key_candidates": self.foreign_key_candidates,
            "description": self.description,
        }


@dataclass
class SourceMetadata:
    tables: Dict[str, TableMetadata]
    relationship_candidates: List[Dict[str, Any]] = field(default_factory=list)
    extracted_at: str = ""
    source_kind: str = "dataframe"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "extracted_at": self.extracted_at,
            "tables": {k: v.to_dict() for k, v in self.tables.items()},
            "relationship_candidates": self.relationship_candidates,
        }


# ---------------------------------------------------------------------------
# 提取逻辑
# ---------------------------------------------------------------------------
def _column_name_hints(col: str) -> List[str]:
    hints = []
    cl = col.lower()
    if cl in ("id", "pk") or cl.endswith("_id") or cl.endswith("id"):
        hints.append("id_like")
    if "name" in cl or "title" in cl or "label" in cl:
        hints.append("label_like")
    if any(x in cl for x in ("email", "mail")):
        hints.append("email")
    if any(x in cl for x in ("phone", "mobile", "tel")):
        hints.append("phone")
    if any(x in cl for x in ("date", "time", "at", "created", "updated")):
        hints.append("temporal")
    if any(x in cl for x in ("amount", "price", "cost", "qty", "quantity", "count")):
        hints.append("measure")
    if cl.startswith("is_") or cl.startswith("has_") or cl.endswith("_flag"):
        hints.append("flag")
    return hints


def extract_table_metadata(table_name: str, df: "pd.DataFrame") -> TableMetadata:
    if pd is None:
        raise RuntimeError("pandas is required for metadata extraction")

    work = df.copy()
    work.columns = [str(c).strip() for c in work.columns]
    work = work.loc[:, [c for c in work.columns if c != ""]]

    columns: List[ColumnMetadata] = []
    pk_candidates: List[str] = []

    for col in work.columns:
        series = work[col]
        n = len(series)
        null_count = int(series.isna().sum())
        null_ratio = (null_count / n) if n else 1.0
        non_null = series.dropna()
        unique_count = int(non_null.nunique()) if len(non_null) else 0
        unique_ratio = (unique_count / len(non_null)) if len(non_null) else 0.0
        is_unique = _is_unique_non_null(series)
        logical = _infer_logical_type(series)
        samples = [str(v) for v in non_null.head(5).tolist()]

        min_v = max_v = None
        avg_len = None
        try:
            if logical in {"integer", "number"} and len(non_null):
                nums = pd.to_numeric(non_null, errors="coerce").dropna()
                if len(nums):
                    min_v, max_v = str(nums.min()), str(nums.max())
            elif len(non_null):
                as_str = non_null.astype(str)
                avg_len = float(as_str.str.len().mean())
                min_v, max_v = as_str.min(), as_str.max()
        except Exception:
            pass

        hints = _column_name_hints(str(col))
        suspected_pk = is_unique and null_ratio == 0.0
        if suspected_pk:
            pk_candidates.append(str(col))

        columns.append(
            ColumnMetadata(
                name=str(col),
                logical_type=logical,
                pandas_dtype=str(series.dtype),
                nullable=null_ratio > 0,
                null_ratio=round(null_ratio, 4),
                unique_count=unique_count,
                unique_ratio=round(unique_ratio, 4),
                is_unique=is_unique,
                sample_values=samples,
                min_value=min_v,
                max_value=max_v,
                avg_length=round(avg_len, 2) if avg_len is not None else None,
                suspected_pk=suspected_pk,
                name_hints=hints,
            )
        )

    suggested = _suggest_primary_key(table_name, columns, pk_candidates)

    return TableMetadata(
        name=table_name,
        row_count=len(work),
        column_count=len(columns),
        columns=columns,
        primary_key_candidates=pk_candidates,
        suggested_primary_key=suggested,
        description=f"数据表 {table_name}，{len(work)} 行 × {len(columns)} 列",
    )


def _suggest_primary_key(
    table_name: str, columns: List[ColumnMetadata], candidates: List[str]
) -> Optional[str]:
    if not candidates:
        return None
    tl = table_name.lower()

    def score(name: str) -> Tuple[int, int]:
        cl = name.lower()
        s = 0
        if cl == "id":
            s += 100
        if cl == f"{tl}_id":
            s += 90
        if cl.endswith("_id"):
            s += 50
        if cl == "pk":
            s += 80
        # prefer shorter names slightly
        return (-s, len(name))

    ranked = sorted(candidates, key=score)
    return ranked[0] if ranked else None


def _detect_fk_relationships(
    tables: Dict[str, TableMetadata],
    data: Dict[str, "pd.DataFrame"],
) -> List[Dict[str, Any]]:
    """基于列名约定 + 值包含关系，生成外键/关系候选。"""
    rels: List[Dict[str, Any]] = []
    table_names = list(tables.keys())

    pk_by_table = {
        t: tables[t].suggested_primary_key
        for t in table_names
        if tables[t].suggested_primary_key
    }

    for src in table_names:
        src_df = data.get(src)
        if src_df is None:
            continue
        src_meta = tables[src]
        for col in src_meta.columns:
            cl = col.name.lower()
            for tgt in table_names:
                if tgt == src:
                    continue
                tgt_pk = pk_by_table.get(tgt)
                if not tgt_pk:
                    continue
                tgt_pk_l = tgt_pk.lower()
                tgt_l = tgt.lower()

                name_match = (
                    cl == f"{tgt_l}_id"
                    or cl == tgt_pk_l
                    or cl.endswith(f"_{tgt_l}_id")
                    or cl == f"{tgt_l}{tgt_pk_l}"
                )
                if not name_match:
                    continue

                # value overlap check (soft)
                coverage = None
                try:
                    src_vals = set(src_df[col.name].dropna().astype(str))
                    tgt_vals = set(data[tgt][tgt_pk].dropna().astype(str))
                    if src_vals:
                        coverage = len(src_vals & tgt_vals) / len(src_vals)
                except Exception:
                    coverage = None

                confidence = 0.6
                if name_match:
                    confidence += 0.2
                if coverage is not None:
                    confidence += 0.2 * min(1.0, coverage)

                rels.append(
                    {
                        "name": f"{src}_to_{tgt}_via_{col.name}",
                        "source_table": src,
                        "target_table": tgt,
                        "source_column": col.name,
                        "target_column": tgt_pk,
                        "confidence": round(min(confidence, 0.99), 3),
                        "value_coverage": None if coverage is None else round(coverage, 3),
                        "detection": "name_pattern+value_overlap",
                    }
                )
                col.suspected_fk = True
                col.fk_candidates.append(
                    {"target_table": tgt, "target_column": tgt_pk}
                )

    # stable order
    rels.sort(key=lambda r: (-r["confidence"], r["name"]))
    return rels


def extract_source_metadata(
    data: Dict[str, "pd.DataFrame"],
    *,
    source_kind: str = "dataframe",
) -> SourceMetadata:
    """从多表 DataFrame 提取完整源元数据。"""
    from datetime import datetime, timezone

    if pd is None:
        raise RuntimeError("pandas is required")

    tables: Dict[str, TableMetadata] = {}
    normalized: Dict[str, "pd.DataFrame"] = {}

    for name, df in data.items():
        if df is None:
            continue
        meta = extract_table_metadata(str(name), df)
        tables[str(name)] = meta
        work = df.copy()
        work.columns = [str(c).strip() for c in work.columns]
        normalized[str(name)] = work

    relationships = _detect_fk_relationships(tables, normalized)

    # attach fk candidates onto table metadata
    for rel in relationships:
        t = tables.get(rel["source_table"])
        if t:
            t.foreign_key_candidates.append(
                {
                    "column": rel["source_column"],
                    "target_table": rel["target_table"],
                    "target_column": rel["target_column"],
                    "confidence": str(rel["confidence"]),
                }
            )

    return SourceMetadata(
        tables=tables,
        relationship_candidates=relationships,
        extracted_at=datetime.now(timezone.utc).isoformat(),
        source_kind=source_kind,
    )
