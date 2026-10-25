# B6 缺括号响应：上游证据核查

2026-10-21，只读核查远程 Ubuntu 已有日志和 OpenClaw 会话记录。未请求模型、搜索或采集，
未启动/重启服务，未修改生产代码、配置、预算、依赖或原 B6 材料。

**已将故障边界前移到 Gateway 持久化的 assistant 消息或更早。**
该消息、Gateway 控制台输出与 MIC 原响应完全相同，均已缺少最外层右括号。
现有证据仍不足以区分提供方输出不完整、流解析/拼接丢失及 Gateway 持久化前处理问题。
本次不是根因修复，也不改变 B6 的业务 INCONCLUSIVE。

## 调用关联与新证据

证据目录：`tools/market_intelligence_collector/logs/acceptance/json_upstream_audit_20261021_200457/`。
入口为 [最终边界证据](../logs/acceptance/json_upstream_audit_20261021_200457/final_boundary_evidence.json)。

| 层次 | 标识或记录 |
| --- | --- |
| MIC | `run_70304b4f43fc`，`call_04_a6405324c48d44dc8cc5418b8663d358` |
| MIC model_run | `mrun_79e263ef3ce7`，来源 `link_f895b64b472b` |
| Gateway runId / 客户端 response.id | `chatcmpl_2cb04204-33d9-45f6-8a44-c9f752f3360c` |
| Gateway session | `f1cba8c4-cf9e-4909-8b85-a95832a6dd2a`，`transcript_events.seq=5` |
| session key | `agent:main:openai:850603e8-9a3c-412f-a1cc-6729dfe6abdb` |
| 会话记录的提供方 | `volcengine-agent-plan`，API `openai-completions` |
| 配置模型 / 响应模型 | `deepseek-v4.1-flash` / `deepseek-v4-1-flash-261017` |
| 会话记录的提供方 responseId | `02179248040107076e706cca72a7c4abc35179fe35310d8ea2d06` |

MIC 中名为 `provider_request_id` 的字段实际来自 SDK `resp.id`；本次对应 Gateway runId，
不是提供方原始响应 ID，也不是 HTTP `x-request-id`。MIC 记录的 `openclaw/main` 是网关路由别名。
本次会话中的 `__openclaw.runId` 与该 ID 精确一致，因此关联不依赖仅按时间猜测。

读取的是远端 `/home/yu/.openclaw/agents/main/agent/openclaw-agent.sqlite`，
使用 SQLite `mode=ro` 和 `query_only=ON`。只解码目标 session 的 seq=5：
Zstandard 压缩记录 6600 字节，解码 23417 字节，与数据库记录长度一致。
仅导出标识、状态、长度、SHA 和验证结论；未复制整个数据库、提示词或推理内容。

| 已观察到的边界 | 北京时间 2026-10-20 | 正文 |
| --- | --- | --- |
| 持久化 assistant 消息 | 15:14:23.481 | 唯一 text block，已缺括号 |
| Gateway 启动日志第 66 行 | 15:14:23.508 | 与该 text block 完全一致 |
| Gateway 日志第 67 行 | 15:14:23.509 | 同一 runId，Agent 终态 `stopReason=stop` |
| MIC SDK / trace | 15:14:23 收到响应 | 与上述正文完全一致 |

三处正文均为 **13011 字符、21297 UTF-8 字节**，SHA256 均为
`ab48399f0c20599a938869d7b8c9593820e56bf120996cdc45e347612c7dccc1`。
解码失败位置仍为字符偏移 13011、第 1 行第 13012 列。
因此可排除“在已持久化 assistant 之后的 Gateway 输出、客户端读取、MIC 清洗或 trace 保存阶段，
才首次丢掉末尾括号”这一解释。

相关脚本与原始核验输出：
[会话检索](../logs/acceptance/json_upstream_audit_20261021_200457/target_session_metadata.json)、
[单条消息核验](../logs/acceptance/json_upstream_audit_20261021_200457/inspect_final_boundary.py)、
[Gateway 日志定位](../logs/acceptance/json_upstream_audit_20261021_200457/gateway_target_matches.json)。

## 尚缺的证据及不能推出的结论

Gateway 日志第 65 行记录 15:13:22.041 收到提供方 HTTP 200、`text/event-stream`，
但常规日志没有 SSE 分片原件或提供方原始终止帧。
目前 `/home/yu/.openclaw/state/openclaw.sqlite` 的 `capture_sessions`、`capture_events`、
`capture_blobs` 三表均为 0 行；窗口检索也没有记录。
启动日志指定的 **`/tmp/openclaw/openclaw-2026-10-20.log` 当前不存在**。
`/home/yu/.openclaw/logs/` 仅发现 `gateway-restart.log`，未读取其无关正文。
这说明在已核查位置没有可用的原始 HTTP/SSE 记录，不证明当时必然关闭 capture，也不证明记录从未存在。

会话记录的 `stopReason=stop` 和提供方 `responseId` 仍处于解析之后，不能替代原始流。
当前安装的 OpenClaw 2026.9.6 代码还显示：普通非流式客户端响应会把 Agent 的 `length` 映射为
`finish_reason=length`，其他正常文本终态映射为 `stop`。因此 MIC 的 stop 本身不是提供方终止帧证据。

MIC 请求上限 65536、实际 output_tokens 11308、reasoning_tokens 6136 均保留；
11308 不能全部解释为最终 JSON 正文 token，也不能证明提供方实际收到并执行了 65536 上限。
MIC 日志中的 HTTP `Content-Length=23035` 包含 ChatCompletion 外层封装及转义，
与正文的 21297 字节属于不同层，差值不代表丢失字符。

本次取回的 4 份 OpenClaw 代码快照及 SHA 见
[不变性与快照清单](../logs/acceptance/json_upstream_audit_20261021_200457/invariants.json)。
它们是核查时安装版本，不是 B6 冻结代码；静态逻辑用于解释边界、规划取证，不能补造 B6 原始请求。
其中 `openai-http-Gs9PjNdV.mjs` 将 response format/token 上限传给内部 streamParams，
但本次没有提供方实际请求字段，不能断言这些参数当时已传达、被支持或被忽略。

## 下一步的有界取证方案（尚未实施）

先做**零真实调用的取证通路验证**，然后才评估是否具备新真实批次的依据。
不重跑 B6，不直接重发旧 call04，不以同一输入反复采样等待缺括号复现。

1. 在提供方响应进入 OpenClaw SSE 规范化/SDK 解析之前记录受限诊断，并通过 runId/sessionId/
   provider responseId 与既有 MIC trace 关联。先确认这一位置实际可用，不修改 MIC 业务审核。
2. 请求仅保留 model、stream、max_tokens/max_completion_tokens、response_format、stop 等非秘密参数；
   不保存完整 system prompt、配置、Authorization、Cookie 或完整请求体。
3. 响应保留状态、content-type、提供方 ID、顺序/字节数、最终 finish_reason、usage、结束标记或读流异常；
   单独提取可见 `delta.content` 并核对其拼接 SHA、持久化 assistant SHA 和 MIC SHA。
   不保存 reasoning 分片；经过筛选的记录必须明确标记，不能冒称完整原始 wire。
4. 日志仍使用原响应 2MiB、每 MIC run 总量 32MiB 的额度，共享核算、不额外叠加一套额度。
   超限则标记取证不完整，不扩大预算；日志故障不能修改模型结果或触发额外调用。
5. 先用本地固定 SSE 样本验证完整 JSON、缺末括号、UTF-8 跨片、异常终止、超限和秘密字段排除；
   成功标准是可见文本不变、边界诊断可关联、无额外请求、准入仍由原 parser 和统一审核执行。
6. 若后续进入真实取证，纳入另行登记的正常 Agent 独立批次，沿用原任务数、调用预算和 1800 秒停止条件；
   每次已有调用最多记录一次，不增加调用。未复现或证据不足均如实收尾，不循环尝试。

当前安装的 `fetch-guard-EFfAF2PS.mjs:350` 显示，`OPENCLAW_DEBUG_PROXY_ENABLED`
为 `1/true/yes/on` 且未显式关闭 capture 时，会加载既有 capture runtime；其插入点早于 SSE 规范化。
但该接口会接触请求 headers/body，落盘过滤和限额还需验证，**本轮未启用该全局开关**。
不能直接用全局 HTTP 抓取替代上述受限记录；若无法满足既有范围与配额，停止真实取证准备并报告缺口。

本轮核验 399 个本地源文件 SHA 未变，B6 封存 ZIP SHA 未变；没有新测试、真实调用或服务操作。
原远端 220 项定向测试结果仍有效，但不能替代缺失的上游原始响应证据。
