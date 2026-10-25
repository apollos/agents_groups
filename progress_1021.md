# 项目进展：2026年10月21日

本文件基于实际 B6 导出及逐项内容审阅，不把接口 success、任务完成、记录条数或离线测试通过当成业务验收。
历史默认预算、通用修复及 B1–B5 结果见 [progress_1020.md](progress_1020.md)，旧批次不改写或重跑。

## 当前状态

**最新批次 B6 `mthreads_e2e_20261020_144725` 已完成远程真实端到端执行、正式读回、暂停和清理；整体 INCONCLUSIVE。**
报告、证据ZIP和归档校验均已完成，1129项校验全部通过。没有仍在执行的采集或待重试任务。

**B6之后的最新开发进展：** 通用JSON解析边界与诊断修复已同步至远程Ubuntu；
10/21 20:00–20:01使用`agents_groups`环境完成一次220项定向离线测试，全部通过、零跳过。
本地原验证仍保留219项首轮通过、1项修正测试假设后复验通过的记录。
远端B6报告与封存ZIP已补齐；7份旧审阅文件保留，完整终版以ZIP为准。未启动新真实批次。
详情见本文末节及
[JSON修复报告](tools/market_intelligence_collector/docs/json_response_contract_fix_20261021.md)。

**上游核查新进展：** 现有Gateway会话证据显示，持久化assistant消息已经缺括号，
与Gateway输出及MIC原文逐字节相同。故障边界已前移至该消息或更早；
已找到提供方responseId及响应模型，但缺少原始HTTP/SSE记录，尚不能在模型/提供方/流组装之间定责。
详见[上游核查报告](tools/market_intelligence_collector/docs/json_response_upstream_audit_20261021.md)。

| 部分 | 实际结论 |
| --- | --- |
| 行情 | Q01–Q05 PASS，9/15–10/20 的 20 日收盘/量/额；10/20 单 AKShare 来源，保留回退警告。 |
| 财报 | 三项两期六金额正确，F02–04 数值子项 PASS；F01/共同合并口径/财报整体 INCONCLUSIVE。 |
| 近期事项 | 正式业务集合全空，5 gaps，E01–04 INCONCLUSIVE；E05 仅未擅建股价因果约束通过。 |
| Agent | 83 项一致性检查通过，24 个缺口未丢；A05 有 facts/metrics 而 events=0 分支未触发。 |

32k 已实际生效，不再只是离线容量回放。近期目录10条解析/8条选段，8个候选全部提交模型，
5个read入队，1个导航子公告实际读取并独立审核。其余4个未尝试，受队列优先级和成功读取/模型预算约束，
不是4次HTTP失败。不能将“导航已执行”写成“近期业务已验收通过”。

## 运行环境、版本与约束

- 本地权威开发目录 `/mnt/c/workspace/agents_groups`，环境 `/home/yu/.venvs/mydev`。
- 真实运行在 `ssh home-ubuntu`，目录 `/home/yu/Workspace/agents_groups`，Python `/home/yu/.venv/agents_groups/bin/python`。
- main，HEAD `3450926ea77fc4682a4cfe3d2b2a9f9988e98c52`，大量既有未提交修改保持；没有 Git 提交、推送、reset。
- B6冻结396文件，补丁SHA256 `c1a93c7e36fd9f1e982c9416543e15d1dcc2198786c793bf32179b2bdd9da4f5`。
  运行后与10/21交付前复核均无变化；新增报告/审阅脚本不修改被冻结生产代码和driver。
- OpenClaw准备时未监听18791，经一次已授权的detached启动成功（PID276184，`logs/openclaw-gateway-b6-20261020.log`），收尾后保持共享网关运行。
  禁止systemd；若未来启动失败，立即停止且不排障/重试/换环境。
- 无新环境或依赖安装。模型语义与共享审核保持，不增公司/关键词特判，不放宽日期或评分准入。

真实T0 `2026-10-20T15:00:55.097654+08:00`，结束 `15:20:44.510699+08:00`，
墙钟1189.413045秒。目录时间14:47:25不是T0，10/21审阅归档不计入运行墙钟。
行情T=10/20，20日窗口9/15–10/20。近期任务reference为 `2026-10-20T07:11:44.501845+00:00`，
90d cutoff为 `2026-07-22T07:11:44.501845+00:00`。

| 实际消耗 | 财报 | 近期 | 每任务上限 |
| --- | ---: | ---: | ---: |
| 查询 | 4 | 4 | 4 |
| Reader选择尝试 | 7 | 4 | 与物理成功另计 |
| 成功正文 | 4 | 4 | 4 |
| HTTP正文 | 7 | 4 | 8 |
| Browser正文 | 0 | 0 | 4 |
| 模型/Gateway | 6/6 | 8/8 | 8/8 |
| MIC秒数 | 560.40 | 523.32 | 600 |

合计1083.72 MIC秒；全批上限1800秒，MIC合计上限1200秒。搜索另有12页面访问。
stock12入口（2预检+8fetch+2读回），28提供方结果；内部HTTP/重试总数未知，不能说只有12网络请求。
三任务各执行一次，未重投；两Demand suspended，无待采集/重试消息票据、活worker或profile锁。

## 主要业务证据与未解决问题

财报：task `task_660d7c24a35c4f748632` → Agent `run_5dbd206b21b242af8a15` → MIC `run_91e3d7e9b161`，
正式11facts、13metrics、2events、1risk、19gaps。来源 `link_b7df8e871bfe` 原半年报摘要的2026H1/2025H1：

| 指标（人民币元） | 本期 | 同期 |
| --- | ---: | ---: |
| 营业收入 | 1,736,309,066.25 | 701,761,937.72 |
| 归母净利润 | -11,563,095.44 | -270,942,340.80 |
| 经营现金流净额 | -2,168,986,796.01 | -1,164,254,274.78 |

收入同比147.42%；另两项原表“不适用”。不能称盈利或把负基期套常规增长率。
官方披露日期、最新披露序列及共同合并口径仍缺证；URL日期不是独立发布日期。
财报初筛没有选择官网IR，两个历史IPO文件占用成功读取额度；未准入为当前经营正式facts/metrics/events。
媒体库存风险是有条件的风险，不是已经减值；两来源事件是同一半年报事项，不是两个独立进展。

近期：task `task_a52f8e6aa90b4c59a329` → Agent `run_2013f5c07ead4660b0c0` → MIC `run_70304b4f43fc`。
4源全部publication_time_unverified，正式业务0、gaps5。实际child `link_4d776f206145` 走call07：
H股仍为拟发行、待股东会/监管批准，董事会8/10落款的signature/source_publication角色未确定。
父目录标签不能自动作为child已核实发布日期；本批没有证据证明共享审核错误清空了已核实日期。
其他三个成功读取是父目录、原SERP半年报摘要、原SERP限售公告，不是四个导航child。

近期call04 `call_04_a6405324c48d44dc8cc5418b8663d358` 为json_invalid：
原响应13011字符/21297字节，SHA256 `ab48399f0c20599a938869d7b8c9593820e56bf120996cdc45e347612c7dccc1`，
EOF第13012列失败；内存补一个最外层右括号可解析，但原文未改、诊断结果未入库。
finish_reason=stop、output_tokens11308、上限65536、trace完整；不能直接归因于字符/输出预算或某个传输层。

stock：task `task_8707670d5d83431e9ed9`，行情Agent run `run_7dc3e37f3bef43abafae`、request `req_825441811cbc4e5a`。
350.40→363.20，未复权窗口首末变化+3.6529680365%。Tushare/Baostock到10/19，末日仅AKShare `stock_zh_a_hist`，
不是本批腾讯回退接口；provider_update_time为空，is_complete不保证收盘后不会修订。
换手率19日为空、末日11.84%。附属估值108 medium、财务指标34 medium、交易状态1 high冲突，
公司行动和资金流部分失败，完整stock任务仍failed / partial_or_failed。

Agent近期外层status=success，但quality=accept_degraded、usable=false、output_status=no_structured_output；
这些字段及5缺口明确保留，不能仅取success。财报2events，近期0facts/0metrics/0events，所以A05未触发。

## 验证证据及范围

- 预算119项、补充阶段计数24项、stock内容14项、Agent一致性83项、窗口/导航63项通过。
- 模型14trace完整，13success、1json_invalid。输入仅业务user材料白名单投影，不含system/schema/config/完整wire。
  原限额保持input256KiB、response2MiB、run32MiB、调用数受有效8次且最多16约束。
- B6前补齐导航取证字段，本地35、远程60（trace35+navigation25）通过；导出离线9及driver静态检查通过。
- 32k改动前全量MIC1213+Agent371属于8k版本；32k远程定向326通过，不能混称1584项在32k版本重跑通过。
- stock审阅初版沿用旧腾讯字段导致KeyError；导航检查初版错要求final URL等于原URL导致FAIL。
  两者均仅修正离线审阅脚本，旧失败证据保留；未改产品/正式记录或重采。
- 主报告经独立复核无实质矛盾；交付前396文件hash无变化、85本地报告引用无缺失。

## 交付与下一项工作

- [B6书面报告](agents/intelligence_collector_agent/docs/acceptance/mthreads_e2e_20261020_144725.md)
- [交付索引](agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261020_144725/INDEX.md)
- [实际用户资料](agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261020_144725/research_brief.md)
- [逐项验收](agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261020_144725/acceptance_review.md)

10/21归档阶段远端SSH超时；采集、读回、导出和回传此前已完成，不能把它写成运行环境阻塞了B6测试。
改为本地对已有导出执行冻结打包逻辑，保留原始文件哈希及包内脱敏字节哈希。
远端环境秘密值当前无法重新加载；正式导出/worker副本此前在远端已脱敏，本地执行同一脱敏器，
当时新报告和归档尚未同步回远端；20:00之后完成补齐，见文末追加记录。

归档已在本地完成：283个ZIP成员（281个证据文件及两份包内清单），**1129项校验全部通过**，
包括ZIP SHA256、CRC、成员范围/唯一性、原件SHA及脱敏后包内字节SHA。校验失败列表为空。

- [证据ZIP](agents/intelligence_collector_agent/logs/acceptance/agent_mthreads_e2e_20261020_144725.zip)
- [归档清单](agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261020_144725/package_manifest.json)
- [校验结果](agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261020_144725/package_verification.json)
- [本地打包依据](agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261020_144725/preparation/packaging_local_fallback.json)

ZIP SHA256：`4ee889addda1882b934ec101ae9fe18462aaee86aaf6cf17a2638c9d7ad241e6`。
包内报告/证据已封存，不再改写；校验结果和本进度文件保存在包外，避免归档自引用。
归档通过仅证明交付完整性，整体业务仍为INCONCLUSIVE。

原计划的“不完整JSON离线定位及通用处理”现已推进完成本地修复，见下节。
当前B6及历史driver/结果/ZIP不重跑或覆盖，不提高预算、不为公司加特判、不在同版本反复抽样求PASS。

## B6之后：JSON解析边界和诊断修复（先完成本地验证）

实际读取B6 call04并沿SDK正文→adapter.raw_text→trace核对后，确认缺括号已存在于适配器收到的文本，
不是MIC的strip、围栏处理或trace截断所致。现有证据没有SDK之前的wire数据，
不能定位模型、提供方、Gateway或SDK哪层造成缺失。旧代码拒绝call04本身是正确的。

另外复现了确定的通用缺陷：旧parser会接受非对象根、从不完整外层结构中抽出内部对象、
忽略尾部垃圾或未闭合围栏，并允许重复键覆盖、NaN/Infinity及浮点溢出。
格式失败只有status，没有位置和可正式读回的解释。

本地改动：

1. 新增`mic/modeling/json_response.py`，只接受完整单个JSON对象；仅兼容完整独占围栏，
   拒绝上述非法/歧义结构。不补括号、改业务值、追加模型调用或改变原预算。
2. adapter保留原raw及`parsed=None`，失败带`error_type=json_invalid`和`json_parse_error`，
   语法失败记录原文字符offset/line/column/at_end，诊断不复制正文。
   `finish_reason=length`仍优先判output_truncated，不把stop+EOF武断归因于传输截断。
3. 诊断经原pipeline/model_run/report/explain路径透传，trace仅保存白名单诊断，不扩大日志。
   完整对象仍需通过BundleValidator及共享审核，未审核事实/日期/口径不会因解析成功放行。

验证材料在MIC `logs/acceptance/json_response_fix_20261021/`：

- 14份B6原响应纯解析回放：13份有效对象值不变；call04仍拒绝，错误位置13011/1/13012；原件及封存ZIP哈希保持。
- 10个文件、220项定向用例（新增59项）。首轮219通过、1失败，零错误/跳过；
  测试误假定2048层嵌套超过本地Python3.12解码限制，改用10000层压力样本后该项复验通过。
  生产代码未变，没有重复219项，也没有抹掉首轮失败；不是一次全绿重跑或全量回归。
- 首轮419.03秒，单项复验2.63秒。有1条既有关系explain测试的SQLAlchemy空主键警告；未扩展范围修复它。
- 独立代码复核无阻断问题，tracked diff格式检查通过。
  本轮增量补丁SHA256 `4b31cdeef18005f87d71dc8be5cab19071080fe7d9b78f2e238806890a0e6010`，
  包含3个生产文件、2个测试文件；此前dirty改动保留，未提交/推送。

[验证汇总](tools/market_intelligence_collector/logs/acceptance/json_response_fix_20261021/verification_summary.json)、
[修复报告](tools/market_intelligence_collector/docs/json_response_contract_fix_20261021.md)、
[首轮日志](tools/market_intelligence_collector/logs/acceptance/json_response_fix_20261021/regression/pytest.stdout)、
[单项复验](tools/market_intelligence_collector/logs/acceptance/json_response_fix_20261021/regression/followup.stdout)。

上述本地修复阶段的只读远程连通检查在沙箱外仍SSH超时，实际测试使用本地`mydev`和隔离替身/数据库；
无真实模型、搜索或采集调用，无服务启动/排障或依赖安装。**该阶段尚未同步或远程验证，未运行B7。**

该阶段待办的远端基线核对、同步与定向离线复核现已完成，见下节。
上游生成缺括号的原因仍需更上游证据定位；财报选源/最新性/共同口径与近期发布日期缺口也未解决。
不要仅凭本轮parser修复开同内容随机重试或承诺下一批能全量通过。

## 10/21晚间：远端同步和定向离线验证完成

远端连接恢复后，先只读核对396个B6冻结文件，全部相符，3个新增文件尚不存在。
随后仅同步3个生产文件和2个测试文件，未改变其他生产代码、配置、预算或依赖，未提交或推送Git。
修复后的399个文件在远端测试前后全部匹配；三个模块均实际从项目checkout导入。

测试环境为远程Ubuntu的`/home/yu/.venv/agents_groups/bin/python`，CPython 3.12.3。
北京时间20:00:09启动、20:01:05结束；10文件、220项一次全通过，0失败/错误/跳过，
pytest耗时54.72秒，驱动计时55.136秒，未触及900秒停止上限。
唯一警告仍为既有关系explain用例的SQLAlchemy空主键警告。
使用隔离SQLite及搜索/模型替身，无真实搜索、模型或浏览器采集；未启动服务或运行B7。

新证据目录：`tools/market_intelligence_collector/logs/acceptance/json_response_remote_20261021_195713/`。

- [结果](tools/market_intelligence_collector/logs/acceptance/json_response_remote_20261021_195713/result.json)、
  [测试日志](tools/market_intelligence_collector/logs/acceptance/json_response_remote_20261021_195713/regression/pytest.stdout)、
  [JUnit](tools/market_intelligence_collector/logs/acceptance/json_response_remote_20261021_195713/regression/result.xml)。
- [同步前核验](tools/market_intelligence_collector/logs/acceptance/json_response_remote_20261021_195713/remote_before.json)、
  [同步后核验](tools/market_intelligence_collector/logs/acceptance/json_response_remote_20261021_195713/remote_after_sync.json)。
- B6交付285文件中原有227份同哈希、51份缺失、7份旧审阅文件不同。仅补齐缺失项，
  包括报告、索引、归档清单和ZIP；完成时278份同哈希，7份旧文件未被覆盖。
  后续只更新包外书面报告，包内终版及ZIP哈希保持。
- [七份差异及使用说明](tools/market_intelligence_collector/logs/acceptance/json_response_remote_20261021_195713/delivery_differences.md)：
  差异是此前已记录的离线审阅完善，远端原目录不是逐文件相同的终版目录；完整终版以封存ZIP为准，
  不在原目录重跑旧审阅或打包脚本。

原本地验证汇总和失败日志未改写，B6原始响应及封存材料未改写；本次不提交或推送Git。
离线验证通过不补足业务证据，B6整体仍为INCONCLUSIVE。

该阶段下一项工作是对已保存call04核查SDK之前的响应证据，区分上游返回不完整与中间层丢失。
成功标准是用可关联同次调用的原始证据定位边界；若现有记录不足，应明确缺失字段并提出有界取证方案，
不能把parser验证通过当成生成问题已解决，也不据此直接重跑同内容真实批次。

## 10/21晚间：既有上游证据核查完成

本轮只读远端已有日志和SQLite记录，未修改生产代码/配置，未执行测试、模型调用、采集或服务操作。
399个本地源文件哈希及B6 ZIP哈希保持；原始响应未补括号或重新入库。

- Gateway session `f1cba8c4-cf9e-4909-8b85-a95832a6dd2a` 的 `transcript_events.seq=5`，
  通过 `__openclaw.runId=chatcmpl_2cb04204-33d9-45f6-8a44-c9f752f3360c` 与MIC call04精确关联。
- 持久化消息时间15:14:23.481、Gateway日志正文15:14:23.508；两者与MIC原文均为13011字符/21297字节，
  SHA均为 `ab48399f0c20599a938869d7b8c9593820e56bf120996cdc45e347612c7dccc1`，已缺最外层右括号。
- 会话记录提供方 `volcengine-agent-plan`，实际响应模型 `deepseek-v4-1-flash-261017`，
  提供方responseId `02179248040107076e706cca72a7c4abc35179fe35310d8ea2d06`。
  MIC的 `openclaw/main` 是路由名，原 `provider_request_id` 字段实为Gateway返回的response.id。
- 已核查OpenClaw capture三表当前均为空；启动日志指向的 `/tmp/openclaw/openclaw-2026-10-20.log` 当前不存在。
  不能由此断言当时未启用capture，也不能把HTTP200或stop视为提供方原始正文完整的证据。

[最终边界证据](tools/market_intelligence_collector/logs/acceptance/json_upstream_audit_20261021_200457/final_boundary_evidence.json)、
[完整报告及尚未实施的取证方案](tools/market_intelligence_collector/docs/json_response_upstream_audit_20261021.md)。
本次排除了“已持久化assistant之后才首次丢括号”，仍未解决其之前的根因；业务缺口和INCONCLUSIVE保持。

下一项具体工作：先以固定SSE样本离线验证受限的上游取证通路，核对请求中的JSON模式/输出上限，
以及提供方可见文本、持久化assistant和MIC正文之间的长度/SHA。
成功标准是关联明确、原文不变、无额外模型请求、排除秘密及推理内容、共享原日志限额且正常收尾。
取证方案尚未实施；没有启用全局capture，没有新真实批次或同内容循环重试。

## 10/21晚间：增加预算内的一次模型重新生成，远端离线验证完成

用户询问缺括号是否应交给模型处理并要求继续后，本轮先实现格式恢复；
上一节的上游取证通路仍是尚未实施的计划，未开启全局capture。

正文提取在原有模型策略没有可解析成功结果、且末次为`json_invalid`时，
可以使用同一模型和原始来源材料重新生成一次，整个MIC run最多一次。
开关`call_governance.extraction.json_recovery.enabled`默认`true`，可设`false`关闭。
不追加总预算；恢复消耗现有model/source/Gateway/时间额度，可能减少后续来源调用名额。
提取、恢复、分块与仲裁共用来源额度；初筛、视觉、仲裁、分块不触发恢复，
可解析但证据不足、网络失败或输出截断也不触发。不补括号，不加入公司关键词规则。

原响应和失败诊断保留，新响应仍经过严格解析及原统一内容审核、日期窗口和入库检查。
父子ModelRun和trace关联可读回。若模型请求期间超时或取消，抽取调用正常返回pipeline后，
先保存实际调用审计与费用，随后停止业务写入；强杀/崩溃/未返回不在此保证范围。
完整正文仍只在原受限trace开启时保存，不扩大日志。
SDK请求timeout取run剩余秒数，外层worker继续负责整体截止。

远端同步前399既有文件一致；仅同步本次10个文件，新增两份测试后401文件在测试前后全部同哈希。
远程Ubuntu使用`/home/yu/.venv/agents_groups/bin/python`、Python3.12.3，
北京时间20:36:43–20:38:10，18文件、**359项一次全部通过**，0失败/错误/跳过。
pytest耗时86.57秒、驱动87.2秒，未触及900秒上限。新增35项覆盖预算/策略及入库读回：
恢复成功、审核缺失、日期角色歧义、取消/超时等。唯一警告仍为既有关系explain的空主键警告。
使用隔离数据库及模型/搜索/读取替身，无真实采集或模型请求，未启动服务或运行B7。

[修复报告及下一步](tools/market_intelligence_collector/docs/json_recovery_fix_20261021.md)、
[验证结果](tools/market_intelligence_collector/logs/acceptance/json_recovery_fix_20261021_203134/result.json)、
[测试日志](tools/market_intelligence_collector/logs/acceptance/json_recovery_fix_20261021_203134/regression/pytest.stdout)。
独立复核未发现阻断；B6 call04原文及封存ZIP哈希保持，未提交或推送Git。

目前完成的是恢复机制和离线程序验证，B6业务仍为INCONCLUSIVE，未解决上游缺括号根因或业务证据缺口。
下一项为新版本下的一次有界摩尔线程Agent联动验收，先冻结新批次配置与预算，遵循原文档停止条件。
成功标准仍是用户各问题得到准确、自洽、可追溯且证据充分的交付；恢复若触发还须可审计且不超预算。
模型替身测试通过不能替代这一真实内容验收。
