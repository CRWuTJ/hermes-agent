# Hermes Remaining Retrofit Repair Plan

Goal: finish the remaining safe Hermes self-retrofit work in one repair pass while preserving the live Telegram gateway and keeping unrelated dirty work separated.

## Scope

This pass may fix:

- Broken or incomplete tests caused by the current dirty workspace.
- Live-gateway continuity checks that falsely report stale state.
- Terminal stdin behavior needed to avoid PowerShell/WSL heredoc interception.
- Task status, task queue, context routing, and model-policy regressions.
- Documentation or ledger updates needed to record evidence and boundaries.

This pass must not:

- Restart or stop the live Telegram gateway outside a quiet-window activation script.
- Downgrade model policy away from `gpt-5.5`.
- Bundle unrelated model-chain, detached-worker, and workspace-hygiene work into one commit without separate verification.
- Delete user or historical `.hermes` data.

## Repair Order

1. Run broad verification to find the current failing surface.
2. For each real failure, add or keep a focused regression test first.
3. Fix the smallest implementation area that explains the failure.
4. Re-run the failed test group.
5. Re-run the retrofit verification bundle.
6. Re-run whitespace and syntax checks.
7. Update the workspace ledger with changed files, test evidence, rollback notes, and any remaining non-blocking risks.

## Expected Verification

- `pytest -n0 -q --maxfail=1`
- focused failing test commands, if full verification reveals a failure
- retrofit bundle covering harness, status, live activation, systemd service, terminal stdin, and model/task policy
- `git diff --check`
- `py_compile` for touched Python files
- live gateway status check without manual restart

## Repair Pass Evidence

Recorded on 2026-04-24 after the remaining repair pass.

Fixed during this pass:

- Optional ACP tests no longer abort collection when `hermes-agent[acp]` is not installed.
- Experimental Codex OAuth adapter tests skip cleanly when `fastapi` is absent.
- CLI quick-command fallback/error branches now print through the active CLI console.
- Gateway blocking approval E2E tests clear leaked approval environment state.
- Background and main gateway agent startup exclude default MCP server names from ordinary `enabled_toolsets`.
- Housekeeping memory flush uses the task-lane registry tracking surface.
- `@folder:` context references fall back to Python directory walking when `rg` cannot execute.
- Gateway busy-input-mode config tests no longer leak `HERMES_BUSY_INPUT_MODE=queue`.
- `/plan` gateway command tolerates minimal `GatewayRunner` test instances without `_topic_routing`.
- `gateway run --replace` now signals process-tree children before the root process and releases stale runtime state through a testable helper.
- `hermes_cli.setup` regained `run_setup_wizard`, OpenClaw migration offer, setup sections, non-interactive guidance, and section summaries.
- Local STT tests can mock `faster_whisper.WhisperModel` even when the optional package is not installed.
- Codex auxiliary fallback expectations are aligned to `gpt-5.5`.

Verification completed:

- `tests/agent`: `921 passed`
- `tests/gateway`: `2324 passed, 41 skipped`
- `tests/hermes_cli`: `1517 passed, 6 skipped`
- `tests/tools`: `2621 passed, 30 skipped`
- `tests/run_agent`: `675 passed, 6 skipped`
- `tests/cli`: `359 passed`
- top-level `tests/test_*.py`: `448 passed, 1 skipped`
- `tests/cron tests/skills tests/plugins tests/honcho_plugin`: `500 passed, 4 skipped`
- `tests/acp tests/e2e`: `14 passed, 1 xfailed`
- `py_compile` passed for the touched Python files in this repair pass.
- `git diff --check` passed.
- `hermes-gateway.service` remained running as PID `2876419` since `Fri 2026-04-24 20:40:53 CST`.

Notes:

- A single full-suite command still exceeds the practical tool timeout in this environment, so verification was split by test directory.
- Integration tests under `tests/integration` remain outside this pass because they require external services and are excluded by the project default marker policy.

## Rollback Boundary

Rollback is per workstream:

- Live activation and gateway stale checks: revert `gateway/live_activation.py`, `hermes_cli/gateway.py`, related tests, and re-run the pending activation probe.
- Terminal stdin: revert `tools/terminal_tool.py`, `tools/code_execution_tool.py`, and related terminal tests.
- Governance/status/context: revert `agent/harness.py`, `agent/topic_router.py`, `agent/context_compressor.py`, `gateway/run.py`, `gateway/status.py`, `gateway/task_control.py`, `run_agent.py`, and focused tests.
- Dirty workspace split: ledger-only changes can be reverted independently.

## Split Commit Completion

Recorded on 2026-04-25 after the dirty workspace was split into reviewable
commits.

Additional verification completed during split execution:

- CLI/status/setup/gateway repair bundle: `547 passed`.
- Telegram network and gateway approval/media bundle: `131 passed`.
- Tool-environment stability bundle: `164 passed, 15 skipped`.
- ACP optional-extra guard: `32 passed`.
- `npm install --package-lock-only --ignore-scripts --no-audit --no-fund`
  completed successfully; this environment reported Node engine warnings because
  local Node is `v18.19.1` and several transitive packages request Node 20+.
- `git diff --cached --check` passed before each split commit.
- `hermes-gateway.service` remained `SubState=running` with `MainPID=2876419`
  and `ExecMainStartTimestamp=Fri 2026-04-24 20:40:53 CST` throughout these
  split commits.

Completion boundary:

- Behavior, tests, docs, and lockfile updates were committed separately.
- Runtime `.hermes/*`, `.tmp/*`, `runtime/*.log`, and unrelated
  `knowledge_collection_nvidia.md` artifacts remain untracked by design.
- No live gateway restart or stop was performed during this split execution.
