"""One-command collection requests for industries, companies and stocks.

The research-pool tracking plan needs a low-friction way to say "start collecting
industry X / company Y / stock Z". A request does up to three things:

1. Upsert the target into MIC's ``config/target_profiles.yaml`` (industries and
   companies only; MIC refuses to collect for unregistered target_ids).
2. Append the target to a managed daily_collection Demand and re-register it,
   which bumps the demand version and publishes ``demand.registered`` so the
   Runtime Controller picks it up on the next tick.
3. Register A-share tickers in the pool_members table (watchlist layer) so the
   target is ready for the later intraday-monitoring stage.

Stock-only requests skip MIC (``collect_mic: false``); HK tickers skip
stock_data_collector (``collect_stock: false``) because it only covers A-shares.

``request_batch`` registers a whole research pool from one YAML/JSON spec:
MIC profiles are written in a single file save and each managed demand is
re-registered once (one version bump) regardless of how many targets it gains.
"""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from .db import SQLiteStore
from .demand import DemandRegistry
from .ids import stable_hash
from .logging_setup import get_logger
from .pools import PoolRepository
from .queue import SQLiteMessageQueue

logger = get_logger("request_center")

INDUSTRY_DEMAND_ID = "demand_industry_research_daily"
COMPANY_DEMAND_ID = "demand_company_research_daily"
STOCK_DEMAND_ID = "demand_stock_eod_daily"

_MIC_PROFILES_HEADER = (
    "# Target profiles for MIC collection.\n"
    "# NOTE: this file is managed by `intel-agent request industry|company|batch` as well as by hand.\n"
    "# The request tool rewrites the whole file, so keep durable notes elsewhere.\n"
)

_A_SHARE_TICKER = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)
_HK_TICKER = re.compile(r"^\d{4,5}\.HK$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# MIC target profile file access
# ---------------------------------------------------------------------------


def resolve_mic_config_dir(cfg) -> Path:
    """Locate MIC's config directory: agent YAML > installed mic package > repo layout."""
    configured = cfg.tools.mic_config_dir
    if configured:
        return Path(configured)
    try:
        import mic  # type: ignore

        candidate = Path(mic.__file__).resolve().parent.parent / "config"
        if candidate.is_dir():
            return candidate
    except Exception:
        pass
    # Repo layout fallback: <repo>/agents/intelligence_collector_agent/... and
    # <repo>/tools/market_intelligence_collector/config.
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tools" / "market_intelligence_collector" / "config"
        if (candidate / "target_profiles.yaml").exists():
            return candidate
    raise FileNotFoundError(
        "cannot locate MIC config dir; set tools.market_intelligence_collector.config_dir in the agent YAML"
    )


def load_mic_profiles(config_dir: Path) -> dict[str, Any]:
    path = Path(config_dir) / "target_profiles.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("target_profiles") or {}


def save_mic_profiles(config_dir: Path, profiles: dict[str, Any]) -> Path:
    """Atomically replace target_profiles.yaml so an interrupted write never leaves half a file."""
    path = Path(config_dir) / "target_profiles.yaml"
    body = yaml.safe_dump(
        {"target_profiles": profiles},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".target_profiles.", suffix=".tmp", delete=False
    ) as f:
        tmp = Path(f.name)
        f.write(_MIC_PROFILES_HEADER + body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


@contextmanager
def mic_profiles_lock(config_dir: Path) -> Iterator[None]:
    """Advisory file lock covering the load -> merge -> save cycle.

    Prevents two concurrent `request batch` invocations (or a batch racing a single request)
    from silently dropping each other's targets in the read-modify-write of the profiles file.
    """
    lock_path = Path(config_dir) / ".target_profiles.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX fallback keeps behaviour unlocked
            pass
        yield
    finally:
        os.close(fd)


def list_mic_targets(config_dir: Path) -> list[dict[str, Any]]:
    out = []
    for target_id, profile in load_mic_profiles(config_dir).items():
        out.append(
            {
                "target_id": target_id,
                "type": (profile or {}).get("type"),
                "canonical_name": (profile or {}).get("canonical_name"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# target-id helpers
# ---------------------------------------------------------------------------


def _industry_target_id(name: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    return f"industry_{stable_hash(name, 8)}"


def _company_target_id(name: str, ticker: str | None, explicit: str | None) -> str:
    if explicit:
        return explicit
    if ticker:
        code = ticker.split(".")[0]
        if _HK_TICKER.match(ticker):
            # Zero-pad HK codes to 5 digits so 0700.HK and 00700.HK map to the same id.
            return f"company_hk_{code.zfill(5)}"
        return f"company_{code}"
    return f"company_{stable_hash(name, 8)}"


def _is_a_share(ticker: str | None) -> bool:
    return bool(ticker and _A_SHARE_TICKER.match(ticker))


def normalize_ticker(ticker: str | None) -> str | None:
    """Canonical ticker form: uppercase suffix, HK codes zero-padded to 5 digits (00700.HK)."""
    if not ticker:
        return ticker
    text = str(ticker).strip().upper()
    if _HK_TICKER.match(text):
        code, suffix = text.split(".")
        return f"{code.zfill(5)}.{suffix}"
    return text


def _as_list(value: Any) -> list[str] | None:
    """Accept YAML lists or comma-separated strings."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
        return [part for part in items if part] or None
    return [str(v) for v in value] or None


# ---------------------------------------------------------------------------
# RequestCenter
# ---------------------------------------------------------------------------


class RequestCenter:
    def __init__(self, cfg, *, data_store: SQLiteStore, bus_store: SQLiteStore):
        self.cfg = cfg
        self.data_store = data_store
        self.queue = SQLiteMessageQueue(bus_store)
        self.registry = DemandRegistry(data_store, self.queue)
        self.pool_repo = PoolRepository(data_store)

    # -- public entry points -------------------------------------------------

    def request_industry(self, **item: Any) -> dict[str, Any]:
        tid, profile, target = self._industry_entry(item)
        profile_path = self._upsert_mic_profiles({tid: profile})
        demand_result = self._merge_targets_into_demand(
            item.get("demand_id") or INDUSTRY_DEMAND_ID,
            [target],
            demand_kind="industry",
            priority=item.get("priority") or "normal",
            test_mode=bool(item.get("test_mode")),
        )
        return {
            "status": "requested",
            "kind": "industry",
            "target_id": tid,
            "mic_profile_path": str(profile_path),
            "demand": demand_result,
        }

    def request_company(self, **item: Any) -> dict[str, Any]:
        item.setdefault("pool_layer", "watchlist")
        tid, profile, target, pool_op = self._company_entry(item)
        profile_path = self._upsert_mic_profiles({tid: profile})
        demand_result = self._merge_targets_into_demand(
            item.get("demand_id") or COMPANY_DEMAND_ID,
            [target],
            demand_kind="company",
            priority=item.get("priority") or "normal",
            test_mode=bool(item.get("test_mode")),
        )
        pool_result = self._apply_pool_op(pool_op)
        return {
            "status": "requested",
            "kind": "company",
            "target_id": tid,
            "ticker": item.get("ticker"),
            "mic_profile_path": str(profile_path),
            "demand": demand_result,
            "pool": pool_result,
        }

    def request_stock(self, **item: Any) -> dict[str, Any]:
        item.setdefault("pool_layer", "watchlist")
        target, pool_op = self._stock_entry(item)
        demand_result = self._merge_targets_into_demand(
            item.get("demand_id") or STOCK_DEMAND_ID,
            [target],
            demand_kind="stock",
            priority=item.get("priority") or "normal",
            test_mode=bool(item.get("test_mode")),
        )
        pool_result = self._apply_pool_op(pool_op)
        return {
            "status": "requested",
            "kind": "stock",
            "ticker": item["ticker"],
            "demand": demand_result,
            "pool": pool_result,
        }

    def request_batch(self, spec: dict[str, Any], *, update_demand_config: bool = False) -> dict[str, Any]:
        """Register a whole research pool from one spec (YAML/JSON).

        Spec layout::

            defaults: {priority: normal, test_mode: false, pool_layer: watchlist}
            demands:                     # optional per-demand scaffold overrides
              demand_company_watch_daily:
                kind: company
                priority: low
                task_profile: {mic: {budget_profile: {max_queries: 6}}}
            industries: [{name: ..., target_id: ..., products: [...], ...}]
            companies:  [{name: ..., ticker: ..., demand_id: ..., ...}]
            stocks:     [{ticker: ..., company_name: ...}]

        On re-runs, ``demands:`` overrides only apply to *existing* demands when
        ``update_demand_config`` is true; otherwise the run keeps the stored demand-level config
        (budget/priority/task_profile) and reports a warning, so "I edited the YAML but nothing
        changed" is always visible in the output.
        """
        defaults = spec.get("defaults") or {}
        overrides = spec.get("demands") or {}
        profiles: dict[str, dict[str, Any]] = {}
        groups: dict[str, dict[str, Any]] = {}
        pool_ops: list[dict[str, Any] | None] = []
        summary = {"industries": 0, "companies": 0, "stocks": 0}

        def add(demand_id: str, kind: str, target: dict[str, Any], item: dict[str, Any]) -> None:
            group = groups.setdefault(
                demand_id,
                {
                    "kind": (overrides.get(demand_id) or {}).get("kind") or kind,
                    "targets": [],
                    "priority": item.get("priority") or defaults.get("priority") or "normal",
                    "test_mode": bool(item.get("test_mode", defaults.get("test_mode", False))),
                },
            )
            group["targets"].append(target)

        for item in spec.get("industries") or []:
            item = dict(item)
            tid, profile, target = self._industry_entry(item)
            profiles[tid] = profile
            add(item.get("demand_id") or INDUSTRY_DEMAND_ID, "industry", target, item)
            summary["industries"] += 1
        for item in spec.get("companies") or []:
            item = dict(item)
            item.setdefault("pool_layer", defaults.get("pool_layer", "watchlist"))
            tid, profile, target, pool_op = self._company_entry(item)
            profiles[tid] = profile
            add(item.get("demand_id") or COMPANY_DEMAND_ID, "company", target, item)
            pool_ops.append(pool_op)
            summary["companies"] += 1
        for item in spec.get("stocks") or []:
            item = dict(item)
            item.setdefault("pool_layer", defaults.get("pool_layer", "watchlist"))
            target, pool_op = self._stock_entry(item)
            add(item.get("demand_id") or STOCK_DEMAND_ID, "stock", target, item)
            pool_ops.append(pool_op)
            summary["stocks"] += 1

        profile_path = self._upsert_mic_profiles(profiles) if profiles else None
        demand_results = []
        for demand_id, group in groups.items():
            demand_results.append(
                self._merge_targets_into_demand(
                    demand_id,
                    group["targets"],
                    demand_kind=group["kind"],
                    priority=group["priority"],
                    test_mode=group["test_mode"],
                    scaffold_overrides=overrides.get(demand_id),
                    update_demand_config=update_demand_config,
                )
            )
        pool_count = sum(1 for op in pool_ops if self._apply_pool_op(op))
        warnings = [w for entry in demand_results for w in entry.get("warnings", [])]
        return {
            "status": "requested",
            "kind": "batch",
            "registered": summary,
            "mic_profile_path": str(profile_path) if profile_path else None,
            "mic_profiles_written": len(profiles),
            "demands": demand_results,
            "pool_members_upserted": pool_count,
            "warnings": warnings,
        }

    def remove_target(self, *, demand_id: str, target_id: str | None = None, ticker: str | None = None) -> dict[str, Any]:
        if not target_id and not ticker:
            raise ValueError("remove needs --target-id or --ticker")
        demand = self.registry.get(demand_id)
        if not demand:
            raise ValueError(f"demand not found: {demand_id}")
        before = list(demand.get("targets") or [])
        kept = [
            t
            for t in before
            if not ((target_id and t.get("target_id") == target_id) or (ticker and t.get("ticker") == ticker))
        ]
        if len(kept) == len(before):
            return {"status": "not_found", "demand_id": demand_id, "target_id": target_id, "ticker": ticker}
        demand["targets"] = kept
        result = self._register(demand)
        return {
            "status": "removed",
            "demand_id": demand_id,
            "remaining_targets": len(kept),
            "demand_version": result["version"],
        }

    def status(self) -> dict[str, Any]:
        try:
            mic_targets = list_mic_targets(resolve_mic_config_dir(self.cfg))
            mic_error = None
        except FileNotFoundError as exc:
            mic_targets, mic_error = [], str(exc)
        demands = []
        managed_ids = [INDUSTRY_DEMAND_ID, COMPANY_DEMAND_ID, STOCK_DEMAND_ID]
        # Include any extra demands created via batch demand_id overrides.
        for row in self.registry.list():
            demand_id = row.get("demand_id")
            if row.get("source_type") == "research_pool_request" and demand_id not in managed_ids:
                managed_ids.append(demand_id)
        for demand_id in managed_ids:
            demand = self.registry.get(demand_id)
            if not demand:
                continue
            demands.append(
                {
                    "demand_id": demand_id,
                    "status": (demand.get("_registry") or {}).get("status"),
                    "version": demand.get("current_version"),
                    "targets": [
                        {
                            k: t.get(k)
                            for k in (
                                "target_type",
                                "target_id",
                                "ticker",
                                "company_name",
                                "industry_name",
                                "industry_id",
                                "tracking_variables",
                            )
                        }
                        for t in (demand.get("targets") or [])
                    ],
                }
            )
        return {
            "mic_registered_targets": mic_targets,
            "mic_error": mic_error,
            "managed_demands": demands,
            "pool_members": self.pool_repo.list_members(),
        }

    def unregistered_mic_targets(self, demand: dict[str, Any]) -> list[str]:
        """target_ids that will hit MIC but are missing from MIC's target_profiles.yaml."""
        try:
            known = {t["target_id"] for t in list_mic_targets(resolve_mic_config_dir(self.cfg))}
        except FileNotFoundError:
            return []
        missing = []
        for target in demand.get("targets") or []:
            if target.get("collect_mic") is False:
                continue
            tid = target.get("target_id")
            if not tid or tid.startswith("ticker_"):
                continue
            if tid not in known:
                missing.append(tid)
        return missing

    # -- entry builders --------------------------------------------------------

    def _industry_entry(self, item: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        name = item.get("name")
        if not name:
            raise ValueError("industry entry needs a name")
        tid = _industry_target_id(name, item.get("target_id"))
        profile = _clean(
            {
                "target_id": tid,
                "type": "industry",
                "canonical_name": name,
                "aliases": _as_list(item.get("aliases")),
                "products": _as_list(item.get("products")),
                "upstream_terms": _as_list(item.get("upstream")),
                "downstream_terms": _as_list(item.get("downstream")),
                "core_metrics": _as_list(item.get("metrics")),
                "representative_companies": _as_list(item.get("companies")),
            }
        )
        target = _clean(
            {
                "target_type": "industry",
                "target_id": tid,
                "industry_name": name,
                "tracking_variables": _as_list(item.get("tracking_variables")),
                "collect_mic": True,
            }
        )
        return tid, profile, target

    def _company_entry(self, item: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        name = item.get("name")
        if not name:
            raise ValueError("company entry needs a name")
        ticker = normalize_ticker(item.get("ticker"))
        tid = _company_target_id(name, ticker, item.get("target_id"))
        alias_list = _as_list(item.get("aliases")) or []
        if name not in alias_list:
            alias_list.insert(0, name)
        if ticker and ticker.split(".")[0] not in alias_list:
            alias_list.append(ticker.split(".")[0])
        profile = _clean(
            {
                "target_id": tid,
                "type": "company",
                "canonical_name": name,
                "aliases": alias_list,
                "markets": _as_list(item.get("markets")),
                "products": _as_list(item.get("products")),
                "business_segments": _as_list(item.get("segments")),
                "known_customers": _as_list(item.get("customers")),
                "competitors": _as_list(item.get("competitors")),
                "upstream_terms": _as_list(item.get("upstream")),
                "downstream_terms": _as_list(item.get("downstream")),
            }
        )
        target = _clean(
            {
                "target_type": "company",
                "target_id": tid,
                "ticker": ticker,
                "company_name": name,
                # Research-pool metadata: which industry line the company belongs to and which
                # research variables it should be tracked on (used by reports/analyst agent).
                "industry_id": item.get("industry_id"),
                "tracking_variables": _as_list(item.get("tracking_variables")),
                "collect_mic": True,
                # stock_data_collector only covers A-share tickers.
                "collect_stock": _is_a_share(ticker),
            },
            keep_false=True,
        )
        pool_op = None
        pool_layer = item.get("pool_layer")
        if pool_layer and _is_a_share(ticker):
            pool_op = {"pool_layer": pool_layer, "ticker": str(ticker), "target_id": tid, "company_name": name}
        return tid, profile, target, pool_op

    def _stock_entry(self, item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        ticker = normalize_ticker(item.get("ticker"))
        if not _is_a_share(ticker):
            raise ValueError(f"stock requests need an A-share ticker like 600519.SH, got: {ticker}")
        company_name = item.get("company_name")
        target = {
            "target_type": "ticker",
            "target_id": f"ticker_{ticker}",
            "ticker": ticker,
            "company_name": company_name,
            # No MIC profile exists for a bare ticker; only stock data is collected.
            "collect_mic": False,
            "collect_stock": True,
        }
        pool_op = None
        if item.get("pool_layer"):
            pool_op = {
                "pool_layer": item["pool_layer"],
                "ticker": str(ticker),
                "target_id": f"ticker_{ticker}",
                "company_name": company_name,
            }
        return target, pool_op

    # -- internals -----------------------------------------------------------

    def _apply_pool_op(self, pool_op: dict[str, Any] | None) -> dict[str, Any] | None:
        if not pool_op:
            return None
        return self.pool_repo.upsert_member(**pool_op)

    def _upsert_mic_profiles(self, new_profiles: dict[str, dict[str, Any]]) -> Path:
        config_dir = resolve_mic_config_dir(self.cfg)
        with mic_profiles_lock(config_dir):
            profiles = load_mic_profiles(config_dir)
            for target_id, profile in new_profiles.items():
                existing = profiles.get(target_id) or {}
                # Merge so a lightweight re-request never wipes hand-curated fields.
                merged = dict(existing)
                for key, value in profile.items():
                    if value not in (None, [], ""):
                        merged[key] = value
                profiles[target_id] = merged
            path = save_mic_profiles(config_dir, profiles)
        logger.info("MIC profiles upserted: %s (%s)", ", ".join(new_profiles), path)
        return path

    def _merge_targets_into_demand(
        self,
        demand_id: str,
        targets: list[dict[str, Any]],
        *,
        demand_kind: str,
        priority: str,
        test_mode: bool,
        scaffold_overrides: dict[str, Any] | None = None,
        update_demand_config: bool = False,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        config_updated = False
        override_payload = {k: v for k, v in (scaffold_overrides or {}).items() if k != "kind"}
        demand = self.registry.get(demand_id)
        if not demand:
            demand = self._demand_scaffold(demand_id, demand_kind, priority, test_mode)
            if override_payload:
                demand = _deep_merge(demand, override_payload)
                config_updated = True
        elif override_payload:
            if update_demand_config:
                demand = _deep_merge(demand, override_payload)
                config_updated = True
            else:
                warnings.append(
                    f"demand {demand_id} already exists; demand-level overrides (budget/priority/task_profile) "
                    "were skipped. Re-run with --update-demand-config to apply them."
                )
        existing = list(demand.get("targets") or [])
        added = updated = 0
        for target in targets:
            key = target.get("target_id") or target.get("ticker")
            for i, current in enumerate(existing):
                if (current.get("target_id") or current.get("ticker")) == key:
                    existing[i] = target
                    updated += 1
                    break
            else:
                existing.append(target)
                added += 1
        demand["targets"] = existing
        result = self._register(demand)
        return {
            "demand_id": demand_id,
            "targets_added": added,
            "targets_updated": updated,
            "target_count": len(existing),
            "demand_version": result["version"],
            "message_id": result["message_id"],
            "demand_config_updated": config_updated,
            "warnings": warnings,
        }

    def _register(self, demand: dict[str, Any]) -> dict[str, Any]:
        demand.pop("_registry", None)
        demand.pop("current_version", None)
        return self.registry.register(demand, activate=demand.get("status") == "active")

    def _demand_scaffold(self, demand_id: str, kind: str, priority: str, test_mode: bool) -> dict[str, Any]:
        focus = {
            "industry": ["industry_supply_demand", "policy", "price_cost_margin", "operating_update", "risk"],
            "company": ["operating_update", "financial_leading_indicator", "risk", "capital_markets"],
            "stock": [],
        }[kind]
        summaries = {
            "industry": "研究池行业信息每日采集",
            "company": "研究池核心公司每日深采",
            "stock": "股票池日线数据盘后刷新",
        }
        demand: dict[str, Any] = {
            "schema_version": "demand.v1",
            "demand_id": demand_id,
            "demand_type": "daily_collection",
            "source_type": "research_pool_request",
            "status": "active",
            "created_by": "request_center",
            "owner": "operator",
            "priority": priority,
            "market": "A_SHARE_AND_HK",
            "timezone": self.cfg.runtime.timezone,
            "description": summaries[kind],
            "target_scope": {"scope_type": "explicit_targets"},
            "targets": [],
            "schedule_window": {"allow_non_trading_day": kind != "stock"},
            "alert_policy": {"notify_on": ["P0", "P1"], "notify_owner": False, "notify_channels": ["ticket"]},
            "output_contract": {
                "emit_event_ticket": True,
                "emit_data_quality_ticket": True,
                "emit_collection_result_ticket": True,
                "write_public_data_pool": True,
            },
            "test_mode": test_mode,
            "idempotency_key": f"research_pool:{demand_id}",
        }
        if focus:
            demand["task_profile"] = {"mic": {"enabled": True, "focus": focus}}
        else:
            demand["task_profile"] = {"mic": {"enabled": False}, "stock_data": {"enabled": True}}
        return demand


def _clean(data: dict[str, Any], *, keep_false: bool = False) -> dict[str, Any]:
    """Drop empty values so profiles/targets stay tidy."""
    out = {}
    for key, value in data.items():
        if value is None or value == [] or value == "":
            continue
        if value is False and not keep_false:
            continue
        out[key] = value
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
