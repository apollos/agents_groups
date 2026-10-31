# 跨行表格引用修复与复核（2026-10-28）

本轮真实响应中的引用失配已消除，三项指标的本期和上年同期六个金额完整交付；财报专项整体仍未通过。
新响应把营业收入同比147.42%写成比例0.14742，统一审核拒绝该比较，依赖摘要和任务覆盖未通过。
按预先记录的停止条件，未运行第二例、未重试、未启动完整端到端。

## 原因与修改

上一轮原文为“经 营 活 动 产 生 的 现”换行“金流量净额 …”，响应把第二行改成“金 流 量 净 额 …”。
数值正确，引用不再是对应选段的连续原文。`_quote_supported` 在共享审核中逐字校验；
失配 claim 及其依赖被隔离，存储或 Agent 没有另加关键词判断。

修改唯一规范 `mic/content_review_policy.py`，在现有抽取调用中要求模型区分语义陈述与原文摘录：

- 陈述可整理跨行名称，引用逐字保留各行空格、换行、负号、标点及数值格式。
- 多行或多段依据可分别引用，保留行名、期间列、单位与数值归属；不能只取孤立数字。
- 输出前核对每条引用在对应选段的位置；嵌套金额、比较、日期引用复用已摘录片段。
- 增加通用应收款表格示例，无公司、指标关键词特判；没有扩大证据准入或自动修补引用。

审核版本更新为 `quotation_fidelity_20261028`，沿用共享版本/缓存检查。
共享执行器、存储与导出的逐字校验逻辑未改动。旧响应与旧数据库不改写。

## 验证结果

本地 WSL 修改、定向 rsync 同步至 `home-ubuntu`。
远程使用 `/home/yu/.venv/agents_groups/bin/python`；Gateway 18791 原已监听，无启动或安装操作。

16 个文件定向回归 **269 项通过，0 失败/错误/跳过**，墙钟76.951秒，3条既有SQLAlchemy警告。
新增8项覆盖原样跨行/分段引用、空格/换行/负号改变、错误段落、共享claim及依赖隔离、合并存储、Agent回读和导出。
这些人工标注样例验证执行契约，不能证明真实模型总会正确摘录。
驱动离线预检重新审核上轮失败响应，确认错误引用仍然被拒绝；预检无模型调用。

真实复核使用冻结B8财报选段，重新构造当前提示。原文、原响应、共享审核输出及交付结果逐项核对：

| 所需指标（人民币元） | 2026年半年度 | 上年同期（2025年半年度） | 交付 |
| --- | ---: | ---: | --- |
| 营业收入 | 1,736,309,066.25 | 701,761,937.72 | 两期正确 |
| 归属于上市公司股东的净利润 | -11,563,095.44 | -270,942,340.80 | 两期正确 |
| 经营活动产生的现金流量净额 | -2,168,986,796.01 | -1,164,254,274.78 | 两期正确 |

六项分别形成指标；单位、币种、正负号与期间正确。现金流引用保留原换行与空格。
本次响应审核部分的56处引用（包括嵌套引用）均可在对应原文定位。
审核后、MIC读取、Agent回读与导出中的指标、比较及scope一致。
净利润与现金流“不适用”比较按来源保留，未自行计算其同比。

## 本轮未通过的原因

`revenue_yoy` 的语义陈述、引用与 `comparison_input.yoy={value:147.42, unit:"percent"}` 都正确，
但原响应在 `field_values.comparison.yoy` 和收入指标的 `comparison.yoy` 同时填写0.14742。
147.42%应换算为1.4742；这次是模型输出的算术表示不一致，不能据单例推断为某个数值范围的固定模式。

共享 `_comparison_claim_reason` 用Decimal核对后给出 `comparison_value_conflicting`，没有放出错误同比。
`results_summary` 依赖该claim，降为 `dependency_not_supported`，相关事实、事件和一句话摘要被隔离。
模型拟给出partial，却在回答引用中包含上述待审判断，最终为
`not_assessed / coverage_answer_not_published`。六项金额通过不等于整个问题通过。
另有可选 `brief.uncertainty` 因observation与所需limitation角色不匹配被隔离，未将其伪装为已交付。

最新定期报告及披露日期仍缺证，期间没有替代发布日期；共同合并口径没有获得额外确认。
该驱动不执行Reader、搜索、完整发布日期准入或Agent真实任务调度，因此不是端到端验收；旧B8整体FAIL不变。

## 预算与下一步

预设最多2模型/2Gateway、累计600秒，第一例通过后才运行第二例，失败停止。
实际只用了 **1模型/1Gateway、176.518秒**；输出上限65536，finish_reason=stop，未截断。
搜索/正文读取/重试均0。缺单位的第二例未执行，不能用上一版本的结果代替本版本通过证据。

下一步先核对比较值契约：模型已经给出原始百分数与单位，却还须再填写换算值，增加了两份表示冲突的机会。
应评估由模型决定含义、比较对象、单位和支持状态，由统一模块执行确定性单位换算的单一数值来源设计，
同时保留原始输出、冲突诊断及旧数据兼容边界。此方案尚未实施，不是本轮完成项。
成功标准仍包括来源同比、两期六项金额、有效部分覆盖与全部交付阶段一致，再推进独立预算下的端到端。

基线main / `4621fa02587b016259e68747fe92df82cfb54134`；本轮源文件哈希在验证前后相同。
保留此前未提交改动，未提交或推送Git。

## 证据位置

`tools/market_intelligence_collector/logs/acceptance/quotation_fidelity_20261028/`（gitignored）：

- `before_manifest.json`、`before_diagnosis.json`、`verification_plan.json`、`driver_manifest.json`：基线、原始失配、范围与预算。
- `regression_r1.json/xml/log`、`driver_preflight_result.json`：回归与离线预检。
- `live_result_explicit_header.json`、`explicit_header_validated.json`、`explicit_header_reviewed.json`：真实调用与审核结果。
- `inspection_explicit_header.json`、`semantic_explicit_header.json`、`final_audit.json`：逐字检查、逐项内容结论与停止状态。
- `delivery_explicit_header/`：隔离MIC/Agent数据库、正式查询、Agent回读及导出。

原响应：`acceptance_model_io/quotation_fidelity_20261028_explicit_header/call_01_b2dbe436367c4c7d92ad0f1930ac60c6/response.txt`。
