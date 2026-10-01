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


# --- browser runtime (design 14) ------------------------------------------------

browser_app = typer.Typer(help="Local windowed-browser runtime: doctor / setup / cookies")
cookies_app = typer.Typer(help="User-authorised cookie fallback (metadata only; values never printed)")
browser_app.add_typer(cookies_app, name="cookies")
search_app = typer.Typer(help="Search-only probes (no body read, no model call)")
reader_app = typer.Typer(help="Single-page body scope probes (no model call)")
app.add_typer(browser_app, name="browser")
app.add_typer(search_app, name="search")
app.add_typer(reader_app, name="reader")


def _emit(payload: dict, json_out: bool) -> None:
    if json_out:
        console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
        return
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str, indent=1))


def _fail(code: str, message: str, exit_code: int = 2) -> None:
    console.print(f"[red]{code}[/red]: {message}")
    raise typer.Exit(code=exit_code)


@browser_app.command("doctor")
def browser_doctor(
    launch: bool = typer.Option(False, "--launch", help="Open the dedicated profile on about:blank and close it"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Check Python/Playwright/Edge/GUI/profile/config. No web access, no model calls."""
    from mic.browser.config import ConfigError
    from mic.browser.doctor import run_doctor

    try:
        cfg = load_config()
    except ConfigError as exc:
        _fail("config_error", str(exc))
        return
    result = run_doctor(cfg, launch=launch)
    _emit(result, json_out)
    if not result.get("ok"):
        raise typer.Exit(code=1)


@browser_app.command("setup")
def browser_setup(
    engine: str = typer.Option(None, "--engine", help="bing | google | baidu"),
    url: str = typer.Option(None, "--url", help="Allowed site to open for manual login/consent"),
    max_seconds: float = typer.Option(1800, "--max-seconds", help="Hard cap on the interactive session"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Open the MIC-only Edge profile for manual interaction; closes normally afterwards."""
    from mic.browser.config import ConfigError
    from mic.browser.profile_lock import ProfileBusy
    from mic.browser.session import BrowserUnavailable
    from mic.browser.setup import run_setup

    try:
        result = run_setup(load_config(), engine=engine, url=url, max_seconds=max_seconds)
    except ConfigError as exc:
        _fail("config_error", str(exc))
        return
    except ProfileBusy as exc:
        _fail("profile_busy", str(exc), exit_code=3)
        return
    except BrowserUnavailable as exc:
        _fail(exc.code, str(exc))
        return
    _emit(result, json_out)


@cookies_app.command("import")
def cookies_import(
    origin: str = typer.Option(..., "--origin", help="e.g. https://www.bing.com"),
    file: str = typer.Option(None, "--file", help="playwright_cookies_json_v1 file (defaults to source_file_env)"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Validate and register a user-provided cookie file. No web access; values are never printed."""
    from pathlib import Path

    from mic.browser.config import ConfigError
    from mic.browser.session_fallback import CookieImportError, SessionStore

    try:
        store = SessionStore.from_runtime(load_config().browser_runtime)
        meta = store.import_cookies(origin, Path(file) if file else None)
    except (ConfigError, CookieImportError) as exc:
        _fail(getattr(exc, "code", "config_error"), str(exc))
        return
    _emit(meta.public(), json_out)


@cookies_app.command("status")
def cookies_status(json_out: bool = typer.Option(False, "--json")) -> None:
    """Show non-sensitive metadata of registered credentials."""
    from mic.browser.config import ConfigError
    from mic.browser.session_fallback import SessionStore

    try:
        rows = SessionStore.from_runtime(load_config().browser_runtime).status()
    except ConfigError as exc:
        _fail("config_error", str(exc))
        return
    _emit({"credentials": rows}, json_out)


@cookies_app.command("remove")
def cookies_remove(
    credential_id: str = typer.Argument(..., help="credential_id shown by `cookies status`"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Revoke a credential: delete stored values and clear managed cookies from the profile."""
    from mic.browser.config import ConfigError, resolve_profile_dir
    from mic.browser.profile_lock import ProfileBusy
    from mic.browser.session import BrowserSession, BrowserUnavailable
    from mic.browser.session_fallback import SessionStore

    try:
        cfg = load_config()
        runtime = cfg.browser_runtime
        store = SessionStore.from_runtime(runtime)
        result = store.remove(credential_id)
        domains = result.get("domains_to_clear") or []
        cleared = None
        if domains and runtime.get("enabled"):
            session = BrowserSession(runtime=runtime, run_id="cookies-remove", attempt_id="cookies-remove",
                                     profile_dir=resolve_profile_dir(runtime))
            try:
                session.start()
                cleared = session.clear_cookies_for_domains(domains)
            finally:
                session.close()
        result["profile_cookies_cleared"] = cleared
    except ConfigError as exc:
        _fail("config_error", str(exc))
        return
    except ProfileBusy as exc:
        _fail("profile_busy", f"{exc}; registry entry revoked, profile cookies NOT cleared yet", exit_code=3)
        return
    except BrowserUnavailable as exc:
        _fail(exc.code, f"{exc}; registry entry revoked, profile cookies NOT cleared yet")
        return
    _emit(result, json_out)


@search_app.command("probe")
def search_probe(
    query: str = typer.Option(..., "--query"),
    engine: str = typer.Option("bing", "--engine", help="bing | google | baidu"),
    pages: int = typer.Option(1, "--pages", min=1, max=3),
    target_id: str = typer.Option(None, "--target-id", help="Optional target for relevance rules"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Search only: organic results + per-page diagnostics. No body read, no model."""
    from mic.browser.config import ConfigError
    from mic.browser.contracts import SearchRequest
    from mic.browser.coordinator import SearchCoordinator
    from mic.browser.engines import build_engine
    from mic.browser.profile_lock import ProfileBusy
    from mic.browser.session import BrowserUnavailable
    from mic.browser_search import BrowserSearchProvider
    from mic.budget import RunBudget
    from mic.profile import TargetProfile
    from mic.run_context import RunContext, TargetIdentity
    from mic.utils import new_id

    try:
        cfg = load_config()
        runtime = cfg.browser_runtime
        if not runtime.get("enabled"):
            _fail("config_error", "browser_runtime.enabled is false")
            return
        limits = dict(runtime.get("limits") or {})
        limits["max_search_pages_per_query"] = pages
        limits["max_search_pages_per_run"] = pages
        limits["max_engines_per_query"] = 1
        identity = None
        if target_id:
            pcfg = cfg.get_target_profile(target_id)
            if pcfg is None:
                _fail("config_error", f"unknown target_id {target_id}")
                return
            identity = TargetIdentity.from_profile(TargetProfile.from_config(pcfg))
        context = RunContext(run_id=new_id("probe"), budget=RunBudget(limits=limits), identity=identity,
                             browser_runtime=runtime)
        provider = BrowserSearchProvider({"type": "browser", "engine_order": [engine], "enabled_engines": [engine]},
                                         provider_name=f"probe:{engine}")
        provider.coordinator = SearchCoordinator([build_engine(engine)], desired_relevant_articles=10 ** 6)
        try:
            batch = provider.search_with_context(SearchRequest(query=query, limit=50), context)
        finally:
            cleanup = context.close()
    except ConfigError as exc:
        _fail("config_error", str(exc))
        return
    except ProfileBusy as exc:
        _fail("profile_busy", str(exc), exit_code=3)
        return
    except BrowserUnavailable as exc:
        _fail(exc.code, str(exc))
        return
    _emit({"query": query, "engine": engine, "outcome": batch.outcome, "stop_reason": batch.stop_reason,
           "hits": [h.model_dump(mode="json") for h in batch.hits],
           "pages": [{k: v for k, v in p.to_record().items() if k != "hits"} for p in batch.page_attempts],
           "quality": batch.quality, "budget_used": context.budget.used_summary(), "cleanup": cleanup},
          json_out)


@reader_app.command("probe")
def reader_probe(
    url: str = typer.Option(..., "--url"),
    transport: str = typer.Option("http", "--transport", help="http | browser | http_then_browser"),
    target_id: str = typer.Option(None, "--target-id", help="Target profile for passage selection"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Single-page fetch + strict article scope check. No model call."""
    from mic.browser.config import ConfigError
    from mic.browser.profile_lock import ProfileBusy
    from mic.browser.session import BrowserUnavailable
    from mic.budget import RunBudget
    from mic.profile import TargetProfile
    from mic.reader import LinkReader
    from mic.run_context import RunContext
    from mic.utils import new_id

    try:
        cfg = load_config()
        runtime = cfg.browser_runtime
        pcfg = None
        if target_id:
            pcfg = cfg.get_target_profile(target_id)
            if pcfg is None:
                _fail("config_error", f"unknown target_id {target_id}")
                return
        if pcfg is None:
            pcfg = next(iter(cfg.target_profiles.values()), {"target_id": "probe", "canonical_name": "probe"})
        profile = TargetProfile.from_config(pcfg)
        reader = LinkReader(cfg)
        strategy = {"http": "http_only", "browser": "browser_only"}.get(transport, transport)
        if strategy != "http_only" and not runtime.get("enabled"):
            _fail("config_error", "browser transport requested but browser_runtime.enabled is false")
            return
        context = RunContext(run_id=new_id("probe"), budget=RunBudget(limits=dict(runtime.get("limits") or {})),
                             browser_runtime=runtime)
        try:
            result = reader.read("probe", url, profile, context=context, strategy=strategy)
        finally:
            cleanup = context.close()
    except ConfigError as exc:
        _fail("config_error", str(exc))
        return
    except ProfileBusy as exc:
        _fail("profile_busy", str(exc), exit_code=3)
        return
    except BrowserUnavailable as exc:
        _fail(exc.code, str(exc))
        return
    _emit({"url": url, "read_status": result.read_status, "failure_reason": result.failure_reason,
           "transport": result.transport, "final_url": result.final_url, "title": result.title,
           "publish_time": result.publish_time, "content_length": result.content_length,
           "content_hash": result.content_hash, "body_scope": result.body_scope,
           "passages": [{"id": p.passage_id, "section": p.section, "chars": len(p.text),
                         "preview": p.text[:80]} for p in result.passages],
           "fetch": result.fetch_diagnostics, "budget_used": context.budget.used_summary(),
           "cleanup": cleanup}, json_out)


def _print_report(report: dict) -> None:
    s = report["summary"]
    console.print(f"\n[bold]Batch Report[/bold] — {report['target']} "
                  f"(window={report['time_window']})")
    console.print(f"run_id: {report['search_run_id']}")
    diag = report.get("collection_diagnostics") or {}
    if diag:
        console.print(
            f"execution={diag.get('execution_status')} search={diag.get('search_status')} "
            f"read={diag.get('read_status')} output={diag.get('output_status')} "
            f"usable={diag.get('usable')} budget_used={diag.get('budget_used')}")
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
