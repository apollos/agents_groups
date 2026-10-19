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

# Development and test environments

- Develop and perform version-control operations in the local WSL checkout,
  `/home/yu/workspace/agents_groups`, which resolves to the Windows drive
  (`/mnt/c/Workspace/agents_groups`). The local virtual environment is `mydev`
  (`~/.venvs/mydev`).
- The primary runtime and test host is the user's home Ubuntu machine, accessible
  with `ssh home-ubuntu`. Its project checkout is at
  `/home/yu/Workspace/agents_groups` (note the capital `W` in `Workspace`), and
  its runtime environment is the `agents_groups` virtual environment
  (`~/.venv/agents_groups`). That host also has a `~/.venv/mydev`; do not use it
  for this project. Each SSH command starts a fresh shell, so activate the
  environment inside the remote command, e.g.
  `ssh home-ubuntu 'cd /home/yu/Workspace/agents_groups && source ~/.venv/agents_groups/bin/activate && …'`.
- On neither host create virtual environments or install or upgrade packages on
  your own. If a dependency is missing, stop and ask the user before installing
  anything.
- Basic or simple tests may run locally. Prefer the remote Ubuntu host for other
  tests, and run end-to-end and application tests there. Local debugging/testing
  is an acceptable fallback when debugging remotely is particularly difficult.
- Keep the local checkout authoritative for code: change code only locally, copy
  it to the remote checkout with `rsync` or an equivalent file-sync method, and
  retrieve remote logs and verification artifacts into the appropriate local
  component's `logs/` directory.
- When pushing code, exclude `.git/` and gitignored paths so the remote's
  repository metadata, `logs/`, `data/`, `.env` and environments are never
  overwritten or deleted. Do not copy file permissions either: every file on the
  Windows drive reports mode 777. For example:
  `rsync -rlt --chmod=D755,F644 --exclude=.git/ --filter=':- .gitignore' /home/yu/workspace/agents_groups/ home-ubuntu:/home/yu/Workspace/agents_groups/`
- Ask the user for approval before using local `git push` followed by remote
  `git pull` as a code synchronization workflow.

# OpenClaw startup

- This project requires OpenClaw. Its gateway listens on `127.0.0.1:18791` on
  both hosts. Check whether that port is listening in the environment needed for
  the task; if it is not, starting it is already authorized.
- On remote Ubuntu, start it with `openclaw gateway --port 18791`.
- In local WSL, start it with `aiw oc`.
- Both commands run in the foreground. Start them detached from the agent's shell
  or SSH session, with output redirected to a log file under the repo's `logs/`
  (e.g. `nohup … > logs/openclaw-gateway.log 2>&1 < /dev/null &`). Startup has
  succeeded once port 18791 is listening; if the process exits or the port never
  comes up, it has failed.
- On neither host should OpenClaw be installed, started, or managed as a
  `systemd` service through `systemctl` or an equivalent service-management tool.
  This includes OpenClaw's own service subcommands (`openclaw gateway install`,
  `start`, `stop`, `restart` and `uninstall`) and the disabled
  `openclaw-gateway.service` user unit installed in WSL.
- If an OpenClaw startup attempt fails, stop the startup/testing workflow
  immediately and notify the user. Do not retry, troubleshoot, attempt repairs,
  or switch environments to work around that failure. The user will handle it.
