# 2026-10-25 进展

## 当前结论

摩尔线程 Agent 联动 stock_data_collector 和 MIC 的应用验收尚未通过。
B6 已封存，整体 INCONCLUSIVE；B7 在证券元数据预检被阻塞，三个业务任务未执行。
已定位无界等待风险、完成 BaoStock 证券预检的有界进程修复并通过远程离线回归。
随后B8真实链路及收尾已完成，整体应用验收FAIL：20日行情PASS，财报证据INCONCLUSIVE，近期金额单位审核自洽性FAIL。
具体以本文件末尾B8完成节为准；前文下一项工作与启动记录保留为当时进度。

本地提交基线的详细范围、历史验证适用版本、当前限制和下一项开发见
[2026-10-25提交说明](agents/intelligence_collector_agent/docs/acceptance/checkpoint_20261025.md)。
本文件中“未提交/推送”及HEAD记录描述各历史运行时点；本次按用户要求保存本地commit，由用户自行push，不改写旧批次版本证据。

## B7 实际执行与封存

- 批次 `mthreads_e2e_20261021_204415` 于 10/21 20:50:19.930–20:54:30.017 +08 运行约 250.087 秒。
  交易日历预检成功，证券基础信息预检 180 秒等待后终止（另约 10 秒退出等待），rc=-15，无自动重试。
- Agent 需求/任务/运行/attempt、消息、ticket 均0；MIC 未启动，无模型或 Gateway 调用。
  正式只读查询成功只是查询可用，不代表业务成功。两份冻结需求没有注册。
- Tushare 与 AKShare 证券原始数据各一条已落盘，耗时约0.630秒与37.714秒；之后无 BaoStock 完成结果。
  无端点级日志，不能断言卡在登录、行业、证券查询还是分页，也不能认定是某种网络故障。
- stock request/run 仍残留 running，证券主表与行情均为空；cleanup 没有 clean=true。
  10/25 只读检查未见命令行引用该批路径的进程，但完整锁/资源清理未经证明。保留历史状态，不补写成功或取消。
- 已补齐 INDEX、用户资料缺口说明、Q/F/E/A 对照及定位证据，冻结驱动与历史业务记录保持。
  整体 BLOCKED；业务 NOT_EXECUTED；JSON 恢复 NOT_TRIGGERED。

[B7 报告](agents/intelligence_collector_agent/docs/acceptance/mthreads_e2e_20261021_204415.md)。
证据目录：`agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261021_204415/`。
封存包：`agents/intelligence_collector_agent/logs/acceptance/agent_mthreads_e2e_20261021_204415.zip`，
SHA256 `7274f3b2c8188e9b832a0337a80da267ae674456b9e72c7d46261bd07d09faca`。
92 个成员、275 项文件/清单/来源哈希及排除路径检查全部通过，见包外 `package_validation.json`。
原打包器漏选的 launcher/export 摘要以 review/ 原样副本及哈希登记纳入；未修改冻结打包器。
进度文件 progress_1021.md 最后一节原来只记录 B7 计划，现在由本文件补充实际失败结果，不把旧计划误当完成。

## 本轮修复与远程验证

BaoStock 安装 SDK 0.9.4 的 connect/recv 没有时限，证券预检原调用链没有使用已配置的30秒值。
本次仅默认 BaoStock security_master 整次调用进入独立进程，用原30秒配置加最多2秒终止等待，
包括登录、查询及分页；超时返回 PROVIDER_TIMEOUT，保留已取得的其他提供方数据。
无法确认回收时停止请求，不继续采集。正常保存时数据库终态为 partial_success，不能称所有来源成功。
未修改 MIC 语义/统一审核机制，未加公司关键词或测试案例特判；其他 stock 请求超时治理没有在本轮扩展。

本地开发并同步4文件，远端 `/home/yu/.venv/agents_groups/bin/python`（CPython3.12.3）验证。
首轮44通过、2处新增测试夹具错误；修正后相同七文件 **46项全部通过**，0失败/错误/跳过。
最终运行时间11:21:15–11:21:25 +08，10.451秒；10个验证文件本地/远端哈希相同且运行前后未变化。
挂起/强杀/父进程退出、回收失败停止、原始数据与正式SQLite终态均有覆盖。
全部为假 SDK、真实子进程和临时数据库的离线测试，无新增真实采集、模型请求或服务操作。

[修复报告](tools/stock_data_collector/docs/baostock_security_deadline_fix_20261025.md)。
首轮材料 `tools/stock_data_collector/logs/acceptance/baostock_security_deadline_20261025/` 保留；
最终材料 `tools/stock_data_collector/logs/acceptance/baostock_security_deadline_20261025_r2/`。
分支 main，HEAD `3450926ea77fc4682a4cfe3d2b2a9f9988e98c52`；原有未提交工作保留，未提交/推送 Git。

## 下一项工作

在修复版本下冻结独立的新摩尔线程验收批次，保持原两需求、三任务、模型/读取/时间预算和停止条件，
先验证两个元数据预检，再执行正常 Agent 联动流程；不复用 B7 状态或重跑其冻结驱动。

成功标准：用户可从正式出口获得可追溯20日行情、截至新T0最新官方报告及披露日与共同合并口径下三指标两期值、
至少一项符合90日窗口的实质内容，结果/存储/资料一致，预算和最终清理通过。
JSON恢复若触发须在原调用额度内可审计；未触发照实记录。离线通过不能代替真实内容验收。
上游缺括号在持久化assistant之前的根因仍未知，受限HTTP/SSE取证方案仍未实施。

## B8 已启动（后续状态以本节续记和批次证据为准）

用户再次要求继续后，新批次 `mthreads_e2e_20261025_114700` 已于 10/25 11:49:37 +08 在远程 Ubuntu 启动。
409个源码文件本地/远端全部同哈希；14个helper/contract冻结，需求正文和原预算不变。
补丁SHA256 `08e163e798c93a558ca019df6385d756ffc815109739563eee4ef0a543c70282`。
原Gateway已监听，无需启动服务；本地静态及7项驱动监督替身检查通过。
当前只是运行开始，尚无业务验收结论。工作最多1800秒、收尾最多120秒，无自动重试。
独立证据目录 `agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261025_114700/`。

## B8 已执行并审阅：应用整体FAIL

10/25 11:49:37.555–12:07:14.531 +08，T0至收尾1056.976秒，真实运行在home-ubuntu的agents_groups环境。
两个需求、三个任务已执行；两MIC done并accept_degraded，股票快照failed。需求已暂停，worker/浏览器/锁清理完成，无自动重投。

- **核心行情PASS**：09/18–10/23规定20日完整，120个字段、正式查询和Agent一致；三来源各37行原始归档hash及首中末抽查通过。
  未复权首末410→384.18，变化−6.2976%。
- **财报INCONCLUSIVE**：半年报营收/归母净利/经营现金流三指标两期值及原文同比正确，最新官方报告、披露日和共同合并口径仍缺证。
- **近期内容FAIL**：已有窗内9事实/9指标；新浪发布者日期2026-08-10正确保留。金额单位在正式记录标source_supported，
  同来源却又声明单位币种缺少原文核验（metric_a7ad62fcb6ce与gap_79ee929036b5），审核状态不自洽。
  尚未证明金额数值错误；其他已读来源的发布日期未核实，保留隔离。
- **Agent链路通过、完整语义交付未通过**：正式传递93项核对通过，A05实际触发：Agent事件0、事实9/指标9仍usable；
  MIC原来源事件pending，未被当成Agent确认新事件。同一半年报不双算独立进展。
- **真实JSON恢复已验证**：近期call07 json_invalid→call08原材料重生成，父响应完整、父子输入SHA一致；
  计入原8次模型/Gateway预算。恢复来源仍经统一审核和日期过滤，0业务记录/1gap；不是恢复后自动放行。
- **BaoStock修复真实生效**：证券预检约30秒超时、无重试，保留其他来源并继续任务；不能称BaoStock证券接口已恢复。
- **附带股票问题保留**：估值111项、财务指标34项medium冲突，质量层判不可用；公司行动NaT/空来源降级。
  核心20日行情通过不代表全量快照完全成功，未自动扩展专项修复。

预算每MIC查询4、成功来源4、模型/Gateway8，HTTP正文8/5、浏览器正文0/0；MIC469.99/486.38秒，stock2预检+8采集+2读回=12入口。
409源码、14 helper/contract冻结一致，无追加真实调用、无新服务、无生产改动或Git提交推送。
原材料日志按既定白名单保存，不含完整wire；HTTP/SSE上游根因仍未知。

只读审阅首轮的raw文件未回传、SQL JSON null解码和prompt任务名误判均保留原结果并补核验，未通过改运行结果消除失败。
[B8报告](agents/intelligence_collector_agent/docs/acceptance/mthreads_e2e_20261025_114700.md)；
[业务审阅](agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261025_114700/acceptance_review.md)。

**当前下一项工作**：以本批已存选段和模型输出离线定位并修复“金额单位待审却以原文支持发布”的统一审核一致性，
核对表头上下文、模型依赖与共享审核传播，不加关键词或公司特判。成功标准是单位/币种/范围/数值和限制在所有正式出口一致，证据不足则待审/隔离。
最新财报确认仍为后续独立缺口；不在同一冻结版本重复真实抽样。

B8证据已封存回传：`agents/intelligence_collector_agent/logs/acceptance/agent_mthreads_e2e_20261025_114700.zip`。
SHA256 `d861f439e3b77060f4952d0634d9d001928b1ec0ccfec895559007a2abb14e0a`；278成员、1113项完整性检查全部通过。
包外`package_validation.json`保留校验结果，ZIP内保留源文件与脱敏成员各自哈希。

## 本地提交基线

按用户要求，将三个组件的累计修复、新模块、测试源码及书面报告一并保存到本地 `main`，推送由用户执行。
提交前HEAD为 `3450926ea77fc4682a4cfe3d2b2a9f9988e98c52`；原120个改动文件加本次详细说明，共121文件。
本轮只做审查、记录和版本保存，没有改生产代码、启动服务、重新测试或采集。
87个Python文件静态解析、4个YAML解析和diff格式核对通过；409个B8冻结文件与当前完全一致。
既有远程359项MIC定向回归、46项BaoStock定向回归各自的源码对应与范围限制已写入提交说明，未宣称当前全仓库回归通过。
日志、数据库、原始响应及封存ZIP继续留在gitignored路径；Git仅保存代码、配置、测试源码和报告。
当前业务结论仍是B8整体FAIL，下一项仍是基于已存证据修复金额单位审核一致性。
