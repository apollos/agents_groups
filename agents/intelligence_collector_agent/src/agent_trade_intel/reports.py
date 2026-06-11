from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .db import SQLiteStore, dumps_json, loads_json
from .ids import new_id
from .tickets import TicketRepository


class DailyReportBuilder:
    def __init__(
        self,
        data_store: SQLiteStore,
        output_dir: str | Path,
        agent_id: str,
        bus_store: SQLiteStore | None = None,
        state_store: SQLiteStore | None = None,
    ):
        self.data_store = data_store
        self.bus_store = bus_store or data_store
        self.state_store = state_store or data_store
        self.output_dir = Path(output_dir)
        self.agent_id = agent_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tickets = TicketRepository(self.bus_store)

    def build(self, *, trade_date: str) -> dict[str, Any]:
        summary = self._summary(trade_date)
        report_id = f"collection_report_{trade_date.replace('-', '')}"
        json_path = self.output_dir / f"{report_id}.json"
        html_path = self.output_dir / f"{report_id}.html"
        json_path.write_text(dumps_json(summary), encoding="utf-8")
        html_path.write_text(self._html(summary), encoding="utf-8")
        with self.data_store.session() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO daily_collection_reports(report_id, trade_date, summary_json, html_path, json_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (report_id, trade_date, dumps_json(summary), str(html_path), str(json_path)),
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
        return {"report_id": report_id, "json_path": str(json_path), "html_path": str(html_path), "ticket_id": ticket_id, "summary": summary}

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
                SELECT ticker, event_type, event_date, summary_cn, confidence, data_quality
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
                SELECT ticker, priority, status, description
                FROM coverage_gaps WHERE date(created_at)=date(?)
                ORDER BY created_at DESC LIMIT ?
                """,
                (trade_date, top_n),
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
</body></html>"""
