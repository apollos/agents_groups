# 2026-10-19 规划驱动与任务生命周期修复

本轮修复摩尔线程批次暴露的规划驱动和任务状态问题，并使用离线替身验证执行、取消、
重试及恢复路径。它不是一次新的真实采集验收，不证明模型理解或业务内容已经合格。

基线为本地 `main`，HEAD `3450926ea77fc4682a4cfe3d2b2a9f9988e98c52`。
修改尚未提交或推送。代码在本地修改后，通过排除 `.git/` 和 gitignored 路径的 rsync
同步至 `home-ubuntu:/home/yu/Workspace/agents_groups`，测试使用 `~/.venv/agents_groups`。

## 已修复的行为

- 原规划驱动把队列前两次消费都当作 Request，同秒生成的 Task 可能使这一假设失效。
  新驱动只在新隔离批次中规划，使用 SQLite UTC 整秒屏障、前后队列快照、真实返回 ID
  和正式 Reader 核对两条 Request、三条 Task。问题、窗口和预算必须与冻结合同一致；
  条件不满足立即停止。没有改生产队列排序，也没有添加公司特判。
- `collection_tasks.status` 从 Ticket 和采集消息同步。成功、失败、重试、取消、过期和
  dead letter 不再永久显示为 `open`；正式查询同步失败时报告错误。状态同步故障不会
  使已完成工具调用再次执行。消息 ack 本身不被当成可用内容或业务验收通过。
- 每次领取消息增加不会因人工重试而归零的租约代次。旧线程不能 ack/nack 新租约，
  取消、结果落库和本地通知发布通过 bus 事务互斥。短租约在过期前安排心跳。
- MIC 执行记录在最终提交时再次校验取消状态；迟到结果不能恢复被取消的工作。
  清理失败的进程证据保留为阻挡状态，即使取消先于清理结果到达，也不能放行下一次采集。
- Ticket 终态与完成通知载荷在同一事务保存。Recovery 幂等补发原始载荷，不重复调用
  工具；结果、事件、质量通知不会被当成采集工作提前 ack，也不会在重试通知时重开源任务。
- 不同执行代次保留独立运行记录；成功缓存同时匹配任务、工具、操作和精确幂等键，
  避免首次失败遮住后续成功，或错误复用另一需求合同的结果。
- 同库部署复用外层事务，语义事件和通知写入使用嵌套事务保护，不提前提交外层锁。
  精确重放中缺少 `gap_id` 的同一缺口使用稳定 ID；没有改变模型语义判断或统一审核规则。

数据库迁移新增 `messages.lease_generation`，历史消息默认从 0 开始。没有删除旧表或
覆盖历史结果。本轮测试使用新建测试数据库；未对封存验收批次执行迁移、修复或续跑。

## 验证与证据

Agent 日志目录：`agents/intelligence_collector_agent/logs/acceptance/lifecycle_fix_20261019/`。
失败日志保留，后续轮次使用不同临时目录，没有覆盖前一轮证据。
测试日志、代码指纹及规划 JSON 证据已取回本地；完整临时测试数据库保留在 Ubuntu
对应 `logs/acceptance/` 目录，不要求将所有离线夹具数据库复制到本地。

| 验证 | 实际结果 | 说明 |
| --- | --- | --- |
| 定向回归 round1 | 55 通过、8 失败 | `finish` 遗漏 `worker_pid` 参数；已修正 |
| 定向回归 round2 | 78 通过 | 状态投影、分库/同库、取消、通知恢复与并发转换 |
| Agent 全套 round3 | 344 通过、5 失败 | 4 项新行情测试能力夹具错误；1 项不同幂等键缓存误复用；均已修正 |
| Agent 全套 round4 | 357 通过，106.12 秒 | `pytest_full_round4.log` |
| 最后清理竞态专项 | 65 通过，44.94 秒 | `pytest_final_targeted.log`；覆盖最后补充的清理终态 CAS 与 supervised/执行回归 |
| 新规划驱动 verification_01 | 17 通过 | 工具、模型、socket 与 subprocess 执行入口均被离线桩拦截 |
| 新规划驱动 verification_02 | 17 通过，19.010 秒 | 在更新后的队列/状态代码上复核，仍未消费真实 Task |

`full_round4_manifest.json` 记录全套通过时的 78 个源码/测试/夹具文件指纹。
其后只修改 `agent.py` 的最后清理 CAS 和对应 `test_attempt_finish_fencing.py`，
上述 65 项专项覆盖这次变更。两文件的远端 SHA-256 与本地一致，其余 76 文件未变。
最终指纹保存在 `final_local_manifest.json`：
`a87399a671eaf21207f468db6490be555dc14fa9d4cf1a9e6dead1a7788029ab`。

规划驱动及证据目录：
`agents/intelligence_collector_agent/logs/acceptance/mthreads_driver_fix_20261019_183855/`。
其中 `README.md` 说明约束及未来批次集成方式；`verification_*/summary.json` 和每例
`external_call_count.json` 记录离线结果。驱动脚本和临时数据库保留在 gitignored 的
`logs/` 下，不放入 `docs/`。

全套测试包括本机临时 HTTP 服务和 supervised stub 子进程；没有真实 MIC/stock
采集或付费模型调用。原始行情和语义审核替身测试只能证明程序行为，不能替代业务内容审阅。

## 业务验收边界与下一步

原 `mthreads_e2e_20261019_135238` 批次仍封存为 `INCONCLUSIVE`：业务任务完成数为
0/3。stock_data_collector 既有应用验收和 MIC 沐曦场景验收不因本轮回归而重跑。

下一步是在新批次、独立数据库和既定运行预算内，将修正的规划驱动接入完整验收流程。
先确认两条需求产生准确的三条任务，再执行原合同规定的两条 MIC 与一条 stock 任务。
成功标准是用户通过正式查询实际拿到与需求对应、准确、自洽且来源可追溯的内容，
能够明确区分事实、推断、待核查项和缺口，并核对 Ticket、任务、运行记录与通知一致。
不能以三条任务结束、接口成功或输出数量代替该标准。
