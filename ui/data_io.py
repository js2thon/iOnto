"""File and PostgreSQL data loading helpers."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

def read_uploaded_file(uploaded_file: Any) -> pd.DataFrame:
    filename = uploaded_file.name.lower()
    uploaded_file.seek(0)
    if filename.endswith(".csv"):
        try:
            return pd.read_csv(uploaded_file)
        except UnicodeDecodeError:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, encoding="gb18030")
    if filename.endswith((".xlsx", ".xls")):
        return pd.read_excel(uploaded_file)
    raise ValueError(f"不支持的文件格式：{uploaded_file.name}")


# ---------------------------------------------------------------------
# PostgreSQL 数据源
# ---------------------------------------------------------------------
def _pg_dependency_error() -> Optional[str]:
    """检查 SQLAlchemy / psycopg2 是否可用。"""
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:
        return (
            "缺少 sqlalchemy 依赖，请执行：pip install sqlalchemy psycopg2-binary"
        )
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return (
            "缺少 psycopg2 依赖，请执行：pip install psycopg2-binary"
        )
    return None


def build_pg_engine(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    connect_timeout: int = 8,
):
    """创建 SQLAlchemy Engine（PostgreSQL）。"""
    from sqlalchemy import create_engine
    from urllib.parse import quote_plus

    host = (host or "").strip() or "localhost"
    database = (database or "").strip()
    user = (user or "").strip()
    if not database:
        raise ValueError("数据库名不能为空")
    if not user:
        raise ValueError("用户名不能为空")

    url = (
        f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password or '')}"
        f"@{host}:{int(port)}/{quote_plus(database)}"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": int(connect_timeout)},
    )


def list_pg_schemas(engine) -> List[str]:
    """列出可用 schema（排除系统 schema）。"""
    from sqlalchemy import text

    sql = text(
        """
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND schema_name NOT LIKE 'pg_temp_%'
          AND schema_name NOT LIKE 'pg_toast_temp_%'
        ORDER BY schema_name
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [str(r[0]) for r in rows]


def list_pg_tables(engine, schema: str = "public") -> List[Dict[str, Any]]:
    """列出指定 schema 下的基表（含估算行数，若可用）。"""
    from sqlalchemy import text

    schema = (schema or "public").strip() or "public"
    sql = text(
        """
        SELECT
            t.table_name,
            COALESCE(s.n_live_tup, 0)::bigint AS approx_rows
        FROM information_schema.tables t
        LEFT JOIN pg_stat_user_tables s
          ON s.schemaname = t.table_schema
         AND s.relname = t.table_name
        WHERE t.table_schema = :schema
          AND t.table_type = 'BASE TABLE'
        ORDER BY t.table_name
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"schema": schema}).fetchall()
    return [
        {"table": str(r[0]), "approx_rows": int(r[1] or 0)}
        for r in rows
    ]


def load_pg_tables(
    engine,
    schema: str,
    table_names: List[str],
    row_limit: Optional[int] = None,
) -> Dict[str, pd.DataFrame]:
    """按表名加载为 DataFrame；表名作为实体类型名。

    row_limit 为 None 或 <=0 时全量加载；否则每表最多加载 row_limit 行。
    使用双引号安全引用标识符，避免 SQL 注入与大小写问题。
    """
    from sqlalchemy import text

    schema = (schema or "public").strip() or "public"
    result: Dict[str, pd.DataFrame] = {}

    def _quote_ident(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    for table in table_names:
        table = str(table).strip()
        if not table:
            continue
        qualified = f"{_quote_ident(schema)}.{_quote_ident(table)}"
        if row_limit is not None and int(row_limit) > 0:
            sql = text(f"SELECT * FROM {qualified} LIMIT :limit")
            params = {"limit": int(row_limit)}
        else:
            sql = text(f"SELECT * FROM {qualified}")
            params = {}
        df = pd.read_sql(sql, engine, params=params)
        # 实体类型名使用表名（不含 schema），与 CSV 文件名逻辑一致
        result[table] = df
    return result


def test_pg_connection(engine) -> str:
    """执行简单查询验证连接，返回服务端版本摘要。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version()")).scalar()
    return str(version or "connected")


# ---------------------------------------------------------------------
# LLM 配置持久化
# ---------------------------------------------------------------------
LLM_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "llm_config.json"
)


