# 模型 JSON 响应边界与诊断修复

本轮针对 B6 的 JSON 格式失败做离线定位，修复通用解析边界和诊断缺失。
**没有修补原始响应，没有重新采集，也不能据此将 B6 改判为通过。**

## 已定位与尚不能定位的原因

B6近期目录抽取call04的原响应为13,011字符、21,297字节，
SHA256为 `ab48399f0c20599a938869d7b8c9593820e56bf120996cdc45e347612c7dccc1`。
其嵌套内容和content_review闭合后，缺少最外层对象的右括号，标准解析在字符偏移13011、
第1行第13012列失败。内存追加一个括号可以解析，只用于确认格式形态，未作为业务答案。

代码路径为 SDK `choice.message.content` → `ModelCallResult.raw_text` → 旁路trace。
本例raw/saved/redacted哈希相同，trace完整、未脱敏；文本没有代码围栏或首尾空白，
旧解析器的strip和正则回退没有缩短文本。因而没有证据指向MIC本地清洗或日志截断。
当时本地材料没有SDK之前的wire证据，不能在模型、提供方、Gateway和SDK之间定位责任层。
10/21晚间后续只读核查已找到Gateway持久化assistant消息，确认其正文与MIC完全一致且已缺括号；
故障边界已前移至该消息或更早，详见[上游核查报告](json_response_upstream_audit_20261021.md)。
`finish_reason=stop`、输出11308 tokens、请求上限65536不能证明每个上游环节都没有其他限制。

本例旧解析器拒绝响应是正确行为。另有以下可复现的通用缺陷：

- 未检查顶层类型，数组/数字/布尔值会被当成成功，下游`.get()`可能异常。
- `\{.*\}`回退可从不完整外层数组或带垃圾前后缀的文本中提取内部对象，丢失响应完整性边界。
- 去围栏不要求闭合。
- 重复键静默取末值，NaN/Infinity及溢出为inf的数值可被接受。
- 格式失败只有`status=json_invalid`，缺少错误类型、位置和可正式读回的解析诊断。

## 实际改动

新增 `mic/modeling/json_response.py`，统一解析完整单个JSON对象。
仅兼容完整、独占响应的无语言/JSON代码围栏；不抽取任意对象子串、不补括号、
不删除错误值、不改写业务字段。重复键、非标准常量、非有限浮点数及非对象根均拒绝。
解释器的嵌套和整数解码限制作为格式失败保留。

适配器失败仍返回`parsed=None`、`status=json_invalid`，现在同时记录：

- `error_type=json_invalid`；
- 不含模型原文的简短`error_message`；
- `request_diagnostics.json_parse_error`，包含固定的`kind`；
  语法错误另含`offset / line / column / at_end`。

`offset`为原始响应的字符偏移，不是字节；行列同样对应原始响应。
`at_end`只表示错误位于实际解析材料末尾（排除JSON空白和合法闭合围栏），不表示传输截断。
trace发生脱敏时，不能直接用原文坐标定位脱敏后的文件。

`finish_reason=length`仍优先判`output_truncated`，即使片段碰巧可解析也不准入；
纯文本模式和正常对象的既有成功诊断结构保持。
调用数、SDK重试策略、65536输出上限、32k输入配置及共享语义审核均未改。
原有planner中的配置化fallback路径仍受原预算控制，本修复没有新增重试或额外调用。

诊断沿现有pipeline写入model_run和`collection_diagnostics.model_requests`，
由`explain_source_analysis(...).model_requests`正式读回；无需新数据库字段或迁移。
验收trace的`result.json`只增加固定种类及受限整数/布尔位置字段，
不复制自由错误文本，不扩大原日志限额。
解析成功仍须通过BundleValidator及统一content_review，不能据此发布未审核事实、日期或口径。

## 验证与证据

证据目录：`tools/market_intelligence_collector/logs/acceptance/json_response_fix_20261021/`。
所有新增脚本、修改前快照和运行结果均在该目录，未改B6封存材料。

- [原响应独立诊断](../logs/acceptance/json_response_fix_20261021/b6_json_evidence_diagnosis.md)。
- [14份原响应解析回放](../logs/acceptance/json_response_fix_20261021/saved_response_replay.json)：13份有效对象解析值不变，
  call04继续失败且给出精确位置；所有原件SHA保持。七个通用反例由旧解析器误接受变为明确拒绝。
- [定向验证汇总](../logs/acceptance/json_response_fix_20261021/verification_summary.json)覆盖10个测试文件、220个用例，
  其中新增解析/交付用例59个。首轮219通过、1失败、0错误、0跳过；
  唯一失败是测试误以为2048层嵌套必然超过解码器上限。本地CPython3.12.13可以解析它，
  将压力样本改为实际触发解码限制的10000层后，该项复验通过。未新增生产深度上限，
  生产代码在两次验证间未改，不重复已通过219项。
- [首轮日志](../logs/acceptance/json_response_fix_20261021/regression/pytest.stdout)及
  [单项复验](../logs/acceptance/json_response_fix_20261021/regression/followup.stdout)分别保留。
  首轮耗时419.03秒、复验2.63秒；既有关系explain测试有1条SQLAlchemy空主键警告，未产生失败。
  这是220项分别完成验证，不是一次220项全绿重跑，也不是完整MIC/Agent回归或真实端到端重测。
- 独立代码复核未发现阻断问题；已跟踪文件的`git diff --check`通过。
  [本轮增量补丁](../logs/acceptance/json_response_fix_20261021/scoped_change.patch)
  SHA256为 `4b31cdeef18005f87d71dc8be5cab19071080fe7d9b78f2e238806890a0e6010`，
  只包含本轮3个生产文件和2个测试文件的变化，不把之前的未提交工作算作本轮新增。
  B6封存ZIP的SHA仍与归档清单一致。

本地修复阶段，远程SSH只读连通检查在沙箱外仍连接超时，因此当时使用本地`/home/yu/.venvs/mydev/bin/python`
执行隔离、替身客户端的离线验证；没有启动OpenClaw或浏览器，也没有尝试修复远端服务、安装依赖。
当时变更尚未同步至远程Ubuntu；晚间连接恢复后的同步和验证见下节。

## 远程Ubuntu验证完成（10/21晚间）

同步前396个B6冻结文件逐一匹配，三个新增文件均不存在。只同步本修复的五个文件，
修复后的399个文件在测试前后均匹配预期SHA；adapter、acceptance_trace和json_response
均实际从`/home/yu/Workspace/agents_groups/tools/market_intelligence_collector/`导入。

北京时间20:00:09–20:01:05，使用`/home/yu/.venv/agents_groups/bin/python`（CPython 3.12.3），
对同一10文件清单运行一次完整定向验证：**220通过，0失败，0错误，0跳过**。
pytest耗时54.72秒，驱动计时55.136秒，900秒硬上限未触发。
有1条既有SQLAlchemy空主键警告，与本地相同。

证据：[结果](../logs/acceptance/json_response_remote_20261021_195713/result.json)、
[日志](../logs/acceptance/json_response_remote_20261021_195713/regression/pytest.stdout)、
[JUnit](../logs/acceptance/json_response_remote_20261021_195713/regression/result.xml)、
[导入路径](../logs/acceptance/json_response_remote_20261021_195713/import_paths.json)。
日志已回传本地对应`logs/`；原本地219+1历史记录不改写。
本轮使用隔离数据库/配置/浏览器profile和替身，无真实搜索、模型或浏览器采集；
未启动服务、安装依赖或执行新的真实端到端批次。

B6缺失的51份报告/交付文件已补至远端，原7份旧审阅文件保留；
完整终版以原封存ZIP为准，SHA保持不变。
具体差异及使用边界见[同步说明](../logs/acceptance/json_response_remote_20261021_195713/delivery_differences.md)。

## 剩余工作

本轮修复阻止格式误接受并保留可解释诊断，不会让缺括号的响应变完整，
更不能补足财报最新性/独立披露日期/共同口径或近期发布日期证据。
B6仍为INCONCLUSIVE。修复同步与远端定向复核已完成；
对既有上游证据的核查已完成：Gateway持久化assistant/控制台输出/MIC正文三者SHA相同，
已有提供方responseId及实际响应模型，但已核查位置没有原始HTTP/SSE记录。
下一步先离线验证受限上游取证通路，字段、限额和停止条件见[方案](json_response_upstream_audit_20261021.md)。
该方案尚未实施；
不能通过自动补答案、扩大预算或在同版本反复采样求通过。
