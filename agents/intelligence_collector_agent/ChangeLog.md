# ChangeLog

本文件记录情报收集员 Agent 的版本变更。日期为变更落地日期。

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
