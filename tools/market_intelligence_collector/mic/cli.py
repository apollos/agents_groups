"""MIC command-line interface."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from mic.api import AnalystAPI
from mic.config import load_config
from mic.logging_utils import configure_logging

app = typer.Typer(add_completion=False, help="MIC - Market Intelligence Collector")
console = Console()


@app.callback()
def _main(
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG/INFO/WARNING"),
    log_file: str = typer.Option("mic.log", "--log-file", help="Log filename under logs/"),
) -> None:
    """Configure logging for all subcommands (logs go to ./logs/)."""
    configure_logging(log_file=log_file, level=log_level, console=False)


def _api() -> AnalystAPI:
    return AnalystAPI(load_config())


@app.command()
def targets() -> None:
    """List configured target profiles."""
    cfg = load_config()
    table = Table(title="Target Profiles")
    table.add_column("target_id")
    table.add_column("type")
    table.add_column("canonical_name")
    for tid, spec in cfg.target_profiles.items():
        table.add_row(tid, spec.get("type", ""), spec.get("canonical_name", ""))
    console.print(table)


@app.command()
def collect(
    target_id: str = typer.Argument(..., help="Target id from target_profiles.yaml"),
    focus: str = typer.Option(
        "operating_update,customer_change,supply_chain,policy,risk",
        help="Comma-separated focus areas"),
    time_window: str = typer.Option("30d", help="e.g. 7d, 30d, 90d"),
    max_queries: int = typer.Option(80),
    max_search_hits: int = typer.Option(800, "--max-search-hits", help="Maximum raw search hits to persist/process"),
    max_links: int = typer.Option(40),
    max_model_calls: int = typer.Option(30),
    json_out: bool = typer.Option(False, "--json", help="Print raw JSON report"),
) -> None:
    """Run a full collection pipeline for a target."""
    task_profile = {
        "focus": [f.strip() for f in focus.split(",") if f.strip()],
        "time_window": time_window,
        "budget_profile": {
            "max_queries": max_queries, "max_search_hits": max_search_hits,
            "max_links_to_read": max_links, "max_model_calls": max_model_calls,
        },
    }
    api = _api()
    with console.status(f"Collecting intelligence for {target_id}..."):
        report = api.collect_intelligence(target_id, task_profile)

    if json_out:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    _print_report(report)


@app.command()
def events(target_id: str, since: str = "30d", min_confidence: float = 0.0) -> None:
    """Show recent events for a target."""
    rows = _api().get_recent_events(target_id, since=since, min_confidence=min_confidence)
    table = Table(title=f"Recent events: {target_id}")
    table.add_column("type")
    table.add_column("summary")
    table.add_column("conf", justify="right")
    for r in rows[:30]:
        table.add_row(r["event_type"], (r["summary"] or "")[:60], f"{r['confidence']:.2f}")
    console.print(table)


@app.command()
def relations(target_id: str, since: str = "180d") -> None:
    """Show relation records for a target."""
    rows = _api().get_relations(target_id, since=since)
    table = Table(title=f"Relations: {target_id}")
    table.add_column("subject")
    table.add_column("relation")
    table.add_column("object")
    table.add_column("conf", justify="right")
    for r in rows[:30]:
        subj = (r["subject_entity"] or {}).get("name", "")
        obj = (r["object_entity"] or {}).get("name", "")
        table.add_row(subj, r["relation_type"], obj, f"{r['confidence']:.2f}")
    console.print(table)


@app.command()
def questions(target_id: str, priority: str | None = None) -> None:
    """Show open analyst questions for a target."""
    rows = _api().get_analyst_questions(target_id, priority=priority, status="open")
    for r in rows[:30]:
        console.print(f"[bold]{r['priority']}[/bold] {r['question']}")
        if r.get("reason"):
            console.print(f"    [dim]{r['reason']}[/dim]")


@app.command()
def gaps(target_id: str, priority: str | None = None) -> None:
    """Show open coverage gaps for a target."""
    rows = _api().get_coverage_gaps(target_id, priority=priority, status="open")
    table = Table(title=f"Coverage gaps: {target_id}")
    table.add_column("priority")
    table.add_column("type")
    table.add_column("description")
    for r in rows[:30]:
        table.add_row(r["priority"] or "", r["gap_type"] or "",
                      (r["description"] or "")[:60])
    console.print(table)


@app.command()
def explain(source_link_id: str) -> None:
    """Explain why a source was selected and what was extracted."""
    console.print_json(json.dumps(_api().explain_source_analysis(source_link_id),
                                  ensure_ascii=False))


def _print_report(report: dict) -> None:
    s = report["summary"]
    console.print(f"\n[bold]Batch Report[/bold] — {report['target']} "
                  f"(window={report['time_window']})")
    console.print(f"run_id: {report['search_run_id']}")
    sm = Table(show_header=False, box=None)
    for k, v in s.items():
        sm.add_row(k, str(v))
    console.print(sm)

    so = Table(title="Structured outputs")
    so.add_column("object")
    so.add_column("count", justify="right")
    for k, v in report["structured_outputs"].items():
        so.add_row(k, str(v))
    console.print(so)

    if report["top_events"]:
        te = Table(title="Top events")
        te.add_column("summary")
        te.add_column("channels")
        te.add_column("conf", justify="right")
        for e in report["top_events"]:
            te.add_row((e["summary"] or "")[:60], ",".join(e.get("impact_channels", [])),
                       f"{e.get('confidence', 0):.2f}")
        console.print(te)


if __name__ == "__main__":
    app()
