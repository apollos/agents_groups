# Intelligence content review

For MIC and intelligence collector work, follow
`.cursor/rules/intelligence-content-review.mdc` and the canonical policy in
`tools/market_intelligence_collector/mic/content_review_policy.py`.
Keep semantic decisions in the model and their enforcement in the shared review module.
Do not introduce separate keyword-based content approval logic in summaries, storage or exports.

# Run artifacts go to `logs/`, not `docs/`

Intermediate output of local runs and verifications (JSONL logs, per-call response dumps,
DB exports, acceptance evidence, driver scripts for such runs) belongs under the component's
`logs/` directory, e.g. `agents/intelligence_collector_agent/logs/acceptance/<run>/`.
`logs/` is gitignored at the repo root and per component; keep it that way.
`docs/` holds only the written reports, which reference the local `logs/` path.
