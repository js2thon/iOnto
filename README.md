# iOnto V5.0 — 企业本体建模 Pipeline 工作台

企业本体建模 Pipeline 工作台。

## 功能概览

- **数据源**：CSV / Excel 多文件导入；PostgreSQL 连接、Schema 选表、按表加载
- **本体建模（旧版工作台）**：表结构驱动的对象类型生成、主键推断、关系识别、Schema/实例图谱、质量检测、JSON/OWL 导出
- **V5 领域模型**：ObjectType、Property、Relationship、Identity、Constraint、MappingContract；独立实例模型与字段级血缘
- **V5 映射引擎**：审核通过的映射契约确定性执行，支持属性映射、白名单转换、关系 Join、实例物化与血缘记录（当前为独立执行服务）
- **V5 SQLite 持久化基础**：本体版本、映射运行、实例、血缘与不可变发布包；默认本地数据库 `ontology_workspace/v5_ontology.db`
- **智能问答**：LLM 语义理解 → 结构化查询计划 → 本体确定性执行 → 结果校验 → 证据链 → LLM 答案表达
- **问题测评**：CQ 问题集、自动测评、确定性比对、失败归因
- **版本管理**：快照、差异比较、生命周期、回滚

## 智能问答架构

问答链路采用确定性执行架构，LLM 不再直接调用本体数据工具：

1. **语义理解**：LLM 读取本体 Schema，将自然语言问题解析为查询意图。
2. **查询规划**：生成结构化 Query Plan（JSON），校验对象类型、属性、关系、过滤操作符和查询边界。
3. **本体执行**：Python 执行引擎只执行通过校验的计划，查询本体对象实例和关系图。
4. **结果校验**：对返回对象重新与本体实例存储核对，检查主键、记录内容和计数一致性。
5. **证据溯源**：记录查询计划、对象标识、对象记录、关系路径、版本号和结果 SHA-256。
6. **答案生成**：LLM 只接收已验证结果和证据，负责组织简洁自然语言，不得补充证据之外的事实。

旧版在线问答的证据链达到**本体对象/关系级**。V5 新增的映射执行服务可在内存物化实例并记录字段级血缘；V5.1 增加了 SQLite repository API，可持久化 Schema、运行记录、实例和发布快照。但这些能力尚未接入旧版 Streamlit 数据导入、映射画布和问答执行路径；不能将底层服务能力误认为工作台已端到端启用。

运行核心查询模块测试：

```bash
python -m unittest discover -s tests -v
```

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
| pandas / openpyxl / xlrd | 表格、Excel（xlsx/xls） |
| networkx / pyvis | 图与可视化 |
| rdflib | OWL 导出 |
| openai | LLM 问答 |
| sqlalchemy / psycopg2-binary | PostgreSQL |

## 常见问题

- **图谱空白**：检查浏览器是否拦截脚本；PyVis 使用内联 CDN 资源
- **PG 连接失败**：确认主机网络、防火墙、`pg_hba.conf` 与用户权限
- **LLM JSON 模式不兼容**：查询规划器会尝试兼容模式重试；仍失败时检查模型服务的 OpenAI 兼容接口
- **大表内存不足**：PostgreSQL 导入时先限制每表行数

## 许可证

按你的项目需要自行约定。本仓库为工作台源码交付包。


## 工程状态与整体 Review

V5.0 当前已具备领域模型、实例/血缘模型和确定性映射执行服务，但映射画布、
持久化和 V5 问答尚未完成端到端集成。已完成的代码审查、缺陷修复和剩余
工程事项见 [V5 Overall Code Review](docs/OVERALL_CODE_REVIEW_V5.md)。

## V5 映射引擎快速使用（Python API）

V5 映射引擎位于 `ontology_domain.mapping_engine`，面向已构建的
`OntologySchema` 和内存中的源数据行执行。映射契约必须显式审核通过
（`review_status="approved"`）并激活（`status="active"` 或
`"released"`）。具体模型、转换操作、关系 Join 约定和当前集成边界见
[`docs/V5_DOMAIN_MODEL.md`](docs/V5_DOMAIN_MODEL.md)。

目前它还不是 Streamlit 中可直接点击使用的映射画布；旧版数据源/建模/问答
仍使用兼容运行时结构。V5 工作台 UI、数据库持久化、增量更新和端到端查询
切换将在后续集成阶段完成。


V5.1 SQLite repository 的 API 和发布门禁见 [V5 Domain Model 文档](docs/V5_DOMAIN_MODEL.md#sqlite-persistence-and-release-bundles-v51-foundation)。
