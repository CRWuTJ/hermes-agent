# Hermes Self-Retrofit Workspace Ledger

Generated from `git status --short --untracked-files=all` on 2026-04-24.

This ledger is the boundary for splitting the current dirty workspace into separate workstreams. Do not mix workstreams in one commit unless a later verification note explains why the files cannot be separated.

## Latest Completion Evidence

Recorded on 2026-04-25 after the split stack was activated on the live Telegram gateway.

Completed slice:

- Safe live activation rechecked the quiet window and moved the gateway from PID `2876419` to PID `2939282`.
- Post-activation probe reports `code_stale=false`, `unit_definition_current=true`, `service_definition_stale=false`, and no active or queued tasks.
- The user confirmed Telegram private-chat send/receive worked after activation.
- Verified the configured main model with a real `gpt-5.5` request through `auto -> gpt-mainline-codex-local -> CodexAuxiliaryClient`; the response was `hermes-live-5.5-ok`.
- Verified detached conversation worker behavior with the runtime smoke: the gateway conversation worker and nested background unit both ran under `hermes-worker.slice`, wrote a result artifact, and completed successfully.
- Confirmed the pending live reload watcher now exits cleanly when the gateway is already fresh.

Verification evidence:

- `pytest tests/agent/test_auxiliary_client.py tests/hermes_cli/test_codex_models.py tests/test_codex_oauth_adapter.py tests/test_litellm_control_config.py -q -o addopts=` passed with `138 passed`.
- `pytest tests/gateway/test_conversation_worker.py tests/tools/test_detached_runtime.py tests/tools/test_browser_detached_runtime.py tests/tools/test_rl_training_detached_runtime.py -q -o addopts=` passed with `34 passed`.
- `pytest tests/test_gateway_pending_live_reload.py tests/gateway/test_live_activation.py tests/hermes_cli/test_gateway_runtime_health.py tests/hermes_cli/test_gateway_service.py -q -o addopts=` passed with `85 passed`.
- `scripts/gateway_pending_live_reload.py` reported `gateway already fresh; nothing to do` after activation.
- `hermes-gateway.service` remained running as PID `2939282`, started at `Sat 2026-04-25 02:28:04 CST`.

Commit boundaries for this slice:

- Documentation evidence only. No behavior code changed in this ledger update.

Recorded on 2026-04-24 after the safe live-activation pass.

Completed slice:

- Safe live activation was applied during an empty-queue window. The gateway moved from PID `2766381` to PID `2876419` and stayed active.
- Post-activation gateway probe reports `code_stale=false`, `unit_definition_current=true`, `service_definition_stale=false`, and no active or queued tasks.
- Fixed false service-definition drift when the checker runs from `.venv` while the installed systemd service intentionally uses `venv`.
- Added foreground terminal `stdin` support so multi-line scripts can be passed as input instead of relying on shell heredoc syntax that Windows PowerShell can intercept.
- Verified the configured main model with a real `gpt-5.5` request through `auto -> CodexAuxiliaryClient`; the response was `hermes-5.5-ok`.
- Verified Telegram bot auth/network with `getMe` for `WuTJBot` without sending a chat message.
- Marked `/home/wutj/.hermes/.live_activation_pending.json` as `applied`; the Telegram status digest is now empty for pending activation.

Verification evidence:

- `pytest -n0 tests/agent/test_harness.py tests/gateway/test_status_command.py tests/gateway/test_live_activation.py tests/run_agent/test_harness_integration.py tests/agent/test_topic_router.py tests/agent/test_context_compressor.py tests/hermes_cli/test_gateway_service.py::TestSystemdServiceRefresh tests/tools/test_terminal_stdin.py tests/tools/test_local_persistent.py tests/tools/test_code_execution.py::TestHermesToolsGeneration tests/tools/test_terminal_tool_requirements.py -q --maxfail=1` passed with `121 passed`.
- `git diff --check` passed for the full worktree.
- `py_compile` passed for the changed gateway, CLI, and terminal-tool Python files.
- `systemd_unit_path_is_current('/etc/systemd/system/hermes-gateway.service', system=True)` returned `True`.

Commit boundaries for this slice:

- Self-retrofit task governance and visibility: `agent/harness.py`, `agent/topic_router.py`, `agent/context_compressor.py`, `gateway/run.py`, `gateway/live_activation.py`, `hermes_cli/config.py`, `run_agent.py`, and their focused tests.
- Terminal stdin bridge repair: `tools/terminal_tool.py`, `tools/code_execution_tool.py`, `tests/tools/test_terminal_stdin.py`, and related terminal/code-execution tests.
- Gateway service false-stale repair: `hermes_cli/gateway.py` and `tests/hermes_cli/test_gateway_service.py`.
- Documentation hygiene: `website/docs/user-guide/features/api-server.md` only removed a trailing EOF whitespace issue.
- Remaining dirty model-chain, detached-worker, and workspace-hygiene files below still need separate review and must not be bundled into this slice without a new verification note.

## Remaining Repair Evidence

Recorded on 2026-04-24 after the follow-up repair pass.

Additional fixes completed:

- ACP and experimental FastAPI tests now degrade cleanly when optional extras are missing.
- CLI quick-command no-output, timeout, error, missing-command, alias, and unsupported-type branches use the active console.
- Gateway approval, busy-input, plan-command, MCP toolset, housekeeping, and replace-cleanup regressions were repaired.
- `@folder:` context expansion now falls back when `rg` cannot be executed.
- `hermes_cli.setup` regained the setup wizard compatibility surface required by CLI and migration tests.
- Local STT tests can mock optional `faster_whisper` without installing the voice extra.
- Codex fallback tests now expect `gpt-5.5`, matching the current model policy.

Verification evidence:

- `tests/agent`: `921 passed`
- `tests/gateway`: `2324 passed, 41 skipped`
- `tests/hermes_cli`: `1517 passed, 6 skipped`
- `tests/tools`: `2621 passed, 30 skipped`
- `tests/run_agent`: `675 passed, 6 skipped`
- `tests/cli`: `359 passed`
- top-level `tests/test_*.py`: `448 passed, 1 skipped`
- `tests/cron tests/skills tests/plugins tests/honcho_plugin`: `500 passed, 4 skipped`
- `tests/acp tests/e2e`: `14 passed, 1 xfailed`
- `py_compile` passed for touched Python files.
- `git diff --check` passed.
- `hermes-gateway.service` stayed running as PID `2876419`; this pass did not restart the live Telegram gateway.

## Split And Commit Boundary

Recorded on 2026-04-25.

The current dirty workspace is verified but not safe to commit as whole files. Several files contain hunks from multiple retrofit lanes, especially:

- `gateway/run.py`
- `hermes_cli/config.py`
- `tools/terminal_tool.py`
- `tools/code_execution_tool.py`
- `hermes_cli/setup.py`
- `tests/gateway/test_status_command.py`

Use `plans/2026-04-25-hermes-retrofit-split-and-commit-plan.md` as the current split guide. The next safe step is patch-level staging by commit lane, not `git add` on whole shared files.

## Split Execution Result

Recorded after executing the split plan on 2026-04-25.

The original dirty workspace was split into focused commits instead of one large
mixed commit. The final stack covers task governance, harness integration, safe
live activation, terminal stdin repair, gpt-5.5 model routing, credential
rotation state, context-reference fallback, optional dependency guards,
detached gateway workers, API/cron task control, delegate/MCP worker detaching,
CLI/status repair visibility, Telegram fallback networking, deterministic tool
tests, docs, lockfiles, and ACP optional-extra collection.

Current explicit non-code residue:

- `.hermes/patches/*`
- `.hermes/plans/*`
- `.hermes/runtime-shadow/*`
- `.tmp/*`
- `runtime/gateway-*.log`
- `knowledge_collection_nvidia.md`

These files are not part of the retrofit code boundary. Keep them untracked
unless a future task explicitly asks to archive or promote the artifacts.

## Current Slice

The active slice is task governance, tool-limit pause behavior, topic routing, and Telegram-visible task status.

- `agent/harness.py`
- `tests/agent/test_harness.py`
- `tests/run_agent/test_harness_integration.py`
- `agent/topic_router.py`
- `tests/agent/test_topic_router.py`
- `run_agent.py`
- `hermes_cli/config.py`
- `gateway/run.py`
- `tests/gateway/test_status_command.py`

## Model Policy

- Main model policy: use `gpt-5.5`.
- Rate-limit policy: pause, record retry time, and resume later.
- Do not downgrade to a cheaper model for Hermes self-retrofit work.

## Ledger

Hermes workspace change ledger

Total dirty entries: 124

- task_governance: 5
  - `?? agent/harness.py`
  - `?? tests/agent/test_harness.py`
  - `?? tests/hermes_cli/test_harness_config.py`
  - `?? tests/run_agent/test_harness_integration.py`
  - `?? tests/test_model_tools_harness.py`
- model_chain: 18
  - `M agent/auxiliary_client.py`
  - `M agent/copilot_acp_client.py`
  - `M agent/credential_pool.py`
  - `M agent/model_metadata.py`
  - `M hermes_cli/codex_models.py`
  - `M hermes_cli/models.py`
  - `M model_tools.py`
  - `M tests/agent/test_auxiliary_client.py`
  - `M tests/agent/test_credential_pool.py`
  - `M tests/hermes_cli/test_codex_models.py`
  - `M tests/hermes_cli/test_setup_model_selection.py`
  - `M tests/run_agent/test_run_agent.py`
  - `M tests/test_model_tools.py`
  - `?? experimental/codex_oauth_adapter.py`
  - `?? scripts/hermes_litellm_control_config.py`
  - `?? tests/agent/test_copilot_acp_client.py`
  - `?? tests/test_codex_oauth_adapter.py`
  - `?? tests/test_litellm_control_config.py`
- gateway_worker: 24
  - `M gateway/config.py`
  - `M gateway/platforms/api_server.py`
  - `M gateway/platforms/telegram_network.py`
  - `M tests/gateway/test_api_server.py`
  - `M tests/gateway/test_api_server_jobs.py`
  - `M tests/gateway/test_api_server_toolset.py`
  - `M tests/gateway/test_approve_deny_commands.py`
  - `M tests/gateway/test_background_command.py`
  - `M tests/gateway/test_config.py`
  - `M tests/gateway/test_media_download_retry.py`
  - `M tests/gateway/test_reasoning_command.py`
  - `M tests/gateway/test_status.py`
  - `M tests/gateway/test_task_control.py`
  - `M tests/gateway/test_telegram_network.py`
  - `M tests/gateway/test_wecom.py`
  - `?? gateway/worker_runtime.py`
  - `?? scripts/gateway_detached_cutover.sh`
  - `?? scripts/gateway_detached_restart_verify.sh`
  - `?? tests/gateway/test_conversation_worker.py`
  - `?? tests/gateway/test_run_replace_cleanup.py`
  - `?? tests/tools/test_browser_detached_runtime.py`
  - `?? tests/tools/test_detached_runtime.py`
  - `?? tests/tools/test_rl_training_detached_runtime.py`
  - `?? tools/detached_runtime.py`
- tool_limits: 12
  - `M hermes_cli/config.py`
  - `M run_agent.py`
  - `M tests/tools/test_mcp_tool.py`
  - `M tests/tools/test_mcp_tool_issue_948.py`
  - `M tests/tools/test_process_registry.py`
  - `M tools/browser_tool.py`
  - `M tools/delegate_tool.py`
  - `M tools/mcp_tool.py`
  - `M tools/process_registry.py`
  - `M tools/terminal_tool.py`
  - `?? .hermes/runtime-shadow/run_agent.py`
  - `?? .tmp/gwconv-smoke/run_agent.py`
- context_management: 3
  - `M agent/topic_router.py`
  - `M tests/agent/test_topic_router.py`
  - `M tests/gateway/test_session_hygiene.py`
- visibility: 5
  - `M gateway/run.py`
  - `M gateway/status.py`
  - `M gateway/task_control.py`
  - `M tests/gateway/test_status_command.py`
  - `M tests/tools/test_notify_on_complete.py`
- workspace_hygiene: 57
  - `M cron/scheduler.py`
  - `M hermes_cli/cron.py`
  - `M hermes_cli/env_loader.py`
  - `M hermes_cli/gateway.py`
  - `M hermes_cli/main.py`
  - `M hermes_cli/setup.py`
  - `M hermes_cli/status.py`
  - `M hermes_cli/tools_config.py`
  - `M package-lock.json`
  - `M tests/cron/test_scheduler.py`
  - `M tests/hermes_cli/test_api_key_providers.py`
  - `M tests/hermes_cli/test_cron.py`
  - `M tests/hermes_cli/test_gateway_service.py`
  - `M tests/hermes_cli/test_status.py`
  - `M tests/hermes_cli/test_tools_config.py`
  - `M tests/tools/test_browser_camofox_state.py`
  - `M tests/tools/test_code_execution.py`
  - `M tests/tools/test_delegate.py`
  - `M tests/tools/test_docker_environment.py`
  - `M tests/tools/test_managed_media_gateways.py`
  - `M tests/tools/test_vision_tools.py`
  - `M tests/tools/test_voice_mode.py`
  - `M tests/tools/test_web_tools_tavily.py`
  - `M tests/tools/test_website_policy.py`
  - `M tools/code_execution_tool.py`
  - `M tools/rl_training_tool.py`
  - `M uv.lock`
  - `M website/docs/user-guide/features/api-server.md`
  - `M website/docs/user-guide/features/delegation.md`
  - `?? .hermes/patches/2026-04-24-gateway-control-plane-detached-worker.patch`
  - `?? .hermes/patches/2026-04-24-opencode-review.jsonl`
  - `?? .hermes/patches/2026-04-24-static-scan.txt`
  - `?? .hermes/plans/2026-04-22_154507-ps-c-users-wutj-c-windows-system32-wsl.md`
  - `?? .hermes/plans/2026-04-22_155715-replying-to-live-gateway-worker-cgroup-queue-uni.md`
  - `?? .hermes/plans/2026-04-22_160511-the-user-sent-an-image-here-s-what.md`
  - `?? .hermes/plans/2026-04-22_173958-replying-to-live-gateway-worker-cgroup-queue-uni.md`
  - `?? .hermes/plans/2026-04-22_175336-conversation-plan.md`
  - `?? .hermes/plans/2026-04-22_185238-conversation-plan.md`
  - `?? .hermes/plans/2026-04-23_010908-the-user-sent-an-image-here-s-what.md`
  - `?? .hermes/plans/2026-04-23_011915-replying-to-detached-worker-exited-before-sendin.md`
  - `?? .hermes/plans/2026-04-23_015852-conversation-plan.md`
  - `?? .hermes/plans/2026-04-23_154314-the-user-sent-an-image-here-s-what.md`
  - `?? .hermes/plans/2026-04-23_162626-the-user-sent-an-image-but-i-couldn.md`
  - `?? .hermes/plans/2026-04-23_180113-replying-to-live-gateway-worker-cgroup-queue-uni.md`
  - `?? .hermes/plans/2026-04-23_182246-conversation-plan.md`
  - `?? .hermes/runtime-shadow/agent/__init__.py`
  - `?? .hermes/runtime-shadow/agent/credential_pool.py`
  - `?? .tmp/gwconv-smoke/drive.py`
  - `?? .tmp/live_restart_verify.py`
  - `?? .tmp/verify_gwconv_runtime.py`
  - `?? knowledge_collection_nvidia.md`
  - `?? runtime/gateway-cutover.log`
  - `?? runtime/gateway-restart-verify.log`
  - `?? scripts/gateway_canonical_repair.py`
  - `?? scripts/gateway_pending_live_reload.py`
  - `?? tests/test_gateway_canonical_repair.py`
  - `?? tests/test_gateway_pending_live_reload.py`
