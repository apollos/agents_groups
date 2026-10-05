# 情报收集员 Agent × MIC 验收第三轮：Codex 复核 R1–R3 修复（2026-10-05）

> 后续：Codex 对本轮代码的复核（`run_1b8ec1dde3b5`）确认单位与价格比较通过，但事件归并仍漏合并（公司全称/简称、项目名变体）。修复与生产路径回放见 `collector_acceptance_20261005_round4.md`。

依据：`/home/yu/Downloads/codex_e2e_review_693b3df_20261005.md`（Codex 对提交 `693b3df` 的独立复核：工程链路通过；内容及增量去重未通过——R1 事件去重漏合并且会误合并不同项目、R2 功率/能量单位混用、R3 省略另一侧数值的价格比较绕过校验）。本轮在 `693b3df` 之上修复三项，补齐 T1–T5 回归，并按复核 §6 用最终代码跑了真实端到端。前两轮见 `collector_acceptance_20261004.md`、`collector_acceptance_20261004_round2.md`。

## 首屏判定

| 项 | 结论 |
| --- | --- |
| 基线 | `693b3df`（用户提交"端到端测试第二轮"）+ 本轮未提交 diff。未 reset / checkout / stash；Codex 工作区 `codex-e2e-20261005-002420-202417996` 只读；未清库 |
| 判定 | **CURSOR_SELF_TEST_PASS（第三轮）**：复核 §3–§5 三个"通过条件"均在生产校验 / 持久化路径上以真实输入断言通过；批次 v6（最终代码）工程 20/20、10 条正式记录逐项对照原文无单位错误、无缺证据比较 |
| R1 | 业务身份加入**事项范围**（项目键 + 标段）：`business_key = 主体|动作族|项目|标段`；复核的 4 条真实源事件 → **2 个业务事项、各 2 条来源**（§2.1）；甲地/乙地同金额同容量同日期负例 → **2 个事项**；中标类事件缺少项目 → `unresolved`，不凭相同数量合并 |
| R2 | 新增 `mic/quantity_units.py`：按所引正文把功率 / 能量拆成 `power_mw` / `energy_mwh`（各带单位、引文、必要时加法操作数）；`unit="MW/400MWh"` 拆为 `100 MW` + `scope.energy_mwh=400`；fact `unit="万元/MWh"` 拆为 `unit=MWh`（金额已有 `amount_unit=元`）；正文未成对出现的数字 → `unit_unverified`，不进入规范字段；Agent `capacity_mwh()` 只读有单位的字段，裸数字 → 未知 |
| R3 | `_price_comparison` 不再以"句中两个价格数字"为前提：单价 + 排序词（低于 / 一半 / 相比更低 …）→ 从本文档解析唯一另一侧报价并附 `comparison_evidence(resolved_from=elided_reference)` + 口径限制；无法唯一解析 → 比较从句移入 `comparison_review`，保留报价观察；0.518 原值不改 |
| 真实运行 | v5（修复 R1–R3 后）：工程通过，但 BJX 篇因模型把 `"10MW/40MWh"` 当 `metric_value` 字符串输出而整篇 schema 失败 → 补"功率/能量 token 拆分"预修复 → v6（最终代码）：2 Gateway、79 s、energytrend 76 分入库、10 条正式记录（§4）；BJX 本次返回滑动验证页（`anti_bot_page`） |
| 重投 / 下一周期 | v5、v6 均 PASS：复用 run、0 新模型调用；改写摘要 / 换类型 / 换来源 id 的同一事项在下一周期重存 → 0 新事件、全部 linked；原样重放 → 全部 replayed |
| 离线回归 | MIC 528 passed（+10：`test_quantity_units_and_elided_comparison.py`）、Agent 198 passed（`test_business_event_identity.py` 重写为 10 项，含两次复核的真实 fixture） |
| 遗留 | ① 项目键依赖"…项目/电站/基地/园区"命名；无此类词的事项（如"XX 框架协议"）落入 `unresolved`，不合并也不误合并；② 多来源归并在 v5/v6 真实运行中因 BJX 失败未再次触发，T1 以 Codex 真实 4 行离线重放 + 回归证明；③ 冻结提交待用户提交 |

## 1. 逐项修复

### 1.1 R1（P1）事件去重：漏合并与误合并

根因（复核 §3）：`EventSignature` 只有主体 + 动作族 + 数量；项目名"独立"二字差异与 100/400 单位混淆使同一公示分裂；不同项目同数量被合并；回归负例用"金额不同"冒充"项目不同"。

修复（`agent_trade_intel/event_identity.py`）：

- `scope_of(event)`：项目键取自 `entities.object / counterparty_candidate / counterparty / 摘要（去主体与动词前缀）/ 主体`，规则：取到首个"项目|电站|基地|园区"为止 → 去容量 token、括注 → 去通用描述词（新型技术路线 / 独立 / 共享 / 储能 / 试点 / 示范 / 磷酸铁锂 / 钠电池 / 设备采购 / 公示 …）→ 去省级前缀（河北任丘智弘 ≡ 任丘智弘）→ 归一。标段取主体所在从句的"N标段"，否则摘要中唯一标段，否则空。
- 主体若本身是项目名（公示类事件），同样归一为项目键，"河北任丘智弘独立储能试点项目" 与 "河北任丘智弘储能试点项目" 得到同一主体。
- `business_key = 主体|动作族|项目|标段`；`compatible_with`：双方已知数量须一致（0.5%）、已知事件日期一致、发布相差 ≤45 天。**中标类（award）事件没有项目键 → `signature()` 返回 None → `dedup_status=unresolved`，永不合并**；非中标类无范围时沿用数量 / 产品规则。
- 不同项目同数量：项目键不同 → key 不同 → 不比较数量即分开。

T1（真实 4 行，fixture `tests/fixtures/codex_e2e_693b3df_all_events.json`）：`events=2, events_linked=2`，key 为 `宁德时代|award|任丘智弘|标段二` 与 `任丘智弘|award|任丘智弘|`，各 `source_count=2`、`multi_source`，公示日期 2026-09-15 保留，账本 primary 2 + linked 2，两个 `source_link_id` 与 URL 均保留。T2（甲地/乙地示例项目，主体 / 二标段 / 40 MWh / 41,416,220 元 / 2026-09-15 全同）：签名数量相等但 `project` 不同 → `events=2, linked=0`；同项目写成"河北甲地示例独立储能试点项目" → linked。

### 1.2 R2（P1）功率与能量单位

修复（`mic/quantity_units.py`，在 `normalize_bundle_amounts` 之后、严格审查之前运行，严格与非严格模式都生效，只改副本）：

| 输入 | 处理 |
| --- | --- |
| metric `metric_value=100, unit="MW/400MWh"` | `metric_value=100, unit="MW"`，`scope.unit_raw="MW/400MWh"`，`scope.power_mw={100, MW, 引文 "100MW"}`，`scope.energy_mwh={400, MWh, 引文 "400MWh"}` |
| metric `metric_value="10MW/40MWh"`（字符串，v5 BJX 实际输出，原本整篇 schema 失败） | 校验前仅对该 token 形状做拆分：值 10、单位 "MW/40MWh"，再走上一行；其他字符串值仍是 schema 错误 |
| metric `100 MWh` 而正文为 `100MW/400MWh` | `unit→MW`，`scope.unit_review={corrected_from_passage, model_unit: MWh, passage_quote: 100MW}` |
| metric `360 MWh` 而正文 `120MWh+240MWh` | 沿用既有显式加法规则：`energy_mwh.canonical=360, operands=[120,240]` |
| metric 数值正文未出现 | `unit_review=unit_unverified`；严格模式下由既有 gate 隔离 |
| event `capacity=100, volume=400`（无单位） | 正文 100MW / 400MWh 成对出现 → `power_mw=100, capacity_unit=MW`，`energy_mwh=400, volume_unit=MWh`，`energy_evidence` |
| event `capacity=400`（另一副本） | `energy_mwh=400`；两副本能量一致 |
| event / fact `capacity="90MW/360MWh"`（字符串） | 两个量各自核验（含加法），`capacity_unit="mixed"`，原字符串保留 |
| fact `volume=40, unit="万元/MWh"` | `unit="MWh"`、`unit_raw="万元/MWh"`、`energy_mwh=40`；金额路径不变（`amount=41416220, amount_unit=元`） |
| `0.4GWh` | `energy_mwh=400` |

Agent `capacity_mwh()`：优先 `energy_mwh`；否则 `capacity/volume` 需带 `*_unit`（kWh/MWh/GWh 换算）或字符串中唯一能量 token；功率单位、裸数字、多能量 token → None。`numeric_quote` 的单位后缀规则放宽为"斜杠后紧跟数字不算单位延续"，使 `100MW` 在 `100MW/400MWh` 中可被 gate 认可。

### 1.3 R3（P2）省略式价格比较

`statement_review._price_comparison`：有 ≥1 个报价且含排序 / 可比词即为比较。显式两价 → 原逻辑。单价省略另一侧 → 在全文各段寻找同单位的其他报价：恰好 1 个 → 附 `comparison_evidence=[{value, passage_id, resolved_from: elided_reference}]`，并在口径存疑时写 `source_price_basis_status=pending_review / usable_as_price_benchmark=false / price_basis_passages`（与显式版本一致）；0 个或多个 → 比较从句移入 `comparison_review(held_clauses, candidate_prices)`，正式文本保留报价观察 + "与其他报价的比较缺少另一侧的数值依据，待核查。"排序词表扩展：一半 / 更低 / 更高 / 较低 / 较高 / 偏低 / 偏高 / 相比 / 便宜 / 昂贵。

T4：`fact_b24346cb8be2` 原句、"与另一标段相比更低"、"约为钠电标段的一半"三种写法与显式两价版本得到同样的证据与限制字段；只给 p1 时比较被 hold、`unit_price=0.518` 保留；文档中有两个候选（0.95 / 1.035）时 hold 并列出候选；单价无排序词不动。

## 2. 离线回归（T1–T5）

| 命令 | 结果 |
| --- | --- |
| `cd tools/market_intelligence_collector && pytest tests` | 528 passed（第二轮 518） |
| `cd agents/intelligence_collector_agent && pytest tests` | 198 passed（第二轮 194） |

| 组 | 测试 |
| --- | --- |
| T1 | `test_business_event_identity.py::test_t1_second_review_rows_yield_two_matters_with_two_sources_each`（Codex 真实 4 行）+ `test_real_run_yields_three_business_events_from_five_source_rows`（第一轮真实 5 行 → 3 事项） |
| T2 | `test_t2_different_projects_with_identical_quantities_date_and_subject_are_two_events`；`test_rows_without_subject_or_matter_are_unresolved_and_never_merged`（同公司同规模无项目 → 两条 unresolved）；`test_different_lot_project_subject_date_or_news_cycle_are_not_merged`（同项目不同标段 / 不同金额 / 不同容量 / 不同主体 / 不同动作族 / 不同事件日期 / 106 天前 → 9 个独立事件） |
| T3 | `test_t3_energy_is_taken_only_from_unit_bearing_fields`、`test_scope_extraction_handles_variants_and_lot_by_clause`；MIC `test_quantity_units_and_elided_comparison.py`（功率/能量分离、混合单位拆分、单位被正文否定时的修正、显式加法、事件/事实字段、GWh、字符串 token 预修复） |
| T4 | 同文件 `test_t4_*`（3 项） |
| T5 | 既有全部测试不变（F1 营收/竞争推论、报价冲突、69/70/71、64K/截断、时间窗、重投、下一周期、旧库迁移）；两套件全绿 |

### 2.1 Codex 真实运行离线重验

`docs/acceptance/20261005/codex-693b3df-offline-revalidation-with-fixed-code.json`：复核工作区 `run_3549da9f8002` 的两篇原始模型输出（只读 `model_output`）用修复后代码重新校验并经生产 `ResultPersister` 写入临时库——`metric_d40a8a1b598e → 100 MW + energy 400 MWh`，`metric_8faafc286d45 → 10 MW + 40 MWh`，两条公示事件均 `energy_mwh=400`，`fact_b24346cb8be2` 附 `comparison_evidence 1.035@p2` 与 `usable_as_price_benchmark=false`；持久化 `events=2, events_linked=2`。

## 3. 真实端到端（复核 §6）

| 批次 | 代码指纹 | 入库 | Gateway / 耗时 | 工程检查 | 内容 |
| --- | --- | --- | --- | --- | --- |
| v5 `run_923d9ac70417` | tracked `b63e41dd…` / untracked `35d232f0…` | energytrend 72，13 条；BJX 模型输出 `metric_value="10MW/40MWh"` 字符串 → schema 失败（正确拒绝，但整篇损失） | 3 / 101 s | 20/20 | 通过；由此补 token 预修复 |
| v6 `run_3ba4490403ff` | tracked `4af5468a…` / untracked `edadc720…`（最终代码） | energytrend 76，10 条；BJX `anti_bot_page` | 2 / 79 s | 20/20 | 通过（§4） |

两批均经 `prepare → run → redeliver → next-cycle → summary`，30 天窗口、真实运行时间（任务 key 日期 2026-10-05）、70 分、严格审查、64K、既有预算；每批 1 个任务，合计 5 次 Gateway。

## 4. 批次 v6 内容审阅（10 条正式记录 + brief）

原文三句同前（energytrend 转载公示）。

| # | 类型 | 正式文本 / 规范字段 | 引用 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | fact/order | 宁德时代中标二标段钠离子储能系统（10MW/40MWh），4141.622 万元，1.035 元/Wh；`unit=MWh, energy_mwh=40`，`amount 41416220 元` | p2 | 成立 |
| 2 | fact/price | 1.035 元/Wh 明显高于同项目远景能源 0.518 元/Wh；`comparison_evidence 0.518@p1`，`source_price_basis_status=pending_review`，`usable_as_price_benchmark=false` | p2 | 两价均有证据，限制随记录 |
| 3 | fact/order | 远景能源中标一标段（30MW/120MWh+60MW/240MWh），19461.6 万元，0.518 元/Wh；`energy_mwh=360`（加法）；口径 pending | p1 | 成立 |
| 4 | fact/capacity | 项目规模 100MW/400MWh，分两个标段；`unit=MWh, energy_mwh=400` | p0 | 成立 |
| 5 | fact/technology | 磷酸铁锂 + 钠电池新型技术路线独立储能试点，钠电池标段单独采购 | p0 | 成立（原文两标段分列） |
| 6 | metric | 钠离子中标金额 4141.622 万元：原"订单绝对金额较小，约占公司储能业务体量的很小比例"→ `materiality_review`，解释改为来源订单金额观察 | p2 | F1 行为正确 |
| 7 | metric | 钠离子中标单价 1.035：原"明显高于同期磷酸铁锂，反映钠电当前成本/定价仍偏高"→ `economic_interpretation_review` | p2 | 正确 |
| 8 | metric | 二标段容量 40 MWh：`energy_mwh={40, MWh, 引文 40MWh}`；"规模偏小"→ `materiality_review` | p2 | 单位正确、量级判断 hold |
| 9 | metric | 磷酸铁锂单价 0.518：限制说明；`source_price_basis_review` 含 0.5406 条件说明，原值不改 | p1 | 正确 |
| 10 | event/tender | 公示 + 宁德时代中标二标段 10MW/40MWh，4141.622 万元；`capacity=40, capacity_unit=MWh, energy_mwh=40`；positive / revenue, demand | p2 | 主体为目标，事实在 p2，保留 |

brief：`why_it_matters` 为"…可观察其钠电储能产品的投标价格带与商业化落地节奏；订单对公司收入的贡献尚未核实。同项目两个标段报价的供货范围与口径尚未核实，不作为可比基准。"；`uncertainty` 注明媒体转载、缺招标平台原文。

## 5. 遗留限制

1. **项目键的命名依赖**：范围识别依赖"项目 / 电站 / 基地 / 园区"结尾；其他事项类型（框架协议、供货合同）目前落入 `unresolved`，不合并也不误合并。扩展需要新的事项范围词表，属策略变更。
2. **多来源归并的真实触发**：v5（BJX schema 失败）与 v6（BJX 反爬）均只入库一篇，真实运行里本轮未再出现两源归并；T1 以 Codex 真实 4 行经生产路径离线重放证明（§2.1），并有第一轮 5 行 fixture 回归。
3. **`capacity_unit="mixed"`**：字符串 token 作为字段值时原文保留、两个量各自规范化；该标记只表示原字段是混合文本。
4. **冻结提交**：本轮代码为 `693b3df` + 未提交 diff，指纹见 v6 manifest；v6 运行后只新增了文档与产物文件。

## 6. 产物索引

```
agents/intelligence_collector_agent/docs/acceptance/
├── collector_acceptance_20261005_round3.md                  本报告
└── 20261005/
    ├── codex-693b3df-offline-revalidation-with-fixed-code.json   复核原始输出用最终代码重验 + 生产持久化
    ├── batch-v5/{acceptance-manifest,acceptance-summary}.json
    ├── batch-v5/run-1/{started,steps,result,mic-report,content-review,redeliver,next-cycle}.json
    ├── batch-v6/{acceptance-manifest,acceptance-summary}.json
    └── batch-v6/run-1/{started,steps,result,mic-report,content-review,redeliver,next-cycle}.json
```

代码：`tools/market_intelligence_collector/mic/quantity_units.py`（新）、`mic/statement_review.py`、`mic/validate.py`、`mic/evidence_review.py`；`agents/intelligence_collector_agent/src/agent_trade_intel/event_identity.py`；测试 `tests/test_quantity_units_and_elided_comparison.py`（新）、`tests/test_business_event_identity.py` + `tests/fixtures/codex_e2e_693b3df_all_events.json`。

工作区（含数据库，不入库）：`~/.local/state/agents_groups/collector-acceptance-20261005-v5`、`-v6`。产物经凭据模式扫描，无命中。
