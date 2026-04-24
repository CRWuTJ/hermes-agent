#!/usr/bin/env python3
"""
Delegate Tool -- Subagent Architecture

Spawns child AIAgent instances with isolated context, restricted toolsets,
and their own terminal sessions. Supports single-task and batch (parallel)
modes. The parent blocks until all children complete.

Each child gets:
  - A fresh conversation (no parent history)
  - Its own task_id (own terminal session, file ops cache)
  - A restricted toolset (configurable, with blocked tools always stripped)
  - A focused system prompt built from the delegated goal + context

The parent's context only sees the delegation call and the summary result,
never the child's intermediate tool calls or reasoning.
"""

import json
import logging
logger = logging.getLogger(__name__)
import os
import re
import secrets
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests


# Tools that children must never have access to
DELEGATE_BLOCKED_TOOLS = frozenset([
    "delegate_task",   # no recursive delegation
    "clarify",         # no user interaction
    "memory",          # no writes to shared MEMORY.md
    "send_message",    # no cross-platform side effects
    "execute_code",    # children should reason step-by-step, not write scripts
])

MAX_CONCURRENT_CHILDREN = 3
MAX_DEPTH = 2  # parent (0) -> child (1) -> grandchild rejected (2)
DEFAULT_MAX_ITERATIONS = 50
DEFAULT_TOOLSETS = ["terminal", "file", "web"]
DEFAULT_CHILD_KEY_TTL_SECONDS = 24 * 60 * 60
DEFAULT_CODEX_KEYS_PATH = Path("/home/wutj/.hermes/codex_oauth_adapter_keys.json")
DELEGATE_ACTIVITY_POLL_INTERVAL = 1.0
UNSUPPORTED_CHILD_KEY_ENDPOINT_STATUS_CODES = frozenset({404, 405, 501})
UNSUPPORTED_CHILD_KEY_ENDPOINT_URLS: set[str] = set()


def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _child_api_key_settings(cfg: dict) -> Dict[str, Any]:
    raw = cfg.get("child_api_keys", {})
    if isinstance(raw, bool):
        raw_cfg: Dict[str, Any] = {"enabled": raw}
    elif isinstance(raw, dict):
        raw_cfg = raw
    else:
        raw_cfg = {}

    return {
        "enabled": _as_bool(raw_cfg.get("enabled"), False),
        "revoke_on_completion": _as_bool(raw_cfg.get("revoke_on_completion"), True),
        "label_prefix": str(raw_cfg.get("label_prefix") or "delegate").strip() or "delegate",
        "ttl_seconds": int(raw_cfg.get("ttl_seconds") or DEFAULT_CHILD_KEY_TTL_SECONDS),
        "admin_key": str(raw_cfg.get("admin_key") or "").strip(),
        "keys_path": str(raw_cfg.get("keys_path") or "").strip(),
        "timeout_seconds": float(raw_cfg.get("timeout_seconds") or 5.0),
        "allow_remote": _as_bool(raw_cfg.get("allow_remote"), False),
    }


def _safe_label_fragment(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")
    return (text or fallback)[:48]


def _internal_keys_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/internal/keys"
    return f"{base}/v1/internal/keys"


def _is_local_adapter_url(base_url: str) -> bool:
    lowered = str(base_url or "").lower()
    return (
        lowered.startswith("http://127.0.0.1:")
        or lowered.startswith("http://localhost:")
        or lowered.startswith("http://[::1]:")
    )


def _read_delegate_admin_key(settings: Dict[str, Any]) -> str:
    configured = str(settings.get("admin_key") or "").strip()
    if configured:
        return configured
    env_value = (
        os.getenv("HERMES_CODEX_ADAPTER_ADMIN_KEY", "").strip()
        or os.getenv("CODEX_ADAPTER_ADMIN_KEY", "").strip()
    )
    if env_value:
        return env_value

    keys_path = Path(
        settings.get("keys_path")
        or os.getenv("CODEX_ADAPTER_KEYS_PATH", "")
        or DEFAULT_CODEX_KEYS_PATH
    )
    try:
        payload = json.loads(keys_path.read_text())
    except Exception as exc:
        raise ValueError(
            f"Child API key creation is enabled, but admin key file is unreadable: {keys_path}"
        ) from exc
    admin_key = str(payload.get("admin_key") or "").strip()
    if not admin_key:
        raise ValueError("Child API key creation is enabled, but no adapter admin key was found.")
    return admin_key


def _issue_delegate_child_api_key(
    cfg: dict,
    creds: dict,
    task_index: int,
    goal: str,
    parent_agent,
) -> Optional[Dict[str, Any]]:
    settings = _child_api_key_settings(cfg)
    if not settings["enabled"]:
        return None

    api_mode = str(creds.get("api_mode") or "")
    base_url = str(creds.get("base_url") or "").strip()
    if api_mode != "codex_responses" or not base_url:
        return None
    if not settings["allow_remote"] and not _is_local_adapter_url(base_url):
        return None

    admin_key = _read_delegate_admin_key(settings)
    parent_session_id = getattr(parent_agent, "session_id", None)
    session_part = _safe_label_fragment(parent_session_id, "session")
    prefix = _safe_label_fragment(settings["label_prefix"], "delegate")
    label = f"{prefix}-{session_part}-{task_index}-{secrets.token_hex(4)}"

    ttl_seconds = int(settings.get("ttl_seconds") or 0)
    payload: Dict[str, Any] = {
        "label": label,
        "created_by": "delegate_task",
        "purpose": "child-agent",
        "session_id": str(parent_session_id or ""),
        "task_id": f"delegate-{task_index}",
    }
    if ttl_seconds > 0:
        payload["expires_at"] = int(time.time()) + ttl_seconds

    url = _internal_keys_url(base_url)
    if url in UNSUPPORTED_CHILD_KEY_ENDPOINT_URLS:
        logger.debug(
            "Skipping child API key mint at %s because this endpoint was previously marked unsupported.",
            url,
        )
        return None
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=settings["timeout_seconds"],
        )
    except requests.RequestException as exc:
        raise ValueError(f"Failed to create child API key through local codex adapter: {exc}") from exc

    if response.status_code in UNSUPPORTED_CHILD_KEY_ENDPOINT_STATUS_CODES:
        UNSUPPORTED_CHILD_KEY_ENDPOINT_URLS.add(url)
        logger.warning(
            "Child API key minting unavailable at %s (HTTP %s); falling back to parent credentials.",
            url,
            response.status_code,
        )
        return None
    if response.status_code >= 400:
        raise ValueError(f"Failed to create child API key: adapter returned HTTP {response.status_code}")
    data = response.json()
    api_key = str(data.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Failed to create child API key: adapter response did not include api_key")
    return {
        "label": str(data.get("label") or label),
        "api_key": api_key,
        "base_url": base_url,
        "admin_key": admin_key,
        "revoke_on_completion": bool(settings["revoke_on_completion"]),
        "timeout_seconds": settings["timeout_seconds"],
    }


def _revoke_delegate_child_api_key(lease: Dict[str, Any]) -> None:
    if not lease or not lease.get("revoke_on_completion"):
        return
    label = str(lease.get("label") or "").strip()
    base_url = str(lease.get("base_url") or "").strip()
    admin_key = str(lease.get("admin_key") or "").strip()
    if not label or not base_url or not admin_key:
        return
    url = f"{_internal_keys_url(base_url)}/{quote(label, safe='')}"
    try:
        response = requests.delete(
            url,
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=float(lease.get("timeout_seconds") or 5.0),
        )
        if response.status_code >= 400:
            logger.debug("Child API key revoke returned HTTP %s for %s", response.status_code, label)
    except requests.RequestException as exc:
        logger.debug("Failed to revoke child API key %s: %s", label, exc)


def _revoke_prepared_child_api_keys(children: List[tuple]) -> None:
    for _i, _task, child in children:
        lease = getattr(child, "_delegate_child_api_key", None)
        if isinstance(lease, dict):
            _revoke_delegate_child_api_key(lease)


def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
) -> str:
    """Build a focused system prompt for a child agent."""
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            "\nWORKSPACE PATH:\n"
            f"{workspace_path}\n"
            "Use this exact path for local repository/workdir operations unless the task explicitly says otherwise."
        )
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Important workspace rule: Never assume a repository lives at /workspace/... or any other container-style path unless the task/context explicitly gives that path. "
        "If no exact local path is provided, discover it first before issuing git/workdir-specific commands.\n\n"
        "Be thorough but concise -- your response is returned to the "
        "parent agent as a summary."
    )
    return "\n".join(parts)


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    """Best-effort local workspace hint for child prompts.

    We only inject a path when we have a concrete absolute directory. This avoids
    teaching subagents a fake container path while still helping them avoid
    guessing `/workspace/...` for local repo tasks.
    """
    candidates = [
        os.getenv("TERMINAL_CWD"),
        getattr(getattr(parent_agent, "_subdirectory_hints", None), "working_dir", None),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            text = os.path.abspath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if os.path.isabs(text) and os.path.isdir(text):
            return text
    return None


def _strip_blocked_tools(toolsets: List[str]) -> List[str]:
    """Remove toolsets that contain only blocked tools."""
    blocked_toolset_names = {
        "delegation", "clarify", "memory", "code_execution",
    }
    return [t for t in toolsets if t not in blocked_toolset_names]


def _build_child_progress_callback(task_index: int, parent_agent, task_count: int = 1) -> Optional[callable]:
    """Build a callback that relays child agent tool calls to the parent display.

    Two display paths:
      CLI:     prints tree-view lines above the parent's delegation spinner
      Gateway: batches tool names and relays to parent's progress callback

    Returns None if no display mechanism is available, in which case the
    child agent runs with no progress callback (identical to current behavior).
    """
    spinner = getattr(parent_agent, '_delegate_spinner', None)
    parent_cb = getattr(parent_agent, 'tool_progress_callback', None)

    if not spinner and not parent_cb:
        return None  # No display → no callback → zero behavior change

    # Show 1-indexed prefix only in batch mode (multiple tasks)
    prefix = f"[{task_index + 1}] " if task_count > 1 else ""

    # Gateway: batch tool names, flush periodically
    _BATCH_SIZE = 5
    _batch: List[str] = []

    def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
        # event_type is one of: "tool.started", "tool.completed",
        # "reasoning.available", "_thinking", "subagent_progress"

        # "_thinking" / reasoning events
        if event_type in ("_thinking", "reasoning.available"):
            text = preview or tool_name or ""
            if spinner:
                short = (text[:55] + "...") if len(text) > 55 else text
                try:
                    spinner.print_above(f" {prefix}├─ 💭 \"{short}\"")
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            # Don't relay thinking to gateway (too noisy for chat)
            return

        # tool.completed — no display needed here (spinner shows on started)
        if event_type == "tool.completed":
            return

        # tool.started — display and batch for parent relay
        if spinner:
            short = (preview[:35] + "...") if preview and len(preview) > 35 else (preview or "")
            from agent.display import get_tool_emoji
            emoji = get_tool_emoji(tool_name or "")
            line = f" {prefix}├─ {emoji} {tool_name}"
            if short:
                line += f"  \"{short}\""
            try:
                spinner.print_above(line)
            except Exception as e:
                logger.debug("Spinner print_above failed: %s", e)

        if parent_cb:
            _batch.append(tool_name or "")
            if len(_batch) >= _BATCH_SIZE:
                summary = ", ".join(_batch)
                try:
                    parent_cb("subagent_progress", f"🔀 {prefix}{summary}")
                except Exception as e:
                    logger.debug("Parent callback failed: %s", e)
                _batch.clear()

    def _flush():
        """Flush remaining batched tool names to gateway on completion."""
        if parent_cb and _batch:
            summary = ", ".join(_batch)
            try:
                parent_cb("subagent_progress", f"🔀 {prefix}{summary}")
            except Exception as e:
                logger.debug("Parent callback flush failed: %s", e)
            _batch.clear()

    _callback._flush = _flush
    return _callback


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    parent_agent,
    # Credential overrides from delegation config (provider:model resolution)
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    # ACP transport overrides — lets a non-ACP parent spawn ACP child agents
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
):
    """
    Build a child AIAgent on the main thread (thread-safe construction).
    Returns the constructed child agent without running it.

    When override_* params are set (from delegation config), the child uses
    those credentials instead of inheriting from the parent.  This enables
    routing subagents to a different provider:model pair (e.g. cheap/fast
    model on OpenRouter while the parent runs on Nous Portal).
    """
    from run_agent import AIAgent

    # When no explicit toolsets given, inherit from parent's enabled toolsets
    # so disabled tools (e.g. web) don't leak to subagents.
    # Note: enabled_toolsets=None means "all tools enabled" (the default),
    # so we must derive effective toolsets from the parent's loaded tools.
    parent_enabled = getattr(parent_agent, "enabled_toolsets", None)
    if parent_enabled is not None:
        parent_toolsets = set(parent_enabled)
    elif parent_agent and hasattr(parent_agent, "valid_tool_names"):
        # enabled_toolsets is None (all tools) — derive from loaded tool names
        import model_tools
        parent_toolsets = {
            ts for name in parent_agent.valid_tool_names
            if (ts := model_tools.get_toolset_for_tool(name)) is not None
        }
    else:
        parent_toolsets = set(DEFAULT_TOOLSETS)

    if toolsets:
        # Intersect with parent — subagent must not gain tools the parent lacks
        child_toolsets = _strip_blocked_tools([t for t in toolsets if t in parent_toolsets])
    elif parent_agent and parent_enabled is not None:
        child_toolsets = _strip_blocked_tools(parent_enabled)
    elif parent_toolsets:
        child_toolsets = _strip_blocked_tools(sorted(parent_toolsets))
    else:
        child_toolsets = _strip_blocked_tools(DEFAULT_TOOLSETS)

    workspace_hint = _resolve_workspace_hint(parent_agent)
    child_prompt = _build_child_system_prompt(goal, context, workspace_path=workspace_hint)
    # Extract parent's API key so subagents inherit auth (e.g. Nous Portal).
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # Build progress callback to relay tool calls to parent display
    child_progress_cb = _build_child_progress_callback(task_index, parent_agent)

    # Each subagent gets its own iteration budget capped at max_iterations
    # (configurable via delegation.max_iterations, default 50).  This means
    # total iterations across parent + subagents can exceed the parent's
    # max_iterations.  The user controls the per-subagent cap in config.yaml.

    child_thinking_cb = None
    if child_progress_cb:
        def _child_thinking(text: str) -> None:
            if not text:
                return
            try:
                child_progress_cb("_thinking", text)
            except Exception as e:
                logger.debug("Child thinking callback relay failed: %s", e)

        child_thinking_cb = _child_thinking

    # Resolve effective credentials: config override > parent inherit
    effective_model = model or parent_agent.model
    effective_provider = override_provider or getattr(parent_agent, "provider", None)
    effective_base_url = override_base_url or parent_agent.base_url
    effective_api_key = override_api_key or parent_api_key
    effective_api_mode = override_api_mode or getattr(parent_agent, "api_mode", None)
    effective_acp_command = override_acp_command or getattr(parent_agent, "acp_command", None)
    effective_acp_args = list(override_acp_args if override_acp_args is not None else (getattr(parent_agent, "acp_args", []) or []))

    child = AIAgent(
        base_url=effective_base_url,
        api_key=effective_api_key,
        model=effective_model,
        provider=effective_provider,
        api_mode=effective_api_mode,
        acp_command=effective_acp_command,
        acp_args=effective_acp_args,
        max_iterations=max_iterations,
        max_tokens=getattr(parent_agent, "max_tokens", None),
        reasoning_config=getattr(parent_agent, "reasoning_config", None),
        prefill_messages=getattr(parent_agent, "prefill_messages", None),
        enabled_toolsets=child_toolsets,
        quiet_mode=True,
        ephemeral_system_prompt=child_prompt,
        log_prefix=f"[subagent-{task_index}]",
        platform=parent_agent.platform,
        skip_context_files=True,
        skip_memory=True,
        clarify_callback=None,
        thinking_callback=child_thinking_cb,
        session_db=getattr(parent_agent, '_session_db', None),
        parent_session_id=getattr(parent_agent, 'session_id', None),
        parent_task_id=getattr(parent_agent, '_active_task_id', None),
        providers_allowed=parent_agent.providers_allowed,
        providers_ignored=parent_agent.providers_ignored,
        providers_order=parent_agent.providers_order,
        provider_sort=parent_agent.provider_sort,
        tool_progress_callback=child_progress_cb,
        iteration_budget=None,  # fresh budget per subagent
    )
    child._delegate_child_toolsets = list(child_toolsets)
    child._delegate_child_system_prompt = child_prompt
    child._delegate_child_acp_args = list(effective_acp_args)
    child._print_fn = getattr(parent_agent, '_print_fn', None)
    # Set delegation depth so children can't spawn grandchildren
    child._delegate_depth = getattr(parent_agent, '_delegate_depth', 0) + 1

    # Share a credential pool with the child when possible so subagents can
    # rotate credentials on rate limits instead of getting pinned to one key.
    child_pool = _resolve_child_credential_pool(effective_provider, parent_agent)
    if child_pool is not None:
        child._credential_pool = child_pool

    # Register child for interrupt propagation
    if hasattr(parent_agent, '_active_children'):
        lock = getattr(parent_agent, '_active_children_lock', None)
        if lock:
            with lock:
                parent_agent._active_children.append(child)
        else:
            parent_agent._active_children.append(child)

    return child


def _should_use_detached_delegate_worker(child, parent_agent) -> bool:
    try:
        from tools.detached_runtime import transient_unit_launcher_available
        from tools.terminal_tool import _has_live_or_origin_messaging_context
    except Exception:
        return False
    return _has_live_or_origin_messaging_context() and transient_unit_launcher_available()


def _detached_delegate_pool_hint(child) -> tuple[Optional[str], Optional[str]]:
    child_pool = getattr(child, "_credential_pool", None)
    if child_pool is None:
        return None, None
    provider = str(getattr(child_pool, "provider", "") or "").strip() or None
    preferred_id = None
    try:
        current_entry = child_pool.current()
    except Exception:
        current_entry = None
    if current_entry is None and hasattr(child_pool, "peek"):
        try:
            current_entry = child_pool.peek()
        except Exception:
            current_entry = None
    if current_entry is not None:
        preferred_id = str(getattr(current_entry, "id", "") or "").strip() or None
    return provider, preferred_id


def _build_detached_delegate_request(task_index: int, goal: str, child, parent_agent) -> Dict[str, Any]:
    child_task_id = getattr(child, "session_id", None) or f"delegate-{task_index}"
    runtime_kwargs: Dict[str, Any] = {
        "base_url": getattr(child, "base_url", None),
        "api_key": getattr(child, "api_key", None),
        "provider": getattr(child, "provider", None),
        "api_mode": getattr(child, "api_mode", None),
        "acp_command": getattr(child, "acp_command", None),
        "acp_args": list(getattr(child, "_delegate_child_acp_args", None) or getattr(child, "acp_args", []) or []),
        "max_tokens": getattr(child, "max_tokens", None),
        "skip_context_files": True,
        "skip_memory": True,
        "parent_session_id": getattr(child, "parent_session_id", None) or getattr(parent_agent, "session_id", None),
        "parent_task_id": getattr(child, "parent_task_id", None) or getattr(parent_agent, "_active_task_id", None),
        "log_prefix": getattr(child, "log_prefix", None),
    }
    credential_pool_provider, preferred_credential_id = _detached_delegate_pool_hint(child)
    return {
        "message": goal,
        "conversation_history": [],
        "max_iterations": int(getattr(child, "max_iterations", DEFAULT_MAX_ITERATIONS) or DEFAULT_MAX_ITERATIONS),
        "enabled_toolsets": list(getattr(child, "_delegate_child_toolsets", None) or getattr(child, "enabled_toolsets", None) or []),
        "ephemeral_system_prompt": getattr(child, "_delegate_child_system_prompt", None) or getattr(child, "ephemeral_system_prompt", None),
        "prefill_messages": getattr(child, "prefill_messages", None),
        "reasoning_config": getattr(child, "reasoning_config", None),
        "providers_allowed": getattr(child, "providers_allowed", None),
        "providers_ignored": getattr(child, "providers_ignored", None),
        "providers_order": getattr(child, "providers_order", None),
        "provider_sort": getattr(child, "provider_sort", None),
        "provider_require_parameters": bool(getattr(child, "provider_require_parameters", False)),
        "provider_data_collection": getattr(child, "provider_data_collection", None),
        "credential_pool_provider": credential_pool_provider,
        "preferred_credential_id": preferred_credential_id,
        "session_id": child_task_id,
        "platform": getattr(child, "platform", None),
        "model": getattr(child, "model", None),
        "with_session_db": bool(getattr(child, "_session_db", None)),
        "runtime_kwargs": runtime_kwargs,
        "fallback_model": getattr(child, "fallback_model", None),
    }


def _attach_detached_delegate_worker(task_index: int, goal: str, child, parent_agent):
    if not _should_use_detached_delegate_worker(child, parent_agent):
        return None
    try:
        from gateway.worker_runtime import start_gateway_conversation_worker
    except Exception:
        logger.debug("Detached delegate worker runtime unavailable", exc_info=True)
        return None
    try:
        handle = start_gateway_conversation_worker(
            request=_build_detached_delegate_request(task_index, goal, child, parent_agent),
        )
    except Exception:
        logger.warning("Detached delegate worker launch failed; falling back to in-process child execution.", exc_info=True)
        return None
    child._delegate_detached_handle = handle
    child.interrupt = handle.interrupt
    child.get_activity_summary = handle.get_activity_summary
    return handle


def _run_single_child(
    task_index: int,
    goal: str,
    child=None,
    parent_agent=None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Run a pre-built child agent. Called from within a thread.
    Returns a structured result dict.
    """
    child_start = time.monotonic()

    # Get the progress callback from the child agent
    child_progress_cb = getattr(child, 'tool_progress_callback', None)

    # Restore parent tool names using the value saved before child construction
    # mutated the global. This is the correct parent toolset, not the child's.
    import model_tools
    _saved_tool_names = getattr(child, "_delegate_saved_tool_names",
                                list(model_tools._last_resolved_tool_names))

    detached_handle = _attach_detached_delegate_worker(task_index, goal, child, parent_agent)
    child_pool = getattr(child, '_credential_pool', None)
    leased_cred_id = None
    if detached_handle is None and child_pool is not None:
        leased_cred_id = child_pool.acquire_lease()
        if leased_cred_id is not None:
            try:
                leased_entry = child_pool.current()
                if leased_entry is not None and hasattr(child, '_swap_credential'):
                    child._swap_credential(leased_entry)
            except Exception as exc:
                logger.debug("Failed to bind child to leased credential: %s", exc)

    try:
        child_task_id = getattr(child, "session_id", None) or f"delegate-{task_index}"
        if detached_handle is not None:
            result = detached_handle.wait_for_result_blocking()
        else:
            try:
                result = child.run_conversation(user_message=goal, task_id=child_task_id)
            except TypeError as exc:
                if "task_id" not in str(exc) or "unexpected keyword" not in str(exc):
                    raise
                result = child.run_conversation(user_message=goal)

        # Flush any remaining batched progress to gateway
        if detached_handle is None and child_progress_cb and hasattr(child_progress_cb, '_flush'):
            try:
                child_progress_cb._flush()
            except Exception as e:
                logger.debug("Progress callback flush failed: %s", e)

        duration = round(time.monotonic() - child_start, 2)

        summary = result.get("final_response") or ""
        completed = result.get("completed", False)
        interrupted = result.get("interrupted", False)
        api_calls = result.get("api_calls", 0)

        if interrupted:
            status = "interrupted"
        elif summary:
            # A summary means the subagent produced usable output.
            # exit_reason ("completed" vs "max_iterations") already
            # tells the parent *how* the task ended.
            status = "completed"
        else:
            status = "failed"

        # Build tool trace from conversation messages (already in memory).
        # Uses tool_call_id to correctly pair parallel tool calls with results.
        tool_trace: list[Dict[str, Any]] = []
        trace_by_id: Dict[str, Dict[str, Any]] = {}
        messages = result.get("messages") or []
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "assistant":
                    for tc in (msg.get("tool_calls") or []):
                        fn = tc.get("function", {})
                        entry_t = {
                            "tool": fn.get("name", "unknown"),
                            "args_bytes": len(fn.get("arguments", "")),
                        }
                        tool_trace.append(entry_t)
                        tc_id = tc.get("id")
                        if tc_id:
                            trace_by_id[tc_id] = entry_t
                elif msg.get("role") == "tool":
                    content = msg.get("content", "")
                    is_error = bool(
                        content and "error" in content[:80].lower()
                    )
                    result_meta = {
                        "result_bytes": len(content),
                        "status": "error" if is_error else "ok",
                    }
                    # Match by tool_call_id for parallel calls
                    tc_id = msg.get("tool_call_id")
                    target = trace_by_id.get(tc_id) if tc_id else None
                    if target is not None:
                        target.update(result_meta)
                    elif tool_trace:
                        # Fallback for messages without tool_call_id
                        tool_trace[-1].update(result_meta)

        # Determine exit reason
        if interrupted:
            exit_reason = "interrupted"
        elif completed:
            exit_reason = "completed"
        else:
            exit_reason = "max_iterations"

        # Extract token counts (safe for mock objects)
        if detached_handle is not None:
            _input_tokens = getattr(detached_handle, "session_prompt_tokens", 0)
            _output_tokens = getattr(detached_handle, "session_completion_tokens", 0)
            _model = getattr(detached_handle, "model", None) or getattr(child, "model", None)
        else:
            _input_tokens = getattr(child, "session_prompt_tokens", 0)
            _output_tokens = getattr(child, "session_completion_tokens", 0)
            _model = getattr(child, "model", None)

        entry: Dict[str, Any] = {
            "task_index": task_index,
            "status": status,
            "summary": summary,
            "api_calls": api_calls,
            "duration_seconds": duration,
            "model": _model if isinstance(_model, str) else None,
            "exit_reason": exit_reason,
            "tokens": {
                "input": _input_tokens if isinstance(_input_tokens, (int, float)) else 0,
                "output": _output_tokens if isinstance(_output_tokens, (int, float)) else 0,
            },
            "tool_trace": tool_trace,
        }
        if status == "failed":
            entry["error"] = result.get("error", "Subagent did not produce a response.")

        return entry

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logging.exception(f"[subagent-{task_index}] failed")
        return {
            "task_index": task_index,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "api_calls": 0,
            "duration_seconds": duration,
        }

    finally:
        child_key_lease = getattr(child, "_delegate_child_api_key", None)
        if isinstance(child_key_lease, dict):
            _revoke_delegate_child_api_key(child_key_lease)

        if child_pool is not None and leased_cred_id is not None:
            try:
                child_pool.release_lease(leased_cred_id)
            except Exception as exc:
                logger.debug("Failed to release credential lease: %s", exc)

        # Restore the parent's tool names so the process-global is correct
        # for any subsequent execute_code calls or other consumers.
        import model_tools

        saved_tool_names = getattr(child, "_delegate_saved_tool_names", None)
        if isinstance(saved_tool_names, list):
            model_tools._last_resolved_tool_names = list(saved_tool_names)

        # Remove child from active tracking

        # Unregister child from interrupt propagation
        if hasattr(parent_agent, '_active_children'):
            try:
                lock = getattr(parent_agent, '_active_children_lock', None)
                if lock:
                    with lock:
                        parent_agent._active_children.remove(child)
                else:
                    parent_agent._active_children.remove(child)
            except (ValueError, UnboundLocalError) as e:
                logger.debug("Could not remove child from active_children: %s", e)


def _relay_child_activity_to_parent(
    parent_agent,
    child,
    task_index: int,
    seen_activity_ts: Dict[int, float],
) -> None:
    """Mirror fresh child activity into the parent activity tracker.

    Cron inactivity monitoring only polls the parent agent. While delegate_task
    waits on child futures, the parent can look idle even though children are
    actively streaming, thinking, or calling tools. We mirror only *new* child
    activity timestamps so genuinely hung children still time out upstream.
    """
    touch_activity = getattr(parent_agent, "_touch_activity", None)
    if not callable(touch_activity) or not hasattr(child, "get_activity_summary"):
        return

    try:
        activity = child.get_activity_summary() or {}
    except Exception:
        return

    raw_ts = activity.get("last_activity_ts")
    try:
        last_ts = float(raw_ts)
    except (TypeError, ValueError):
        try:
            last_ts = time.time() - float(activity.get("seconds_since_activity", 0.0))
        except (TypeError, ValueError):
            return

    previous_ts = float(seen_activity_ts.get(task_index, 0.0) or 0.0)
    if last_ts <= previous_ts:
        return

    seen_activity_ts[task_index] = last_ts
    desc = str(activity.get("last_activity_desc") or "active").strip() or "active"
    current_tool = str(activity.get("current_tool") or "").strip()
    if current_tool and current_tool not in desc:
        desc = f"{desc} [{current_tool}]"
    try:
        touch_activity(f"delegate child[{task_index + 1}] {desc}")
    except Exception:
        pass



def _run_single_child_with_activity_monitor(
    task_index: int,
    goal: str,
    child,
    parent_agent,
    poll_interval: float = DELEGATE_ACTIVITY_POLL_INTERVAL,
) -> Dict[str, Any]:
    """Run one child in a worker thread while mirroring its activity upstream."""
    seen_activity_ts: Dict[int, float] = {}
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _run_single_child,
            task_index=task_index,
            goal=goal,
            child=child,
            parent_agent=parent_agent,
        )
        while True:
            done, _ = wait({future}, timeout=poll_interval)
            _relay_child_activity_to_parent(parent_agent, child, task_index, seen_activity_ts)
            if done:
                return future.result()


def delegate_task(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None,
    acp_command: Optional[str] = None,
    acp_args: Optional[List[str]] = None,
    parent_agent=None,
) -> str:
    """
    Spawn one or more child agents to handle delegated tasks.

    Supports two modes:
      - Single: provide goal (+ optional context, toolsets)
      - Batch:  provide tasks array [{goal, context, toolsets}, ...]

    Returns JSON with results array, one entry per task.
    """
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    # Depth limit
    depth = getattr(parent_agent, '_delegate_depth', 0)
    if depth >= MAX_DEPTH:
        return json.dumps({
            "error": (
                f"Delegation depth limit reached ({MAX_DEPTH}). "
                "Subagents cannot spawn further subagents."
            )
        })

    # Load config
    cfg = _load_config()
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    effective_max_iter = max_iterations or default_max_iter

    # Resolve delegation credentials (provider:model pair).
    # When delegation.provider is configured, this resolves the full credential
    # bundle (base_url, api_key, api_mode) via the same runtime provider system
    # used by CLI/gateway startup.  When unconfigured, returns None values so
    # children inherit from the parent.
    try:
        creds = _resolve_delegation_credentials(cfg, parent_agent)
    except ValueError as exc:
        return tool_error(str(exc))

    # Normalize to task list
    if tasks and isinstance(tasks, list):
        task_list = tasks[:MAX_CONCURRENT_CHILDREN]
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [{"goal": goal, "context": context, "toolsets": toolsets}]
    else:
        return tool_error("Provide either 'goal' (single task) or 'tasks' (batch).")

    if not task_list:
        return tool_error("No tasks provided.")

    # Validate each task has a goal
    for i, task in enumerate(task_list):
        if not task.get("goal", "").strip():
            return tool_error(f"Task {i} is missing a 'goal'.")

    overall_start = time.monotonic()
    results = []

    n_tasks = len(task_list)
    # Track goal labels for progress display (truncated for readability)
    task_labels = [t["goal"][:40] for t in task_list]

    # Save parent tool names BEFORE any child construction mutates the global.
    # _build_child_agent() calls AIAgent() which calls get_tool_definitions(),
    # which overwrites model_tools._last_resolved_tool_names with child's toolset.
    import model_tools as _model_tools
    _parent_tool_names = list(_model_tools._last_resolved_tool_names)

    # Build all child agents on the main thread (thread-safe construction)
    # Wrapped in try/finally so the global is always restored even if a
    # child build raises (otherwise _last_resolved_tool_names stays corrupted).
    children = []
    try:
        for i, t in enumerate(task_list):
            child_key_lease = None
            try:
                child_key_lease = _issue_delegate_child_api_key(
                    cfg=cfg,
                    creds=creds,
                    task_index=i,
                    goal=t["goal"],
                    parent_agent=parent_agent,
                )
                child_api_key = (
                    child_key_lease["api_key"]
                    if child_key_lease is not None
                    else creds["api_key"]
                )
            except ValueError as exc:
                _revoke_prepared_child_api_keys(children)
                return tool_error(str(exc))

            try:
                child = _build_child_agent(
                    task_index=i, goal=t["goal"], context=t.get("context"),
                    toolsets=t.get("toolsets") or toolsets, model=creds["model"],
                    max_iterations=effective_max_iter, parent_agent=parent_agent,
                    override_provider=creds["provider"], override_base_url=creds["base_url"],
                    override_api_key=child_api_key,
                    override_api_mode=creds["api_mode"],
                    override_acp_command=t.get("acp_command") or acp_command,
                    override_acp_args=t.get("acp_args") or acp_args,
                )
            except Exception:
                if child_key_lease is not None:
                    _revoke_delegate_child_api_key(child_key_lease)
                raise
            if child_key_lease is not None:
                child._delegate_child_api_key = child_key_lease
            # Override with correct parent tool names (before child construction mutated global)
            child._delegate_saved_tool_names = _parent_tool_names
            children.append((i, t, child))
    finally:
        # Authoritative restore: reset global to parent's tool names after all children built
        _model_tools._last_resolved_tool_names = _parent_tool_names

    if n_tasks == 1:
        # Single task -- still poll child activity so cron/origin watchdog sees progress.
        _i, _t, child = children[0]
        result = _run_single_child_with_activity_monitor(0, _t["goal"], child, parent_agent)
        results.append(result)
    else:
        # Batch -- run in parallel with per-task progress lines and mirror active
        # child timestamps into the parent so cron inactivity tracking reflects
        # real subagent work instead of treating delegate_task as idle.
        completed_count = 0
        spinner_ref = getattr(parent_agent, '_delegate_spinner', None)
        child_by_index = {i: child for i, _t, child in children}
        seen_activity_ts: Dict[int, float] = {}

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN) as executor:
            futures = {}
            for i, t, child in children:
                future = executor.submit(
                    _run_single_child,
                    task_index=i,
                    goal=t["goal"],
                    child=child,
                    parent_agent=parent_agent,
                )
                futures[future] = i

            pending = set(futures)
            while pending:
                done, pending = wait(
                    pending,
                    timeout=DELEGATE_ACTIVITY_POLL_INTERVAL,
                    return_when=FIRST_COMPLETED,
                )

                for future in list(pending):
                    idx = futures[future]
                    child = child_by_index.get(idx)
                    if child is not None:
                        _relay_child_activity_to_parent(parent_agent, child, idx, seen_activity_ts)

                if not done:
                    continue

                for future in done:
                    try:
                        entry = future.result()
                    except Exception as exc:
                        idx = futures[future]
                        entry = {
                            "task_index": idx,
                            "status": "error",
                            "summary": None,
                            "error": str(exc),
                            "api_calls": 0,
                            "duration_seconds": 0,
                        }
                    results.append(entry)
                    completed_count += 1

                    # Print per-task completion line above the spinner
                    idx = entry["task_index"]
                    label = task_labels[idx] if idx < len(task_labels) else f"Task {idx}"
                    dur = entry.get("duration_seconds", 0)
                    status = entry.get("status", "?")
                    icon = "✓" if status == "completed" else "✗"
                    remaining = n_tasks - completed_count
                    completion_line = f"{icon} [{idx+1}/{n_tasks}] {label}  ({dur}s)"
                    if spinner_ref:
                        try:
                            spinner_ref.print_above(completion_line)
                        except Exception:
                            print(f"  {completion_line}")
                    else:
                        print(f"  {completion_line}")

                    # Update spinner text to show remaining count
                    if spinner_ref and remaining > 0:
                        try:
                            spinner_ref.update_text(f"🔀 {remaining} task{'s' if remaining != 1 else ''} remaining")
                        except Exception as e:
                            logger.debug("Spinner update_text failed: %s", e)

        # Sort by task_index so results match input order
        results.sort(key=lambda r: r["task_index"])

    # Notify parent's memory provider of delegation outcomes
    if parent_agent and hasattr(parent_agent, '_memory_manager') and parent_agent._memory_manager:
        for entry in results:
            try:
                _task_goal = task_list[entry["task_index"]]["goal"] if entry["task_index"] < len(task_list) else ""
                parent_agent._memory_manager.on_delegation(
                    task=_task_goal,
                    result=entry.get("summary", "") or "",
                    child_session_id=getattr(children[entry["task_index"]][2], "session_id", "") if entry["task_index"] < len(children) else "",
                )
            except Exception:
                pass

    total_duration = round(time.monotonic() - overall_start, 2)

    return json.dumps({
        "results": results,
        "total_duration_seconds": total_duration,
    }, ensure_ascii=False)


def _resolve_child_credential_pool(effective_provider: Optional[str], parent_agent):
    """Resolve a credential pool for the child agent.

    Rules:
    1. Same provider as the parent -> share the parent's pool so cooldown state
       and rotation stay synchronized.
    2. Different provider -> try to load that provider's own pool.
    3. No pool available -> return None and let the child keep the inherited
       fixed credential behavior.
    """
    if not effective_provider:
        return getattr(parent_agent, "_credential_pool", None)

    parent_provider = getattr(parent_agent, "provider", None) or ""
    parent_pool = getattr(parent_agent, "_credential_pool", None)
    if parent_pool is not None and effective_provider == parent_provider:
        return parent_pool

    try:
        from agent.credential_pool import load_pool
        pool = load_pool(effective_provider)
        if pool is not None and pool.has_credentials():
            return pool
    except Exception as exc:
        logger.debug(
            "Could not load credential pool for child provider '%s': %s",
            effective_provider,
            exc,
        )
    return None


def _resolve_delegation_credentials(cfg: dict, parent_agent) -> dict:
    """Resolve credentials for subagent delegation.

    If ``delegation.base_url`` is configured, subagents use that direct
    OpenAI-compatible endpoint. Otherwise, if ``delegation.provider`` is
    configured, the full credential bundle (base_url, api_key, api_mode,
    provider) is resolved via the runtime provider system — the same path used
    by CLI/gateway startup. This lets subagents run on a completely different
    provider:model pair.

    If neither base_url nor provider is configured, returns None values so the
    child inherits everything from the parent agent.

    Raises ValueError with a user-friendly message on credential failure.
    """
    configured_model = str(cfg.get("model") or "").strip() or None
    configured_provider = str(cfg.get("provider") or "").strip() or None
    configured_base_url = str(cfg.get("base_url") or "").strip() or None
    configured_api_key = str(cfg.get("api_key") or "").strip() or None

    if configured_base_url:
        api_key = (
            configured_api_key
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        if not api_key:
            raise ValueError(
                "Delegation base_url is configured but no API key was found. "
                "Set delegation.api_key or OPENAI_API_KEY."
            )

        base_lower = configured_base_url.lower()
        provider = "custom"
        api_mode = "chat_completions"
        provider_lower = (configured_provider or "").lower()
        if "chatgpt.com/backend-api/codex" in base_lower or "codex" in provider_lower:
            provider = configured_provider or "openai-codex"
            api_mode = "codex_responses"
        elif "api.anthropic.com" in base_lower:
            provider = "anthropic"
            api_mode = "anthropic_messages"

        return {
            "model": configured_model,
            "provider": provider,
            "base_url": configured_base_url,
            "api_key": api_key,
            "api_mode": api_mode,
        }

    if not configured_provider:
        # No provider override — child inherits everything from parent
        return {
            "model": configured_model,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    # Provider is configured — resolve full credentials
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested=configured_provider)
    except Exception as exc:
        raise ValueError(
            f"Cannot resolve delegation provider '{configured_provider}': {exc}. "
            f"Check that the provider is configured (API key set, valid provider name), "
            f"or set delegation.base_url/delegation.api_key for a direct endpoint. "
            f"Available providers: openrouter, nous, zai, kimi-coding, minimax."
        ) from exc

    api_key = runtime.get("api_key", "")
    if not api_key:
        raise ValueError(
            f"Delegation provider '{configured_provider}' resolved but has no API key. "
            f"Set the appropriate environment variable or run 'hermes auth'."
        )

    return {
        "model": configured_model,
        "provider": runtime.get("provider"),
        "base_url": runtime.get("base_url"),
        "api_key": api_key,
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
    }


def _load_config() -> dict:
    """Load delegation config from CLI_CONFIG or persistent config.

    Checks the runtime config (cli.py CLI_CONFIG) first, then falls back
    to the persistent config (hermes_cli/config.py load_config()) so that
    ``delegation.model`` / ``delegation.provider`` are picked up regardless
    of the entry point (CLI, gateway, cron).
    """
    try:
        from cli import CLI_CONFIG
        cfg = CLI_CONFIG.get("delegation", {})
        if cfg:
            return cfg
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config
        full = load_config()
        return full.get("delegation", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OpenAI Function-Calling Schema
# ---------------------------------------------------------------------------

DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Spawn one or more subagents to work on tasks in isolated contexts. "
        "Each subagent gets its own conversation, terminal session, and toolset. "
        "Only the final summary is returned -- intermediate tool results "
        "never enter your context window.\n\n"
        "TWO MODES (one of 'goal' or 'tasks' is required):\n"
        "1. Single task: provide 'goal' (+ optional context, toolsets)\n"
        "2. Batch (parallel): provide 'tasks' array with up to 3 items. "
        "All run concurrently and results are returned together.\n\n"
        "WHEN TO USE delegate_task:\n"
        "- Reasoning-heavy subtasks (debugging, code review, research synthesis)\n"
        "- Tasks that would flood your context with intermediate data\n"
        "- Parallel independent workstreams (research A and B simultaneously)\n\n"
        "WHEN NOT TO USE (use these instead):\n"
        "- Mechanical multi-step work with no reasoning needed -> use execute_code\n"
        "- Single tool call -> just call the tool directly\n"
        "- Tasks needing user interaction -> subagents cannot use clarify\n\n"
        "IMPORTANT:\n"
        "- Subagents have NO memory of your conversation. Pass all relevant "
        "info (file paths, error messages, constraints) via the 'context' field.\n"
        "- Subagents CANNOT call: delegate_task, clarify, memory, send_message, "
        "execute_code.\n"
        "- Each subagent gets its own terminal session (separate working directory and state).\n"
        "- Results are always returned as an array, one entry per task."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What the subagent should accomplish. Be specific and "
                    "self-contained -- the subagent knows nothing about your "
                    "conversation history."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Background information the subagent needs: file paths, "
                    "error messages, project structure, constraints. The more "
                    "specific you are, the better the subagent performs."
                ),
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Toolsets to enable for this subagent. "
                    "Default: inherits your enabled toolsets. "
                    "Common patterns: ['terminal', 'file'] for code work, "
                    "['web'] for research, ['terminal', 'file', 'web'] for "
                    "full-stack tasks."
                ),
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Task goal"},
                        "context": {"type": "string", "description": "Task-specific context"},
                        "toolsets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Toolsets for this specific task. Use 'web' for network access, 'terminal' for shell.",
                        },
                        "acp_command": {
                            "type": "string",
                            "description": "Per-task ACP command override (e.g. 'claude'). Overrides the top-level acp_command for this task only.",
                        },
                        "acp_args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Per-task ACP args override.",
                        },
                    },
                    "required": ["goal"],
                },
                "maxItems": 3,
                "description": (
                    "Batch mode: up to 3 tasks to run in parallel. Each gets "
                    "its own subagent with isolated context and terminal session. "
                    "When provided, top-level goal/context/toolsets are ignored."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "description": (
                    "Max tool-calling turns per subagent (default: 50). "
                    "Only set lower for simple tasks."
                ),
            },
            "acp_command": {
                "type": "string",
                "description": (
                    "Override ACP command for child agents (e.g. 'claude', 'copilot'). "
                    "When set, children use ACP subprocess transport instead of inheriting "
                    "the parent's transport. Enables spawning Claude Code (claude --acp --stdio) "
                    "or other ACP-capable agents from any parent, including Discord/Telegram/CLI."
                ),
            },
            "acp_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Arguments for the ACP command (default: ['--acp', '--stdio']). "
                    "Only used when acp_command is set. Example: ['--acp', '--stdio', '--model', 'claude-opus-4-6']"
                ),
            },
        },
        "required": [],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"),
        context=args.get("context"),
        toolsets=args.get("toolsets"),
        tasks=args.get("tasks"),
        max_iterations=args.get("max_iterations"),
        acp_command=args.get("acp_command"),
        acp_args=args.get("acp_args"),
        parent_agent=kw.get("parent_agent")),
    check_fn=check_delegate_requirements,
    emoji="🔀",
)
