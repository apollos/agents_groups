# 情报收集员 Agent × MIC 验收第四轮：Codex 复核 run_1b8ec1dde3b5 事件身份归并（2026-10-05）

依据：`/home/yu/Downloads/codex_e2e_review_run_1b8ec1dde3b5_20261005.md`（Codex 第三次独立复核：工程链路通过；上轮 R2 单位、R3 省略式比较在本次样本中通过；**剩余阻塞是事件身份归并**——5 条来源事件行被计为 5 个独立事件，业务上应为 3 个）。按复核要求，本轮不做新的网页采集，直接以该次已保存的 5 条事件作为生产持久化路径的回放输入完成修复与正反例验证。前三轮见 `collector_acceptance_20261004.md`、`_round2.md`、`_round3.md`。

## 首屏判定

| 项 | 结论 |
| --- | --- |
| 基线 | `693b3df` + 第三轮未提交 diff（即 Codex 本次复核实际运行的代码，未跟踪指纹 `edadc720…` 一致）。未 reset / checkout；Codex 工作区 `codex-e2e-20261005-140117-361081759` 只读，文件时间戳未变 |
| 判定 | **CURSOR_SELF_TEST_PASS（第四轮）**：复核 §4 最小回归六项全部在生产持久化路径（`ResultPersister.save_mic_structures`）上断言通过 |
| 根因 | 两处身份差异都落在"主体/项目键"层：① `宁德时代新能源科技股份有限公司` 归一后为 `宁德时代新能源科技` ≠ `宁德时代`（没有企业别名解析）；② 项目键靠"全局删除描述词"得到，北极星摘要里的"钠电"不在词表 → `任丘智弘钠电` ≠ `任丘智弘` |
| 修复 ① | 企业身份用 **target profile 的 canonical name + aliases** 解析（MIC 报告新增 `target_aliases`；Agent 任务 `company_name` + 报告 `target` 兜底）。只做精确别名匹配，不做前缀/模糊猜测：同一 target_id 下的 `远景能源` 仍是远景能源，`远景能源科技有限公司` 与 `远景能源` 不合并 |
| 修复 ② | 项目键改为结构规则：项目名 = `<识别性头部><类型描述词>项目`，键取**第一个描述词之前的头部**（去省级前缀），不再逐词删除；`河北任丘智弘储能项目 / 储能试点项目 / 独立储能试点项目 / 磷酸铁锂+钠电独立储能试点项目 / 新型技术路线（…）独立储能试点项目` → 同一键 `任丘智弘`；`甲地示例` ≠ `乙地示例`；`任丘智弘二期` 是另一个项目；没有识别性头部（"新型技术路线储能试点项目"）→ 无项目 → 中标类事件 `unresolved`，不合并 |
| 来源上下文 | 每条源行的 payload 新增 `business_identity`：主体原文 → 键（`resolution: target_alias / normalized`）、项目名原文 → 键、标段、能量 MWh、金额万元。原始 5 条源行、引用链、发布日期全部保留 |
| 回放结果 | 复核 5 条原样输入 → **3 个业务事项、5 条来源证据、2 条 linked、0 unresolved**（§2），映射与复核 §3 预期表逐项一致；第二次复核的 4 条 → 2 事项、第一轮 5 条 → 3 事项、v6 1 条 → 1 事项不变 |
| 新入口 | `collector_acceptance.py replay-events`：只读回放任意工作区已保存 run 的事件行，输出事项 → 来源映射与账本计数（复核 §4 "复用生产路径而不是另做绕过持久化的计数脚本"） |
| 离线回归 | MIC 528 passed（浏览器流水线测试增加 `target_aliases` 断言）、Agent 203 passed（身份测试 15 项，+5） |
| 未做 | 没有新的真实采集（复核明确不要求）；冻结提交待用户提交并 push 后由 Codex 用不同材料做独立端到端 |

## 1. 修复细节

### 1.1 企业别名（`event_identity.EntityAliases`，`persistence.py`，`mic/pipeline.py`）

- MIC `_summary` 报告新增 `target_aliases`（profile `aliases`），与已有 `target`（profile `canonical_name`）一起随结果传给 Agent。
- `ResultPersister.save_mic_structures` 构造 `EntityAliases([company_name, report.target, *report.target_aliases], key=company_name)`：别名组内任一拼写 → 同一键；键优先用任务里的短名（`宁德时代`），保证与前几轮已入库的 `宁德时代|award|…` 键一致。
- 非别名组的主体只做既有后缀归一（`有限公司`/`股份有限公司` 等），不做前缀匹配——复核边界 1。
- 旧报告没有 `target_aliases` 时（本次复核保存的报告即如此），组 = {任务短名, 报告 canonical}，已足以归并本次 5 条。

### 1.2 项目键（`event_identity.project_name / project_key`）

- `project_name(text)`：去容量 token 与括注后取首个"…项目|电站|基地|园区"，并去掉其前面的动词（中标/承建/签约…）—— 这是保留下来的**项目名原文**。
- `project_key`：在项目名里找第一个类型描述词（新型技术路线/独立/共享/储能/试点/示范/磷酸铁锂/钠电/锂电/电池/采购/公示/+ …）的位置，键 = 其之前的头部；头部 ≥2 字且去省级前缀后 ≥2 字才成立，否则无项目。
- 与上一轮"删除词表中出现的词"的差别：描述词只用来定位切点，词表不全（"钠电"不含"池"）不会把残片带进键里；头部本身不被改写（"大唐和平共享储能电站" 的 "和" 不会被当连接词删掉）。

### 1.3 已有行的重键（`persistence._rekey_business_events`）

业务键是派生索引而非数据：每次保存前对同一 target 的已键行用当前规则重算，键或状态变化则只更新 `business_key / dedup_status`（计数 `events_rekeyed`），不删除、不合并、不改事件与来源。这样前几轮工作区（含试点库）里用旧规则键入的行，在下一周期能被新副本找到而不是再生成一条。

## 2. 复核 §4 最小回归 → 测试与结果

| 用例 | 测试（`tests/test_business_event_identity.py`） | 断言结果 |
| --- | --- | --- |
| 本次 5 条原样输入 | `test_t_round3_five_real_rows_yield_three_matters_with_five_sources`（fixture `tests/fixtures/codex_e2e_run_1b8ec1dde3b5_all_events.json`，由只读 `mic.db` 导出） | 空隔离库：`events=3, events_linked=2, source_event_rows=5, unresolved=0`；键 `宁德时代\|award\|任丘智弘\|标段二`（2 源 multi_source）、`任丘智弘\|award\|任丘智弘\|`（2 源 multi_source，日期 2026-09-15）、`远景能源\|award\|任丘智弘\|标段一`（1 源）；账本 5 行、URL / source_link_id 全保留；`business_identity.subject.resolution == target_alias` |
| 同一结果重投 | 同上第二次保存 | `events=0, linked=0, replayed=5`；无模型请求（纯持久化） |
| 新周期等价表述 | 同上第三次：新任务 key、新 run、摘要加前缀、类型互换、来源 id 变、`宁德时代` 改写为别名 `CATL` | `events=0, linked=5`；库中仍 3 个事项 |
| 不同项目其他数量全同 | `test_t2_different_projects_with_identical_quantities_date_and_subject_are_two_events`（上一轮）继续通过；`test_project_key_is_the_head_before_type_descriptors_not_a_word_deletion` 断言 `甲地示例 ≠ 乙地示例`、`任丘智弘二期 ≠ 任丘智弘` | 2 个独立事项 |
| 不同主体 / 不同标段 / 真实变更 | `test_different_lot_project_subject_date_or_news_cycle_are_not_merged`（9 个独立）、`test_non_target_companies_sharing_the_task_are_not_one_subject`（`远景能源` 与 `远景能源科技有限公司` 同 target_id 同标段同数量 → 2 事项） | 不误合并 |
| 单位与价格比较回归 | MIC `test_quantity_units_and_elided_comparison.py` 10 项、Agent `test_t3_*` 不变 | 通过 |
| 旧规则键入的行 | `test_rows_keyed_under_earlier_rules_are_rekeyed_not_duplicated` | `events_rekeyed=1, events=0, linked=1`，库中 1 个事项 2 源 |

### 2.1 生产路径回放（`replay-events`）

```
python tools/collector_acceptance.py replay-events --workspace ~/.local/state/agents_groups/codex-e2e-20261005-140117-361081759
```

输出 `docs/acceptance/20261005/codex-run_1b8ec1dde3b5/replay-events-with-fixed-code.json`：

| 业务事项 | 来源行 |
| --- | --- |
| `宁德时代\|award\|任丘智弘\|标段二`（40 MWh、4141.622 万元） | energytrend `evt_258e426f7023` primary；北极星 `evt_c0f486a8dc4a` linked（主体全称经别名解析） |
| `任丘智弘\|award\|任丘智弘\|`（2026-09-15、400 MWh） | energytrend `evt_dc3c091f7738` primary；北极星 `evt_d68d828b789a` linked（项目名变体经头部规则） |
| `远景能源\|award\|任丘智弘\|标段一`（360 MWh、19461.6 万元） | energytrend `evt_51cb9de99af0` primary |

账本 `{mic_event_rows: 5, source_rows_recorded: 5, independent_events: 3, new_events: 3, linked_rows: 2, replayed_rows: 0, unresolved_events: 0}`，与复核 §3 期望一致。同一命令对第二次复核工作区 → `4/2/2`，对 v6 → `1/1/0`。

## 3. 遗留限制

1. 头部规则依赖项目名以"项目/电站/基地/园区"结尾且头部在描述词之前；头部里若本身含描述词（如地名"示范区"）会被截短，但两个来源会被同样截短，不产生误分裂；无头部的项目名使中标类事件保持 `unresolved`。
2. 企业别名只覆盖采集目标（profile）。非目标公司（远景能源）的全称/简称差异不会归并——这是复核边界 1 的要求，代价是对手方为主体的事件可能分列，账本会如实显示为独立事项。
3. 本轮无真实采集；`target_aliases` 在真实 MIC 报告中的出现由流水线测试断言，Agent 侧在旧报告（无该字段）下也能用任务短名 + 报告 canonical 归并。
4. 代码仍为 `693b3df` + 未提交 diff；需用户提交并 push 后，Codex 的下一次独立端到端才能对应到可读取的代码版本。

## 4. 产物与变更

```
agents/intelligence_collector_agent/
├── docs/acceptance/collector_acceptance_20261005_round4.md                        本报告
├── docs/acceptance/20261005/codex-run_1b8ec1dde3b5/replay-events-with-fixed-code.json
├── src/agent_trade_intel/event_identity.py      EntityAliases、project_name、头部式 project_key、business_identity
├── src/agent_trade_intel/persistence.py         别名组、business_identity 入 payload、_rekey_business_events
├── tools/collector_acceptance.py                replay-events 子命令
├── tests/fixtures/codex_e2e_run_1b8ec1dde3b5_all_events.json
└── tests/test_business_event_identity.py        +5 测试
tools/market_intelligence_collector/
├── mic/pipeline.py                              报告新增 target_aliases
└── tests/test_browser_pipeline.py               target / target_aliases 断言
docs/COMMANDS.md                                 replay-events 用法
```

产物经凭据模式扫描，无命中。
