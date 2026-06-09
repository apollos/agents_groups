# MIC — 市场情报采集器（Market Intelligence Collector，V0.3）

MIC 是一个**搜索结果驱动、模型完全配置化**的情报采集与结构化分析工具，面向分析师和分析员 Agent。给定公司、行业、产品或专题，它会：

1. 基于"分析师需求驱动"的 query family 生成并**打分搜索计划**；
2. 执行搜索，对结果**去重**并优先用**规则初筛**；
3. **读取**入选链接，只抽取相关段落（丢弃原文）；
4. 按配置规划**一个或多个模型调用**（系统不预设便宜/强模型）；
5. **本地校验并合并**多模型输出；
6. 只持久化**链接 + 结构化结果**（事实、指标、事件、关系、风险、催化剂、信号、后续问题、覆盖缺口），**从不保存网页原文**。

本项目是对设计文档 [`market_intelligence_collector.md`](./market_intelligence_collector.md) 的完整实现。

---

## 核心设计原则

- **模型选择完全由配置决定**（`config/model_registry.yaml`、`config/model_policies.yaml`）。系统只提供机制，不替业务方决定"便宜还是强"。已实现 spec 全部调用模式：
  `no_model / single_model / priority_fallback / parallel_ensemble / cascade / arbitration / batch_triage`。
- **冲突处理**：多模型合并会检测金额冲突（保留加权中位数并标记）、影响方向冲突、以及关系方向冲突（`A supplier_of B` vs `A customer_of B`）。关系方向/关键字段冲突会触发**仲裁模型调用**，仲裁结果以更高权重重新合并。
- **反馈闭环**（`submit_feedback`）：分析师反馈可标注 `model_config_id / query_family / source_type`，回流去调整模型合并权重（spec §22）。
- **费用控制 = 减少无效调用**，而非选便宜模型：URL/内容指纹复用、query 去重、规则先行、SERP 批量初筛、段落选择、一次性 bundle 抽取、早停。
- **只持久化链接与结构化结果**：HTML / PDF / 全文 / 截图都不入库（`config/storage_policy.yaml`）。
- **关系型数据是一等公民**，但用关系表实现，不上图数据库。

---

## 架构

```
QueryPlanner（搜索计划） → SearchProvider（搜索源） → SearchHitTriage（初筛）
        → LinkReader（读取 + 段落选择） → ModelCallPlanner（调用计划）
        → ModelAdapter（多模型适配） → BundleValidator（本地校验）
        → MultiModelMerger（多模型合并） → Repository（Postgres/SQLite）
        → AnalystAPI（分析员 Agent 接口）
```

| 模块 | 设计文档章节 | 文件 |
|---|---|---|
| 目标画像 Target Profile | 5 | `mic/profile.py` |
| 分析师需求分类 | 6 | `config/analyst_taxonomy.yaml` |
| Query Planner 搜索计划 | 8 | `mic/planner.py` |
| 搜索源层 | 4 | `mic/search.py` |
| 搜索结果初筛 Triage | 9 | `mic/triage.py` |
| Link Reader / 内容预处理 | 10 | `mic/reader.py` |
| 模型调用计划器 | 11、15 | `mic/modeling/call_planner.py` |
| 模型适配层 | 3、11 | `mic/modeling/adapter.py` |
| Bundle Schema + 领域信号 | 12、13 | `mic/schemas.py`、`mic/modeling/prompts.py` |
| 多模型合并 | 14 | `mic/merge.py` |
| 本地校验 | 17 | `mic/validate.py` |
| 存储 | 16 | `mic/store/` |
| 运行流程 + Batch Report | 18、19 | `mic/pipeline.py` |
| 分析员 Agent API | 20 | `mic/api.py` |
| 配置 | 21 | `config/` |
| 反馈与优化 | 22 | `mic/store/models.py::Feedback`、`mic/store/repository.py` |

---

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
# 可选：PostgreSQL 驱动
pip install -e ".[postgres]"
# 可选：开发依赖（pytest / ruff）
pip install -e ".[dev]"
```

也可以用拆分后的 requirements 文件按需安装：

```bash
pip install -r requirements.txt          # 核心运行依赖（离线/mock 模式即可跑通）
pip install -r requirements-model.txt    # 仅真实模型调用需要（openai，按需懒加载）
pip install -r requirements-dev.txt      # 测试 / lint
```

> 运行日志、E2E 报告、临时 SQLite DB 默认写入 `logs/`（已在 `.gitignore` 中忽略）。
> 可用 `MIC_LOG_DIR` / `MIC_LOG_LEVEL` 覆盖。

复制环境变量模板：

```bash
cp .env.example .env
```

---

## 离线演示（无需任何 API Key）

默认 `MIC_ALLOW_MOCK=true` 时，搜索源与模型适配层会回退到**确定性 mock**，整条流水线可在无外部服务的情况下端到端跑通。

命令行：

```bash
mic targets                                                          # 列出已配置目标
mic collect company_300750 --focus operating_update,customer_change,risk --time-window 30d
mic events company_300750 --since 30d                                # 查看最近事件
mic relations company_300750                                         # 查看关系记录
mic questions company_300750                                         # 查看分析师后续问题
mic gaps company_300750                                              # 查看覆盖缺口
mic explain <source_link_id>                                         # 解释某来源为何入选及抽取内容
```

> 全局选项 `--log-level` / `--log-file` 控制日志（写入 `logs/`）。

Python 调用：

```python
from mic.api import AnalystAPI

api = AnalystAPI()
report = api.collect_intelligence(
    target_id="company_300750",
    task_profile={
        "focus": ["operating_update", "customer_change", "supply_chain", "policy", "risk"],
        "time_window": "30d",
        "budget_profile": {"max_queries": 80, "max_links_to_read": 40, "max_model_calls": 30},
    },
)
print(report["summary"])
events = api.get_recent_events("company_300750", since="30d")
```

---

## 接入真实模型与搜索源

1. 在 `.env` 填入密钥（`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`SILICONFLOW_API_KEY`、`SERPAPI_API_KEY` 等）。
2. 在 `config/search_providers.yaml` 把 `active` 改为 `serpapi`（或 `bing`）。
3. 设 `MIC_ALLOW_MOCK=false` 强制走真实调用（否则缺 Key 时会自动回退 mock）。
4. 生产环境把 `MIC_DATABASE_URL` 指向 PostgreSQL，例如
   `postgresql+psycopg://user:pass@localhost:5432/mic`。

所有模型供应商均为 OpenAI 兼容接口，在 `config/model_registry.yaml` 注册；
任务级调用策略在 `config/model_policies.yaml` 配置。

---

## 分析员 Agent API（`mic/api.py`，对应 spec §20）

| 方法 | 用途 |
|---|---|
| `collect_intelligence(target_id, task_profile)` | 运行完整采集流水线 |
| `get_recent_events(target_id, since, event_types, min_confidence)` | 最近事件 |
| `get_metric_observations(target_id, metrics, since)` | 指标观察 |
| `get_relations(target_id, relation_types, since)` | 关系记录 |
| `search_facts(target_id, query, filters)` | 事实检索 |
| `get_risks(target_id, since, severity)` | 风险 |
| `get_catalysts(target_id, from_date, to_date)` | 催化剂日历 |
| `get_analyst_questions(target_id, priority, status)` | 后续问题 |
| `get_coverage_gaps(target_id, priority, status)` | 覆盖缺口 |
| `explain_source_analysis(source_link_id)` | 来源分析溯源（含入选原因与命中信号） |
| `submit_feedback(feedback)` | 提交反馈（驱动权重优化） |

---

## 降低无效调用的机制（spec §15）

- **去重复用**：相同 `canonical_url` / `content_hash` 只分析一次；多 query/多搜索源命中同一链接只读一次。**跨 run 复用**：同一目标在历史 run 已分析过的链接会直接克隆结构化结果（`clone_latest_analysis`），不再重复读取与调模型。
- **规则先行**：标题/摘要/来源/时间足够判断低价值时不调模型。
- **批量初筛**：边界分值的搜索结果合并到一次模型调用判断（带独立子预算，不挤占抽取调用）。
- **段落选择**：只把相关段落送模型，而非全文。
- **一次性 bundle 抽取**：一篇文章尽量一次完成多类抽取；正文过长时才分块。
- **早停**：首个模型输出 schema 合法且置信足够即停止后续 fallback。
- **预算治理**：每次运行 / 每条链接 / 并行组 / 批量初筛 各有上限（`config/call_governance.yaml`）。

---

## 持久化对象（spec §13、§16）

链接与读取记录、模型运行与输出、合并结果，以及结构化对象：
分析摘要、事实、指标、事件、关系、风险、催化剂、
客户/供应商信号、价格成本毛利信号、政策监管信号、后续问题、覆盖缺口、反馈。

> 原始 HTML / PDF / 全文 / 截图均不持久化。

---

## 测试

```bash
pip install -e ".[dev]"
pytest -q
```

覆盖：搜索计划打分与预算、端到端采集、API 回读、预算上限、批量初筛省调用、
领域信号产出、新增 focus family、关系方向冲突触发仲裁、cascade/arbitration 执行、
任务拆分、反馈调权、跨 run 复用克隆、入选溯源元数据、"不持久化原始内容"存储策略等。

**端到端验证工具**（离线 mock，无需任何 Key）：

```bash
python tools/e2e_validate.py --quiet      # 跑通整条链路并断言结果，PASS/FAIL
bash tools/run_offline_e2e.sh             # 同上，强制 mock 模式
bash tools/run_tests.sh                   # pytest + e2e，日志落 logs/
```

检查项包括 query/search/read/model/validate/merge/store/API/explain，以及 DB 行数与
"不存在原始内容列（html/full_text/raw_content/...）"。

---

## 配置文件（`config/`）

`access_profiles`、`search_providers`、`target_profiles`、`analyst_taxonomy`、
`query_families`、`query_scoring`、`source_packs`、`model_registry`、
`model_policies`、`merge_policy`、`call_governance`、`output_schema`、
`storage_policy`。
