from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class SQLiteStore:
    """Small SQLite helper used for state, bus or data stores.

    The intelligence collector treats state, bus and data as separate logical stores. They may be
    backed by the same SQLite file in a tiny local deployment, or by separate files in OpenClaw
    multi-Agent mode.
    """

    def __init__(self, sqlite_path: str | Path):
        self.sqlite_path = Path(sqlite_path).expanduser().resolve()
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.sqlite_path), timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            yield con
        finally:
            con.close()

    def init_schema(self) -> None:
        with self.session() as con:
            con.executescript(SCHEMA_SQL)
            _apply_migrations(con)


def _apply_migrations(con: sqlite3.Connection) -> None:
    """Idempotent in-place migrations for databases created by earlier versions."""
    message_cols = {row["name"] for row in con.execute("PRAGMA table_info(messages)")}
    if "expires_at" not in message_cols:
        con.execute("ALTER TABLE messages ADD COLUMN expires_at TEXT")
    con.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)")
    # v3: per-event evidence fields so research quality (source authority, freshness) is queryable.
    event_cols = {row["name"] for row in con.execute("PRAGMA table_info(structured_events)")}
    for column in ("source_url", "source_domain", "source_type", "published_at", "retrieved_at"):
        if column not in event_cols:
            con.execute(f"ALTER TABLE structured_events ADD COLUMN {column} TEXT")
    con.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
    # v4: query_family so dashboards/reports can judge which query families produce events.
    if "query_family" not in event_cols:
        con.execute("ALTER TABLE structured_events ADD COLUMN query_family TEXT")
    con.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
    # v5: event -> tracking_variable links and HK-connect structured snapshots (V0.8 research
    # loop). Both tables are created via CREATE TABLE IF NOT EXISTS in SCHEMA_SQL, which runs
    # before migrations, so old databases pick them up automatically; only stamp the version.
    con.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")
    # v6 (V0.8.1): HK snapshot completeness columns for databases created before the columns
    # existed; market_context_snapshots and research_cards are CREATE TABLE IF NOT EXISTS.
    hk_cols = {row["name"] for row in con.execute("PRAGMA table_info(hk_connect_snapshots)")}
    for column, default in (
        ("field_completeness_json", "'{}'"),
        ("missing_fields_json", "'[]'"),
        ("provider_status_json", "'{}'"),
    ):
        if column not in hk_cols:
            con.execute(
                f"ALTER TABLE hk_connect_snapshots ADD COLUMN {column} TEXT NOT NULL DEFAULT {default}"
            )
    con.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)")
    # v7: golden_eval_runs persists `eval golden` results so the dashboard can show the
    # latest recall without reading arbitrary file paths at request time. The table is
    # created via CREATE TABLE IF NOT EXISTS in SCHEMA_SQL; only stamp the version.
    con.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)")


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads_json(value: str | bytes | None, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if value == "":
        return default
    return json.loads(value)


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  priority INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL,
  correlation_id TEXT,
  idempotency_key TEXT UNIQUE,
  target_agent_id TEXT,
  target_agent_group TEXT,
  available_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  lease_owner TEXT,
  lease_until TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  error_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_topic_status ON messages(topic, status, priority, available_at);
CREATE INDEX IF NOT EXISTS idx_messages_target ON messages(target_agent_id, target_agent_group, status);
CREATE INDEX IF NOT EXISTS idx_messages_correlation ON messages(correlation_id);

CREATE TABLE IF NOT EXISTS tickets (
  ticket_id TEXT PRIMARY KEY,
  ticket_type TEXT NOT NULL,
  schema_version TEXT NOT NULL DEFAULT 'ticket.v1',
  parent_ticket_id TEXT,
  correlation_id TEXT,
  priority TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  expires_at TEXT,
  source_agent TEXT,
  target_agent_group TEXT,
  target_agent_id TEXT,
  related_tickers_json TEXT NOT NULL DEFAULT '[]',
  summary_cn TEXT,
  payload_ref TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT UNIQUE,
  audit_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_type_status ON tickets(ticket_type, status);
CREATE INDEX IF NOT EXISTS idx_tickets_correlation ON tickets(correlation_id);

CREATE TABLE IF NOT EXISTS ticket_events (
  event_id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  old_status TEXT,
  new_status TEXT,
  message TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(ticket_id) REFERENCES tickets(ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id, created_at);

CREATE TABLE IF NOT EXISTS collection_demands (
  demand_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL DEFAULT 'demand.v1',
  current_version INTEGER NOT NULL DEFAULT 1,
  demand_type TEXT NOT NULL,
  source_type TEXT NOT NULL,
  status TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'normal',
  owner TEXT,
  active_from TEXT,
  active_to TEXT,
  target_scope_json TEXT NOT NULL DEFAULT '{}',
  payload_json TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  test_mode INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_demands_status ON collection_demands(status, active_from, active_to);

CREATE TABLE IF NOT EXISTS collection_demand_versions (
  demand_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  created_by TEXT,
  PRIMARY KEY(demand_id, version),
  FOREIGN KEY(demand_id) REFERENCES collection_demands(demand_id)
);

CREATE TABLE IF NOT EXISTS collection_tasks (
  task_id TEXT PRIMARY KEY,
  demand_id TEXT,
  request_ticket_id TEXT,
  task_ticket_id TEXT,
  task_type TEXT NOT NULL,
  target_id TEXT,
  ticker TEXT,
  bucket_start TEXT,
  bucket_size TEXT,
  tool_name TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  priority TEXT NOT NULL DEFAULT 'normal',
  payload_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_collection_tasks_status ON collection_tasks(status, task_type, ticker);

CREATE TABLE IF NOT EXISTS collection_runs (
  run_id TEXT PRIMARY KEY,
  task_id TEXT,
  ticket_id TEXT,
  demand_id TEXT,
  tool_name TEXT NOT NULL,
  operation TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  request_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT NOT NULL DEFAULT '{}',
  result_ref TEXT,
  raw_result_ref TEXT,
  quality_json TEXT NOT NULL DEFAULT '{}',
  errors_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON collection_runs(task_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_ticket ON collection_runs(ticket_id);

CREATE TABLE IF NOT EXISTS structured_events (
  event_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL DEFAULT 'structured_event.v1',
  target_id TEXT,
  ticker TEXT,
  company_name TEXT,
  event_type TEXT NOT NULL,
  event_subtype TEXT,
  event_date TEXT,
  summary_cn TEXT NOT NULL,
  impact_json TEXT NOT NULL DEFAULT '{}',
  source_refs_json TEXT NOT NULL DEFAULT '[]',
  source_level TEXT,
  source_url TEXT,
  source_domain TEXT,
  source_type TEXT,
  published_at TEXT,
  retrieved_at TEXT,
  query_family TEXT,
  confidence REAL,
  data_quality REAL,
  source_corroboration_status TEXT,
  source_run_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_structured_events_target ON structured_events(target_id, ticker, event_date);
CREATE INDEX IF NOT EXISTS idx_structured_events_type ON structured_events(event_type, event_date);

CREATE TABLE IF NOT EXISTS market_features (
  feature_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL DEFAULT 'market_feature.v1',
  ticker TEXT NOT NULL,
  target_id TEXT,
  feature_window TEXT NOT NULL,
  bucket_start TEXT NOT NULL,
  bucket_end TEXT,
  timestamp TEXT,
  abnormality_score REAL,
  data_quality REAL,
  summary_cn TEXT,
  feature_json TEXT NOT NULL DEFAULT '{}',
  source_refs_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_market_features_ticker ON market_features(ticker, feature_window, bucket_start);

CREATE TABLE IF NOT EXISTS data_quality_issues (
  issue_id TEXT PRIMARY KEY,
  severity TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  target_id TEXT,
  ticker TEXT,
  tool_name TEXT,
  request_id TEXT,
  summary_cn TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quality_issues_status ON data_quality_issues(status, severity, ticker);

CREATE TABLE IF NOT EXISTS coverage_gaps (
  gap_id TEXT PRIMARY KEY,
  target_id TEXT,
  ticker TEXT,
  priority TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL DEFAULT 'open',
  description TEXT NOT NULL,
  suggested_next_queries_json TEXT NOT NULL DEFAULT '[]',
  source_run_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_coverage_gaps_status ON coverage_gaps(status, priority, target_id);

CREATE TABLE IF NOT EXISTS daily_collection_reports (
  report_id TEXT PRIMARY KEY,
  trade_date TEXT NOT NULL,
  report_type TEXT NOT NULL DEFAULT 'COLLECTION_DAILY_REPORT',
  summary_json TEXT NOT NULL DEFAULT '{}',
  html_path TEXT,
  json_path TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_sessions (
  session_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  model_ref TEXT,
  status TEXT NOT NULL DEFAULT 'created',
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  stopped_at TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent ON agent_sessions(agent_id, status, started_at);

CREATE TABLE IF NOT EXISTS agent_checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  session_id TEXT,
  checkpoint_type TEXT NOT NULL DEFAULT 'runtime',
  state TEXT NOT NULL,
  trade_date TEXT,
  market_phase TEXT,
  current_ticket_id TEXT,
  current_task_id TEXT,
  open_ticket_ids_json TEXT NOT NULL DEFAULT '[]',
  next_due_tasks_json TEXT NOT NULL DEFAULT '[]',
  checkpoint_json TEXT NOT NULL,
  state_checksum TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_agent ON agent_checkpoints(agent_id, created_at);

CREATE TABLE IF NOT EXISTS agent_memories (
  memory_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  content_cn TEXT NOT NULL,
  source_ticket_ids_json TEXT NOT NULL DEFAULT '[]',
  validity_condition TEXT,
  confidence REAL,
  decay_score REAL NOT NULL DEFAULT 0.0,
  tags_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_agent_type ON agent_memories(agent_id, memory_type, created_at);

CREATE TABLE IF NOT EXISTS runtime_heartbeats (
  heartbeat_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  worker_id TEXT,
  session_id TEXT,
  state TEXT NOT NULL,
  message_id TEXT,
  ticket_id TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_agent ON runtime_heartbeats(agent_id, created_at);

CREATE TABLE IF NOT EXISTS circuit_breakers (
  tool_name TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'closed',
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  opened_at TEXT,
  cooldown_until TEXT,
  last_error_json TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pool_members (
  pool_layer TEXT NOT NULL,
  ticker TEXT NOT NULL,
  target_id TEXT,
  company_name TEXT,
  sellability TEXT,
  is_st INTEGER NOT NULL DEFAULT 0,
  is_suspended INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (pool_layer, ticker)
);
CREATE INDEX IF NOT EXISTS idx_pool_members_layer ON pool_members(pool_layer, status);

CREATE TABLE IF NOT EXISTS event_variable_links (
  event_id TEXT NOT NULL,
  target_id TEXT,
  ticker TEXT,
  tracking_variable TEXT NOT NULL,
  direction TEXT,
  strength REAL,
  mapping_method TEXT NOT NULL,
  mapping_confidence REAL,
  review_status TEXT NOT NULL DEFAULT 'pending',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(event_id, tracking_variable, mapping_method)
);
CREATE INDEX IF NOT EXISTS idx_event_variable_links_target
ON event_variable_links(target_id, tracking_variable, review_status);

CREATE TABLE IF NOT EXISTS hk_connect_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  target_id TEXT,
  ticker TEXT NOT NULL,
  company_name TEXT,
  as_of TEXT NOT NULL,
  hk_connect_eligible INTEGER,
  last_price_hkd REAL,
  turnover_hkd REAL,
  southbound_holding_shares REAL,
  southbound_holding_market_value_hkd REAL,
  southbound_holding_pct REAL,
  southbound_mv_change_1d REAL,
  southbound_mv_change_5d REAL,
  southbound_mv_change_10d REAL,
  buyback_amount_hkd REAL,
  ah_premium_pct REAL,
  hk_liquidity_score REAL,
  field_completeness_json TEXT NOT NULL DEFAULT '{}',
  missing_fields_json TEXT NOT NULL DEFAULT '[]',
  provider_status_json TEXT NOT NULL DEFAULT '{}',
  source_url TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_hk_connect_snapshots_ticker ON hk_connect_snapshots(ticker, as_of);

CREATE TABLE IF NOT EXISTS market_context_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  context_id TEXT NOT NULL,
  context_type TEXT NOT NULL,
  name TEXT,
  symbol TEXT,
  as_of TEXT NOT NULL,
  value REAL,
  unit TEXT,
  change_1d REAL,
  change_5d REAL,
  change_20d REAL,
  source_url TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_market_context_snapshots ON market_context_snapshots(context_id, as_of);

CREATE TABLE IF NOT EXISTS research_cards (
  target_id TEXT PRIMARY KEY,
  ticker TEXT,
  company_name TEXT,
  industry_id TEXT,
  theme_ids_json TEXT NOT NULL DEFAULT '[]',
  as_of TEXT,
  card_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS golden_eval_runs (
  run_id TEXT PRIMARY KEY,
  golden_file TEXT NOT NULL,
  trade_date TEXT,
  expected_count INTEGER,
  matched_count INTEGER,
  recall REAL,
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_golden_eval_runs_created ON golden_eval_runs(created_at);

CREATE TABLE IF NOT EXISTS tool_capabilities (
  capability_id TEXT PRIMARY KEY,
  tool_name TEXT NOT NULL,
  checked_at TEXT NOT NULL DEFAULT (datetime('now')),
  status TEXT NOT NULL,
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  errors_json TEXT NOT NULL DEFAULT '[]',
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_capabilities_tool ON tool_capabilities(tool_name, checked_at);

INSERT OR IGNORE INTO schema_migrations(version) VALUES (1);
"""
