# iOnto V5.1 — 企业本体建模 Pipeline 工作台

企业本体建模 Pipeline 工作台。

## 架构要点（V5）

- **结构真相源**：`OntologySchema`（V5）。编辑与发布以 V5 为准，再投影到 legacy 运行时图。
- **Legacy 运行时**：`ontology_domain/legacy_runtime.Ontology` 承载实例图（ABox），供图谱与问答使用。
- **桥接**：`ontology_domain/runtime_bridge.py`、`ui/session.py`（`set_v5_schema` / `after_structural_edit`）。
- **UI 模块**：`ui/pages/`（总览 / 数据源 / 建模）、`ui/graphs.py`、`ui/analysis.py`、`ui/data_io.py`；入口仍为 `ontology_studio_v3.py`。
- **元数据 → TBox**：`metadata_extractor` + `metadata_workbench`（多 Bundle 可编辑可组合）→ `tbox_generator`。
- **导出 / 推理**：TriG 命名图对接 Ontosphere；`reasoning_gate` + 可选 OWL-RL。

## 功能概览

- **数据源**：CSV / Excel 多文件导入；PostgreSQL 连接、Schema 选表、按表加载
- **本体建模（旧版工作台）**：表结构驱动的对象类型生成、主键推断、关系识别、Schema/实例图谱、质量检测、JSON/OWL 导出
- **V5 领域模型**：ObjectType、Property、Relationship、Identity、Constraint、MappingContract；独立实例模型与字段级血缘
- **V5.1 映射工作台**：字段映射编辑、目标类型配置、关系 Join Key、模拟预览、人工审核、确定性执行、SQLite 持久化和发布门禁
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

旧版在线问答的证据链达到**本体对象/关系级**。V5.1 映射工作台已接入现有 Streamlit 数据源会话、V5 Mapping Engine 和 SQLite Repository，可配置字段/关系映射、模拟预览、审核、执行、查看运行与血缘并发布不可变版本。V5 查询运行时尚未迁移到新的 V5 实例存储，问答仍沿用兼容运行时。

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

V5.1 已完成领域模型、实例/血缘、确定性映射引擎、SQLite 持久化和映射工作台第一轮集成。V5 问答迁移、增量/大规模数据处理和浏览器端部署验收仍是后续工作。整体代码审查见 [V5 Overall Code Review](docs/OVERALL_CODE_REVIEW_V5.md)。

## V5 映射引擎快速使用（Python API）

V5 映射引擎位于 `ontology_domain.mapping_engine`，面向已构建的
`OntologySchema` 和内存中的源数据行执行。映射契约必须显式审核通过
（`review_status="approved"`）并激活（`status="active"` 或
`"released"`）。具体模型、转换操作、关系 Join 约定和当前集成边界见
[`docs/V5_DOMAIN_MODEL.md`](docs/V5_DOMAIN_MODEL.md)。

映射工作台入口为侧边导航中的「🧩 映射工作台」。旧版数据源和本体建模仍负责数据加载与初始对象发现；映射工作台将其转换为 V5 Schema 草稿，并要求用户明确确认字段映射和关系 Join。


V5.1 SQLite repository 的 API 和发布门禁见 [V5 Domain Model 文档](docs/V5_DOMAIN_MODEL.md#sqlite-persistence-and-release-bundles-v51-foundation)。


## V5.1 映射工作台操作

1. 在「数据源」加载 CSV / Excel / PostgreSQL 数据，并在「本体建模」确认对象类型与关系。
2. 打开「映射工作台」，选择目标业务对象、源数据集、源字段和目标属性类型；保存后契约处于 pending/draft。
3. 在「关系 Join」配置关系两端数据集和 Join Key（支持复合键）。
4. 在「审核与发布」模拟预览，查看实例数、映射错误和阻断契约；人工批准后才可正式执行。
5. 正式执行结果、实例和字段/关系血缘写入 `ontology_workspace/v5_ontology.db`；满足门禁后可发布不可变版本。

预览会在内存副本上临时放行草稿契约，不改变正式审核状态；正式执行只运行已批准且激活的契约。当前数据源仍由现有连接器加载到内存，尚未实现 CDC、增量刷新或大数据分块执行。
