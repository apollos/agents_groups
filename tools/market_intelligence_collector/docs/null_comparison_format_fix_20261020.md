# 空比较字段格式兼容修复

实际失败来自 Agent 批次 `mthreads_e2e_20261020_013906`：近期任务唯一通过90日来源窗口的媒体正文已返回具体内容，但两个指标的 `comparison=null` 不符合字典结构，整份结果在统一内容审核前被拒。原批次正式近期事实、指标和事件均为空，业务仍为 FAIL。证据见该批 `review/developments_review.md`、`review/mic_content_audit.md` 及完整模型取证。

唯一规范 `mic/content_review_policy.py` 已明确 null/空 comparison 只代表没有交付比较判断。`MetricObservation` 现在仅将该字段显式 `None` 在结构校验前规范为空字典；缺省与空字典行为保持一致。列表、字符串、布尔和数值等错误类型仍拒绝，非空字典不修值，其他字段的空值契约不改变。没有新增比较、不适用状态或覆盖判断，没有改变模型语义、统一审核、时间窗口、入库门槛或预算。

隔离草稿 `logs/acceptance/third_fix_draft_20261020/` 记录149份基线文件SHA；实际只改 `mic/schemas.py` 并新增 `tests/test_null_comparison_format.py`。根代理应用前核对正式文件与草稿基线一致，文件清单记录于 Agent 新批 `logs/acceptance/mthreads_e2e_20261020_044817/preparation/applied_fix_manifest.json`。

本地82项不同定向测试全部通过，含19项新增用例。第一轮79项通过，另3项因隔离草稿缺公开config而在fixture初始化失败；补齐原字节配置后仅复跑这3项并通过，没有业务断言失败。记录及初次失败均保留在草稿logs。

精确离线回放使用B3 call06原始响应、8个实际正文选段、原target_names及与冻结SHA一致的output_schema配置。`BundleValidator` 内的完整共享审核执行后保留4事实、4指标及1事件，事件日期2026-08-09及连续短引用通过。37条隔离警告仍保留，未定义摘要claim、无审核的问题等没有被批准；原输入、原响应、原正式结果均未修改。`source_metadata`、task_context原样记录，但不是BundleValidator参数；本回放没有运行合并、入库、Agent消费或完整pipeline，不能当作B3业务交付成功。一次额外调用覆盖最终化的初版回放保留为非最终版本，以无额外调用的v2为准，见草稿 `logs/replay_review.md`。

远程home-ubuntu回归已通过：MIC全套1036项、Agent交付定向50项，零失败/错误/跳过。新的独立真实批次尚未启动，准备目录为 `mthreads_e2e_20261020_044817`。常规每任务预算仍为4查询、4次物理成功读取、8次HTTP尝试、4次浏览器尝试、8次模型/Gateway及600秒，整批1800秒。最新财报、披露日期与共同合并口径仍须实际证据支持，格式修复不消除这些缺口。
