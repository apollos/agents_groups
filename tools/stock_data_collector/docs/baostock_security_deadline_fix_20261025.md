# BaoStock 证券基础信息预检超时修复（2026-10-25）

B7 的证券预检被外层 180 秒限额终止，Agent 三个业务任务均未启动。
本次给默认 BaoStock `security_master` 调用增加可终止进程边界，接通原有
`providers.baostock.timeout_seconds: 30`；没有增加预算或调用次数。
远程 Ubuntu 七个测试文件、46 项离线回归最终全通过；尚未执行修复后的真实端到端批次。

## 定位依据与限制

B7 证据目录：`agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261021_204415/`，
主要定位材料是 `review/preflight_failure_evidence.json`。
实际证券请求顺序为 Tushare→AKShare→BaoStock：前两者约 0.630 秒和 37.714 秒后各返回一条原始记录并写入对象文件；
随后无 BaoStock 完成结果。AKShare 的可选详情连接失败已被捕获，它并非本批未返回的供应商。
没有接口级日志或挂起栈，不能进一步确认 BaoStock 登录、行业查询、基础查询或分页中的具体阻塞位置。

配置已有 30 秒超时，但此调用链此前没有使用它。已只读核对 Ubuntu 安装的 BaoStock 0.9.4：
`socketutil.py` 的 connect/recv 没有设置 timeout，接收循环也没有 EOF 退出处理；logout 仍需网络响应。
这些是无界等待风险，不能据此断言历史故障一定是某一种网络原因。
安装源码路径、SHA 与行号保存在本次 `installed_sdk_evidence.json`。

## 修改范围

- `stock_data_ingestion/adapters/baostock_worker.py`：同一 Python 环境启动独立 worker，完整执行 SDK 导入、登录、行业/证券查询及分页。
  30 秒是整次该供应商步骤的墙钟上限；超时先 TERM 并等待 1 秒，必要时 KILL 并等待 1 秒。
  不重试，不退回无界直接调用；无法确认回收时抛出 `WorkerCleanupError`，中止请求。
- `services/ingestion_runner.py`：仅默认适配器集合中的 BaoStock `security_master` 使用此边界。
  显式注入适配器及其他请求保持原路径；本次并未实现所有供应商/请求的统一 deadline。
  已取得的其他供应商结果继续走原归一化、原始材料保存和数据库持久化流程。
  BaoStock 超时保留 `PROVIDER_TIMEOUT`，整体诚实返回 `partial_success`，不伪装全部成功。
- `config/data_sources.yaml`：仅补充现有 30 秒值在此路径的含义，没有修改数值。
- `tests/test_baostock_worker.py`：新增 24 项离线测试。

worker 继承 CLI 进程组，兼容现有外层超时监督；Linux 另在导入 SDK 前设置父进程死亡保护并核对父 PID。
正常退出仅关闭本进程 SDK socket，不调用可能再次阻塞的 logout。
stdout 只传完整结构化结果，SDK 打印不混入业务 JSON；失败保留结构化错误码和计数，避免透出任意 SDK 异常文本。
成功原始字段不改写，不涉及 MIC 模型语义、审核规则或公司特判。
非 Linux 没有父进程死亡保护，仅保留常规 timeout/异常清理；本项目已验证环境为 Ubuntu Linux。

## 验证结果

环境 `/home/yu/.venv/agents_groups/bin/python`，CPython 3.12.3；没有安装依赖或启动服务。
用假 SDK 和真实子进程模拟挂起，不连接真实提供方；数据库与原始对象目录均为隔离测试目录。

| 验证 | 实际结果 |
| --- | --- |
| 首轮 11:15:34–11:15:45 +08 | 44 通过、2 失败；两个新增测试夹具错误：非证券请求缺必填 ticker，及读取原始记录时遗漏 raw_data 包装。 |
| 修正夹具后 11:21:15–11:21:25 +08 | 同七文件 46 通过，0 失败/错误/跳过；驱动耗时 10.451 秒，未触及 180 秒上限。 |
| 实际挂起 | 登录、行业查询、基础查询、分页均在限时后退出；忽略 TERM 时 KILL，随后不出现延迟 API 副作用。 |
| 父进程与回收失败 | KeyboardInterrupt、Linux 父 SIGKILL；无法确认回收时异常透传，不能按普通提供方失败继续。 |
| 数据保留 | 前两来源 raw/hash、正式证券身份及来源字段保留；source_fetch_logs 两成功一失败、raw 索引 2 项，ingestion_run 结束为 partial_success。 |
| 范围和接口 | 配置值传递、其他请求/注入适配器路径保持，完整原始字段往返、错误/输出隔离及无效返回处理。 |

首轮材料：`tools/stock_data_collector/logs/acceptance/baostock_security_deadline_20261025/`。
最终材料：`tools/stock_data_collector/logs/acceptance/baostock_security_deadline_20261025_r2/`，
含 `verification_plan.json`、`result.json`、`regression/pytest.stdout`、JUnit 与四个改动文件的 `source_snapshot/`。
10 个源文件/测试文件在最终远端执行前后均与本地哈希一致。首轮日志未覆盖，生产代码在两轮间未改动。
验证结束后只读检查未见匹配本次 worker/回归驱动的进程，见 `post_verification_process_check.json`。
首轮 `working_tree.patch` 包含 ingestion_runner 更早的未提交修改，不能当作本次增量补丁；本次范围以以上四文件及源码快照为准。
独立审阅提出的“强杀仍未回收被降为普通错误”问题已修复并增加验证，最终复核无新阻断。

## 当前结论与下一步

B7 保持 BLOCKED、业务 NOT_EXECUTED、JSON 恢复 NOT_TRIGGERED；其 stock DB 残留 running 未被改写。
此次证明可终止边界与数据保存有效，不证明 BaoStock 服务恢复，也不补足任何业务内容证据。

下一项工作是在修复版本下冻结新的独立验收批次，保持原需求、业务标准、预算和停止条件，
先过两个元数据预检，再由正常 Agent 执行股票快照及两个 MIC 任务。
成功仍需：可追溯的 20 日行情、最新官方报告与披露日/共同合并口径及三指标两期值、
至少一项符合 90 日窗口的实质信息、正式查询一致，以及预算与收尾通过。
不得重启 B7 或把预检/离线回归通过当成端到端通过。
