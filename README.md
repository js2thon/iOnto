# Ontology Studio V4.0

企业本体建模 Pipeline 工作台。

## 功能概览

- **数据源**：CSV / Excel 多文件导入；PostgreSQL 连接、Schema 选表、按表加载
- **本体建模**：主键推断、自动外键、手动关系、Schema/实例图谱、质量检测、Action、JSON/OWL 导出
- **智能问答**：Tool Calling 按需查询（search / get / neighbors / path / count）+ 降级检索；证据溯源
- **问题测评**：CQ 问题集、自动测评、确定性比对、失败归因
- **版本管理**：快照、差异比较、生命周期、回滚

## 环境要求

- Python 3.10+（推荐 3.11 / 3.12）
- 可选：PostgreSQL（使用「数据源 → PostgreSQL」时需要）
- 可选：OpenAI 兼容 API（智能问答 / 问题测评）

## 安装

```bash
# 建议使用虚拟环境
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 运行

```bash
streamlit run ontology_studio_v3.py
```

浏览器会自动打开（默认 `http://localhost:8501`）。

## 使用提示

### 文件导入

1. **数据源 → 文件上传**，选择 CSV / Excel（可多选）
2. 预览后点击「读取文件并进入建模配置」
3. **本体建模**中确认主键与关系，生成本体

### PostgreSQL

1. **数据源 → PostgreSQL**，填写 Host / Port / 数据库 / 用户名 / 密码 / Schema
2. 「测试连接」或「加载表列表」
3. 多选要导入的表，可设置每表最大行数（0 = 全量）
4. 「加载选中表并进入建模配置」→ 进入本体建模

表名将作为实体类型（Object Type）名称；外键列名符合常见约定时会自动识别关系。

### LLM（可选）

侧边栏配置：

- API Key
- Base URL（如 `https://api.openai.com/v1`，或其他 OpenAI 兼容端点）
- 模型名称

配置可保存到同目录 `llm_config.json`（下次启动自动加载）。

### 工作区目录

运行后会在脚本同级生成：

```
ontology_workspace/
  versions/        # 版本快照
  question_sets/   # CQ 问题集
  eval_runs/       # 测评运行记录
  current.json     # 当前版本指针
```

## 依赖说明

| 包 | 用途 |
|---|---|
| streamlit | Web UI |
| pandas / openpyxl | 表格与 Excel |
| networkx / pyvis | 图与可视化 |
| rdflib | OWL 导出 |
| openai | LLM 问答 |
| sqlalchemy / psycopg2-binary | PostgreSQL |

## 常见问题

- **图谱空白**：检查浏览器是否拦截脚本；PyVis 使用内联 CDN 资源
- **PG 连接失败**：确认主机网络、防火墙、`pg_hba.conf` 与用户权限
- **模型不支持 Tool Calling**：侧边栏问答策略改为「智能检索（降级）」
- **大表内存不足**：PostgreSQL 导入时先限制每表行数

## 许可证

按你的项目需要自行约定。本仓库为工作台源码交付包。
