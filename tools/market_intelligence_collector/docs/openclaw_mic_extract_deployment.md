# OpenClaw MIC extraction deployment

MIC text-model tasks use the dedicated `openclaw/mic-extract` Agent route. It
uses the host's existing model provider and credentials, but must have no
callable tools. Apply the sanitized
`config/openclaw_mic_extract.template.json` through OpenClaw's validated config
interface after replacing the workspace placeholders. Keep the workspace
outside the repository and preserve credentials only in the deployment
environment.

The effective policy must be verified after every OpenClaw tool-catalog or
plugin upgrade: a new `mic-extract` session must report `toolCount=0`, compiled
tool count `0`, and no `tool.call` events. Do not treat the current deny-list
length as a permanent invariant.

`model_registry.yaml` sets `openclaw_research` to `openclaw/mic-extract` for
all MIC text-model tasks: batch triage, bundle/cascade extraction, arbitration,
and recovery. `MIC_OPENCLAW_REQUEST_MODEL` is the only deployment override for
that route. It is intentionally distinct from `OPENCLAW_REQUEST_MODEL`, which
may serve other OpenClaw clients.

The model adapter always bounds SDK timeout by the task's remaining budget; it
does not refresh a full timeout for each model call. Enable
`MIC_MODEL_CALL_DEBUG=1` only for bounded investigation runs. It records timing
and byte counts without recording input or response bodies.
