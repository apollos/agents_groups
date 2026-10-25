# 2026-10-25 本地提交说明：累计修复与 B8 验收基线

本提交保存三个组件的累计修复、测试源码、历史验收报告和进度记录，便于后续在明确基线上继续开发。
**摩尔线程端到端流程已执行完并封存证据，整体应用验收仍为 FAIL；本提交不是验收通过或发布声明。**
用户授权在本地 commit，由用户自行 push。本次整理没有修改生产代码、启动服务、运行测试或追加真实采集。

## 提交范围与版本对应

- 本地分支：`main`；提交前 HEAD：`3450926ea77fc4682a4cfe3d2b2a9f9988e98c52`。
- 提交前累计改动共120文件：Agent 43、MIC 66、stock 8、根目录进度3；其中48个已有文件修改、72个新文件。
  本次再加入本说明并更新 `progress_1025.md`，共121文件；87个 Python、4个 YAML、30个 Markdown。
- 这些改动跨组件存在导入、协议和审核状态传递依赖，作为一个完整基线保存，不拆成缺少必需模块的中间版本。
- B8 运行时版本记录仍为上述 HEAD 加未提交补丁，补丁 SHA256 为
  `08e163e798c93a558ca019df6385d756ffc815109739563eee4ef0a543c70282`。
  本次核对409个冻结文件全部与当前一致。新提交号不回写历史 manifest，也不把新提交伪称当时的运行 HEAD。
- 不纳入 Git：运行日志、驱动、原始响应、数据库、浏览器 profile、环境文件和证据 ZIP；这些继续保存在 gitignored 的 `logs/` 等既定位置。

## 已保存的实现

### intelligence_collector_agent

- 将任务状态、ticket 和完成消息的落地统一起来；失败、取消、过期和死信有明确终态，恢复时重放通知而不重复执行工具。
- 用 `lease_generation` 隔离失去租约的旧 worker，将取消、attempt 完成、业务落地和消息写入纳入相应事务边界；晚到结果不能复活已取消任务。
- 保存按 run/attempt 区分的结果，修正嵌套总线事务及幂等路径；清理失败不能被静默当作成功后继续下一项工作。
- 保留 MIC 的 task coverage、审核限制和质量信息。没有确认事件、但存在有效事实/指标时，可以形成有用的结构化输出；摘要、查询和导出不丢审核信息。
- 新增 `collection_results.py`、`task_lifecycle.py`、`work_guard.py`，它们被已有生产代码直接导入，必须一起提交。
  `messages.lease_generation` 的兼容迁移由数据库代码处理；本次未操作或迁移历史验收数据库。

### market_intelligence_collector

- 由模型进行任务相关性、问题覆盖及目录/正文角色判断；目录导航只跟随实际可见、已绑定的 URL，消耗既有搜索、读取、模型和时间预算。
  子页面不继承目录页的日期或内容批准状态，重复 URL、缓存和取消仍受统一计数与执行边界约束。
- 将发布日、事件日、报告期等角色及其原文证据区分；范围、身份、分析和比较判断通过共享 claim、依赖和字段绑定执行。
  `content_review_policy.py` 继续是规范唯一来源，审核器、合并、存储及读取出口共同执行，不增加公司关键词批准规则。
- 保留空比较字段的格式兼容，不借格式规范化提升语义状态。正文输入上限改为32000字符，模型输出上限仍为65536。
- 严格解析完整 JSON 对象，拒绝重复键、非有限数、额外正文和未闭合包装；不通过本地补括号猜测模型原意。
- 默认启用每次 run 最多一次的 JSON 原材料重生成：仅在正常回退后仍为 `json_invalid` 时适用，继续消耗原来源/run/Gateway/时间额度。
  不用于语义不合格、低置信、超时或截断等其他失败；保留失败父响应、输入哈希、父子调用、费用和后续共享审核。
- 新增 `browser/index_scope.py`、`index_navigation.py`、`task_triage.py`、`modeling/json_response.py`、`modeling/acceptance_trace.py`。
  取证是显式启用、受限保存的输入投影与响应，不等于完整 HTTP/SSE 抓包；上游缺括号原因仍未解决。

### stock_data_collector

- 根据已留存的 AKShare/Tencent 返回契约处理现代8列行情的股/元/换手率单位，保留 SDK 原始值；旧6列缺金额路径仍显式标记估算。
  adapter、runner 的字段 provenance 和 comparison 一起修正，使真实金额参与比较、估算金额/vwap 保持可辨认。
- 新增 `baostock_worker.py`，将默认 BaoStock `security_master` 的登录、查询和分页整体放入受监督子进程。
  使用已有30秒时限，另最多2秒 TERM/KILL 回收；确认超时后保留其他提供方已取得的数据，无法确认回收则停止请求。
- Linux 子进程有父进程死亡保护；不修改第三方 SDK，不引入新依赖或自动重试。
  其他 BaoStock 请求和显式注入 adapter 仍走原路径，不能宣称已解决所有供应商操作的无界等待。
- 新增 BaoStock worker 测试、Tencent 行情契约测试及修复报告；runner 已直接导入新 worker，不能漏交该模块。

## 当前默认预算与配置位置

| 配置/模式 | 查询 | 成功正文 | HTTP尝试 | 浏览器尝试 | 模型/Gateway | 时限 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| MIC部署上限、Agent research | 4 | 4 | 8 | 4 | 各8 | 600秒 |
| Agent black_swan | 2 | 2 | 4 | 2 | 各4 | 300秒 |
| Agent test_mode 快速检查 | 2 | 2 | 2 | 2 | 各3 | 300秒 |

MIC 上限在 `tools/market_intelligence_collector/config/browser_runtime.yaml`；Agent 模式和 MIC 外层600秒时限在
`agents/intelligence_collector_agent/config/intelligence_collector.yaml`。
任务只能收紧部署上限；成功获取正文不等于内容通过审核，失败读取也消耗尝试预算。
正式研究验收使用冻结的研究配置，不能把快速检查的完成当作研究需求满足。

`tools/market_intelligence_collector/config/call_governance.yaml` 中
`max_input_chars_per_model_call=32000` 是单次模型调用的材料字符上限，**不是32000 token，也不是输出额度**；
仍受每链接最多8个选段等约束；它也不是包含system提示词在内的所有模型请求总长度硬上限。
`extraction.json_recovery.enabled=true` 不增加模型调用总额度。
BaoStock 的 `timeout_seconds=30` 在 `tools/stock_data_collector/config/data_sources.yaml`，本次累计修复没有提高该数值。

## 已有验证与证据边界

下表记录已执行的验证，不表示本次提交整理重新运行了它们，也不能把不同版本的通过数相加称为当前全仓库通过。
应用和主要测试在 `home-ubuntu` 的 `/home/yu/.venv/agents_groups` 执行，未使用远端 `mydev`。

| 验证 | 已有结果 | 适用范围与限制 |
| --- | --- | --- |
| Agent生命周期修复 | 全套357通过；最后清理/CAS调整后定向65通过 | 历史阶段结果，见生命周期报告；不是本次新测试 |
| 目录导航阶段 | 远程 MIC 1213、Agent 371通过 | MIC当时为8000字符版本；Agent当前生产/配置/测试与对应后续冻结版本一致，不等于当前跨组件全量回归 |
| 32000字符调整 | 远程定向326通过 | 未因此重跑 Agent/stock 全套 |
| 严格JSON响应契约 | 远程10文件220通过 | 解析、交付及相关审核路径的定向验证 |
| JSON原材料重生成 | 远程18文件359通过，0失败/错误/跳过，约86.57秒 | 162个MIC生产/配置/测试文件与该快照全部同哈希，覆盖当前MIC改动；仍不是MIC全套 |
| BaoStock证券调用时限 | 远程7文件46通过，0失败/错误/跳过，10.451秒 | 其中24个新worker测试；10个验证文件当前同哈希；不含Tencent契约测试或stock全套 |
| 本次提交前静态核对 | 87个Python AST解析、4个YAML解析、diff格式检查通过；409冻结文件无漂移 | 仅静态/版本核对，不是运行或业务验收 |

失败历史保留：BaoStock首轮44通过、2个新增夹具错误，修正夹具后46通过；其他修复过程的早期失败见原报告，未覆盖证据。
Tencent契约测试源码一并保存，但本次审查未找到可独立宣称该11项已执行通过的结果；已有真实行情核验不能替代该单测记录。

主要书面依据：

- [Agent生命周期修复](collector_lifecycle_fix_20261019.md)
- [MIC目录导航与历史全套验证](../../../../tools/market_intelligence_collector/docs/index_navigation_follow_fix_20261020.md)
- [JSON响应契约修复](../../../../tools/market_intelligence_collector/docs/json_response_contract_fix_20261021.md)
- [JSON原材料重生成及359项验证](../../../../tools/market_intelligence_collector/docs/json_recovery_fix_20261021.md)
- [BaoStock证券时限修复及46项验证](../../../../tools/stock_data_collector/docs/baostock_security_deadline_fix_20261025.md)

## 最新真实端到端结果：B8已结束，业务未通过

B6 保持 INCONCLUSIVE；B7 证券预检阻塞，三个业务任务未执行，保持 BLOCKED。
随后独立 B8 `mthreads_e2e_20261025_114700` 在远端于2026-10-25 11:49:37.555–12:07:14.531 +08完成，
T0至收尾1056.976秒；两个需求、三个任务实际执行。没有因失败无限重试或扩大预算。

| 业务部分 | B8结论与依据 |
| --- | --- |
| 20日核心行情 | PASS：09/18–10/23完整20日、120字段，三来源原始归档、Agent结果和正式查询一致 |
| 财报 | INCONCLUSIVE：半年报三指标两期六个数及同比正确，但截至T0的最新官方报告、披露日、共同合并口径仍缺证 |
| 近期内容 | FAIL：窗内来源产生9事实/9指标，金额单位却一边标 `source_supported`，一边声明缺少当前来源表头/单位证据 |
| Agent交付 | 正式调用、落地及读取链路通过；事件0而事实/指标有效的A05场景实际通过；完整语义一致性未通过 |
| 运行约束 | 预算及收尾通过；需求已暂停，无待执行重试，worker、浏览器和锁已清理 |

单位矛盾可由 `metric_a7ad62fcb6ce` 与同来源 `gap_79ee929036b5` 重现。
目前未证明金额数值错误；其他来源数值相同也不能补写为当前来源的单位已经审核通过。
新浪2026-08-10发布者日期实际保留且处于窗口；该来源事件在MIC中仍为pending，没有被Agent当成确认新事件。
同一半年报在两个来源出现，不双算独立业务进展。

真实修复路径已触发：BaoStock证券预检约30秒超时后保留其他来源并继续；这不代表该接口恢复成功。
近期MIC第7次调用 `json_invalid` 后，第8次以同一原材料重生成，计入原8次额度；恢复来源仍经统一审核与日期隔离，0正式业务记录、1gap。
两MIC各使用查询4、成功正文4、模型/Gateway各8，HTTP正文尝试分别8/5、浏览器正文0/0；stock是12个业务入口，不能称12次HTTP请求。

股票附带数据仍有估值111项、财务指标34项medium冲突，以及公司行动 `NaT`/空来源问题。
股票快照任务保留failed、质量层判为不可用；核心20日行情通过不等于完整快照通过。

完整结论见 [B8报告](mthreads_e2e_20261025_114700.md) 和 [当前进度](../../../../progress_1025.md)。
旧报告中的“尚未运行新批次”“未提交Git”“下一步启动”等是当时状态，由B8完成记录及本提交说明更新当前进展，不回写历史。

## 材料位置与保存边界

- B8证据目录：`agents/intelligence_collector_agent/logs/acceptance/mthreads_e2e_20261025_114700/`，包含
  `run_manifest.json`、`INDEX.md`、`research_brief.md`、`acceptance_review.md`、`review/` 和包校验材料。
- 本地及远端已保存B8封存包：`agents/intelligence_collector_agent/logs/acceptance/agent_mthreads_e2e_20261025_114700.zip`。
  SHA256：`d861f439e3b77060f4952d0634d9d001928b1ec0ccfec895559007a2abb14e0a`。
  278成员、1113项完整性检查通过；封存包本次不改写、不提交Git。
- 本次提交准备清单、组件审查和版本校验记录：`agents/intelligence_collector_agent/logs/maintenance/local_commit_20261025/`。
- 上述 `logs/` 路径不是GitHub附件。只clone仓库可得到报告及代码，不能据此声称已取得原始运行证据；复核原始内容需使用已保存的日志或封存包。
  本批日志按白名单保存，没有完整HTTP/SSE wire，也未持久化全部网页/PDF；后续重新下载不能替代当时已存证据。

## 下一项开发与成功标准

1. 先用B8已存选段和模型输出离线重现“金额单位待审、正式记录却原文支持”的矛盾，定位表头上下文、模型claim依赖和共享审核传播中实际失效的环节。
2. 在模型判断与统一审核机制内修复通用原因；同步规范、执行器及必要回归，不增加公司/案例关键词，不通过降阈值、扩预算或放宽日期获得通过。
3. 验证同一判断的数值、单位、币种、口径及限制在审核、存储、Agent查询和导出中一致；证据不足应待审/隔离，有充分证据才可批准。
   离线及远端定向验证通过后，再在新冻结版本和独立批次中执行原边界下的真实业务验收。

最新官方财报/披露日/共同合并口径、附带股票字段冲突与公司行动问题、上游JSON缺括号根因及受限HTTP/SSE取证仍未解决。
它们不能因本次commit而标为完成；本次只保存现状，后续不在B8冻结版本上重复抽样。
