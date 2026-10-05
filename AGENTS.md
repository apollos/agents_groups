# Intelligence content review

For MIC and intelligence collector work, follow
`.cursor/rules/intelligence-content-review.mdc` and the canonical policy in
`tools/market_intelligence_collector/mic/content_review_policy.py`.
Keep semantic decisions in the model and their enforcement in the shared review module.
Do not introduce separate keyword-based content approval logic in summaries, storage or exports.
