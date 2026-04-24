# Hermes Retrofit Split And Commit Plan

Recorded on 2026-04-25.

Goal: turn the current dirty Hermes retrofit workspace into reviewable, reversible change groups without breaking the live Telegram gateway.

## Current State

- Branch: `main`
- Live gateway policy: do not restart directly from cleanup work.
- Current workspace is not safe to commit as whole files because several files contain changes from more than one retrofit lane.
- Latest broad verification was completed by test directory, not one monolithic pytest run, because the full single-process command exceeds the tool timeout in this environment.

## Why This Needs Patch-Level Splitting

Some files are shared by multiple retrofit lanes:

- `gateway/run.py`: live activation visibility, task status, background task behavior, MCP toolset isolation, topic routing, approval flow, replace cleanup.
- `hermes_cli/config.py`: model policy, harness/tool-limit config, setup bridge.
- `tools/terminal_tool.py`: stdin bridge, live gateway service guard, tool-limit behavior.
- `tools/code_execution_tool.py`: terminal stdin bridge plus code execution surface updates.
- `hermes_cli/setup.py`: model setup, setup wizard compatibility, OpenClaw migration compatibility.
- `tests/gateway/test_status_command.py`: task visibility plus live activation digest coverage.

Do not stage those files wholesale unless the target commit intentionally owns every changed hunk in that file.

## Proposed Commit Stack

### Commit 1: Preserve Telegram continuity with visible task governance

Intent: make Telegram the lightweight command/reporting surface while long work is tracked and recoverable.

Likely files:

- `agent/harness.py`
- `agent/context_compressor.py`
- `agent/topic_router.py`
- `gateway/status.py`
- `gateway/task_control.py`
- `gateway/run.py` task/status/context hunks only
- `run_agent.py` harness/context hunks only
- `hermes_cli/config.py` harness config hunks only
- `tests/agent/test_harness.py`
- `tests/agent/test_context_compressor.py`
- `tests/agent/test_topic_router.py`
- `tests/run_agent/test_harness_integration.py`
- `tests/gateway/test_status_command.py`
- `tests/gateway/test_task_control.py`
- `tests/test_model_tools_harness.py`
- `tests/hermes_cli/test_harness_config.py`

Verification:

- `pytest tests/agent tests/run_agent/test_harness_integration.py tests/gateway/test_status_command.py tests/gateway/test_task_control.py tests/test_model_tools_harness.py tests/hermes_cli/test_harness_config.py -q -o addopts=`

### Commit 2: Add safe live activation and service stale detection

Intent: load verified retrofit changes into the running gateway only through an explicit quiet-window activation path.

Likely files:

- `gateway/live_activation.py`
- `scripts/gateway_pending_live_reload.py`
- `hermes_cli/gateway.py`
- `gateway/run.py` live activation digest/status hunks only
- `tests/gateway/test_live_activation.py`
- `tests/hermes_cli/test_gateway_service.py`
- `tests/test_gateway_pending_live_reload.py`
- `plans/2026-04-24-hermes-self-retrofit-workspace-ledger.md`

Verification:

- `pytest tests/gateway/test_live_activation.py tests/hermes_cli/test_gateway_service.py tests/test_gateway_pending_live_reload.py -q -o addopts=`
- `python -m py_compile gateway/live_activation.py hermes_cli/gateway.py`

### Commit 3: Repair terminal stdin for PowerShell and WSL bridges

Intent: let multi-line commands pass through tool stdin instead of depending on heredoc syntax that the outer shell can consume.

Likely files:

- `tools/terminal_tool.py` stdin hunks only
- `tools/code_execution_tool.py` stdin hunks only
- `tests/tools/test_terminal_stdin.py`
- `tests/tools/test_local_persistent.py`
- `tests/tools/test_code_execution.py`
- `tests/tools/test_terminal_tool_requirements.py`

Verification:

- `pytest tests/tools/test_terminal_stdin.py tests/tools/test_local_persistent.py tests/tools/test_code_execution.py tests/tools/test_terminal_tool_requirements.py -q -o addopts=`

### Commit 4: Stabilize remaining repair failures

Intent: keep the verified suite collectible and deterministic in the current base environment.

Likely files:

- `agent/context_references.py`
- `cli.py`
- `gateway/run.py` repair hunks only
- `hermes_cli/setup.py`
- `tools/transcription_tools.py`
- `tests/acp/conftest.py`
- `tests/agent/test_context_references.py`
- `tests/gateway/test_approve_deny_commands.py`
- `tests/gateway/test_background_command.py`
- `tests/gateway/test_config.py`
- `tests/gateway/test_reasoning_command.py`
- `tests/gateway/test_run_replace_cleanup.py`
- `tests/hermes_cli/test_setup_openclaw_migration.py`
- `tests/hermes_cli/test_setup_noninteractive.py`
- `tests/run_agent/test_provider_parity.py`
- `tests/test_codex_oauth_adapter.py`
- `plans/2026-04-24-hermes-remaining-retrofit-repair-plan.md`

Verification:

- `pytest tests/agent/test_context_references.py tests/cli/test_quick_commands.py tests/gateway/test_approve_deny_commands.py tests/gateway/test_background_command.py tests/gateway/test_config.py tests/gateway/test_reasoning_command.py tests/gateway/test_run_replace_cleanup.py tests/hermes_cli/test_setup_noninteractive.py tests/hermes_cli/test_setup_openclaw_migration.py tests/tools/test_transcription.py tests/run_agent/test_provider_parity.py tests/test_codex_oauth_adapter.py -q -o addopts=`

### Commit 5: Align model chain to gpt-5.5

Intent: make `gpt-5.5` the real main route without fallback to cheaper models.

Likely files:

- `agent/auxiliary_client.py`
- `agent/copilot_acp_client.py`
- `agent/credential_pool.py`
- `agent/model_metadata.py`
- `hermes_cli/codex_models.py`
- `hermes_cli/models.py`
- `model_tools.py`
- `scripts/hermes_litellm_control_config.py`
- `experimental/codex_oauth_adapter.py`
- model-related tests under `tests/agent`, `tests/hermes_cli`, `tests/run_agent`, and top-level `tests/test_*`.

Verification:

- `pytest tests/agent/test_auxiliary_client.py tests/agent/test_credential_pool.py tests/agent/test_copilot_acp_client.py tests/hermes_cli/test_codex_models.py tests/hermes_cli/test_setup_model_selection.py tests/run_agent/test_provider_parity.py tests/test_model_tools.py tests/test_codex_oauth_adapter.py tests/test_litellm_control_config.py -q -o addopts=`
- Real request check through `auto -> CodexAuxiliaryClient` with model `gpt-5.5`.

### Commit 6: Isolate detached gateway worker work

Intent: keep heavy execution and gateway worker replacement separate from model and status changes.

Likely files:

- `gateway/worker_runtime.py`
- `tools/detached_runtime.py`
- `scripts/gateway_detached_cutover.sh`
- `scripts/gateway_detached_restart_verify.sh`
- `gateway/platforms/api_server.py`
- `gateway/platforms/telegram_network.py`
- detached/runtime tests under `tests/gateway` and `tests/tools`
- `.hermes/patches/2026-04-24-gateway-control-plane-detached-worker.patch`

Verification:

- `pytest tests/gateway/test_conversation_worker.py tests/gateway/test_run_replace_cleanup.py tests/tools/test_detached_runtime.py tests/tools/test_browser_detached_runtime.py tests/tools/test_rl_training_detached_runtime.py -q -o addopts=`

### Commit 7: Documentation and generated artifacts

Intent: keep docs, lockfiles, runtime logs, and historical generated plans out of behavior commits.

Likely files:

- `website/docs/user-guide/features/api-server.md`
- `website/docs/user-guide/features/delegation.md`
- `package-lock.json`
- `uv.lock`
- `knowledge_collection_nvidia.md`
- `runtime/gateway-cutover.log`
- `runtime/gateway-restart-verify.log`
- historical `.hermes/plans/*`
- `.tmp/*` files

Action:

- Review before committing.
- Drop temporary `.tmp/*` and runtime logs unless they are required as evidence.
- Commit docs separately from lockfile changes.

## Commit Protocol

Use the Lore commit protocol from `AGENTS.md`. Each commit must include:

- intent line explaining why
- `Constraint:`
- `Confidence:`
- `Scope-risk:`
- `Tested:`
- `Not-tested:`

## Safety Checks Before Each Commit

- `git diff --check`
- targeted pytest command for that commit
- `python -m py_compile` for touched Python files
- `systemctl show hermes-gateway.service --property=MainPID,SubState,ExecMainStartTimestamp --no-pager`

## Current Decision

This plan has now been executed as a split commit stack. Do not use the earlier
"current decision" as live guidance for new edits; treat the sections below as
the historical split plan and the commit list here as the current handoff point.

Completed commits:

- `5afa94c0` Preserve Telegram continuity through task governance
- `f7b554d0` Keep long Hermes turns governed without dragging chat tools
- `8bfb45d6` Stage live gateway changes behind explicit activation
- `81c3e834` Carry multiline terminal payloads through stdin
- `2264a5eb` Keep Hermes model routes on gpt-5.5
- `24b5fe53` Cover scoped MCP discovery behavior
- `661e51c4` Persist credential rotation outside auth state
- `ad153cd3` Fall back when rg cannot list context files
- `7ec1c4b8` Keep transcription importable without faster-whisper
- `3f226a16` Recognize current MiniMax context windows
- `cc6bc4ee` Move live gateway heavy work into detached workers
- `2f3312e4` Expose API and cron task control lanes
- `93eb085c` Detach delegate and MCP stdio workers from live chat
- `dd0bca35` Surface Hermes repair state without touching live chat
- `b0ef2692` Keep Telegram fallback networking independent of local proxies
- `9516df86` Keep tool tests independent of local runtime state
- `e38d20d2` Document Hermes control-plane visibility endpoints
- `076732c8` Sync generated dependency locks with current manifests
- `630303f3` Keep optional ACP tests collectible without acp installed

Remaining untracked items after the split are intentionally left outside the
commit stack unless a future task explicitly promotes them:

- `.hermes/patches/*`: review/static-scan artifacts and historical patch dumps.
- `.hermes/plans/*`: runtime-generated conversation plans.
- `.hermes/runtime-shadow/*`: runtime shadow files.
- `.tmp/*`: one-off smoke/verification scripts.
- `runtime/gateway-*.log`: local activation/restart logs.
- `knowledge_collection_nvidia.md`: unrelated research note.

## Commit 1 Boundary Refinement

The first commit should be split into two smaller, reviewable parts:

### Commit 1A: Governance primitives and Telegram visibility

Safe whole-file candidates:

- `agent/harness.py`
- `agent/context_compressor.py`
- `agent/topic_router.py`
- `gateway/status.py`
- `gateway/task_control.py`
- `tests/agent/test_harness.py`
- `tests/agent/test_context_compressor.py`
- `tests/agent/test_topic_router.py`
- `tests/gateway/test_task_control.py`

Notes:

- `tests/agent/test_context_compressor.py` was restored to keep the existing test suite and only append the new "Do Not Do / Next Steps" prompt coverage.
- This commit should not include the agent/tool integration hooks yet.

Verification:

- `pytest tests/agent/test_harness.py tests/agent/test_context_compressor.py tests/agent/test_topic_router.py tests/gateway/test_task_control.py -q -o addopts=`
- `python -m py_compile agent/harness.py agent/context_compressor.py agent/topic_router.py gateway/status.py gateway/task_control.py`

### Commit 1B: Harness integration through agent and tool execution

Scope:

- `run_agent.py` harness admission, preflight, finalize, rate-limit pause, and scoped memory/skills review toolsets.
- `model_tools.py` harness preflight/start/complete/error plus lazy MCP discovery, because both reduce tool-chain drag in lightweight Telegram turns.
- `hermes_cli/config.py` harness default config and schema hunks.
- `tests/run_agent/test_harness_integration.py`
- `tests/test_model_tools_harness.py`
- `tests/hermes_cli/test_harness_config.py`

Still mixed and needs careful patch staging:

- `gateway/run.py` and `tests/gateway/test_status_command.py` mix task visibility, live activation, and budget-resume behavior.

Verification:

- `pytest tests/run_agent/test_harness_integration.py tests/test_model_tools_harness.py tests/hermes_cli/test_harness_config.py tests/test_model_tools.py -q -o addopts=`
- `python -m py_compile run_agent.py model_tools.py hermes_cli/config.py`
