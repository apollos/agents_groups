from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .config import CollectorConfig


class OpenClawModelValidator:
    """Validate that the configured brain model is visible to OpenClaw.

    OpenClaw model refs should be provider/model strings. This validator is intentionally optional:
    if the openclaw CLI is not on PATH, it returns a warning instead of blocking local unit tests.
    """

    def __init__(self, config: CollectorConfig):
        self.config = config

    def validate(self) -> dict[str, Any]:
        model = self.config.model.primary
        if self.config.model.allow_openclaw_default and not model:
            return {"status": "skipped", "reason": "allow_openclaw_default=true"}
        if not self.config.model.require_registered:
            return {"status": "skipped", "reason": "require_registered=false", "model": model}
        try:
            proc = subprocess.run(
                ["openclaw", "models", "list", "--plain"],
                text=True,
                capture_output=True,
                timeout=20,
            )
        except FileNotFoundError:
            return {
                "status": "warning",
                "model": model,
                "reason": "openclaw CLI not found; validation must run inside OpenClaw host",
            }
        except Exception as exc:
            return {"status": "warning", "model": model, "reason": str(exc)}
        if proc.returncode != 0:
            return {"status": "failed", "model": model, "stderr": proc.stderr[-1000:]}
        models = [line.strip().lower() for line in proc.stdout.splitlines() if line.strip()]
        ok = model.lower() in models or any(_model_matches(model.lower(), m) for m in models)
        return {"status": "success" if ok else "failed", "model": model, "available_count": len(models)}


def _model_matches(model: str, listed: str) -> bool:
    return model == listed or listed.endswith("/" + model)


class OpenClawArtifactRenderer:
    def __init__(self, config: CollectorConfig):
        self.config = config

    def render(self, output_dir: str | Path) -> dict[str, str]:
        out = Path(output_dir)
        agent_dir = out / "openclaw_agent"
        skill_dir = agent_dir / "skills" / "intelligence-collector"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent.md").write_text(self.agent_md(), encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(self.skill_md(), encoding="utf-8")
        (agent_dir / "openclaw_config_patch.json").write_text(
            json.dumps(self.config_patch(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "agent_md": str(agent_dir / "agent.md"),
            "skill_md": str(skill_dir / "SKILL.md"),
            "config_patch": str(agent_dir / "openclaw_config_patch.json"),
        }

    def config_patch(self) -> dict[str, Any]:
        # This is a patch/snippet, not a full OpenClaw config replacement.
        return {
            "agents": {
                "list": [
                    {
                        "id": self.config.runtime.agent_id,
                        "name": "A股情报收集员",
                        "model": {
                            "primary": self.config.model.primary,
                            "fallbacks": self.config.model.fallbacks,
                        },
                        "skills": {"allow": ["intelligence-collector"]},
                    }
                ]
            }
        }

    def agent_md(self) -> str:
        return f"""# A股情报收集员 Agent

你是 Agent交易公司的情报收集员 Agent，只负责采集、结构化、质量检查、Ticket 输出和日报，不输出买卖建议、仓位、目标价或交易指令。

## 独立状态

- agent_id: `{self.config.runtime.agent_id}`
- State SQLite: `{self.config.runtime.state_sqlite_path}`（本 Agent 私有 memory/checkpoint/session）
- Bus SQLite: `{self.config.runtime.bus_sqlite_path}`（跨 Agent 消息与 Ticket Bus，可共享）
- Data SQLite: `{self.config.runtime.data_sqlite_path}`（采集结果、事件、特征、日报，可共享）
- Memory、Checkpoint、Session 均使用该 agent_id 隔离，不读取其他 Agent 的 memory。

## 模型

本 Agent 不应静默继承 OpenClaw default 模型。当前配置模型：`{self.config.model.primary}`。
如需调整，请修改 intelligence collector YAML 中的 `openclaw.model.primary`，并确认该模型已在 OpenClaw 注册。

## 运行边界

1. 只消费目标为本 Agent 或本 Agent group 的消息。
2. 所有需求从 Demand Registry 编译为 Ticket 后执行。
3. 所有跨 Agent 协作通过消息队列和 Ticket 完成。
4. 工具调用使用真实 MIC 和真实 stock_data_collector；不要在生产路径中使用 mock。
5. Token、Cookie、API Key 不得写入回复、日志或 Memory。
"""

    def skill_md(self) -> str:
        return f"""---
name: intelligence-collector
description: A股情报收集员 Agent 的 CLI 工作流。用于注册 Demand、运行 Agent、验证工具能力、读取状态和生成日报。
---

# 情报收集员 Skill

## 常用命令

初始化数据库：

```bash
intel-agent --config <config.yaml> init-db
```

注册 Demand：

```bash
intel-agent --config <config.yaml> demand register --file demand.json --activate
```

触发 Runtime tick：

```bash
intel-agent --config <config.yaml> runtime tick --now 2026-06-11T10:30:00+08:00
```

运行一次 Agent：

```bash
intel-agent --config <config.yaml> agent run-once
```

运行到队列空：

```bash
intel-agent --config <config.yaml> agent run-until-idle
```

验证 stock_data_collector 盘中能力：

```bash
intel-agent --config <config.yaml> tools verify-capabilities
```

生成日报：

```bash
intel-agent --config <config.yaml> report daily --trade-date 2026-06-11
```

## 操作纪律

- 不要直接调用交易或下单能力。
- 不要绕过验证码、付费墙、Cookie 风控。
- 不要把外部工具失败当作事实缺失；要生成 DATA_QUALITY_TICKET 或 FAULT_TICKET。
- 所有事实结果必须有来源引用或落库 run。
"""
