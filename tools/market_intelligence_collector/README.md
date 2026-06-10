# MIC — 市场情报采集器（Market Intelligence Collector，V0.3）

MIC 是一个**搜索结果驱动、模型完全配置化**的情报采集与结构化分析工具，面向分析师和分析员 Agent。给定公司、行业、产品或专题，它会：

1. 基于"分析师需求驱动"的 query family 生成并**打分搜索计划**；
2. 执行搜索，对结果**去重**并优先用**规则初筛**；
3. **读取**入选链接（HTML 与 PDF），只抽取相关段落与表格行（丢弃原文）；反爬/验证码页会被识别并标记为 `failed`（`failure_reason=anti_bot_page`），不会混入正常结果；**扫描版 PDF**（无文字层）会渲染前几页送多模态模型转写后继续流程（经 OpenClaw 网关 `/v1/responses`，用 `x-openclaw-model` 请求头按次路由到 `.env` 里 `OPENCLAW_VISION_MODEL` 指定的 text+image 模型，不影响网关默认模型）；正文极短但带大图的页面（"公告截图+一句话"）可选启用图片转写（默认关，见 `call_governance.yaml -> vision_extract`）；
4. 按配置规划**一个或多个模型调用**（系统不预设便宜/强模型）；
5. **本地校验并合并**多模型输出（含实体归一化：同 ticker 不同写法的公司视为同一实体，"多家供应商"等集合名词关系被过滤）；
6. 只持久化**链接 + 结构化结果**（事实、指标、事件、关系、风险、催化剂、信号、后续问题、覆盖缺口），**从不保存网页原文**。

当前工程实现覆盖设计文档 [`market_intelligence_collector.md`](./market_intelligence_collector.md) 的主链路和大部分 V0.3 机制；少数生产增强能力（如事件级语义复用、复杂反爬处理）在文档中明确为边界或后续增强项。

---

## 核心设计原则

- **模型选择完全由配置决定**（`config/model_registry.yaml`、`config/model_policies.yaml`）。系统只提供机制，不替业务方决定"便宜还是强"。已实现 spec 全部调用模式：
  `no_model / single_model / priority_fallback / parallel_ensemble / cascade / arbitration / batch_triage`。
- **冲突处理**：多模型合并会检测金额冲突（保留加权中位数并标记）、影响方向冲突、以及关系方向冲突（`A supplier_of B` vs `A customer_of B`）。关系方向/关键字段冲突会触发**仲裁模型调用**，仲裁结果以更高权重重新合并。
- **反馈闭环**（`submit_feedback`）：分析师反馈可标注 `model_config_id / query_family / source_type`，按好评率映射为 **0.5~1.2 的乘数**，分别回流到模型合并权重、Query Planner 的 query 族打分、Triage 的来源类型打分（spec §22）。
- **费用控制 = 减少无效调用**，而非选便宜模型：URL/内容指纹复用、query 去重、规则先行、SERP 批量初筛、段落选择、一次性 bundle 抽取、早停。
- **只持久化链接与结构化结果**：HTML / PDF / 全文 / 截图都不入库（`config/storage_policy.yaml`）。
- **关系型数据是一等公民**，但用关系表实现，不上图数据库。
- **默认不产生外部费用**：交付版默认 `active: mock` 且 `.env.example` 里 `MIC_ALLOW_MOCK=true`；真实搜索/模型必须显式配置 API Key、切换真实 search provider，并把 `MIC_ALLOW_MOCK=false`。

---

## 当前明确边界

- 不登录、不复用个人 Cookie、不绕过验证码/付费墙；遇到反爬/验证码页会标记失败并记录日志。
- `canonical_url` / `content_hash` 复用已实现；**事件级语义复用**暂未启用（`call_governance.yaml -> reuse.same_event_cache: false`），避免把不同来源的相似事件误判为同一事件。
- SearXNG / Tavily / SerpApi / 视觉转写等真实外部服务都可能产生失败或费用，真实模式必须小预算验证后再放大。

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
python -m pip install --upgrade pip
# 推荐：开发/测试安装，后续可直接跑 pytest 和 tools/e2e_validate.py
python -m pip install -e ".[dev]"

# 仅运行程序、不跑测试时可用：
# python -m pip install -e .

# 需要 PostgreSQL 驱动时可用：
# python -m pip install -e ".[postgres]"
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

> 安全要求：`.env`、`mic.db`、`logs/*.log`、`logs/*.db` 都是本地运行产物，**不能提交或打进交付 zip**。如果 `.env` 曾经被分享过，应立即在对应平台撤销/轮换里面的 API Key。

---

## 离线演示（无需任何 API Key）

交付版默认 `config/search_providers.yaml -> active: mock`，且 `MIC_ALLOW_MOCK=true` 时模型适配层也会使用**确定性 mock**，整条流水线可在无外部服务、无外部费用的情况下端到端跑通。

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

1. 在 `.env` 填入密钥（`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`SILICONFLOW_API_KEY`、`SERPAPI_API_KEY` 等）。视觉救援（扫描版 PDF / 页面图片转写）还需 `OPENCLAW_GATEWAY_TOKEN` 加 `OPENCLAW_VISION_MODEL`（OpenClaw 侧一个支持图片输入的模型名，如 `custom-api-siliconflow-cn/Pro/moonshotai/Kimi-K2.6`；不配则视觉调用会落到网关默认模型，纯文本模型看不到图片）。
2. 在 `config/search_providers.yaml` 把 `active: mock` 改为某个真实引擎，或一个引擎列表以**同时**调用多个引擎并合并去重，例如：
   - `active: serpapi_baidu`（仅百度）
   - `active: [serpapi_baidu, serpapi_bing]`（百度 + Bing）
   - `active: [serpapi_baidu, serpapi_google, serpapi_bing]`（百度 + 谷歌 + Bing）

   多引擎结果按 canonical URL 跨引擎去重，每条命中保留各自的 `provider` 标记（如 `serpapi:baidu`）。缺 Key 的引擎会被跳过（记日志）。三个 `serpapi_*` 引擎共用同一个 `SERPAPI_API_KEY`（微软已于 2025-08 停用原生 Bing Search API，Bing 结果统一经 SerpApi 或 SearXNG 获取）。

   **计费与单次结果数**：SerpApi 按 API 调用次数计费，与返回条数无关。每引擎的"结果数"参数由代码自动映射（Google→`num`、Baidu→`rn`、Bing→`count`）。注意 **Google 自 2025-09 起不再支持 num 参数**，每次调用固定返回约 10 条；Baidu/Bing 的参数仍有效（单次最高 50）。`hits_per_query` 配 15 对 Google 不会浪费额度，只是用不满。

### Tavily（默认兜底引擎）

[Tavily](https://tavily.com) 是面向 LLM 的搜索 API，按调用计费（basic 1 积分/次，免费档 1000 积分/月），Key 配在 `.env` 的 `TAVILY_API_KEY`。返回的 snippet 是抽取后的正文片段，比传统 SERP 摘要更长更干净，利好初筛打分。**中文财经站点覆盖弱于 Baidu**（百家号、财联社类站点召回偏低），因此默认定位是 `fallback: tavily`（零运维兜底，已是默认配置）或 active 里的补充引擎，不建议单独作为 A 股目标的唯一主引擎。
3. 设 `MIC_ALLOW_MOCK=false` 强制走真实调用（否则缺 Key 时会自动回退 mock）。`python tools/e2e_validate.py --real` 会自动强制 `MIC_ALLOW_MOCK=false`，并且如果 `active` 仍是 `mock` 会直接失败，避免假阳性。
4. 生产环境把 `MIC_DATABASE_URL` 指向 PostgreSQL，例如
   `postgresql+psycopg://user:pass@localhost:5432/mic`。

### 免费搜索源：自建 SearXNG（可选兜底）

[SearXNG](https://github.com/searxng/searxng) 是自托管元搜索引擎：一条 query 在实例侧扇出到
百度/谷歌/Bing 并返回合并后的 JSON，**免费、无按次费用**。本仓库自带可直接启动的配置：

```bash
cd searxng
sed -i "s|CHANGE_ME_RANDOM_SECRET|$(openssl rand -hex 32)|" config/settings.yml   # 仅首次
docker compose up -d
curl 'http://localhost:8888/search?q=test&format=json' | head                      # 验证 JSON 输出
```

> 只有 docker-compose v1 的环境（v1 与新版 requests 不兼容）可改用等价的 `docker run`：
>
> ```bash
> docker run -d --name mic-searxng -p 8888:8080 \
>   -v "$(pwd)/config:/etc/searxng" \
>   -e SEARXNG_BASE_URL=http://localhost:8888/ \
>   --restart unless-stopped searxng/searxng:latest
> ```

用法二选一（`config/search_providers.yaml`）：

- **作为主引擎**：`active: searxng`（或 `active: [searxng, serpapi_baidu]` 混跑）；
- **作为兜底**：`fallback` 支持单个名字或**有序列表**（默认 `[tavily, searxng]`）——
  当所有 active 引擎对某条 query 失败或返回 0 条结果时，按顺序逐个用兜底引擎重试，
  直到拿到结果。已出现在 `active` 里的名字、不可用的引擎（缺 Key / 实例没起来）
  会被自动跳过；仅 mock 在跑时兜底不生效。

实例地址通过 `.env` 的 `SEARXNG_BASE_URL` 覆盖（默认 `http://localhost:8888`）。
工厂构建时会探活（`/healthz`），实例没起来会被跳过并记日志。

所有模型供应商均为 OpenAI 兼容接口，在 `config/model_registry.yaml` 注册；
任务级调用策略在 `config/model_policies.yaml` 配置。

---

## 分析员 Agent API（`mic/api.py`，对应 spec §20）

| 方法 | 用途 |
|---|---|
| `collect_intelligence(target_id, task_profile, model_policy_version=None, query_plan_version=None)` | 运行完整采集流水线；版本参数可选，传入未知版本会抛 `ValueError` |
| `get_recent_events(target_id, since="30d", event_types=None, min_confidence=0.0)` | 最近事件 |
| `get_metric_observations(target_id, metrics=None, since="60d")` | 指标观测 |
| `get_relations(target_id, relation_types=None, since="180d")` | 关系记录（已跨来源去重） |
| `search_facts(target_id, query="", filters=None)` | 事实检索；`query` 按空格分词、任一词命中 `fact_statement` 即返回，无命中返回 `[]` |
| `get_risks(target_id, since="90d", severity=None)` | 风险 |
| `get_catalysts(target_id, from_date=None, to_date=None)` | 催化剂日历 |
| `get_analyst_questions(target_id, priority=None, status="open")` | 后续问题 |
| `get_coverage_gaps(target_id, priority=None, status="open")` | 覆盖缺口 |
| `explain_source_analysis(source_link_id)` | 来源分析溯源（含入选原因与命中信号） |
| `submit_feedback(feedback)` | 提交反馈（驱动权重优化），返回 feedback id |

> ⚠️ 所有 `get_*` / `search_*` 方法**没有 `limit` 参数**，返回完整列表（已按置信度或时间排序），调用方自行切片。
> `search_facts` 的 `filters` 形如 `{"fact_type": ["order"], "since": "30d"}`。

### `collect_intelligence` 的入参与返回结构

```python
report = api.collect_intelligence("company_300750", {
    "focus": ["orders_tender", "customer_supplier"],   # query family，见 config/query_families.yaml
    "time_window": "30d",
    "budget_profile": {
        "max_queries": 20,          # 执行的 query 条数上限
        "max_links_to_read": 10,    # 读取链接数上限
        "max_model_calls": 10,      # 模型调用次数上限
        # "max_search_hits": ...   # 可省略：自动按 max_queries x 引擎数 x 单次结果数推导
    },
})
```

返回 dict 的关键字段（取数时注意字段名）：

| 路径 | 含义 |
|---|---|
| `report["search_run_id"]` | 本次 run id |
| `report["summary"]["queries_executed"]` | 实际执行 query 数 |
| `report["summary"]["queries_skipped_by_hit_budget"]` | 因 hit 预算被跳过的 query 数（>0 说明预算偏紧） |
| `report["summary"]["search_hits"]` / `["links_read"]` / `["model_calls"]` | 命中 / 读取 / 模型调用计数 |
| `report["summary"]["cached_or_reused_results"]` | 跨 run 复用（克隆历史分析）的链接数 |
| `report["summary"]["vision_calls"]` | 视觉转写调用数（扫描版 PDF / 页面图片救援） |
| `report["summary"]["estimated_model_cost"]` | 估算模型费用（美元） |
| `report["structured_outputs"]` | 各类结构化对象的产出计数 dict |
| `report["top_events"]` / `report["top_relations"]` | 置信度最高的事件/关系预览 |
| `report["log_file"]` | 本次 run 的日志文件路径 |

### 各查询方法的返回字段（每项均为 dict 列表）

| 方法 | 字段 |
|---|---|
| `get_recent_events` | `event_id, event_type, event_date, summary, entities, metrics, impact, source_corroboration_status, confidence, source_link_id` |
| `get_metric_observations` | `metric_id, metric_name, metric_value, unit, period, scope, comparison, interpretation, impact_channels, confidence, source_link_id` |
| `get_relations` | `relation_id, subject_entity, relation_type, object_entity, qualifiers, confidence, source_link_id, source_link_ids` |
| `search_facts` | `fact_id, fact_type, fact_statement, entities, metrics, period, direction, confidence, source_link_id` |
| `get_risks` | `risk_id, risk_type, risk_summary, severity, time_horizon, impact_channels, confidence, source_link_id` |
| `get_catalysts` | `catalyst_id, catalyst_type, expected_date, description, potential_impact, confidence, source_link_id` |
| `get_analyst_questions` | `question_id, question, reason, priority, status, suggested_queries, related_event_id, source_link_id` |
| `get_coverage_gaps` | `gap_id, gap_type, description, suggested_next_queries, priority, status, search_run_id` |
| `explain_source_analysis` | `source{title,url,source_type,publish_time}, why_selected, matched_signals, triage_decision, models_used, merged_decision, facts, metrics, events, relations, risks, questions` |

注意：

- 指标值字段叫 **`metric_value`**（不是 `value`）、事实陈述叫 **`fact_statement`**（不是 `statement`）、风险描述叫 **`risk_summary`**（不是 `description`）。
- `subject_entity` / `object_entity` 是 `{"name", "type", "ticker"}` 结构的 dict。
- `get_relations` 已做**跨来源去重**：同 ticker 的不同公司写法合并为一条（保留最高置信度），集合名词实体（"多家锂电设备商"等）被过滤；`source_link_ids` 列出印证该关系的全部独立来源链接，长度 ≥2 表示多来源交叉印证。
- `explain_source_analysis` 查询**读取失败**的链接时 `models_used` / `facts` 等为空，属正常——先用 `repo.source_links_for_run(run_id, decision="read")` 筛选成功读取的链接。

### `submit_feedback` 字段

```python
api.submit_feedback({
    "object_type": "fact",            # 被评对象类型：fact/event/relation/...
    "object_id": "fact_xxx",          # 被评对象 id
    "correct": True,                  # 内容是否正确
    "useful_for_analysis": True,      # 是否对分析有用
    "impact_direction_correct": None, # 影响方向是否判断正确（可选）
    "missing_fields": None,           # 缺失字段列表（可选）
    "model_config_id": None,          # 归因到模型：影响该模型合并权重（可选）
    "query_family": "orders_tender",  # 归因到 query 族：影响后续搜索计划打分（可选）
    "source_type": "media",           # 归因到来源类型：影响后续初筛打分（可选）
    "notes": "说明",
})
```

好评率按 `0.5 + 0.7 x ratio` 映射为乘数（全差评 0.5，全好评 1.2），在**下一次** `collect_intelligence` 时自动生效。

---

## 降低无效调用的机制（spec §15）

- **去重复用**：相同 `canonical_url` / `content_hash` 只分析一次；多 query/多搜索源命中同一链接只读一次。**跨 run 复用**：同一目标在历史 run 已分析过的链接会直接克隆结构化结果（`clone_latest_analysis`），不再重复读取与调模型。
- **规则先行**：标题/摘要/来源/时间足够判断低价值时不调模型。
- **批量初筛**：边界分值的搜索结果合并到一次模型调用判断（带独立子预算，不挤占抽取调用）。
- **段落选择**：只把相关段落送模型，而非全文。
- **视觉救援按需触发**：PDF 先走免费的 `pypdf` 文字层，只有扫描版（文字层过短）才渲染前 N 页送多模态模型（OpenClaw 网关）；页面图片转写默认关闭，需正文极短才触发。视觉调用有独立的 per-run 上限（`vision_extract.max_calls_per_run`）。
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
python tools/check_release_clean.py .      # 交付/打包前检查是否混入 .env、db、logs 或疑似密钥
```

检查项包括 query/search/read/model/validate/merge/store/API/explain，以及 DB 行数与
"不存在原始内容列（html/full_text/raw_content/...）"。

---

## 配置文件（`config/`）

`access_profiles`、`search_providers`、`target_profiles`、`analyst_taxonomy`、
`query_families`、`query_scoring`、`source_packs`、`model_registry`、
`model_policies`、`merge_policy`、`call_governance`、`output_schema`、
`storage_policy`。
