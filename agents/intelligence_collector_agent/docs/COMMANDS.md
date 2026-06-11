# CLI 命令速查（V0.5.1）

```bash
# 初始化三类 SQLite store（state / bus / data）
intel-agent --config config/intelligence_collector.yaml init-db

# 校验 OpenClaw 模型
intel-agent --config config/intelligence_collector.yaml openclaw validate-model

# 生成 OpenClaw agent.md / SKILL.md / config patch
intel-agent --config config/intelligence_collector.yaml openclaw render-artifacts --output-dir build/openclaw

# 注册 Demand
intel-agent --config config/intelligence_collector.yaml demand register --file examples/demands/held_sellable_10m.json --activate

# Demand 生命周期
intel-agent --config config/intelligence_collector.yaml demand suspend --demand-id demand_xxx
intel-agent --config config/intelligence_collector.yaml demand resume --demand-id demand_xxx
intel-agent --config config/intelligence_collector.yaml demand cancel --demand-id demand_xxx

# Runtime tick
intel-agent --config config/intelligence_collector.yaml runtime tick --now "2026-06-11T10:30:00+08:00"

# 崩溃恢复（requeue 过期 lease、补 ack 已完成消息、dead letter 生成 FAULT_TICKET、补发孤儿 Ticket 消息）
intel-agent --config config/intelligence_collector.yaml runtime recover

# 查看心跳
intel-agent --config config/intelligence_collector.yaml runtime heartbeat --limit 10

# 运行 Agent
intel-agent --config config/intelligence_collector.yaml agent run-once
intel-agent --config config/intelligence_collector.yaml agent run-until-idle --max-messages 100

# 一屏运行状态总览（state/bus/data 路径 / session / checkpoint / 心跳 / 队列深度 / 熔断）
intel-agent --config config/intelligence_collector.yaml agent status

# 恢复后继续运行（recover + run-until-idle）
intel-agent --config config/intelligence_collector.yaml agent resume

# 工具能力验证
intel-agent --config config/intelligence_collector.yaml tools verify-capabilities

# 查看队列和票据
intel-agent --config config/intelligence_collector.yaml queue list --status open
intel-agent --config config/intelligence_collector.yaml queue inspect --message-id msg_xxx
intel-agent --config config/intelligence_collector.yaml queue retry --message-id msg_xxx
intel-agent --config config/intelligence_collector.yaml queue dead-letter
intel-agent --config config/intelligence_collector.yaml ticket list --status open

# 读取结果
intel-agent --config config/intelligence_collector.yaml read collection-status --demand-id test_demand_held_sellable_10m_001
intel-agent --config config/intelligence_collector.yaml read events --ticker 300750.SZ
intel-agent --config config/intelligence_collector.yaml read market-features --ticker 300750.SZ --window 10m
intel-agent --config config/intelligence_collector.yaml read data-quality --status open
intel-agent --config config/intelligence_collector.yaml read capabilities

# 生成 HTML 日报
intel-agent --config config/intelligence_collector.yaml report daily --trade-date 2026-06-11
```
