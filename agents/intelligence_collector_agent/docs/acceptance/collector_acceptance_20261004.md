# 情报收集员 Agent × MIC 真实验收报告（2026-10-04）

依据：`agents_groups_cursor_acceptance_handoff_20261004.md`（验收合同）。本报告由 Cursor 自审产出，所有正式记录已逐项对照源文；最终由 Codex 用新样本独立复核。

## 首屏判定

| 项 | 结论 |
| --- | --- |
| 最终版本 | branch `main`，HEAD `534440bcf3ee2ed86c2510c8cef49a88c476f931` + 未提交 diff（批次 v2 指纹：tracked diff `c2f6d230db81afc1…`，见 `20261004/batch-v2/acceptance-manifest.json`；运行后仅改动验收工具自身和文档，见 §2.3） |
| 判定 | **CURSOR_SELF_TEST_PASS**（批次 v1 作为失败记录保留；批次 v2 run-1 满足 §6.1 全部条件） |
| 真实业务正例 | 有。批次 v2 run-1：Agent 正常路径（demand → tick → plan → MIC 子进程 → `collection.result`）；真实 Bing 搜索 2 查询 / 20 命中；真实读取；真实模型 `openclaw/main` 2 次请求（1 次批量初筛 + 1 次抽取），`finish_reason=stop`，上限 65536；来源 energytrend.cn 发布 `2026-09-15T14:41:00+08:00`（窗口内） |
| 正式产出 | MIC 库 4 facts + 6 metrics + 2 events（共 12 条，与 run 报告、Agent 质量结果一致）；Agent `structured_events` 2 条；`usable=true`，`decision=accept_degraded (P2: low_authority_sources_only)` |
| 内容审阅 | 12/12 条关键字段（主体、角色、金额、单价、容量、日期）与源文原句一致；3 条证据摘录逐字存在于 2026-10-04 只读抓取的实时页面正文中；1 条 fact（#3）含推断性结论且引用段落只覆盖两数值之一，判定"数值成立、结论属推断"，未发现实质错误（§5） |
| 预算与清理 | v1：3/3 Gateway、97 s；v2：2/3 Gateway、65 s；两批合计 5 次 Gateway（≤6/批次，且每批次只运行 1 个任务）；所有 run 预算在上限内；浏览器 `cleanup=complete`，无残留页面/子进程/profile 锁；验收 demand 已挂起、无未处理采集消息 |
| 重复任务与下一周期 | 同一 `collection task` 消息重投：复用 run，0 新模型调用、0 新 attempt、0 新正式记录、0 新 result 消息；同日再次 tick 在任务层去重（无新任务/无重开消息）；次日假时钟生成新 idempotency key、不复用旧 run、旧 key 仍指向旧 run；相同报告重存不新增事件（§4.4） |
| 遗留限制 | ① BJX 页在 v2 对真实浏览器也返回反爬页（14 分钟前的 v1 可读），已诚实归类 `anti_bot_page`，由替代来源满足目标；② 严格证据审查把事件日期/金额置为 `pending_review` 候选，正式 `event_date` 为空；③ 批量初筛调用未落 `model_run` 表（仅在 run 报告 `model_requests` 中可追溯）；④ 截断后的 256K 重试未实现（只做状态标记）；⑤ 单模型合并，`overall_score` 为模型按标尺自评（§6） |
| 复现入口 | `agents/intelligence_collector_agent/tools/collector_acceptance.py`（`prepare / run / redeliver / next-cycle / summary`），命令见 `docs/COMMANDS.md`；离线回归：两项目各自 `pytest tests`（MIC 506 通过，Agent 188 通过） |

## 1. 基线与环境（只读记录）

- 起始状态：branch `main`，HEAD `534440b`，工作区已有未提交改动（见 manifest `status_porcelain`）；全程未 reset / checkout / stash。
- 复用本机已验证环境：`/home/yu/.venv/mydev/bin/python`；MIC 部署配置 `/home/yu/.config/agents_groups/mic`；浏览器 profile `~/.local/share/agents_groups/browser/profiles/mic-edge`；OpenClaw Gateway `http://127.0.0.1:18791/v1`（用户手动启动，未改为自启动）。认证只从现有环境读取，验收材料不含 token / Cookie / 环境文件。
- 既有试验工作区 `~/.local/state/agents_groups/collector-pilot` 未修改（两个 demand 仍为 suspended；最新 mtime 为用户提供的诊断文件）。
- 新验收使用隔离工作区 `~/.local/state/agents_groups/collector-acceptance-20261004-v1` 与 `-v2`，各自独立 `state.db / bus.db / data.db / mic.db`，绝对路径。

## 2. 代码版本摘要

### 2.1 本轮为通过验收所做的修复（含回归测试）

| 问题 | 修复 | 回归 |
| --- | --- | --- |
| 离线测试套件读到 `.env` 中的部署配置（MIC_CONFIG_DIR / 浏览器 profile），28 项失败 | `tests/conftest.py` 将两个变量置空，测试只用仓库配置 | 全套件恢复可重复 |
| `probe_lock` 在锁文件不可打开时抛异常导致 doctor 输出为空 | `profile_lock.probe_lock` 返回 `locked=None, error=…`；`doctor` 增加 `profile_lock` 检查 | `test_probe_lock_reports_unopenable_lock_file_instead_of_raising` |
| §5.1 已知缺陷：BJX 页 `.cc-headline` 可见 `2026-09-15 11:47` 但 `publication_parser.status=unknown` | `publication_time.bjx_headline_fields`：主机 + 单篇 URL + 唯一可见标题栏 + 唯一正文容器 + h1/title 绑定 + 标题栏 `p>span` 日期；naive 时间按 Asia/Shanghai | `test_bjx_publication.py`（8 项：正例、时区、正文/推荐/URL 日期不采用、10 个反例、冲突、重复、与 `extract_article` 集成）；定点读取验证 `20261004/bjx-pinpoint-read-probe.json`（0 模型调用） |
| 入库门槛作用于无定义的 `overall_score`（批次 v1 定位，见 §4.2） | `modeling/prompts.py` 系统提示第 9 条定义 0–100 标尺（材料价值，与来源信誉分离，披露 70 门槛）；阈值与合并策略不变 | `test_overall_score_rubric.py`（标尺存在、与 `merge_policy.yaml` 阈值一致、抽取/仲裁均携带、门槛行为不变） |
| A4 可追溯性：请求上限、`finish_reason`、实际后端未持久化；截断残片可能被当成功 | `ModelCallResult.request_diagnostics()`；`finish_reason=length` → `status=output_truncated`，不解析；`model_run.request_diagnostics` 列（幂等迁移）；run 报告 `collection_diagnostics.model_requests`（含批量初筛） | `test_model_request_traceability.py`（5 项） |
| A5/A7 证据可取回：只有 passage_id 无原文片段 | `EvidenceLocator.excerpt`（≤600 字，来自模型输入，不信任模型给的摘录） | `test_cited_passage_excerpt_is_attached_from_input_not_from_model`、`test_excerpt_attachment_is_not_whole_page_persistence` |
| R7 要求 69/71 同条件对照 | 新增 `test_sixty_nine_is_rejected_and_seventy_one_is_accepted_with_identical_bundles` | — |

### 2.2 新增仓库内验收入口

`agents/intelligence_collector_agent/tools/collector_acceptance.py`：隔离工作区准备（复制部署 MIC 配置并固定预算上限、禁用分析复用）、冻结代码指纹、批次预算（≤2 次运行 / ≤6 次 Gateway）、正常 Agent 路径触发、只读审计（20 项工程检查）、内容审阅导出、消息重投、下一周期（临时副本 + 假时钟）、批次汇总。替代 Downloads 下的一次性脚本。

### 2.3 批次 v2 运行后的改动（诚实披露）

v2 run-1 之后只改了：`collector_acceptance.py`（`next-cycle` 的同日检查改到任务层并选择不同分钟；`code_fingerprint` 改为递归遍历未跟踪目录；`content_review` 导出补充 `model_requests`）、`docs/COMMANDS.md`、本报告与 `docs/acceptance/20261004/*` 产物。MIC / Agent 生产代码与 v2 manifest 指纹一致。注意 v1/v2 manifest 的 `untracked_python_sha256` 用的是修复前算法（未遍历未跟踪目录，因此不含验收工具自身）。

## 3. 离线回归

| 命令 | 结果 |
| --- | --- |
| `cd tools/market_intelligence_collector && pytest tests` | 506 passed |
| `cd agents/intelligence_collector_agent && pytest tests` | 188 passed |

R1–R10 覆盖映射（优先沿用项目现有测试，均在 MIC 目录，除注明外）：

| 组 | 主要测试 |
| --- | --- |
| R1 发布时间正例 | `test_bjx_publication.py`（北极星外置标题栏）、`test_energytrend_publication.py`、`test_source_adapters_followup.py::test_bound_sina_body_and_labelled_publication`、`test_publication_time.py` |
| R2 发布时间反例 | `test_bjx_publication.py::test_body_event_date_and_recommendation_dates_are_never_used / test_url_date_segment… / test_unbound_ambiguous_or_hidden_headlines… / test_conflicting_headline_spans…`、`test_publication_time.py`（body/searchish/modified/jsonld/hidden）、`test_energytrend_publication.py::test_unbound_related_or_hidden_dates_not_accepted` |
| R3 日期边界 | `test_publication_time.py::test_window_boundaries_and_timezone`、`test_bjx_publication.py::test_naive_headline_time_is_interpreted_as_asia_shanghai`（cutoff ±1 分钟）、`test_trial_quality_followup.py::test_offset_datetime_persists_as_same_utc_instant`、`test_collection_time_window.py` |
| R4 正文/目录 | `test_article_evidence_gate.py`（他文/侧栏/推荐块/模糊兄弟容器/WAF）、`test_source_adapters_followup.py::test_directory_forms_and_stock_code_are_not_articles`、`test_bjx_publication.py::test_article_scope_uses_headline_time_and_drops_rail_dates` |
| R5 查询与预算 | `test_query_coverage_followup.py`（两名额跨类、单类退化、目录不耗读取预算、有效上限传递） |
| R6 证据语义 | `test_evidence_review.py`（价格≠总额、单位不互换、金额不可拼凑、他段日期仅候选、错角色不补、报价口径冲突只标记）、`test_trial_quality_followup.py::test_order_amount_does_not_establish_company_revenue_contribution` |
| R7 合并入库 | `test_merge_admission_diagnostics.py`（69 拒 / 70、71 过、model link_only 与评分拒绝区分、输入不原地清空、多模型同样拒绝）、`test_overall_score_rubric.py` |
| R8 输出与消费 | Agent：`test_mic_output_gate.py`（facts-only / metrics-only 可用、空结果 unusable、损坏缓存不复用）、`test_event_evidence_export.py`；MIC：`test_merge_admission_diagnostics.py::test_report_explains_empty_output_without_promoting_candidates` |
| R9 生命周期 | `test_browser_lifecycle.py`（34 项：锁、GUI 不可用不静默无头、用户关窗、清理不完整如实报告、supervisor 超时/kill/取消/不伪造成功）、`test_model_request_traceability.py::test_length_stop_is_output_truncated_not_success_even_if_fragment_parses`、Agent `test_mic_supervised_adapter.py` |
| R10 重复/调度 | Agent：`test_agent_reuses_successful_mic_run_for_same_idempotency_key`、`test_duplicate_delivery_while_attempt_active_does_not_start_second_worker`、`test_request_batch_is_idempotent`、`test_cadence_due_daily_weekly_monthly_quarterly`、`test_variable_links_saved_even_for_duplicate_events`；真实验证见 §4.4 |

离线回放使用固定响应与假时钟，仅证明行为，不用于证明 A4 的真实调用。

## 4. 真实端到端验收

### 4.1 批次登记

两批次均：目标 宁德时代 `company_300750`，focus `operating_update`，窗口 30d，参考时间=真实运行时间，预算 2 查询 / 20 命中 / 6 读取 / 6 HTTP / 2 浏览器 / 3 模型调用 / 3 Gateway / 300 s，`max_output_tokens=65536`，`MIC_ALLOW_MOCK=false`，`reuse_analysis=false`。

| 批次 | 代码指纹（tracked diff） | 任务 | Gateway | 耗时 | 结果 |
| --- | --- | --- | --- | --- | --- |
| v1 | `9976951d1685c7d4…` | 1（task_ba3dd36d…，MIC run `run_f3ba5ed4c1e7`） | 3 | 96.6 s | `usable=false`，正确拒绝（2 候选 62/64 分 < 70） |
| v2 | `c2f6d230db81afc1…` | 1（task_292d764a…，MIC run `run_2ad99a89139a`，Agent run `run_84c3332a6dcc474ea8a5`） | 2 | 65.0 s | `usable=true`，12 条正式记录 |

v1 后没有运行第二个任务：同一查询会再次命中同两篇文章，属"同一文章反复评分"，合同禁止。

### 4.2 批次 v1 原因分解与缺陷定位

漏斗：20 命中 → 6 选读 → 5 读成功 → 2 过日期门（BJX `2026-09-15T11:47+08:00`，energytrend `14:41+08:00`，均由本篇字段绑定；另有 1 过期、2 发布时间未验证、1 正文无法界定）→ 2 模型分析（schema 均合法，严格审查仅隔离 relations）→ 0 入库，原因 `below_min_overall_score`（62、64）。

两篇都是同一具体事件（2026-09-15 公示：宁德时代中标 10MW/40MWh 钠离子储能标段 4141.622 万元）。模型 `decision=save_structured`、`confidence 0.78–0.82`、`source_credibility 0.55–0.6`，但 `overall_score` 落在 60 多分；连同既有 pilot 的 66 分共三个数据点一致。检查代码与规格：`SCHEMA_HINT` 对 `overall_score` 仅写 `"0-100"`，规格文档亦无定义——门槛在对一个无定义的数值生效，属于"类别性材料损失"（任何媒体转载的公开公示都会落在 60 多分）。修复：在稳定系统提示中定义标尺（材料价值，明确不折算来源信誉/佐证状态——它们有各自字段），70 阈值、严格证据审查、合并策略不变；离线回归通过后登记 v2。

### 4.3 批次 v2 结果（A1–A10）

| ID | 证据 |
| --- | --- |
| A1 | `steps.json`：register(suspended) → resume → tick 创建 request ticket+msg → plan 1 task → collect（supervised 子进程 `attempt_c2c8c8f7f9ed49f097b3`，exit 0）→ `collection.result` 发布 1 次 → suspend；ID 链见 `result.json.ids` |
| A2 | 2 查询跨两类：`宁德时代 动力电池 中标`(orders_tender)、`宁德时代 重大合同 公告`(official_ir)；`search_page_attempt` 的 `query_observed == query_requested`，均 `ok`；20 命中、6 选读、读取路径与失败原因逐条记录（`content-review.json.sources`） |
| A3 | 正式来源发布时间 `2026-09-15T14:41:00+08:00` 来自 `energytrend:entry-header.newsdate`（本篇标题栏），窗口按运行时间判断 `in_window`；新浪 2026-01-14 公告判 `outside_time_window`；两份 PDF `pdf_publication_not_verified`；无一例使用正文事件日期/URL 日期 |
| A4 | `model_requests`：`serp_batch_triage` 与 `bundle_extraction` 均 `requested_max_tokens=65536`、`finish_reason=stop`、`served_model=openclaw/main`、`is_mock=false`、`output_truncated=false`；`model_run.request_diagnostics` 持久化并经 `explain_source_analysis` 可查 |
| A5 | schema 合法；12 条记录主体/角色/数值/单位/日期与原句相符（§5）；relations 因 `object_not_literal` 被隔离未入库；金额、事件日期、经济解释进入 `pending_review` 而非直接断言 |
| A6 | 阈值 70 与合并策略未改；v1 两候选被拒且保留诊断、不计入正式；v2 候选 76 分入库；无 URL 特判、无补分 |
| A7 | `mic_database_counts_match_report=true`、`agent_events_match_report=true`；Agent `structured_events` 2 条带 `source_url/published_at`；每条正式记录通过 `evidence_locator.excerpt` 可取回原文片段 |
| A8 | §4.4 |
| A9 | `budget_used`：2 查询、20 命中、6 选读、5 HTTP、1 浏览器、2 模型、2 Gateway、65 s；`cleanup=complete`（3 页开/关），supervisor `completed` 并回收，无活动 attempt、无遗留采集消息、profile 锁释放 |
| A10 | `execution_status=completed, search_status=ok, read_status=partial, output_status=ok, usable=true`；v1 则 `usable=false` 且 `no_structured_output` 原因明确（未被 `accept_degraded` 掩盖） |

### 4.4 重复与下一周期（A8 / R10）

`redeliver`（真实工作区，对已 ack 的 collect 消息重投）：消息被再次处理，`reused=true`，前后计数完全一致（collection_runs 1→1、attempts 1→1、structured_events 2→2、result 消息 1→1、MIC model_run 1→1、正式记录 12→12），`new_model_calls=0`，未启动浏览器。

`next-cycle`（state/bus/data 临时副本，MIC 禁用，假时钟）：同日 +7 分钟再 tick → 产生 request（request key 为分钟粒度，设计如此），规划结果解析到**已完成的同一 task ticket 与同一消息**，`collection_tasks` 行数不变、无重开消息；次日 tick → 1 request → 1 task，新 key `…:2026-10-05`，`_successful_mic_run(new)=None`，旧 key 仍映射旧 run；用旧报告重跑 `save_mic_structures` → 新增事件 0。

## 5. 内容审阅（字段—原文—判定）

审阅方式：开发者自审。原文为 2026-10-04 对 `https://www.energytrend.cn/news/20260915-148614.html` 的只读 HTTP 抓取（无模型、无浏览器），经 `extract_article` 得到正文（正文仅 3 句 + "来源：河北省招标投标公共服务平台"），见 `20261004/batch-v2/run-1/source-live-review-energytrend.json`；每条记录的 `evidence_locator.excerpt`（模型输入段落）均逐字存在于该实时正文。未向被测模型询问其是否正确。

原文三句：
p0 "2026年9月15日，河北任丘智弘100MW/400MWh新型技术路线磷酸铁锂电池+钠电池独立储能试点项目储能系统设备采购中标结果公示。"
p1 "项目共两个标段，一标段为磷酸铁锂电池储能系统，30MW/120MWh大容量磷酸铁锂+60MW/240MWh长寿命磷酸铁锂，远景能源中标价为19461.6万元，合单价0.518元/Wh；"
p2 "二标段为钠电池储能系统，10MW/40MWh钠离子储能系统，宁德时代中标价为4141.622万元，合单价1.035元/Wh。"

| # | 类型 | 关键字段 | 引用 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | fact/order | 宁德时代中标二标段钠离子储能 10MW/40MWh，4141.622 万元，1.035 元/Wh；positive | p2 | 成立（主体/标段/容量/金额/单价全部在 p2） |
| 2 | fact/order | 远景能源中标一标段磷酸铁锂 30MW/120MWh+60MW/240MWh，19461.6 万元，0.518 元/Wh；neutral | p1 | 成立；未把整项目容量算给宁德时代 |
| 3 | fact/price | 钠电单价 1.035 显著高于磷酸铁锂 0.518，"反映钠电储能当前单位成本仍高于锂电"；mixed | p2 | 两数值成立（分别在 p2、p1），引用段落只覆盖 1.035；"单位成本"为推断（原文是中标单价）。判定：数值成立、结论属推断，不构成实质错误，但建议作为推断对待 |
| 4 | fact/technology | 项目定位"磷酸铁锂+钠电池独立储能试点"，100MW/400MWh；period 2026-09-15 | p0 | 成立 |
| 5 | metric | 宁德时代钠电中标金额 4141.622 万元 | p2 | 成立；`amount_status=pending_review`（严格审查） |
| 6 | metric | 钠电中标单价 1.035 元/Wh | p2 | 成立；解释被降为"来源报价观察，经济含义待核查" |
| 7 | metric | 磷酸铁锂中标单价 0.518 元/Wh | p1 | 成立；明确"暂不用于成本或利润比较" |
| 8 | metric | 磷酸铁锂中标金额 19461.6 万元 | p1 | 成立 |
| 9 | metric | 项目总规模 400 MWh；period 2026-09-15 | p0 | 成立（100MW/400MWh） |
| 10 | metric | 宁德时代钠电标段 40 MWh | p2 | 成立（10MW/40MWh） |
| 11 | event/major_order | 宁德时代中标二标段 10MW/40MWh 钠电，4141.622 万元，1.035 元/Wh；positive；single_source | p2 | 成立；`event_date` 为空、候选 2026-09-15 `pending_review`（日期在 p0，事件引用 p2）；counterparty 置 `pending_review`（业主未在正文披露） |
| 12 | event/tender | 一标段磷酸铁锂 90MW/360MWh 由远景能源中标，19461.6 万元，0.518 元/Wh；neutral | p1 | 成立（30+60=90MW，120+240=360MWh，正文两段容量相加，p1 直接列出两段） |

补充：来源为行业媒体转载招标平台公示，Agent 质量结果据此给出 `low_authority_sources_only`（P2）；brief 的 `uncertainty` 已注明"仅中标公示、交付/供货范围/毛利未知、未见官方公告"，来源陈述与独立核实事实有区分。

## 6. 遗留限制与建议

1. **BJX 反爬**：v2 中 HTTP 与浏览器均返回挑战页（14 分钟前的 v1 浏览器可读，更早另有一次定点探测），正确归类 `anti_bot_page`、未误读；本次由 energytrend 满足目标。建议：观察频率限制，不建议为单站增加重试预算。
2. **事件日期保守**：日期在 p0 而事件引用 p2，严格审查只给候选，正式 `event_date=null`；Agent 事件去重 key 因此用 `unknown` 日期 + 摘要哈希。可考虑允许"同篇其它段落的发布日当天日期"作为低置信绑定，但属策略变更，未在本轮实施。
3. **批量初筛未入 `model_run`**：只在 run 报告 `collection_diagnostics.model_requests` 中可追溯（含上限/finish_reason/后端）。
4. **输出截断处理**：`finish_reason=length` 现在是明确的 `output_truncated` 状态，不再可能被当成功；更大上限重试未实现。
5. **单模型合并**：`overall_score` 为模型按标尺自评（76），门槛仍由系统执行；多模型加权仲裁路径未在真实运行中触发。
6. **fact #3 类推断**：评审通过数值，但模型把"中标单价差"写成"单位成本差"；严格审查未覆盖 fact_statement 中的跨段数值与措辞推断。
7. **指纹算法**：v1/v2 manifest 的 `untracked_python_sha256` 未包含未跟踪目录中的文件（已修复，后续批次生效）。

## 7. 产物索引

```
agents/intelligence_collector_agent/docs/acceptance/
├── collector_acceptance_20261004.md            本报告
└── 20261004/
    ├── bjx-pinpoint-read-probe.json             §5.1 定点读取（非发现式，0 模型调用）
    ├── batch-v1/{acceptance-manifest,acceptance-summary}.json
    ├── batch-v1/run-1/{started,steps,result,mic-report,content-review}.json   失败批次完整记录
    ├── batch-v2/{acceptance-manifest,acceptance-summary}.json
    └── batch-v2/run-1/{started,steps,result,mic-report,content-review,redeliver,next-cycle,
                        source-live-review-energytrend}.json
```

工作区（含数据库，不入库）：`~/.local/state/agents_groups/collector-acceptance-20261004-v1`、`-v2`。所有产物经过凭据模式扫描（API key / Bearer / token / Cookie / HTML），无命中。
