# ChangeLog

本文件记录情报收集员 Agent 的版本变更。日期为变更落地日期。

---

## V0.8.1 — 2026-07-05：研究闭环补全（第五轮外部审阅甄别采纳）

第五轮审阅（《Agent Trade Intel V0.8.1 Reviewer 建议与代码修改方案》）列出 5 个剩余缺口并附代码包。逐条核对后：**第 1 条（full YAML 与 ChangeLog 不一致）为误判**，其余 4 条全部采纳落地；代码包中的新文件按本仓库约定改写后合入（含一处 export bug 修正），overlay YAML 未直接采用，理由见文末。

### 驳回：§1 "research_pool_full.yaml 缺少 V0.8 配置块"（不成立）

审阅声称仓库中的 `examples/research_pool_full.yaml` 检索不到 `tracking_variables_by_industry`、`defaults.hk_company`、`derived_from_demands`、`demand_company_monthly_review`、`theme_ids`。在本地仓库对该文件逐一执行审阅给出的 5 条 grep 验收命令，**全部命中**（分别位于第 47/33/107 等行起的对应段落），V0.8 的验收测试 `test_research_pool_full_yaml_v08_sections` 也持续在守护这些键。审阅自述"GitHub raw 预览把部分源码压缩成少数长行"，判断是基于被截断/渲染异常的网页视图得出的结论，而非本地文件。文件本身无需修复；请 Reviewer 以仓库 checkout 为准复核。

### §2 MIC cache/reuse 保留 tracking_variables（采纳，P0）

审阅指出的问题成立：`EventCardRow` 没有 `tracking_variables` 列，cache/reuse 命中时 `clone_latest_analysis` 克隆出的事件不带变量证据，Agent 侧只能退化为 keyword candidate，`eval coverage`（默认只算 accepted）会低估已确认覆盖。修复：

- `mic/store/models.py`：`EventCardRow` 新增 `tracking_variables`（JSON）列；`mic/store/database.py` 的 `create_all` 增加幂等 `ALTER TABLE` 迁移，老库自动补列。
- `mic/store/repository.py`：`save_merged_analysis` 落库模型归因的变量证据；`clone_latest_analysis` 克隆行保留该列，且 `cloned_events` 明细带出 `tracking_variables`，pipeline 的 `_tally_cloned` 路径由此把确认覆盖带回 `all_events`；`_event_to_dict`（analyst 查询）同步输出。

### §3 theme_ids 进 MIC prompt（采纳，P0）

V0.8 把 `theme_ids` 存进了 profile 但没有传给模型。现 `_profile_block` 输出 `theme_ids`，SYSTEM_PROMPT 新增第 8 条：判断事件相关性与变量归因时参考跨主题归因，但"不能因为主题存在而编造证据"（与变量清单的防幻觉约束同款）。

### §4 港股通快照 completeness（采纳，P1）

"有字段 ≠ 有数据"的批评成立。落地为三层：

- **adapter**：`HK_REQUIRED_FIELDS`（价格/成交额/南向持股 6 字段，共 8 个可达字段）与 `HK_UNSOURCED_FIELDS`（buyback/AH溢价/流动性——schema 有列但尚无数据源，单独上报为 unsourced 而非采集失败）；`quality` 输出 `missing_fields` / `unsourced_fields` / `field_completeness{required_count, filled_count, ratio}`。
- **表**（schema v6，老库幂等加列）：`hk_connect_snapshots` 新增 `field_completeness_json` / `missing_fields_json` / `provider_status_json`（含 provider、unsourced_fields、errors）。
- **eval**：`eval hk-connect` 新增 `avg_field_completeness` 与 `low_completeness`（ratio < 1.0 的标的及缺失字段清单），把"有快照"和"快照质量高"区分开。

### §5 market_context_collector（采纳，P1；V0.8 暂缓项兑现）

V0.8 暂缓时承诺"adapter 模式建立后作为下一版增量成本很低"，本轮兑现。md §5.4/§7.1 要求的指数/汇率/商品/利率背景变量靠文本检索答不稳，全链路落地：

- 新增 `adapters/market_context_adapter.py`：采纳审阅"函数名可配置"的设计——每个 context 在 YAML 里声明 `akshare_func` + `akshare_args` + 日期/取值列名，AKShare 接口漂移时改配置即可，不改代码。时序接口输出 1/5/20 期涨跌幅；单行实时接口 change 置空。akshare 惰性导入，未安装仅此链路失败（不可重试的 `AKSHARE_NOT_INSTALLED`）。
- 新增 `market_context_snapshots` 表（schema v6，`(context_id, as_of)` 幂等）、`market_context_daily` demand type、`market_context_snapshot` task type；planner/agent 新增对应分支（熔断、质量工单、重试语义与 hk_connect 一致）。
- `request batch` 支持 `market_contexts:` 段（专属 `demand_market_context_daily`，不写 MIC 档案、不进股票池）；`eval market-context --date` 输出覆盖与缺失清单。
- `research_pool_full.yaml` 新增 5 个 context：沪深300、恒生科技、CNY/HKD、铜、碳酸锂。

### §6 research_cards 实体化（采纳，P1；修正一处 bug）

按审阅方案落地确定性研究卡（不引入 LLM 调用）：新增 `research_cards` 表（schema v6）与 `research_cards.py`（`ResearchCardBuilder`），聚合近 30 天结构化事件、accepted 变量链接（覆盖率/缺失变量）、正负面证据、open coverage gaps、最新港股通快照（含 completeness），并给出启发式 `pool_layer_suggestion`（HK 无资格→核实、覆盖率过低→观察、负面≥3→复核降级、高优先缺口≥3→跟进；仅提示，升降级由人决定）。CLI 新增 `research-card refresh --target-id [--date --lookback-days]` 与 `research-card export [--target-id --demand-id]`。

**代码包 bug 修正**：审阅版 `export --demand-id` 直接对 `research_cards.demand_id` 列过滤，但其自己的建表语句没有这个列，执行必报 SQL 错误。本实现不加冗余列（一个 target 可属多个 demand），而是先解析该 demand 当前的 target 清单再按 `target_id` 过滤。另外代码包测试里手工 `CREATE TABLE research_cards` 的步骤不再需要——表已并入 `SCHEMA_SQL` + v6 迁移。

### 未采用：overlay YAML 与 P2 项

- **`research_pool_full_v081_overlay.yaml` 未直接合入**：其价值部分（`market_contexts` 段、`demand_market_context_daily`）已改写进主 YAML；其余内容基于"主 YAML 缺 V0.8 配置"的误判（见驳回条），且其中变量重命名（如 `ai_server_orders` → 新名）会切断已落库 `event_variable_links` 的连续性，`company_theme_overrides` 段与主 YAML 已有的 `theme_ids` 直写方式重复。
- **P2（dashboard 接入 market context / research card 摘要、golden set 变量级 precision）**：同意方向，待结构化快照与研究卡积累真实数据后排期。

### 验证

- Agent 测试 114 个全部通过（新增 14 个 `tests/test_v081_reviewer_closure.py`：v6 新表/老库补列迁移、HK completeness 三层（adapter quality / persistence / eval low_completeness）、market context adapter 时序与单行实时两种形态、缺 akshare 报错、planner market_context 任务与禁用开关、快照幂等、market-context 覆盖评估、batch 注册 market_contexts、full YAML 段校验、研究卡聚合/幂等 upsert/export 三种过滤）。
- MIC 测试 79 个全部通过（新增 2 个：prompt 含 theme_ids 与防编造指令、clone 保留 tracking_variables 且 analyst 查询可见）。
- 审阅 §7 验收清单全量跑通：3 条 grep 全部命中（外加 `market_contexts:`）；空库上 `eval coverage`（931 期望格）/`eval hk-connect`（40 港股目标）/`eval market-context`（5 context）正确输出 0 覆盖而非报错；`research-card refresh/export`（含 `--demand-id` 过滤）在真实 full YAML 注册的库上正常产出。

### 升级说明

- 老库自动迁移（v6：`hk_connect_snapshots` 补 3 个 completeness 列，新增 `market_context_snapshots` 与 `research_cards` 表；MIC 库自动补 `event_card.tracking_variables` 列），无需手工操作。
- 重跑 `request batch --file examples/research_pool_full.yaml` 即可注册 5 个市场背景 context（幂等）；market_context_collector 与 hk_connect_collector 共用可选依赖 `akshare`。

---

## V0.8 — 2026-07-05：A股 + 港股通可迭代研究池闭环版（第四轮外部审阅采纳）

第四轮审阅（《A股 + 港股通可迭代股票研究池 V0.8 完整方案》）提出"不考虑工程排期，只要能做就做"。本轮采纳其中绝大多数建议——事件→变量归因、港股通结构化采集、多主题归因、运行时目标引用、评估 CLI 全部落地；仅 market_context_collector（宏观/指数/汇率/商品）与 `RunStats.top_events` 内部改名两项暂缓，理由见文末。

### 1. 事件 → tracking_variable 双层标签（审阅 §2-§5，采纳）

前三轮暂缓的核心项本轮完整落地，按审阅的"双层标签"方案：模型归因 + 关键词候选，`mapping_method` 与 `review_status` 全程留痕，互不污染。

- **MIC 侧**（跨包改动）：`EventCard` 新增 `tracking_variables: list[TrackingVariableEvidence]`（variable/direction/strength/reasoning/confidence）；`TargetProfile` 新增 `tracking_variables` 与 `theme_ids` 并透传给模型 prompt（SYSTEM_PROMPT 明确"只能从给定清单选择，无明确证据输出空列表"）；`_tally` 把变量证据带进 `all_events`。
- **Agent 侧（schema v5，老库自动迁移）**：新增 `event_variable_links` 关联表（一事件可覆盖多变量），主键 `(event_id, tracking_variable, mapping_method)`。模型归因写 `mic_model`，confidence ≥ 0.65 记 `accepted`、否则 `pending`；重复事件（幂等命中）也会补写变量链接，不会因事件已存在而丢归因。
- **关键词候选**（新模块 `variable_mapper.py`）：15 组中文关键词规则（订单/毛利率/南向持股/出口管制/CDE受理…），只对 target 声明过的变量生效，产出一律 `keyword_candidate` + `pending`、confidence 上限 0.6，**永不进入 confirmed coverage**。

### 2. 港股通结构化采集 hk_connect_collector（审阅 §6-§7，采纳）

前三轮以"需要新外部数据源"暂缓，本轮按审阅建议用 AKShare（东方财富数据）落地：

- 新增 `adapters/hk_connect_adapter.py`：港股通成分（资格判定）+ 南向持股统计（持股量/市值/占比/1/5/10日变化），akshare **惰性导入**——未安装只影响 HK 快照任务（返回不可重试的 `AKSHARE_NOT_INSTALLED`），其余功能不受影响。附 `calc_ah_premium_pct` 换算函数（A股人民币价折港币相对 H 股溢价）。
- 新增 `hk_connect_snapshots` 表（schema v5）：资格/价格/成交额/南向持股/回购/AH溢价/流动性字段先建全，数据源能取哪个填哪个；`(ticker, as_of)` 幂等键保证每标的每日至多一条。
- planner 新增 `_hk_connect_tasks`：daily_collection 盘后为 `.HK` 且未显式关闭 `collect_hk_connect` 的目标追加 `hk_connect_daily_snapshot` 任务；A股/periodic_review 不生成。配置 `tools.hk_connect_collector.enabled`（默认开）。
- `agent.py` 新增任务分发与执行：成功落快照表；akshare 缺失按不可重试处理，网络类失败走常规重试。
- YAML `defaults.hk_company`：港股公司默认 `collect_hk_connect: true` 并追加港股通变量集（southbound_holding/hk_connect_eligible/hk_liquidity/buyback/ah_premium/dividend_yield）；公司条目支持 `ah_pair_a_ticker` 记录 AH 对。

### 3. theme_ids 多主题归因（审阅 §8，采纳）

上一轮"出海制造不改 primary industry"的处理被审阅指出模型限制不应保留——本轮采纳 `industry_id`（主线）+ `theme_ids`（跨主题）双层归因：batch 条目、`request company`、MIC profile、Demand target 全链路支持；`research_pool_full.yaml` 已给三一重工/中联重科/中国中车/潍柴动力/美的集团/海尔智家打上 `industry_export_manufacturing` 主题，主线归属不变。

### 4. derived_from_demands 运行时引用（审阅 §9，采纳）

`copy_targets_from` 是注册时一次性拷贝，daily 名单增删后复盘 Demand 会漂移。本轮新增 `derived_from_demands`：复盘 Demand 不再存目标快照，`resolve_demand_targets` 在**每次规划时**重新读取来源 Demand 的当前目标并去重合并。`research_pool_full.yaml` 三个复盘 Demand 已全部切换（周报/月报/季报注册时 target_count=0，运行时解析出 108 个）。`copy_targets_from` 保留兼容。审阅 YAML 中的 `derived_sync_policy: runtime_reference` 键未实现——运行时引用是 `derived_from_demands` 的唯一语义，无需二级开关。

### 5. 评估 CLI：coverage / hk-connect / golden（审阅 §12-§13，采纳）

上一轮"等真实数据积累"的理由被审阅驳回（评估框架可以先建，没数据输出 0）——本轮采纳：

- 新增 `evaluation.py`（`CoverageEvaluator`）：`eval coverage --date --demand-id [--include-candidates]` 输出 target × tracking_variable 覆盖矩阵（expected/covered/coverage_ratio + 每格 event_count/max_confidence/权威源标记；默认只算 accepted 链接，`--include-candidates` 纳入 pending 候选）；`eval hk-connect --date` 输出港股通快照覆盖率与缺失清单（支持 `derived_from_demands` 的目标解析，按本地交易日过滤）。
- 新增 `golden_eval.py`（`GoldenSetEvaluator`）+ `examples/golden_events.yaml`：`eval golden --file` 按 target/日期窗/关键词/期望变量/权威源类型匹配已落库事件，输出 expected_count/matched_count/recall 与逐条命中明细。
- dashboard 产出面板新增今日变量映射（按 review_status 分组）与港股通快照数 chips。

### 6. planner 无歧义重构（审阅 §1，采纳）

上一轮已证明无重复规划 bug，但审阅指出 `elif/else` 结构容易被误读——采纳重构为显式 `if/return` 风格：`TEXT_RESEARCH_DEMAND_TYPES` 常量 + 每种 demand_type 一个 return 分支，daily 盘后在 MIC 任务上追加 stock + hk_connect 任务后统一返回。行为不变（V0.7.1/V0.7.3 全部规划测试原样通过）。

### 7. all_events 缓存复用路径补全（审阅 §10，采纳）

审阅正确指出：V0.7.3 的 `all_events` 只覆盖 fresh analysis 路径，cache/reuse 命中时 `clone_latest_analysis` 只回传 counts，克隆事件不进 `all_events`，"全量事件落库"契约在复用场景下不成立。修复：`clone_latest_analysis` 现在返回 `cloned_events` 明细（含 event_type/event_date/impact channels/confidence），pipeline 新增 `_tally_cloned` 把克隆事件连同来源元数据并入事件列表，两处复用调用点（同 target 复用 / 跨 target 复用）都已接入。审阅附带的"`RunStats.top_events` 改名 `all_events`"属内部字段改名，无行为差异，不改（summary 已同时输出 `top_events` 前5 与 `all_events` 全量）。

### 8. 质量闸门变量覆盖规则（审阅 §15，采纳）

`quality.mic` 新增 `flag_tracking_variable_coverage`（默认开）与 `low_variable_coverage_ratio`（默认 0.7）：目标声明了 tracking_variables 但本次 MIC 零覆盖 → medium issue，P2 accept_degraded 留痕；覆盖但缺失比例 ≥ 阈值 → low issue，**不降级**（P3 accept，只记录）。低严重度问题不再触发降级路径，避免变量覆盖提示淹没真正的质量问题。

### 暂缓（2 项，附理由）

- **market_context_collector 宏观/指数/汇率/商品辅助目标（审阅 §11、§14.4）**：方向认可但本轮不做。这是与 hk_connect_collector 同量级的新 demand kind + adapter + 表 + 调度分支；本轮已引入两张新表和一个新采集链路，宏观背景当前可用 industry 型 MIC 档案近似（文本层面）。AKShare 同样覆盖指数/汇率/商品，adapter 模式本轮已建立，V0.8 数据链路跑通后作为下一版增量成本很低。
- **`RunStats.top_events` 改名**：见第 7 条——纯内部改名，无功能收益，不做。

### 验证

- Agent 测试 100 个全部通过（新增 20 个 `tests/test_v08_research_loop.py`：planner HK 任务与 opt-out、v5 迁移、模型/关键词变量链接落库、重复事件补链接、HK adapter 行映射与缺 akshare 报错、快照幂等、AH 溢价计算、derived_from_demands 运行时跟随、theme_ids 与 HK 默认、质量闸门零/低覆盖、coverage/hk-connect/golden 三个评估器）。
- MIC 测试 77 个全部通过（新增 4 个 `tests/test_event_tracking_variables.py`：EventCard 变量证据 schema、profile 透传、prompt 包含变量清单、clone 返回事件明细）。
- 端到端冒烟（临时库 + full YAML）：一次 batch 注册 8 行业 / 175 公司，33 个港股目标全部 `collect_hk_connect=true` 且继承行业+港股通变量集；月度复盘 `derived_from_demands` 注册时 0 目标、运行时解析 108 个；daily 盘后规划 108 MIC + 75 stock + 33 hk_connect 任务；`eval coverage`（931 个期望格）/ `eval hk-connect`（40 个港股目标）/ `eval golden` 三个命令在空库上正确输出 0 覆盖而非报错。

### 升级说明

- 老库自动迁移（v5：新增 `event_variable_links` 与 `hk_connect_snapshots` 两张表），无需手工操作。
- 港股通结构化采集需要 `pip install akshare`（可选依赖）；未安装时仅 HK 快照任务失败留痕，MIC/A股链路不受影响。
- 重跑 `request batch --file examples/research_pool_full.yaml` 即可获得 theme_ids、港股通默认变量与 derived_from_demands 复盘 Demand（幂等）。

---

## V0.7.3 — 2026-07-05：研究池维护闭环（第三轮外部审阅甄别采纳）

第三轮审阅共 11 项意见。经逐条核对代码，**P0-1（planner 重复规划 MIC）为误判**——审阅描述的"先 `_mic_tasks()` 再在 else 分支重复执行"结构与实际代码不符，实际代码自 V0.7 起就是单次规划 + 盘后条件追加 stock 任务，且有 V0.7.1 的 3 个验收测试在守护。本轮采纳其中确实成立的 4 项，其余继续暂缓。

### 采纳并落地

1. **planner 显式支持 `periodic_review`（审阅 P0-2，部分成立）**：此前 periodic_review 落在 planner 的 else 兜底分支，行为恰好正确（每目标一个 MIC 任务）但属于隐式依赖。现将其显式列入 `daily_collection / on_demand_research / coverage_gap_followup / periodic_review` 分支并加注释；新增验收测试（每 collect_mic 目标 1 个 MIC 任务、collect_mic=false 跳过、review 即使盘后也不规划 stock 任务）。
2. **每条主线默认跟踪变量（审阅 P1-1）**：batch spec 新增 `tracking_variables_by_industry` 段——公司条目缺省时按其 `industry_id` 继承该主线变量集，行业条目按 `target_id` 继承，单条目可显式覆盖。`research_pool_full.yaml` 已填入 8 条主线的定制变量集（AI算力：订单/光模块出货/合同负债/出口管制…；创新药：CDE受理/NMPA批准/license-out…；高股息：分红政策/派息率/FCF…等），175 家公司全部获得变量。
3. **周/月/季复盘并入 full YAML + `copy_targets_from`（审阅 P1-4）**：batch `demands:` 段支持 `copy_targets_from: [源demand]`，复盘 Demand 直接复用 daily Demand 的目标清单而不必重复百余条名单。`research_pool_full.yaml` 内置 `demand_industry_weekly_review`（周五）/ `demand_company_monthly_review`（每月1日）/ `demand_earnings_season_review`（1/4/7/10月15日）三个 periodic_review。全链路冒烟验证：一次 batch 注册 6 个 Demand（复盘各 108/108/8 目标），runtime tick 周四不编译周报、周五编译，非 1 日不编译月报。
4. **全量事件落库（审阅 P2-1）**：MIC summary 新增 `all_events`（完整事件列表），`top_events` 仍为展示用 Top5；Agent persister 与质量闸门优先读 `all_events`（老版本 MIC 输出自动回退 `top_events`）。小额回购、库存边际变化、中标候选人公示等未进 Top5 的事件不再丢失，变量覆盖统计有了完整底数。

### 驳回 / 暂缓（附理由）

- **P0-1 planner 重复规划 MIC**：不成立。审阅引用的代码结构（`tasks.extend(_mic_tasks(...))` 后 else 分支再次 `_mic_tasks`）在当前代码中不存在；`plan()` 对上述 demand_type 只调用一次 `_mic_tasks`，V0.7.1 的验收测试（盘前/盘后/on_demand/coverage_gap 各 1 MIC/目标）持续通过。审阅要求的第 5 条验收（collect_mic=false 不生成 MIC）本轮随 periodic_review 测试一并补上。
- **P1-2 事件→tracking_variable 归属**：暂缓。需要 MIC 模型输出合同增加变量分类（或引入关键词映射，误标率高会污染覆盖矩阵）。变量清单本轮已入库（target 级），待 MIC 侧支持逐事件归属后再建关联表（审阅方案 B）。
- **P1-3 港股通结构化数据**：暂缓（连续第三轮说明）。需要新的外部行情/资金数据源适配器，当前 stock_data_collector 仅覆盖 A 股；文本层面 hk_connect 查询族已在跟踪。
- **P1-5 出海制造独立公司层**：不改。当前模型一个公司归属一条主线（industry_id 单值）；美的/海尔/三一/中车按跟踪建议 md 原文归属 3.2/3.6 主线。把它们改挂出海制造线是研究口径决策，改 YAML 的 `industry_id` 重跑 batch 即可生效，留给使用者定夺。
- **P2-2/P2-3 效果面板与 golden set**、**P2-4 宏观/指数辅助目标**：继续暂缓，结论同前两轮（先积累真实数据；宏观主题可用 industry 型 MIC 档案近似注册，结构化指数/汇率数据需新适配器）。

### 验证

- Agent 测试 80 个全部通过（新增 4 个：periodic_review 规划、tracking_variables_by_industry 继承与显式覆盖、copy_targets_from 复制与缺源告警、all_events 优先落库）。
- MIC 测试 73 个全部通过（`all_events` 为新增字段，`top_events` 不变）。
- 端到端冒烟（临时库 + 真实 MIC 档案）：full YAML 一次注册 6 Demand / 183 MIC 档案 / 175 公司全带变量；周报仅周五编译、月报非 1 日不编译。

### 升级说明

- 已跑过旧版 batch 的库直接重跑 `request batch --file examples/research_pool_full.yaml` 即可补上变量与复盘 Demand（幂等；若需更新已有 Demand 的预算/优先级记得加 `--update-demand-config`）。

---

## V0.7.2 — 2026-07-05：研究效果结构化（第二轮外部审阅采纳）

第二轮审阅（《修改建议与验证方案》）确认 P0 三项（planner 去重、MIC keepalive/超时、dashboard 本地交易日）已在 V0.7.1 落地，本次采纳其余仍有缺口的建议。总体判断：审阅 P0 中唯一未完成的是**交易日历为空**；P1 采纳研究元数据与证据字段的低成本部分；P2（完整效果面板、golden set）继续暂缓。

### P0：2026 年 A 股交易日历落地（审阅 2.4）

1. `config/intelligence_collector.yaml` 的 `market_calendar.holidays` 按上交所/深交所《关于2026年部分节假日休市安排的通知》（上证公告〔2025〕45号）填入 2026 全年 19 个工作日休市日（元旦/春节/清明/劳动节/端午/中秋/国庆）。A 股周末一律休市（含调休上班日 5/9、9/20、10/10），`extra_trading_days` 留空。`calendar validate --year 2026` 现在返回 ok。
   - 港股假期与 A 股不同，但 stock_data_collector 仅覆盖 A 股、MIC 采集不依赖交易日，单一日历暂够用；分市场日历继续暂缓。

### P1：研究池目标元数据（审阅 3.1 / 4.5）

2. **target 研究元数据**：`request industry|company` 与 batch 条目支持 `industry_id`（公司归属的研究主线）与 `tracking_variables`（跟踪变量清单），原样存入 Demand target，`request status` 一并展示，供日报/分析员 Agent 按主线与变量聚合。CLI 对应新增 `--industry-id` / `--tracking-variables`。
3. **`examples/research_pool_full.yaml` 全量打标**：175 家公司全部补上 `industry_id`（按 §3/§4 小节归属 8 条主线）；40 个港股代码统一补零为 5 位（`00700.HK`）。`request_center` 新增 `normalize_ticker`，入库 ticker 也统一为 5 位（此前只有 target_id 归一）。
   - 出海制造主线（`industry_export_manufacturing`）目前只有行业档案、无独立公司层；美的/海尔/三一/中车等按 md 原文归在 3.2/3.6 主线下。工具已支持重新打标（改 YAML 的 `industry_id` 重跑 batch 即可），名单取舍留给研究决策。

### P1：周/月/季复盘 Demand（审阅 4.3）

4. **cadence 编译门槛**（`demand.py` 新增 `cadence_due`）：Demand 支持 `cadence: weekly|monthly|quarterly` + `cadence_anchor`（weekly 默认周五，可写 mon..sun；monthly/quarterly 默认 1 号，可写几号；quarterly 限 1/4/7/10 月）。Runtime tick 编译时未到期直接跳过；到期日内任务级幂等键仍按天去重，重复 tick 安全。
5. **示例 `examples/periodic_reviews.yaml`**：演示用 `request batch` 注册周度行业景气复盘、月度公司研究卡更新、季度财报季复盘三类 `periodic_review` Demand（走 MIC 深采，7d/30d/90d 回看窗口）。

### P1：事件效果字段与质量规则（审阅 3.2 / 3.3）

6. **query_family 全链路**（schema v4 迁移，老库自动加列）：MIC 搜索命中本就携带 `query_family`，现在透传进 `top_events[].source.query_family`（`mic/pipeline.py`），Agent 落库到 `structured_events.query_family`。可回答"哪些查询族真正产出事件"。
7. **质量规则**（`quality.mic.require_published_at_for_high_confidence`，默认开，阈值 `high_confidence_threshold: 0.75`）：高置信度事件缺 `published_at`（新鲜度不可判）→ P2 accept_degraded。
8. **dashboard 来源/查询族聚合**：产出面板新增"今日事件按 source_type / query_family"统计 chips（审阅 Panel 3 / Panel 4 的轻量版）。

### 暂缓项（继续记录在案）

- `tracking_variable` 逐事件归属、`source_pack` 逐事件回填、事件级去重键（canonical_event_key）：需要 MIC 输出合同更大改造或模型分类，待字段积累真实数据后评估。
- 港股通结构化跟踪层（南向持股/AH 溢价/回购金额）：需新外部数据源，同 V0.7.1 结论。
- 公司研究卡 scaffold、market_context/macro 目标：属分析员 Agent 输入结构，宏观目标当前可用 industry 型 MIC 档案近似注册。
- dashboard 完整效果面板（Target Coverage Matrix 等 7 面板）与 golden set 评估：按审阅建议在小样本真实运行积累数据后排期。

### 验证

- Agent 测试 76 个全部通过（新增 10 个 `tests/test_reviewer_round2.py`：2026 日历、HK 补零、target 元数据、cadence 到期判定与编译跳过、batch 注册 periodic_review、query_family 落库、v4 迁移、published_at 质量规则）。
- MIC 测试 73 个全部通过（source.query_family 为新增可选字段）。
- CLI 冒烟：`calendar validate --year 2026` 返回 ok（19 个休市日）。

### 升级说明

- 老库自动迁移（v4：structured_events 加 query_family 列），无需手工操作。
- 已注册的 4 位 HK ticker Demand target 会在下次 `request batch` 重跑时按 target_id 幂等更新为 5 位 ticker。
- 每年 12 月交易所公布次年休市安排后，需向 `market_calendar.holidays` 追加次年条目（`calendar validate` 会在次年 1 月起告警）。

---

## V0.7.1 — 2026-07-04：真实运行前加固（外部代码审阅采纳）

本次改动来自一份针对 full pool 真实启动风险的外部代码审阅。审阅共列 11 项建议，采纳情况：P0 两项全部落地（其一在审阅前已修复，本次补齐验收测试）；P1 六项落地（其中两项按现状裁剪范围）；P2 两项（港股结构化数据源、golden set 评估）与 dashboard 效果面板全量版**暂缓**，待第一轮真实小样本跑通后再排期。

### P0：MIC 长任务防重复执行

1. **lease 心跳**（`agent.py` 新增 `lease_heartbeat` 上下文管理器）：MIC 深采是单次长阻塞调用，此前执行期间不续租，超过 `queue.lease_seconds`（默认 300s）消息会被重新投递、由第二个 worker 重复采集。现在采集期间后台线程按 `lease_seconds/3` 间隔自动续租 + 心跳。
2. **MIC 硬超时**（`adapters/mic_adapter.py`）：新增配置 `tools.market_intelligence_collector.timeout_seconds`（默认 900s）。超时返回 retryable 的 `MIC_TIMEOUT` 失败结果，worker 不再被无限期挂住。
3. **执行前成功去重**：同一任务幂等键已有 success run 时（消息重投/重复投递场景），直接复用 run_id 关单，不再花第二次 MIC 预算。
4. **Planner 重复规划 MIC**：审阅指出的 `daily_collection` 双重规划分支在 V0.7 中已修复；本次按审阅要求补齐 3 个验收单测（盘前 1 MIC/目标、盘后 1 MIC + 1 stock 仅限 A 股、on_demand/coverage_gap 不重复）。

### P1：统计口径、并发安全与运营可见性

5. **本地交易日统计**（`dashboard.py` / `reports.py` / `time_utils.local_day_utc_range`）：SQLite `created_at` 存 UTC，此前按 `date(created_at)` 过滤，Asia/Shanghai 凌晨 00:00–08:00 的数据会被算到前一天。现在 dashboard 的"今日"与日报的 `trade_date` 统一换算为本地日的 UTC 起止区间查询；dashboard 同时显示 `today_local` 与 `today_utc`。
6. **MIC 档案写入原子化 + 文件锁**（`request_center.py`）：`target_profiles.yaml` 改为临时文件 + fsync + `os.replace` 原子替换，写入中断不再留半截 YAML；`load -> merge -> save` 全程持有 `flock` 文件锁，并发 batch 不会互相覆盖目标。
7. **batch 重跑 demand 配置显式化**：`request batch` 新增 `--update-demand-config`。不加该参数重跑时，`demands:` 覆盖（预算/优先级/task_profile）对已存在的 Demand 跳过并输出 warning（结果含 `demand_config_updated` 字段），消除"改了 YAML 但没生效"的误判；加参数则深合并生效并升 Demand 版本。
8. **交易日历年度校验**：新增 `intel-agent calendar validate [--year]`；`config validate` 输出 `market_calendar` 段。日历当年无节假日条目时给出明确 warning（否则系统按"工作日=交易日"运行，春节/国庆/调休全错）。审阅建议的独立 calendar_provider 模块与 A股/港股分市场日历暂缓，当前配置结构（holidays + extra_trading_days）先满足单市场需要。
9. **事件证据字段**（schema v3 迁移，老库自动加列）：`structured_events` 新增 `source_url / source_domain / source_type / published_at / retrieved_at`；MIC `top_events` 输出补充 `event_type / event_date / source{url,domain,source_type,published_at}`（`mic/pipeline.py`）；日报 Top 事件与 dashboard 最新事件带来源类型（dashboard 可点击源链接）。审阅建议的 `tracking_variable` / `query_family` / `relevance_score` 等字段依赖 MIC 输出合同更大改造，暂缓。
10. **MIC 质量闸门强化**（`quality.py`，配置 `quality.mic.*` 可开关）：高优先级目标零事件 → P2 降级；全部事件无 source URL（证据不可核验）→ P2；全部事件仅 media/social/unknown 来源、无官方交叉印证 → P2。均为 accept_degraded（数据仍落库），在 run 质量与数据质量记录中可见。

### 暂缓项（记录在案）

- P2 港股通结构化数据适配器（南向持股/回购/AH 溢价）：需要新外部数据源，待研究池文本采集稳定后排期。
- P2 golden set 评估命令与 dashboard 效果面板（变量覆盖/来源覆盖/预算效率矩阵）：依赖第 9 项证据字段先积累真实数据。
- MIC 手工/生成配置分离（manual/generated 双文件）：当前合并式 upsert 已保护手工字段，加文件锁后风险可控。

### 验证

- Agent 测试 66 个全部通过（新增 18 个加固测试 `tests/test_review_hardening.py`）。
- MIC 测试 73 个全部通过（`top_events` 证据字段为新增可选字段，旧消费方不受影响）。
- CLI 冒烟：`calendar validate` 空日历正确告警；`request batch` 重跑无 flag 输出 skip warning、加 `--update-demand-config` 后预算生效且版本 +1。

### 升级说明

- 老库自动迁移（v3：structured_events 加 5 个证据列），无需手工操作。
- 建议在真实启动前：① 向 `market_calendar.holidays` 填入 2026 年 A 股节假日并跑 `calendar validate --year 2026`；② 确认 `tools.market_intelligence_collector.timeout_seconds` 与 `queue.lease_seconds` 符合预期（超时应显著大于单次深采常规耗时）。

---

## V0.7 — 2026-07-04：研究池启动 + 一键/批量申请采集

本次为一次较大改动，目标是按《A股_港股通_可迭代股票研究池_跟踪建议.md》启动"行业信息 + 目标公司信息"的日常采集，为后续分析员 Agent 准备输入数据。

### 新增功能

1. **一键申请采集工具 `intel-agent request`**（新模块 `src/agent_trade_intel/request_center.py`）
   - `request industry`：写入/合并 MIC `target_profiles.yaml` 行业档案，并把目标加入托管 Demand `demand_industry_research_daily`（不存在则自动创建为 active 的 daily_collection），发布 `demand.registered` 供 runtime tick 自动拾取。
   - `request company`：写公司档案 + 加入 `demand_company_research_daily`；A 股代码同时进 `pool_members`（默认 watchlist 层）；港股代码自动 `collect_stock=false`（stock_data_collector 仅覆盖 A 股）。
   - `request stock`：仅行情数据（`collect_mic=false`），加入 `demand_stock_eod_daily`（盘后日线刷新）+ 股票池。
   - `request batch --file <yaml/json>`：一个配置文件一次性注册整个研究池（行业 + 公司 + 股票 + 每个 Demand 的预算/优先级覆盖）。MIC 档案单次写盘；每个托管 Demand 无论增加多少目标只升一个版本；重复执行幂等（0 新增、全量更新）。支持 `--test-mode` 强制小预算试跑。
   - `request remove / status`：从托管 Demand 移除目标；查看 MIC 已注册目标、托管 Demand 目标清单与股票池全景。
   - 港股代码统一补零归一（`0700.HK` 与 `00700.HK` 均映射 `company_hk_00700`），避免同一公司产生重复档案。

2. **研究池完整注册配置 `examples/research_pool_full.yaml`**
   - 覆盖跟踪建议 md 的完整清单：8 条行业主线 + §3 核心研究名单约 108 家（A股 + 港股通候选，含代码）+ §4 上下游扩展观察名单约 67 家。
   - 观察名单走独立低预算 Demand `demand_company_watch_daily`（priority: low，每目标每日 4 次查询/2 次模型调用），不与核心层抢预算，可整体 suspend/resume。

3. **示例 Demand**（`examples/demands/`）
   - `industry_research_daily.json`：8 条行业主线每日情报采集。
   - `company_research_daily.json`：首批 18 家核心公司每日深采 + A 股盘后行情刷新。
   - `market_black_swan.json`：行业级黑天鹅扫描（无需股票代码）。

4. **MIC 注册一致性校验**
   - 新增 `tools list-mic-targets` 列出 MIC 已注册 target_id。
   - `demand register` 时校验将走 MIC 的 target_id 是否已在 MIC `target_profiles.yaml` 注册，未注册的在输出中给出 `mic_unregistered_targets` warning（不阻断），避免执行期才报 `Unknown target_id`。

### 行为变更

5. **Planner 目标级工具开关**（`planner.py`）
   - Demand target 新增 `collect_mic: false`（跳过 MIC 任务）与 `collect_stock: false`（跳过盘后行情刷新）。
   - 修复隐藏 bug：行业目标（无 ticker）此前在 post_market/off_hours 阶段会误生成 `post_close_stock_refresh` 任务并在执行时抛异常。

6. **日报**（`reports.py`）：结构化事件 Top N 与覆盖缺口带上 `target_id`，行业级（无 ticker）产出不再显示为空目标；补采建议在无 ticker 时回退显示 target_id。

### MIC 工具侧配置（`tools/market_intelligence_collector/config/`，纯 YAML 无代码改动）

7. `target_profiles.yaml`：新增 8 条行业主线档案（AI算力 / 高端装备 / 电力系统 / 创新药 / 高股息央国企 / 平台互联网 / 资源周期 / 出海制造，含关注指标、上下游术语、代表公司）+ 首批 18 家核心公司富档案。
8. `query_families.yaml` v0.4：新增 `early_signal` 查询族（涨价函 / 排产 / 中标候选人 / 盈利警告 / 商誉减值等早期信号词，对应跟踪建议 §6.10）与 `hk_connect` 查询族（港股通资格 / 南向持股 / 回购注销，对应 §5.1）。
9. `source_packs.yaml` v0.4：新增 `industry_stats` 源包（统计局 / 能源局 / 工信部 / 行业协会数据）；信源域名映射补充北交所、港交所、医保局、CDE、NMPA 及汽车 / 工程机械 / 船舶 / 航运行业协会（对应 §5）。

### 仓库清理

10. 删除项目根目录误提交的运行产物 `mic.db`（MIC 默认 `sqlite:///mic.db` 相对进程 CWD，在项目根运行会重新生成），`git rm --cached` 解除跟踪并加入 `.gitignore`；有意保留的数据库仍在 `data/` 目录下（该目录已被忽略）。如需固定位置，可设 `MIC_DATABASE_URL=sqlite:///data/mic.db`。

### 验证

- Agent 离线测试：48 个全部通过（新增 13 个 request / batch / planner 测试，`tests/test_request_center.py`）。
- MIC 测试套件：73 个全部通过；QueryPlanner 冒烟确认新行业/公司档案能展开出高质量中文查询计划。
- CLI 端到端冒烟：`request batch`（8 行业 + 175 公司一次注册，档案单次写盘、每 Demand 一个版本）→ 重复执行幂等 → `runtime tick` 消费 `demand.registered` 并编译采集请求 → MIC 加载器解析重写后的档案无误 → `demand register` 对未注册 target 正确告警。

### 升级说明

- 老库无 schema 变更，直接升级即可。
- 建议启动顺序：`request batch --file examples/research_pool_full.yaml --test-mode` 小预算试跑 → 检查 dashboard 与日报中的事件质量 → 去掉 `--test-mode` 重跑一次恢复正式预算（幂等）。
- 全量注册后每日 MIC 任务约 8 行业 + 108 核心公司 + 67 观察公司；预算可通过配置文件 `demands.<id>.task_profile.mic.budget_profile` 或 `demand suspend` 分层控制。

---

## V0.6 — 2026-07-04：按设计说明书补齐功能

- 消息化查询服务（`query.intelligence.request/response` + 对应 Ticket），供其他 Agent 经队列查询情报。
- Demand 消息流：注册/生命周期变更发布 `demand.registered` / `demand.changed`；新增 `RuntimeController`，tick 消费 demand 消息并在暂停/取消时清理未完成工作。
- 动态池目标解析：`pool_members` 表 + `PoolRepository`，`dynamic_pool` scope 按池层与过滤条件解析目标。
- 行情特征增强：涨跌停价/距离、日内区间位置、20 日同时段量比、多条件异动阈值与 risk_control 升级路由。
- 能力验证扩展：CLI 可用性、Cookie、交易状态、日线、meta 摘要等检查 + 推荐盘中模式；支持启动时与盘前自动调度。
- 交易日历（节假日/调休）、消息 TTL（`expires_at`）、cadence_profile 命名档案。
- 日报补齐：Demand 覆盖、Message 统计、成本与 MIC 预算使用、次日补采建议。
- 运维命令：`queue publish`、`config validate`、`db backup`、`init-db --reset`、`agent checkpoint`、session 自动轮转。
- 实时监控看板：`intel-agent dashboard`（前后端一体，轮询三库 JSON 快照）。

## V0.5.1

- 修复代码审核问题：模型占位符硬拦截、`workspace_root` 解析不依赖 CWD、SQLite 拆分 state/bus/data 三库、retryable 错误 nack 语义、非交易时段任务约束、能力验证以 `query bars` 行数为准、QualityGate 阈值生效等。

## V0.5

- 首个功能完整版本：Demand 注册与编译、消息队列（lease/ack/nack/dead-letter）、MIC 与 stock_data_collector 真实适配、质量闸门、结构化事件与行情特征、heartbeat / checkpoint / circuit breaker / 崩溃恢复、HTML/JSON 日报、OpenClaw artifact 渲染。
