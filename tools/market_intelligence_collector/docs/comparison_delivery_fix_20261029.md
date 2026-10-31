# 比较状态交付修复与复核（2026-10-29）

已修复已审核的“不适用”状态没有进入正式指标的问题，并补齐同一路径的嵌套字段、显式冲突和读取校验。
远程337项回归通过；真实财报样例通过本轮固定材料标准。
第二个缺单位反例仍未通过：模型返回`link_only`，且ROE与其变化值形成循环依赖。
两次调用后停止，未继续修改或重试，完整端到端验收尚未完成。

本轮开始于10月28日，跨日至10月29日收尾；审核版本与证据目录保留`comparison_delivery_20261028`。
基线为`main / 4621fa02587b016259e68747fe92df82cfb54134`，保留此前未提交修改，未提交或推送Git。

## 修复及同类排查

原问题是生成路径只处理数值比较，跳过`null`及状态；指标`comparison={}`随后提前返回。
模型已在claim中正确批准并绑定“不适用”，但仍需在指标重复填写，导致摘要与结构化交付不一致。

- 唯一模型规范要求数值比较在`comparison_input`提供原值和单位，非数值状态及上下文在
  `field_values.comparison`表达一次；共享模块依照显式绑定生成省略成员。
  是否可比较、状态含义和依据仍由模型判断，不按公司、指标名称或正文关键词判断。
- 同类嵌套成员使用递归合并；父/子绑定遵守最近显式绑定，不将子字段批准扩展到其兄弟字段。
  `false`、零、空值和“不适用”不互相替代；孤立`null`不自动构成有效回答。
- 显式非数值、嵌套值冲突与数值冲突采用同一审核路径，隔离相关claim并传播到依赖。
  分别绑定的数值与同成员“不适用”状态矛盾也被拦截，不覆盖原响应来消除冲突。
- 正式读取校验全部非空比较成员及对应原claim，避免存储的状态/上下文缺少审核却被继续发布。
  不合格读取投影受到隔离，严格缓存拒绝复用，历史SQL记录保持原样。

同步更新`content_review.py`、`content_review_policy.py`、主提示、README及新增回归测试。
还检查了身份、日期、事件、摘要和scope边界：仍沿用各自角色、证据和依赖要求，未全局自动填充空字段。
本轮排查只覆盖相关审核和交付路径，不能证明MIC/Agent所有问题均已消除。

## 验证环境和预算

本地WSL修改，定向rsync到`home-ubuntu:/home/yu/Workspace/agents_groups`。
远程使用`/home/yu/.venv/agents_groups/bin/python`；Gateway18791原已监听，无启动、安装或升级操作。

本地3文件97项定向测试通过；远程18文件 **337项通过，0失败/错误/跳过**，墙钟78.850秒，
仅3条既有SQLAlchemy警告。新增32项覆盖状态、嵌套绑定、冲突、依赖传播、合并、缓存、Agent读取和导出。
回归与真实复核使用相同生产代码哈希；这些测试验证执行契约，不能代替真实语义核对。

离线重放上轮原响应，两个“不适用”状态现已发布，覆盖为有效`partial`；
更早响应中错误的0.14742仍被隔离。原响应未改动，预检无模型调用。

预先限定最多2模型/2Gateway、累计600秒、输出上限65536，首例通过才运行第二例，失败即停。
实际2模型/2Gateway，合计147.219秒（70.997秒、76.222秒），均正常结束且未截断。
0搜索、正文读取和重试；使用冻结B8选段，不运行Reader、完整发布日期准入或Agent真实任务调度。

## 真实财报样例：本项通过

两期六个请求金额均按来源的人民币元交付，币种、单位及负号正确：

| 指标（人民币元） | 2026年半年度 | 上年同期 |
| --- | ---: | ---: |
| 营业收入 | 1,736,309,066.25 | 701,761,937.72 |
| 归属于上市公司股东的净利润 | -11,563,095.44 | -270,942,340.80 |
| 经营活动产生的现金流量净额 | -2,168,986,796.01 | -1,164,254,274.78 |

收入同比147.42%由共享模块生成`yoy=1.4742`；本期净利润、经营现金流均交付
`{"yoy":null,"yoy_status":"not_applicable"}`。模型原始指标比较为空，已审核claim有明确值与绑定，
因此本例实际验证了新的生成路径。全部79处引用可定位到原文。

摘要含六个金额、正确同比和两个“不适用”；事件审核resolved，覆盖为有效`partial`，9个回答引用均已发布。
审核后、MIC读取、Agent回读与导出的指标、比较和scope一致。额外指标不替代六项请求指标。
上期标签沿用“上年同期”，结合2026半年报理解为2025H1，未在期间字符串中独立规范化。
最新官方报告、披露日期及共同合并口径仍缺独立证据，不能据此宣布原始完整需求全部满足。

## 缺单位反例：未通过，已停止

模型正确隔离无金额单位依据的收入、净利润，收入增长147.42%及EPS -0.02元/股在来源审核后成立；
独立收入增速未重复携带该增速的同比字段。24处引用全部精确。
但以下两处问题使正式交付失败：

1. 原始模型决策为`link_only`，评分68；validator仍保留5项指标，合并阶段沿用该决策并清空结构化记录。
   原响应未明确解释来源整体决策原因；事件比较中的`same_event`理由不能当成`link_only`原因的证明。
   本例不是合并器将`save_structured`降级，不能仅凭评分推断实际触发了哪条准入门槛。
2. `roe` observation依赖`roe_change`，后者又依赖`roe`，形成循环。
   共享审核将ROE、变化值及依赖摘要保留待审；本项不是引用或数值换算错误。

正式各阶段均为零事实/零指标；空结果一致不等于业务通过。
覆盖为`not_assessed / coverage_answer_not_published`，独立百分比/EPS未真正交付给用户。
未修改决策、删除依赖、降低评分或追加调用。缺日期的选段与Reader元数据合并仍不在本轮固定材料范围内。

## 下一步及成功标准

先核对模型来源整体决策与已支持独立信息的关系，以及重复来源场景下已有有效证据如何复用；
同时让模型区分原始ROE观察和变化判断的依赖方向，避免把多个判断捆在同一观察后互相依赖。
不以程序强行改成`save_structured`、提高模型分数或删除依赖边的方式放行。

下一轮需在原有预算与停止条件内验证：未知单位金额继续隔离，独立来源百分比/EPS/ROE有合法交付路径，
依赖无环且方向有依据，覆盖引用对应实际发布内容，MIC/Agent/导出一致，财报状态交付不回退。
通过定向复核后再推进端到端；原B8整体FAIL仍保留。

## 证据位置

目录：`tools/market_intelligence_collector/logs/acceptance/comparison_delivery_20261028/`（gitignored）。

- `before_manifest.json`、`before_source/`、`verification_plan.json`：基线、原代码及预算/标准。
- `related_issue_audit.json`、`local_unit_r1.*`、`regression_r1.*`：排查与测试。
- `driver_preflight_result.json`、`delivery_preflight_saved_status/`：旧响应原样重放。
- `live_result_explicit_header.json`、`inspection_explicit_header.json`、`semantic_explicit_header.json`、
  `delivery_explicit_header/`：财报逐项核对和隔离交付。
- `live_result_missing_header.json`、`inspection_missing_header.json`、`semantic_missing_header.json`、
  `missing_header_diagnosis.json`、`delivery_missing_header/`：反例失败与依赖循环证据。
- `final_audit.json`：实际预算、版本一致性和停止结论。

两个未改动的原响应分别位于该目录下：

- `acceptance_model_io/comparison_delivery_20261028_explicit_header/call_01_7b46d386f31e4c8a8ba188c0f65f01ff/response.txt`
- `acceptance_model_io/comparison_delivery_20261028_missing_header/call_01_76e132e75be942338c7116d0c826903c/response.txt`
