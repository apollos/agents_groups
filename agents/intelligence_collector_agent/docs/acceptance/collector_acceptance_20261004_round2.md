# 情报收集员 Agent × MIC 验收第二轮：Codex 复核 F1–F3 修复（2026-10-04/05）

依据：`/home/yu/Downloads/codex_e2e_review_b62a210_20261004.md`（Codex 对提交 `b62a210` 的独立复核：工程链通过、**内容质量未通过**）。本轮在 `b62a210` 之上修复 F1/F2/F3，补齐 Q1–Q5 回归，并按复核 §5 在冻结版本上跑了新的隔离端到端任务。第一轮报告见 `collector_acceptance_20261004.md`。

## 首屏判定

| 项 | 结论 |
| --- | --- |
| 基线 | `b62a210`（用户提交"第一轮端到端修改完毕"）+ 本轮未提交 diff。全程未 reset / checkout / stash；pilot 工作区、v1/v2/v3 工作区及 Codex 的 `codex-e2e-20261004-225542-024196784` 均未改动、未清库 |
| 判定 | **CURSOR_SELF_TEST_PASS（第二轮）**。批次 v3 作为"工程通过、内容复审发现 4 处残留"保留；批次 v4（最终代码指纹）工程 20/20 检查通过、12 条正式记录逐项对照原文无未支撑的经济影响判断 |
| F1 | 条款级 `StatementReview`（`mic/statement_review.py`）覆盖 fact_statement / event.summary / event.impact / metric.interpretation / brief 四类字段；复核中漏网的 `metric_f94f3891e3ca`、`evt_fe2b0dc639d6`、`evt_47da24c17a62` 原句及 5 种改写在回归中全部进入 `pending_review`；有基数或原文字面句保留 |
| F2 | 报价口径 `pending_review` 随记录传播：事实比较句附 `comparison_evidence`（另一侧段落）、`source_price_basis_status=pending_review`、`usable_as_price_benchmark=false`；brief/fact/event 中"可比参照/基准/对照"措辞被替换为限制说明；两侧报价原文保留，0.518 未改写为 0.5406，远景能源订单记录未删除 |
| F3 | Agent 侧业务事件身份（`event_identity.py`：主体 + 动作族 + 容量/金额 + 45 天同一新闻周期）+ 源级账本 `structured_event_sources`；同一事项改写摘要 / 换类型标签 / 换来源 / 换 run / 新周期 → 0 新事件、全部挂为 `linked`；不同标段 / 项目 / 主体 / 日期 / 周期分开；旧库行迁移时补 key、不删不改 |
| 真实运行 | v3：3 Gateway、107 s、2 篇入库（BJX 78、energytrend 76）、26 条正式记录、4 源级事件行 → 3 独立事件 + 1 linked；v4：2 Gateway、75 s、1 篇入库（energytrend 74；BJX 本次返回滑动验证页，归类 `anti_bot_page`）、12 条正式记录、2 源级行 → 2 事件。两批合计 5 次 Gateway，每批 1 个任务，无同篇反复评分 |
| 重投 / 下一周期 | v3、v4 均 `ACCEPTANCE_REDELIVER_PASS`（复用 run、0 新模型调用、0 新记录）与 `ACCEPTANCE_NEXT_CYCLE_PASS`（新增检查：**改写后的报告在下一周期任务下重存 → 新事件 0、linked = 源行数、账本行数 +N**；原样重放 → 全部 `replayed`） |
| 离线回归 | MIC 518 passed（+12：`test_statement_review.py`）、Agent 194 passed（+6：`test_business_event_identity.py`，含真实 5 行 fixture 与旧库迁移） |
| 遗留 | ① 措辞类推断（"电网侧""落地信号""验证商业化"）不属于 F1 四类，未拦截，见 §5；② 一条 fact 的 `fact_type=product` 标签不准（日期陈述）；③ BJX 反爬具有时变性；④ Codex 复核需要的"冻结提交"仍待用户提交（本轮未提交） |

## 1. 对复核 F1–F3 的逐项修复

### 1.1 F1（P1）正式输出中无依据的经济影响判断

根因：`evidence_review` 的 `MATERIALITY` 只匹配少数措辞，且只检查 metric.interpretation 与 brief；fact_statement、event.summary、event.impact 完全没有审查。

修复（`tools/market_intelligence_collector/mic/statement_review.py`，由 `EvidenceReview.apply()` 末尾调用，只在 `strict_evidence_review=true` 下运行，原始模型输出不改动）：

| 类别 | 判定为"需要依据"的条款 | 放行条件 |
| --- | --- | --- |
| materiality | 含公司级财务基数词（收入/营收/利润/业绩/出货…）且带量级或相对词；或"体量/规模/金额/订单 + 较小/虽小/不大/有限/较大…"而条款内无基数（占 / % / 倍 / 大于…） | 所引段落含基数数字（`BASE_FIGURE`），或条款字面出现在原文 |
| competition | 竞争/同台/替代/丢标/份额/压力… | 原文含竞争线索（竞争/落标/未中标/流标/同台…） |
| economics | 成本/毛利/盈利/利润率/经济性/溢价/降本… | 原文含对应线索 |
| price_basis | 报价词 + 可比/参照/基准/对照… 且本篇存在口径不相容的报价 | 原文自身出现可比措辞 |

被判定的条款从正式文本中移除，追加固定的待核查说明；原句与条款列表写入该记录的 `statement_review`（facts/events 的 `metrics[...]`，metrics 的 `scope[...]`，brief 直接替换）并登记 `quality_reviews` / `coverage_gaps`。事件 `impact`：`competition` 通道无线索、`cost/margin` 无线索，或**事件主体不是目标公司且摘要未提及目标**（如远景能源中标另一标段）而仍给出方向/通道 → `impact_review`，`impact` 复位为 unclear。目标名由 pipeline 按本次 target profile（canonical_name + aliases）注入验证器。

Q1 回归（`tests/test_statement_review.py`）：复核原句"规模相对公司整体营收体量较小"及 4 种改写全部 hold，`metric_value`、`amount_evidence`、excerpt 原样保留，原始 raw 不变；"去年储能收入 572 亿元，本单占比约 0.07%"（有基数）保留；原文字面句保留；fact_statement 与 event.summary 中同类句同样 hold；批次 v3 泄漏句"体量较小 / 规模较小 / 体量虽小" hold，而"占项目总规模 400MWh 的 10%""规模远大于钠电标段"保留。

### 1.2 F2（P1）报价口径状态未传播

修复：`EvidenceReview.source_price_checks` 记录口径不相容的段落 id；`StatementReview._price_comparison` 对含 ≥2 个报价且带排序/可比词的 fact/event：每个报价必须在某一段原文中找到，否则整句 hold；找到则附 `comparison_evidence=[{value, passage_id}]`，并在存在口径问题时写 `source_price_basis_status=pending_review`、`usable_as_price_benchmark=false`、`price_basis_passages`。brief 的 `why_it_matters / uncertainty / one_sentence / what_happened` 对全文做同样的条款审查。

Q3 回归：fact "1.035 明显高于 0.518" 保留两报价、附 p1 证据与限制；metric 0.518 保留 `source_price_basis_review`（含 0.5406 的条件说明，不改原值）；brief "可比参照" 被替换；比较对象不在任何段落（0.60 元/Wh）→ hold；金额/容量/单价一致的来源不凭空加限制。真实 v4 输出：brief `why_it_matters` 原"可比"措辞已替换为"同项目两个标段报价的供货范围与口径尚未核实，不作为可比基准"；metric 0.518 解释为"来源报价观察；供货范围与报价口径待核查，暂不用于成本或利润比较"。

### 1.3 F3（P2）同一业务事件改写后被当作新事件

修复（Agent）：

- `event_identity.py`：`signature(event)` → 主体归一（去空白/公司后缀/标点）+ 动作族（major_order/tender/bid/contract/…→award；capacity_*→capacity；…）+ 容量 MWh（解析 `40MWh`/GWh/数值）+ 金额万元（元 / 万元 / 亿元 归一）+ 事件日期 + 发布时间。`business_key = 主体|动作族`；`compatible_with`：双方都已知的数量须一致（0.5% 容差）、至少共享一个数量（或双方均无数量时产品一致）、已知事件日期一致、发布时间相差 ≤45 天。无主体或动作族未知 → `unresolved`，永不合并。
- `db.py` v9 迁移（幂等）：`structured_events` 增 `business_key / dedup_status / source_count`，新表 `structured_event_sources`（每个源级事件行一条，`link_status ∈ primary/linked/replayed`）。
- `persistence.save_mic_structures`：先对旧库未打 key 的行补 key 并登记为各自事件的 `primary`（只增不删）；每行：原内容 key 已存在 → `replayed`；否则按业务 key 找兼容事件 → `linked`（`source_count+1`，跨域名 → `multi_source`）；否则新建。计数 `source_event_rows / events / events_linked / events_replayed / events_unresolved`，随结果 `quality.event_ledger` 一并输出。

Q4 回归（`tests/test_business_event_identity.py`，fixture 为 Codex 运行产出的真实 5 行事件）：BJX 与 energytrend 两个副本签名一致（4141.622 万元、40 MWh、`宁德时代|award`）；5 行 → 3 事件（任丘智弘项目公示 / 宁德时代二标段 / 远景能源一标段），账本 primary 3 + linked 2；下一周期改写（新 run/link id、"转载："前缀、major_order↔tender）→ 新事件 0、linked 5；不同标段容量 / 不同金额 / 不同主体 / 不同动作族 / 不同事件日期 / 106 天前发布 → 7 个独立事件；无主体行 unresolved 不合并；旧库（无新列）迁移后首次保存补 key，改写副本挂到旧事件。

验收工具相应修改：`result.json` 增 `agent_event_ledger`（MIC 事件行 / 已登记源行 / 独立事件 / 新事件 / linked / replayed），检查项 `agent_events_match_report`（Agent 事件数 == MIC 行数）替换为 `agent_event_ledger_consistent`（源行 == MIC 行，且 primary+linked+replayed == 源行，且 primary == 本次新建）；`next-cycle` 增 `same_facts_rewritten_next_cycle_add_no_events`，保留原样重放检查 `same_facts_exact_replay_is_noop`。

## 2. 离线回归（Q1–Q5）

| 命令 | 结果 |
| --- | --- |
| `cd tools/market_intelligence_collector && pytest tests` | 518 passed（第一轮 506） |
| `cd agents/intelligence_collector_agent && pytest tests` | 194 passed（第一轮 188） |

| 组 | 测试 |
| --- | --- |
| Q1 | `test_statement_review.py::test_q1_*`（4 项） |
| Q2 | `test_statement_review.py::test_q2_*`（3 项：另一标段中标 negative/neutral competition → hold；主体非目标 + revenue → hold，目标自身中标保留，无目标名时不臆断；原文有竞标/落标线索时保留） |
| Q3 | `test_statement_review.py::test_q3_*`（3 项）+ `test_holding_texts_are_not_re_reviewed_idempotently` |
| Q4 | `test_business_event_identity.py`（6 项） |
| Q5 | 第一轮全部测试不变：`test_merge_admission_diagnostics.py`（69/70/71）、`test_collection_time_window.py`、`test_model_request_traceability.py`（64K / 截断）、`test_browser_lifecycle.py`（超时）、Agent 重投/去重测试；两套件全绿 |

## 3. 真实端到端（复核 §5）

两批次均通过正常验收入口 `collector_acceptance.py prepare → run → redeliver → next-cycle → summary`：目标 宁德时代 `company_300750`，30 天窗口，参考时间 = 真实运行时间（v4 启动于 2026-10-05 00:06 CST，任务 key 日期为 10-05，未回拨），70 分门槛，严格证据审查，64K，预算 2 查询 / 20 命中 / 6 读取 / 6 HTTP / 2 浏览器 / 3 模型 / 3 Gateway / 300 s，批次 ≤2 运行 / ≤6 Gateway。

| 批次 | 代码指纹 | 入库 | Gateway / 耗时 | 工程检查 | 内容复审 |
| --- | --- | --- | --- | --- | --- |
| v3 `run_f81909ebb931` | tracked diff `4e246373…` / untracked py `f39b90bd…` | BJX 78 + energytrend 76，26 条 | 3 / 107 s | 20/20 | 发现 4 处残留（§3.1）→ 修复 → 不在 v3 工作区上重跑 |
| v4 `run_2cd5c76ce307` | tracked diff `324f5f8f…` / untracked py `f0627e1e…`（最终代码） | energytrend 74，12 条 | 2 / 75 s | 20/20 | 通过（§4） |

v3 工作区按工具的冻结规则不能再用新代码运行，故开 v4。v3 的两篇原始模型输出已用 v4 代码离线重验（只读 `model_output`，0 模型调用）：`20261004/batch-v3/run-1/offline-revalidation-with-v4-code.json` —— 4 处残留全部进入 `pending_review`，其余观察保留。

### 3.1 v3 内容复审发现的残留（均为 F1 类改写，已修复并有回归）

| 记录 | 正式文本 | 处理 |
| --- | --- | --- |
| `metric_65f97ce281ed` | "单笔中标金额，体量较小，具技术验证与订单锚点意义" | "体量较小"无基数 → `materiality_review`；现规则：量级词 + 规模词且条款内无基数即 hold |
| `metric_a60797d50eda` | "10MW/40MWh…，规模较小，具试点性质" | "规模较小" hold；"具试点性质"（原文"试点项目"）保留 |
| brief(BJX).why_it_matters | "体量虽小，但为钠电路线商业化验证提供价格与订单锚点" | "体量虽小" hold，连带的"但"去除 |
| `evt_cb752f86a31a` | 远景能源中标一标段，impact neutral / revenue / quarter / low | 主体非目标且摘要未提及目标 → `impact_review`，impact 复位 |

### 3.2 v4 事件账本

MIC 事件行 2（宁德时代二标段 major_order；任丘智弘项目 tender）→ 独立事件 2，primary 2。`next-cycle`：改写（"转载："+"成功中标"、类型互换、`_next` run/link id）在次日任务下重存 → `events=0, events_linked=2`，账本 2→4；原样重放 → `events_replayed=2`，账本不变。v3 同项：4 行 → 3 事件 + 1 linked（BJX 与 energytrend 的宁德时代中标行合并为一个 `multi_source` 事件），改写重存 → 0 新事件、linked 4。

## 4. 批次 v4 内容审阅（字段—原文—判定）

原文三句同第一轮 §5（energytrend 转载公示，p0/p1/p2），每条记录的 `evidence_locator.excerpt` 均为模型输入段落。

| # | 类型 | 正式文本（关键字段） | 引用 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | fact/order | 宁德时代中标二标段钠电池储能系统，中标价 4141.622 万元；positive | p2 | 成立 |
| 2 | fact/price | 宁德时代二标段钠电池储能系统中标单价 1.035 元/Wh | p2 | 成立 |
| 3 | fact/order | 一标段磷酸铁锂由远景能源以 19461.6 万元中标，折合 0.518 元/Wh；neutral；`source_price_basis_status=pending_review` | p1 | 成立；口径限制随记录 |
| 4 | fact/order | 项目名称 + 2026-09-15 公示 | p0 | 成立（`fact_type` 应为 event/notice，标签不准，见 §5） |
| 5 | fact/product | 二标段为 10MW/40MWh 钠离子储能系统，属钠电池储能技术路线应用；positive | p2 | 成立（原文"钠电池储能系统"） |
| 6 | metric | 宁德时代钠电储能中标金额 4141.622 万元："公司在该试点项目中的钠电储能中标金额"；revenue | p2 | 成立，无量级判断 |
| 7 | metric | 钠电中标单价 1.035：原"高于同项目磷酸铁锂单价约一倍"（revenue, margin）→ "来源报价观察；经济含义待核查" | p2 | 原句进入 `economic_interpretation_review` |
| 8 | metric | 二标段容量 40 MWh："10MW/40MWh（2 小时系统）"；demand | p2 | 成立（40/10） |
| 9 | metric | 磷酸铁锂单价 0.518：原"作为钠电单价对比基准，反映钠电路线当前成本仍偏高"→ 限制说明；`source_price_basis_review` 含 0.5406 条件说明，原值不改 | p1 | 原句 hold，F2 行为正确 |
| 10 | metric | 一标段金额 19461.6："竞争方远景能源中标金额，用于横向比较规模"→ "用于横向比较规模；对目标公司竞争影响的判断缺少依据，待核查" | p1 | "竞争方"条款 hold |
| 11 | event/major_order | 宁德时代中标二标段…4141.622 万元、1.035 元/Wh；positive / revenue, demand / 1m / low | p2 | 主体即目标，中标事实在 p2，保留 |
| 12 | event/tender | 项目公示，两标段分别由远景能源与宁德时代中标；neutral / demand | p0 | 摘要提及目标且为公示事实，保留 |

brief（非正式记录）：`why_it_matters` 原含可比与量级措辞，现为"…具备技术路线与定价信号意义；同项目两个标段报价的供货范围与口径尚未核实，不作为可比基准。订单对公司收入的贡献尚未核实。"；`uncertainty` 附"成本或盈利含义待核查"。

结论：12/12 正式记录数值、主体、角色与原文一致；复核 F1 四类（量级/竞争/成本盈利/可比）判断在正式文本中无残留；F2 限制到达 fact / metric / brief。

## 5. 遗留限制（诚实披露）

1. **措辞类推断未拦截**："电网侧"（原文为"独立储能"）、"落地信号""验证钠电技术路线商业化"等定性框架不属于 F1 定义的经济影响判断类别，`StatementReview` 不是语义蕴含检查器，这些句子保留在 brief 中。若要求也拦截，需要新增"框架性判断"类别，属策略变更。
2. **标签不准**：v4 #4 `fact_type=order`（日期/名称陈述）、#5 `direction=positive`（描述性事实）；不影响数值与主体，未在本轮改动。
3. **BJX 反爬时变**：v3 浏览器可读（标题栏时间 `2026-09-15 11:47` 绑定成功），v4 返回"滑动验证页面"并被正确归类 `anti_bot_page`；由 energytrend 满足目标。
4. **冻结提交**：复核要求在"冻结提交"上运行；本轮代码为 `b62a210` + 未提交 diff，指纹记录在 v4 manifest；用户提交后，提交号即为 Codex 下轮复核的冻结版本。manifest 的 tracked diff 不含本报告与产物文件之外的改动（v4 运行后只新增了文档/产物与 `batch-v3/run-1/offline-revalidation-with-v4-code.json`）。
5. **旧库补 key 的 `multi_source` 判定**：仅在 linked 行域名与事件首源域名不同时升级；同域重复转载不升级。

## 6. 产物索引

```
agents/intelligence_collector_agent/docs/acceptance/
├── collector_acceptance_20261004.md                第一轮报告
├── collector_acceptance_20261004_round2.md         本报告
└── 20261004/
    ├── batch-v3/{acceptance-manifest,acceptance-summary}.json
    ├── batch-v3/run-1/{started,steps,result,mic-report,content-review,redeliver,next-cycle}.json
    ├── batch-v3/run-1/offline-revalidation-with-v4-code.json   v3 原始模型输出用最终代码离线重验
    ├── batch-v4/{acceptance-manifest,acceptance-summary}.json
    └── batch-v4/run-1/{started,steps,result,mic-report,content-review,redeliver,next-cycle}.json
```

代码：`tools/market_intelligence_collector/mic/statement_review.py`（新）、`mic/evidence_review.py`、`mic/validate.py`、`mic/pipeline.py`；`agents/intelligence_collector_agent/src/agent_trade_intel/{event_identity.py（新）,persistence.py,db.py,agent.py}`；`tools/collector_acceptance.py`；测试 `tests/test_statement_review.py`、`tests/test_business_event_identity.py` + `tests/fixtures/codex_e2e_b62a210_all_events.json`。

工作区（含数据库，不入库）：`~/.local/state/agents_groups/collector-acceptance-20261004-v3`、`-v4`。产物经凭据模式扫描（API key / Bearer / Authorization / Cookie / token），仅命中计数字段 `cookies_injected: 0`。
