# 比较数值单一来源修复与复核（2026-10-28）

同比换算修复已在真实响应中得到验证：模型只给原始147.42和percent，共享模块生成1.4742，
并经MIC、Agent与导出保持一致。两期六项金额和完整数值摘要也正确交付。
**财报样例整体仍未通过**：净利润、现金流的“不适用”只在审核记录和摘要中存在，
对应指标comparison为空，覆盖引用未满足正式发布条件。本轮首例失败后停止。

## 已实现

此前模型须重复生成comparison_input原值和comparison换算值；上轮147.42%被写成0.14742，
共享审核正确拦截，但冗余数值生成增加了失败机会。

- 唯一规范要求模型提供原始数值、单位、原文依据、比较对象与支持状态，以及显式字段绑定。
  数值comparison留空，模型不再重复换算；状态和比较对象仍由模型判断，无公司或正文关键词规则。
- `mic/content_review.py`从已审核的comparison_input按Decimal换算，生成省略的数值成员。
  原claim/model_review不变，字段审核记录原值、单位、输出值和输出单位。
  同一共享函数用于正式字段读取，合并、存储、缓存、Agent和导出沿用原审核链。
- inference、pending_review、unsupported不会因算术有效而提升状态。
  来源引用、嵌套引用、维度、原值、绑定、依赖仍须有效；未绑定输入不填充相关指标。
- 旧式显式数值继续严格核对。claim或指标中显式矛盾值、空值占位、多claim冲突、
  数值与同成员“不适用”并存时仍隔离，不静默改写成通过。
  不改写历史数据库，不把历史冲突响应自动修成成功。
- 审核版本为`comparison_normalization_20261028`，沿用严格缓存版本检查；
  README、模型主提示和唯一规范同步更新。

## 验证

代码在本地WSL修改，定向rsync到home-ubuntu，使用`/home/yu/.venv/agents_groups/bin/python`。
Gateway18791已在运行，无启动或依赖安装。

本地新旧比较契约62项定向测试通过。第一版远程302项通过，随后代码核对发现跨claim、
原指标与“不适用”状态冲突边界，补3项及对应检查；该版未调用模型。
最终版本17文件 **305项通过，0失败/错误/跳过**，墙钟77.227秒，3条既有SQLAlchemy警告。
新增共36项覆盖换算、零/负值、百分比/百分点/基点、错误输入、角色与状态、依赖、
多位置复用、父/子绑定、冲突、合并/缓存/Agent回读/导出。
这些人工标注测试验证执行契约，不替代真实内容验收。

离线预检使用上轮未改动的冲突响应：0.14742仍被判comparison_value_conflicting，
摘要继续隔离，覆盖仍未评估，证明没有放宽旧响应的冲突门槛。

## 真实财报样例

使用冻结B8的单位明确选段，不重新搜索或读取网页，构造当前模型提示。
来源三项指标的2026H1/2025H1六个金额逐项核对如下：

| 指标（人民币元） | 2026H1 | 2025H1 |
| --- | ---: | ---: |
| 营业收入 | 1,736,309,066.25 | 701,761,937.72 |
| 归属于上市公司股东的净利润 | -11,563,095.44 | -270,942,340.80 |
| 经营活动产生的现金流量净额 | -2,168,986,796.01 | -1,164,254,274.78 |

六项独立指标的值、单位、币种、负号与期间正确；额外总资产指标不用于替代任何一项。
本次审核部分71处引用（含嵌套引用）均可定位到对应原文，现金流跨行引用正确。

原响应的收入指标comparison为空，revenue_yoy只有comparison_input的147.42/percent。
共享模块实际生成comparison.yoy=1.4742，并记录换算依据；完整摘要保留147.42%的原始语义。
审核后、MIC读取、Agent回读与导出的指标、比较和scope一致，事件审核也已resolved。
这些结果证明新数值路径在实际响应中运行，不能仅归因为旧式响应恰好填写正确。

### 本轮失败项：非数值比较状态没有交付到指标

`np_yoy_na`、`ocf_yoy_na`均为source_supported，引用正确、依赖通过，且field_values明确给出：
`comparison={"yoy":null,"yoy_status":"not_applicable"}`，分别绑定对应指标的comparison。
但模型在这两个指标的comparison仍写了空字典。

本次改动只为数值成员生成省略值；“不适用”仍沿用要求模型在指标与claim两处填写的旧契约。
`comparison_candidates`跳过null数值，随后`field`对空字典直接返回，故没有生成对应非数值状态。
这不是金额错误、引用错误或“不适用”语义未理解；是该状态尚未采用同一字段生成路径。

最终一句话摘要包含“不适用”，但正式指标仍为空，无法等同为已交付的结构化比较状态。
覆盖评估仍引用这两个claim；只在审核/摘要中存在不能作为正式回答锚点，因此统一覆盖审核给出
`not_assessed / coverage_answer_not_published`。实际无效回答引用恰为这两个，未手工删掉引用以强行通过。

其他可选字段仍有隔离：why_it_matters绑定observation而所需角色为analysis/comparability；
uncertainty无绑定。最新报告、披露日期和共同合并口径的证据边界未改变。

## 停止与下一步

预设最多2模型/2Gateway、累计600秒；首例通过才运行缺单位例，失败即停。
实际 **1模型/1Gateway、167.391秒**，输出上限65536，finish_reason=stop、未截断，
0搜索/正文读取/重试。第二例未执行，未继续改状态交付或追加模型调用。

下一步应把已审核的非数值比较状态也纳入统一生成契约，让模型只在claim中表达一次判断，
由共享模块依照显式绑定生成对应指标字段，同时保留显式冲突拦截和原始输出。
成功标准是六项金额、正确收入同比、净利润/现金流的not_applicable状态、有效partial覆盖、
摘要与MIC/Agent/导出全部一致。该改动尚未实施。

本轮固定材料驱动不执行Reader、搜索、完整发布日期准入或Agent真实任务调度，
不是端到端验收；原B8整体FAIL不变。
main基线仍为`4621fa02587b016259e68747fe92df82cfb54134`，保留此前未提交改动，未提交/推送Git。

## 证据

- `tools/market_intelligence_collector/logs/acceptance/comparison_normalization_20261028/`：
  修改前清单、本地62项、远程302项及其版本记录，无真实模型调用。
- `tools/market_intelligence_collector/logs/acceptance/comparison_normalization_20261028_r2/`：
  最终代码/驱动清单、305项回归、旧响应预检、单次live结果、隔离交付数据库及导出。
  `inspection_explicit_header.json`记录引用和交付比较，`coverage_diagnosis.json`记录无效锚点，
  `semantic_explicit_header.json`和`final_audit.json`记录逐项结论与停止状态。

原响应：`acceptance_model_io/comparison_normalization_20261028_r2_explicit_header/call_01_f2ce9c6ed941472a8d9a40b3bf280e37/response.txt`。
所有中间材料位于gitignored的logs，未改写B8或上轮失败证据。
