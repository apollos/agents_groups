# ChangeLog

本文件记录情报收集员 Agent 的版本变更。日期为变更落地日期。

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
