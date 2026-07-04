from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import make_idempotency_key, new_id
from .queue import SQLiteMessageQueue
from .tickets import TicketRepository


class DailyReportBuilder:
    def __init__(
        self,
        data_store: SQLiteStore,
        output_dir: str | Path,
        agent_id: str,
        bus_store: SQLiteStore | None = None,
        state_store: SQLiteStore | None = None,
        queue: SQLiteMessageQueue | None = None,
    ):
        self.data_store = data_store
        self.bus_store = bus_store or data_store
        self.state_store = state_store or data_store
        self.output_dir = Path(output_dir)
        self.agent_id = agent_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tickets = TicketRepository(self.bus_store)
        self.queue = queue

    def build(self, *, trade_date: str, output_format: str = "both") -> dict[str, Any]:
        summary = self._summary(trade_date)
        report_id = f"collection_report_{trade_date.replace('-', '')}"
        json_path = self.output_dir / f"{report_id}.json"
        html_path = self.output_dir / f"{report_id}.html"
        written_json = written_html = None
        if output_format in {"json", "both"}:
            json_path.write_text(dumps_json(summary), encoding="utf-8")
            written_json = str(json_path)
        if output_format in {"html", "both"}:
            html_path.write_text(self._html(summary), encoding="utf-8")
            written_html = str(html_path)
        with self.data_store.session() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO daily_collection_reports(report_id, trade_date, summary_json, html_path, json_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (report_id, trade_date, dumps_json(summary), written_html, written_json),
            )
        ticket_id = self.tickets.create_ticket(
            ticket_type="COLLECTION_REPORT_TICKET",
            source_agent=self.agent_id,
            target_agent_group="owner_and_chief_analyst",
            priority="normal",
            summary_cn=f"{trade_date} 情报采集日报已生成。",
            payload=summary,
            payload_ref=f"db://daily_collection_reports/{report_id}",
            idempotency_key=f"collection_report:{trade_date}:v1",
        )
        message_id = None
        if self.queue is not None:
            message_id = self.queue.publish(
                "report.collection_daily",
                {"ticket_id": ticket_id, "ticket_type": "COLLECTION_REPORT_TICKET", "trade_date": trade_date, "report_id": report_id},
                priority=10,
                idempotency_key=make_idempotency_key("message", "collection_report", report_id),
            )
        return {
            "report_id": report_id,
            "json_path": written_json,
            "html_path": written_html,
            "ticket_id": ticket_id,
            "message_id": message_id,
            "summary": summary,
        }

    def _summary(self, trade_date: str, *, top_n: int = 10) -> dict[str, Any]:
        with self.data_store.session() as con:
            tasks_total = con.execute("SELECT COUNT(*) c FROM collection_tasks WHERE date(created_at)=date(?)", (trade_date,)).fetchone()["c"]
            runs = con.execute(
                "SELECT tool_name, status, COUNT(*) c FROM collection_runs WHERE date(created_at)=date(?) GROUP BY tool_name, status",
                (trade_date,),
            ).fetchall()
            events_count = con.execute("SELECT COUNT(*) c FROM structured_events WHERE date(created_at)=date(?)", (trade_date,)).fetchone()["c"]
            features_count = con.execute("SELECT COUNT(*) c FROM market_features WHERE date(created_at)=date(?)", (trade_date,)).fetchone()["c"]
            issues = con.execute("SELECT severity, COUNT(*) c FROM data_quality_issues WHERE date(created_at)=date(?) GROUP BY severity", (trade_date,)).fetchall()
            gaps_count = con.execute("SELECT COUNT(*) c FROM coverage_gaps WHERE date(created_at)=date(?)", (trade_date,)).fetchone()["c"]
            top_events = con.execute(
                """
                SELECT target_id, ticker, event_type, event_date, summary_cn, confidence, data_quality
                FROM structured_events WHERE date(created_at)=date(?)
                ORDER BY COALESCE(confidence, 0) DESC, created_at DESC LIMIT ?
                """,
                (trade_date, top_n),
            ).fetchall()
            top_features = con.execute(
                """
                SELECT ticker, feature_window, bucket_start, abnormality_score, summary_cn
                FROM market_features WHERE date(created_at)=date(?)
                ORDER BY COALESCE(abnormality_score, 0) DESC LIMIT ?
                """,
                (trade_date, top_n),
            ).fetchall()
            issue_details = con.execute(
                """
                SELECT severity, issue_type, ticker, summary_cn, status
                FROM data_quality_issues WHERE date(created_at)=date(?)
                ORDER BY severity ASC, created_at DESC LIMIT ?
                """,
                (trade_date, top_n * 2),
            ).fetchall()
            gap_details = con.execute(
                """
                SELECT target_id, ticker, priority, status, description
                FROM coverage_gaps WHERE date(created_at)=date(?)
                ORDER BY created_at DESC LIMIT ?
                """,
                (trade_date, top_n),
            ).fetchall()
            demand_rows = con.execute(
                "SELECT demand_id, demand_type, status, priority FROM collection_demands ORDER BY created_at ASC"
            ).fetchall()
            demand_task_rows = con.execute(
                """
                SELECT demand_id, status, COUNT(*) c FROM collection_tasks
                WHERE date(created_at)=date(?) GROUP BY demand_id, status
                """,
                (trade_date,),
            ).fetchall()
            run_cost_rows = con.execute(
                """
                SELECT tool_name, operation, status, COUNT(*) c FROM collection_runs
                WHERE date(created_at)=date(?) GROUP BY tool_name, operation, status
                """,
                (trade_date,),
            ).fetchall()
            mic_quality_rows = con.execute(
                """
                SELECT quality_json FROM collection_runs
                WHERE date(created_at)=date(?) AND tool_name='market_intelligence_collector'
                """,
                (trade_date,),
            ).fetchall()
            failed_tasks = con.execute(
                """
                SELECT task_type, ticker, status FROM collection_tasks
                WHERE date(created_at)=date(?) AND status='failed'
                ORDER BY created_at DESC LIMIT ?
                """,
                (trade_date, top_n),
            ).fetchall()
            open_gaps = con.execute(
                "SELECT target_id, ticker, description FROM coverage_gaps WHERE status='open' ORDER BY created_at DESC LIMIT ?",
                (top_n,),
            ).fetchall()
        with self.bus_store.session() as con:
            tickets = con.execute(
                "SELECT ticket_type, COUNT(*) c FROM tickets WHERE date(created_at)=date(?) GROUP BY ticket_type",
                (trade_date,),
            ).fetchall()
            faults = con.execute(
                """
                SELECT ticket_id, status, summary_cn FROM tickets
                WHERE ticket_type='FAULT_TICKET' AND date(created_at)=date(?)
                ORDER BY created_at DESC LIMIT ?
                """,
                (trade_date, top_n),
            ).fetchall()
            message_stats = con.execute(
                "SELECT topic, status, COUNT(*) c FROM messages WHERE date(created_at)=date(?) GROUP BY topic, status",
                (trade_date,),
            ).fetchall()
            dead_messages = con.execute(
                "SELECT message_id, topic, error_json FROM messages WHERE status='dead' AND date(created_at)=date(?) LIMIT ?",
                (trade_date, top_n),
            ).fetchall()
        with self.state_store.session() as con:
            capability = con.execute(
                "SELECT tool_name, status, checked_at, capabilities_json FROM tool_capabilities ORDER BY checked_at DESC LIMIT 5"
            ).fetchall()
            breakers = con.execute("SELECT tool_name, status, consecutive_failures, cooldown_until FROM circuit_breakers").fetchall()
        return {
            "report_type": "COLLECTION_DAILY_REPORT",
            "trade_date": trade_date,
            "tasks_total": tasks_total,
            "runs_by_tool_status": [dict(r) for r in runs],
            "tickets_created": [dict(r) for r in tickets],
            "message_stats": [dict(r) for r in message_stats],
            "demand_coverage": _demand_coverage(demand_rows, demand_task_rows),
            "events_created": events_count,
            "market_features_created": features_count,
            "data_quality_issues": [dict(r) for r in issues],
            "coverage_gaps_created": gaps_count,
            "top_events": [dict(r) for r in top_events],
            "top_market_features": [dict(r) for r in top_features],
            "data_quality_issue_details": [dict(r) for r in issue_details],
            "coverage_gap_details": [dict(r) for r in gap_details],
            "fault_tickets": [dict(r) for r in faults],
            "tool_capability_checks": [
                dict(r) | {"capabilities": loads_json(r["capabilities_json"], {})} for r in capability
            ],
            "circuit_breakers": [dict(r) for r in breakers],
            "cost_usage": _cost_usage(run_cost_rows, mic_quality_rows),
            "followup_suggestions": _followup_suggestions(open_gaps, failed_tasks, dead_messages),
        }

    def _html(self, summary: dict[str, Any]) -> str:
        def table(rows: list[dict[str, Any]]) -> str:
            if not rows:
                return "<p>无</p>"
            keys = list(rows[0].keys())
            head = "".join(f"<th>{html.escape(str(k))}</th>" for k in keys)
            body = "".join(
                "<tr>" + "".join(f"<td>{html.escape(str(row.get(k, '')))}</td>" for k in keys) + "</tr>"
                for row in rows
            )
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

        def bullet_list(items: list[Any]) -> str:
            if not items:
                return "<p>无</p>"
            return "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items) + "</ul>"

        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>情报采集日报 {html.escape(summary['trade_date'])}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 32px; line-height: 1.5; }}
h1 {{ margin-bottom: 0.2em; }}
.card {{ border: 1px solid #ddd; border-radius: 10px; padding: 16px; margin: 16px 0; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
th {{ background: #f5f5f5; }}
.metric {{ display: inline-block; margin-right: 24px; }}
</style>
</head>
<body>
<h1>情报采集日报</h1>
<p>交易日：{html.escape(summary['trade_date'])}</p>
<div class="card">
  <span class="metric">任务数：<strong>{summary['tasks_total']}</strong></span>
  <span class="metric">结构化事件：<strong>{summary['events_created']}</strong></span>
  <span class="metric">行情特征：<strong>{summary['market_features_created']}</strong></span>
  <span class="metric">覆盖缺口：<strong>{summary['coverage_gaps_created']}</strong></span>
</div>
<h2>工具运行</h2>
{table(summary['runs_by_tool_status'])}
<h2>Ticket 统计</h2>
{table(summary['tickets_created'])}
<h2>工具能力验证</h2>
{table([{k: v for k, v in row.items() if k != 'capabilities_json'} for row in summary.get('tool_capability_checks', [])])}
<h2>熔断状态</h2>
{table(summary.get('circuit_breakers', []))}
<h2>结构化事件 Top N</h2>
{table(summary.get('top_events', []))}
<h2>行情特征 Top N</h2>
{table(summary.get('top_market_features', []))}
<h2>数据质量问题（按严重度）</h2>
{table(summary.get('data_quality_issue_details', []) or summary['data_quality_issues'])}
<h2>覆盖缺口</h2>
{table(summary.get('coverage_gap_details', []))}
<h2>故障 Ticket</h2>
{table(summary.get('fault_tickets', []))}
<h2>Demand 覆盖情况</h2>
{table(summary.get('demand_coverage', []))}
<h2>Message 处理统计</h2>
{table(summary.get('message_stats', []))}
<h2>成本与调用次数</h2>
{table(summary.get('cost_usage', {}).get('calls_by_tool_operation', []))}
<p>MIC 预算使用：查询 {summary.get('cost_usage', {}).get('mic_budget_usage', {}).get('queries_executed', 0)} 次，
读取链接 {summary.get('cost_usage', {}).get('mic_budget_usage', {}).get('links_read', 0)} 个，
模型调用 {summary.get('cost_usage', {}).get('mic_budget_usage', {}).get('model_calls', 0)} 次。</p>
<h2>次日补采建议</h2>
{bullet_list(summary.get('followup_suggestions', []))}
</body></html>"""


def _demand_coverage(demand_rows, demand_task_rows) -> list[dict[str, Any]]:
    """Per-demand task coverage for the trade date (design §15.2 item 2)."""
    per_demand: dict[str, dict[str, int]] = {}
    for row in demand_task_rows:
        counts = per_demand.setdefault(str(row["demand_id"]), {})
        counts[str(row["status"])] = int(row["c"])
    out = []
    for row in demand_rows:
        demand_id = str(row["demand_id"])
        counts = per_demand.get(demand_id, {})
        out.append(
            {
                "demand_id": demand_id,
                "demand_type": row["demand_type"],
                "demand_status": row["status"],
                "priority": row["priority"],
                "tasks_planned": sum(counts.values()),
                "tasks_done": counts.get("done", 0),
                "tasks_failed": counts.get("failed", 0),
                "tasks_open": counts.get("open", 0) + counts.get("in_progress", 0),
            }
        )
    return out


def _cost_usage(run_cost_rows, mic_quality_rows) -> dict[str, Any]:
    """Tool call counts plus MIC budget usage summed from run quality payloads."""
    mic_usage = {"queries_executed": 0, "links_read": 0, "model_calls": 0}
    for row in mic_quality_rows:
        quality = loads_json(row["quality_json"], {}) or {}
        for key in mic_usage:
            try:
                mic_usage[key] += int(quality.get(key) or 0)
            except (TypeError, ValueError):
                continue
    return {
        "calls_by_tool_operation": [dict(r) for r in run_cost_rows],
        "total_tool_calls": sum(int(r["c"]) for r in run_cost_rows),
        "mic_budget_usage": mic_usage,
    }


def _followup_suggestions(open_gaps, failed_tasks, dead_messages) -> list[str]:
    """Next-day recollection suggestions (design §15.2 item 12)."""
    suggestions: list[str] = []
    for row in open_gaps:
        target = row["ticker"] or row["target_id"] or "未知目标"
        suggestions.append(f"补采覆盖缺口：{target} —— {str(row['description'])[:120]}")
    for row in failed_tasks:
        suggestions.append(f"重跑失败任务：{row['ticker'] or '未知标的'} 的 {row['task_type']}。")
    for row in dead_messages:
        error = loads_json(row["error_json"], {}) or {}
        suggestions.append(
            f"处理 dead-letter 消息 {row['message_id']}（topic={row['topic']}，error={error.get('error_code', 'UNKNOWN')}）。"
        )
    return suggestions
