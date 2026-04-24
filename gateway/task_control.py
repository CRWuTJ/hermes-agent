"""Shared task control-plane helpers for chat and HTTP surfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from task_lanes import normalize_task_lane


TaskDetailPayload = Dict[str, Any]
TaskListPayload = Dict[str, Any]
TaskActionPayload = Dict[str, Any]
TaskActionErrorPayload = Dict[str, Any]

_QUEUED_TASK_PRIORITY_BUCKETS = (
    (20, "now"),
    (50, "next"),
)
_QUEUE_PRIORITY_BUCKET_VALUES = {
    "now": 10,
    "next": 50,
    "later": 80,
}
_QUEUE_PRIORITY_BUCKET_OPTIONS = tuple(_QUEUE_PRIORITY_BUCKET_VALUES.keys())

_TASK_ACTION_ERROR_MESSAGES = {
    "task_not_found": {
        "http": "Task not found",
        "chat": "Task not found: {task_id}",
    },
    "task_already_active": {
        "http": "Task is already active — foreground is only supported for queued tasks.",
        "chat": "Task {task_id} is already active — foreground is only for queued tasks.",
    },
    "foreground_not_supported": {
        "http": "Task cannot be foregrounded from this adapter",
        "chat": "Task {task_id} cannot be foregrounded from this adapter.",
    },
    "cancel_not_supported": {
        "http": "Task cannot be cancelled from this adapter",
        "chat": "Task {task_id} cannot be cancelled from this adapter.",
    },
    "cancel_handle_missing": {
        "http": "Task is active but doesn't expose a cancel handle yet.",
        "chat": "Task {task_id} is active but doesn't expose a cancel handle yet.",
    },
    "reprioritize_not_supported": {
        "http": "Task cannot be reprioritized from this adapter",
        "chat": "Task {task_id} cannot be reprioritized from this adapter.",
    },
    "reprioritize_requires_queued_task": {
        "http": "Task is already active — reprioritize is only supported for queued tasks.",
        "chat": "Task {task_id} is already active — reprioritize is only for queued tasks.",
    },
    "recover_requires_queued_task": {
        "http": "Task is already active — recover is only supported for queued tasks.",
        "chat": "Task {task_id} is already active — recover is only for queued tasks.",
    },
    "recover_not_recommended": {
        "http": "Task doesn't currently have a recovery recommendation.",
        "chat": "Task {task_id} doesn't currently have a recovery recommendation.",
    },
    "invalid_priority_bucket": {
        "http": "Priority bucket must be one of: now, next, later.",
        "chat": "Priority bucket must be one of: now, next, later.",
    },
    "invalid_request_body": {
        "http": "Invalid JSON in request body.",
        "chat": "Invalid JSON in request body.",
    },
}


def normalize_task_actions(raw_actions: Any) -> List[str]:
    if not isinstance(raw_actions, (list, tuple)):
        return []
    actions: List[str] = []
    for raw_action in raw_actions:
        action = str(raw_action or "").strip()
        if action and action not in actions:
            actions.append(action)
    return actions


def normalize_priority_bucket(raw_bucket: Any) -> Optional[str]:
    bucket = str(raw_bucket or "").strip().lower()
    if bucket in _QUEUE_PRIORITY_BUCKET_VALUES:
        return bucket
    return None


def queued_task_priority_bucket_options(raw_options: Any = None) -> List[str]:
    if isinstance(raw_options, (list, tuple)):
        options: List[str] = []
        for raw_option in raw_options:
            bucket = normalize_priority_bucket(raw_option)
            if bucket and bucket not in options:
                options.append(bucket)
        if options:
            return options
    return list(_QUEUE_PRIORITY_BUCKET_OPTIONS)


def queued_task_priority_value(raw_bucket: Any, *, fallback: Optional[int] = None) -> Optional[int]:
    bucket = normalize_priority_bucket(raw_bucket)
    if bucket:
        return _QUEUE_PRIORITY_BUCKET_VALUES[bucket]
    return fallback


def queued_task_actions(adapter: Any) -> List[str]:
    actions: List[str] = []
    if adapter is None:
        return actions
    if callable(getattr(adapter, "foreground_pending_task", None)):
        actions.append("foreground")
    if callable(getattr(adapter, "reprioritize_pending_task", None)):
        actions.append("reprioritize")
    if callable(getattr(adapter, "cancel_pending_task", None)):
        actions.append("cancel")
    return actions


def parse_task_command_args(raw_args: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    tokens = [token for token in str(raw_args or "").split() if token]
    if not tokens:
        return None, None, None

    task_id = tokens[0]
    if len(tokens) == 1:
        return task_id, None, None

    action = str(tokens[1] or "").strip().lower()
    bucket = normalize_priority_bucket(action)
    if bucket:
        return task_id, "reprioritize", bucket
    if action in {"foreground", "cancel", "recover"}:
        return task_id, action, None
    if action == "reprioritize":
        return task_id, "reprioritize", normalize_priority_bucket(tokens[2] if len(tokens) >= 3 else None)
    return task_id, None, None


def task_command_usage_text() -> str:
    try:
        from hermes_cli.commands import command_usage_line

        usage_line = command_usage_line("task")
    except Exception:
        usage_line = None
    return (
        f"{usage_line or 'Usage: /task <task_id> [foreground|cancel|recover|now|next|later|reprioritize <bucket>]'}\n"
        "Examples: /task bg_123abc · /task bg_123abc foreground · /task bg_123abc later · "
        "/task bg_123abc reprioritize later · /task bg_123abc recover · /task bg_123abc cancel"
    )


def queued_task_source_label(message_event: Any) -> str:
    source = getattr(message_event, "source", None)
    platform = getattr(source, "platform", None)
    value = getattr(platform, "value", None)
    if value:
        return str(value)
    if platform is not None:
        return str(platform)
    return "unknown"


def queued_task_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _queued_task_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_queued_task_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_wait_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {remaining_minutes}m" if remaining_minutes else f"{hours}h"
    days = seconds / 86400
    return f"{days:.1f}d"


def queued_task_wait_seconds(queued_at: Any, *, now: Optional[datetime] = None, raw_wait_seconds: Any = None) -> Optional[int]:
    queued_dt = _parse_queued_task_datetime(queued_at)
    if queued_dt is None:
        try:
            return max(0, int(raw_wait_seconds)) if raw_wait_seconds is not None else None
        except (TypeError, ValueError):
            return None
    current = now or _queued_task_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    delta = int((current.astimezone(timezone.utc) - queued_dt).total_seconds())
    return max(0, delta)


def queued_task_wait_age(
    queued_at: Any,
    *,
    now: Optional[datetime] = None,
    raw_wait_seconds: Any = None,
    raw_wait_age: Any = None,
) -> Optional[str]:
    wait_seconds = queued_task_wait_seconds(queued_at, now=now, raw_wait_seconds=raw_wait_seconds)
    if wait_seconds is None:
        text = str(raw_wait_age or "").strip()
        return text or None
    return _format_wait_age(wait_seconds)


def queued_task_priority_bucket(priority: Any, raw_bucket: Any = None) -> str:
    bucket = normalize_priority_bucket(raw_bucket)
    if bucket:
        return bucket
    try:
        priority_value = int(priority if priority is not None else 50)
    except Exception:
        priority_value = 50
    for upper_bound, bucket_name in _QUEUED_TASK_PRIORITY_BUCKETS:
        if priority_value <= upper_bound:
            return bucket_name
    return "later"


def queued_task_payload(envelope: Any, *, actions: Optional[List[str]] = None) -> Dict[str, Any]:
    message_event = getattr(envelope, "message_event", None)
    priority = int(getattr(envelope, "priority", 50) or 50)
    normalized_actions = normalize_task_actions(actions)
    queued_at = queued_task_iso(getattr(envelope, "queued_at", None))
    wait_seconds = queued_task_wait_seconds(queued_at)
    wait_age = queued_task_wait_age(queued_at, raw_wait_seconds=wait_seconds)
    return {
        "task_id": str(getattr(envelope, "task_id", "") or ""),
        "state": "queued",
        "session_key": str(getattr(envelope, "session_key", "") or ""),
        "lane": getattr(envelope, "lane", None),
        "priority": priority,
        "priority_bucket": queued_task_priority_bucket(priority),
        "priority_bucket_options": queued_task_priority_bucket_options() if "reprioritize" in normalized_actions else [],
        "reply_policy": str(getattr(envelope, "reply_policy", "") or ""),
        "cancellation_policy": str(getattr(envelope, "cancellation_policy", "") or ""),
        "queued_at": queued_at,
        "wait_seconds": wait_seconds,
        "wait_age": wait_age,
        "reason": str(getattr(envelope, "reason", "") or ""),
        "preview": getattr(message_event, "text", None),
        "kind": "queued_message",
        "control_mode": "queued",
        "actions": normalized_actions,
        "source": queued_task_source_label(message_event),
    }


def build_gateway_tasks_payload(*, queued: Any = None, live: Any = None) -> TaskListPayload:
    queued_payload = queued if isinstance(queued, dict) else {"tasks": list(queued or [])}
    live_payload = live if isinstance(live, dict) else {"tasks": list(live or [])}

    try:
        from gateway.status import normalize_live_tasks_payload, normalize_queued_tasks_payload

        normalized_queued = normalize_queued_tasks_payload(queued_payload)
        normalized_live = normalize_live_tasks_payload(live_payload)
    except Exception:
        normalized_queued = {
            "queued_count": len(list(queued_payload.get("tasks") or [])),
            "lane_counts": {
                "interactive": 0,
                "cron_scout": 0,
                "housekeeping": 0,
            },
            "tasks": list(queued_payload.get("tasks") or []),
        }
        normalized_live = {
            "active_count": len(list(live_payload.get("tasks") or [])),
            "lane_counts": {
                "interactive": 0,
                "cron_scout": 0,
                "housekeeping": 0,
            },
            "tasks": list(live_payload.get("tasks") or []),
        }

    return {
        "queued": normalized_queued,
        "live": normalized_live,
    }


def _task_looks_queued(task_payload: Dict[str, Any]) -> bool:
    state_text = str(task_payload.get("state") or "").strip().lower()
    if state_text == "queued":
        return True
    control_mode = str(task_payload.get("control_mode") or "").strip().lower()
    if control_mode == "queued":
        return True
    for key in ("priority", "priority_bucket", "queued_at", "reply_policy", "cancellation_policy"):
        value = task_payload.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    actions = normalize_task_actions(task_payload.get("actions"))
    return any(action in {"foreground", "reprioritize"} for action in actions)


def _normalize_task_payload_for_state(state: Any, task: Dict[str, Any]) -> Dict[str, Any]:
    task_payload = dict(task or {})
    state_text = str(state or "").strip().lower()
    treat_as_queued = state_text == "queued" or _task_looks_queued(task_payload)

    try:
        if treat_as_queued:
            from gateway.status import normalize_queued_tasks_payload

            normalized = normalize_queued_tasks_payload({"tasks": [task_payload]})
        else:
            from gateway.status import normalize_live_tasks_payload

            normalized = normalize_live_tasks_payload({"tasks": [task_payload]})
        tasks = normalized.get("tasks") if isinstance(normalized, dict) else None
        if isinstance(tasks, list) and tasks:
            normalized_task = tasks[0]
            if isinstance(normalized_task, dict):
                result = dict(normalized_task)
                if isinstance(task_payload.get("starvation_alert"), dict) and not isinstance(result.get("starvation_alert"), dict):
                    result["starvation_alert"] = dict(task_payload["starvation_alert"])
                return result
    except Exception:
        pass

    return task_payload


def queued_task_starvation_alert(task_id: Any, queued_summary: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(queued_summary, dict):
        return None
    task_id_text = str(task_id or "").strip()
    if not task_id_text:
        return None
    alert = queued_summary.get("starvation_alert")
    if not isinstance(alert, dict):
        return None
    if str(alert.get("oldest_task_id") or "").strip() != task_id_text:
        return None
    return dict(alert)


def format_task_starvation_alert(alert: Any) -> Optional[str]:
    if not isinstance(alert, dict):
        return None
    try:
        from gateway.status import format_status_starvation_alert

        formatted = format_status_starvation_alert(alert)
        if formatted:
            return formatted
    except Exception:
        pass
    level = str(alert.get("level") or "warning").strip()
    reason = str(alert.get("reason") or "").strip()
    suggested_command = str(alert.get("suggested_command") or "").strip()
    parts = [part for part in [level, reason, suggested_command] if part]
    return " · ".join(parts) or None


def queued_task_recovery_plan(task: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(task, dict):
        return None
    alert = task.get("starvation_alert")
    if not isinstance(alert, dict):
        return None
    suggested_action = str(alert.get("suggested_action") or "").strip().lower()
    if suggested_action == "reprioritize":
        target_bucket = normalize_priority_bucket(alert.get("suggested_bucket"))
        if not target_bucket:
            return None
        return {
            "action": "reprioritize",
            "target_bucket": target_bucket,
            "suggested_command": str(alert.get("suggested_command") or "").strip() or None,
        }
    if suggested_action == "foreground":
        return {
            "action": "foreground",
            "target_bucket": None,
            "suggested_command": str(alert.get("suggested_command") or "").strip() or None,
        }
    return None


def render_task_command_hints(
    task_id: str,
    *,
    actions: Optional[List[str]] = None,
    priority_bucket_options: Any = None,
) -> Optional[str]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    commands = [f"/task {task_id}"]
    normalized_actions = normalize_task_actions(actions)
    if "reprioritize" in normalized_actions:
        bucket_options = queued_task_priority_bucket_options(priority_bucket_options)
        if bucket_options:
            commands.append(f"/task {task_id} {'|'.join(bucket_options)}")
        normalized_actions = [action for action in normalized_actions if action != "reprioritize"]
    for action in normalized_actions:
        commands.append(f"/task {task_id} {action}")
    return f"↳ {' · '.join(commands)}"


def render_gateway_tasks_block(
    tasks_payload: Any,
    *,
    preview_formatter: Optional[Callable[[Any], str]] = None,
) -> str:
    payload = tasks_payload if isinstance(tasks_payload, dict) else {}
    queued_payload = payload.get("queued") if isinstance(payload.get("queued"), dict) else {}
    live_payload = payload.get("live") if isinstance(payload.get("live"), dict) else {}
    queued_tasks = queued_payload.get("tasks") if isinstance(queued_payload.get("tasks"), list) else []
    live_tasks = live_payload.get("tasks") if isinstance(live_payload.get("tasks"), list) else []
    queued_count = int(queued_payload.get("queued_count") or len(queued_tasks))
    active_count = int(live_payload.get("active_count") or len(live_tasks))

    lines = [
        "📋 **Hermes Tasks**",
        "",
        f"**Queued for this chat:** {queued_count}",
    ]
    for idx, task in enumerate(queued_tasks, start=1):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "").strip()
        preview = task.get("preview")
        if preview_formatter is not None:
            preview = preview_formatter(preview)
        else:
            preview = " ".join(str(preview or "").split()).strip()
        parts = [
            f"{idx}. `{task_id}`",
            queued_task_priority_bucket(task.get("priority"), task.get("priority_bucket")),
            normalize_task_lane(task.get("lane")),
            f"priority={int(task.get('priority', 0) or 0)}",
        ]
        reply_policy = task.get("reply_policy")
        if reply_policy is not None:
            parts.append(f"reply={reply_policy}")
        if preview:
            parts.append(str(preview))
        lines.append(" · ".join(parts))
        hint_line = render_task_command_hints(
            task_id,
            actions=task.get("actions"),
            priority_bucket_options=task.get("priority_bucket_options"),
        )
        if hint_line:
            lines.append(f"   {hint_line}")
        recent_line = format_harness_recent_control_line(task)
        if recent_line:
            lines.append(f"   {recent_line}")
        alert = task.get("starvation_alert") if isinstance(task.get("starvation_alert"), dict) else None
        alert_line = format_task_starvation_alert(alert)
        if alert_line:
            suggested_command = str((alert or {}).get("suggested_command") or "").strip()
            bucket = str((alert or {}).get("bucket") or "").strip()
            threshold_age = str((alert or {}).get("threshold_age") or "").strip()
            current_wait_age = str((alert or {}).get("current_wait_age") or "").strip()
            if suggested_command:
                detail_parts = []
                if bucket and threshold_age:
                    detail_parts.append(f"{bucket} bucket exceeded {threshold_age} threshold")
                else:
                    reason = str((alert or {}).get("reason") or "").strip()
                    if reason:
                        detail_parts.append(reason)
                if current_wait_age:
                    detail_parts.append(f"waiting {current_wait_age}")
                detail_text = " · ".join(detail_parts)
                if detail_text:
                    lines.append(f"   ⚠ {suggested_command} — {detail_text}")
                else:
                    lines.append(f"   ⚠ {suggested_command}")
            else:
                lines.append(f"   ⚠ {alert_line}")

    lines.extend([
        "",
        f"**Active runtime tasks:** {active_count}",
    ])
    for idx, task in enumerate(live_tasks, start=1):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "").strip()
        parts = [f"{idx}. `{task_id}`", normalize_task_lane(task.get("lane"))]
        label = task.get("label")
        source = task.get("source")
        running_age = str(task.get("running_age") or "").strip()
        if label:
            parts.append(str(label))
        if source:
            parts.append(f"source={source}")
        if running_age:
            parts.append(f"running {running_age}")
        lines.append(" · ".join(parts))
        hint_line = render_task_command_hints(task_id, actions=task.get("actions"))
        if hint_line:
            lines.append(f"   {hint_line}")
        recent_line = format_harness_recent_control_line(task)
        if recent_line:
            lines.append(f"   {recent_line}")

    if not queued_tasks and not live_tasks:
        lines.append("No queued or active tasks.")

    return "\n".join(lines)


def build_task_detail_payload(*, task_id: str, state: str, task: Dict[str, Any]) -> TaskDetailPayload:
    task_payload = _normalize_task_payload_for_state(state, task)
    actions = normalize_task_actions(task_payload.get("actions"))
    task_payload["actions"] = actions
    state_text = str(state or "").strip().lower()
    if state_text == "queued" or "reprioritize" in actions or task_payload.get("priority_bucket_options") is not None:
        task_payload["priority_bucket_options"] = queued_task_priority_bucket_options(task_payload.get("priority_bucket_options"))
        task_payload["queued_at"] = queued_task_iso(task_payload.get("queued_at"))
        task_payload["wait_seconds"] = queued_task_wait_seconds(
            task_payload.get("queued_at"),
            raw_wait_seconds=task_payload.get("wait_seconds"),
        )
        task_payload["wait_age"] = queued_task_wait_age(
            task_payload.get("queued_at"),
            raw_wait_seconds=task_payload.get("wait_seconds"),
            raw_wait_age=task_payload.get("wait_age"),
        )
    if task_payload.get("started_at") is not None:
        running_seconds = queued_task_wait_seconds(
            task_payload.get("started_at"),
            raw_wait_seconds=task_payload.get("running_seconds"),
        )
        running_age = queued_task_wait_age(
            task_payload.get("started_at"),
            raw_wait_seconds=running_seconds,
            raw_wait_age=task_payload.get("running_age"),
        )
        if running_seconds is not None:
            task_payload["running_seconds"] = running_seconds
        else:
            task_payload.pop("running_seconds", None)
        if running_age is not None:
            task_payload["running_age"] = running_age
        else:
            task_payload.pop("running_age", None)
    return {
        "task_id": str(task_id or "").strip(),
        "state": str(state or "").strip(),
        "task": task_payload,
    }


def describe_task_action_result(
    *,
    task_id: str,
    action: str,
    status: str,
    target_bucket: Any = None,
    markdown_task_id: bool = False,
) -> str:
    action_text = str(action or "").strip().lower()
    status_text = str(status or "").strip().lower()
    rendered_task_id = f"`{task_id}`" if markdown_task_id else str(task_id or "").strip()

    if action_text == "foreground":
        if status_text == "queued_next":
            return f"Foregrounded queued task {rendered_task_id} — it will run next after the current task."
        return f"Foregrounded queued task {rendered_task_id} — starting now."

    if action_text == "reprioritize":
        bucket = normalize_priority_bucket(target_bucket) or "next"
        return f"Moved queued task {rendered_task_id} to {bucket} priority."

    if action_text == "recover":
        bucket = normalize_priority_bucket(target_bucket)
        if bucket:
            return f"Recovered queued task {rendered_task_id} — moved it to {bucket} priority."
        if status_text == "queued_next":
            return f"Recovered queued task {rendered_task_id} — it will run next after the current task."
        return f"Recovered queued task {rendered_task_id} — starting now."

    if action_text == "cancel":
        if status_text == "cancellation_requested":
            return f"Cancellation requested for active task {rendered_task_id}."
        return f"Cancelled queued task {rendered_task_id}."

    return f"Updated task {rendered_task_id}."


def build_task_action_payload(
    *,
    task_id: str,
    action: str,
    status: str,
    task: Dict[str, Any],
    message: Optional[str] = None,
    target_bucket: Any = None,
) -> TaskActionPayload:
    raw_task_payload = dict(task or {})
    task_state = "queued" if _task_looks_queued(raw_task_payload) else "active"
    task_payload = _normalize_task_payload_for_state(task_state, raw_task_payload)
    actions = normalize_task_actions(task_payload.get("actions"))
    task_payload["actions"] = actions
    if task_payload.get("priority_bucket_options") is not None or "reprioritize" in actions:
        task_payload["priority_bucket_options"] = queued_task_priority_bucket_options(task_payload.get("priority_bucket_options"))
    effective_bucket = normalize_priority_bucket(target_bucket) or task_payload.get("priority_bucket")
    return {
        "task_id": str(task_id or "").strip(),
        "action": str(action or "").strip(),
        "status": str(status or "").strip(),
        "message": message or describe_task_action_result(
            task_id=task_id,
            action=action,
            status=status,
            target_bucket=effective_bucket,
        ),
        "task": task_payload,
    }


def describe_task_action_error(
    *,
    task_id: str,
    action: str,
    reason_code: str,
    surface: str = "http",
    markdown_task_id: bool = False,
    include_tasks_hint: bool = False,
) -> str:
    surface_key = "chat" if str(surface or "").strip().lower() == "chat" else "http"
    task_id_text = str(task_id or "").strip()
    if markdown_task_id and task_id_text:
        task_id_text = f"`{task_id_text}`"

    template = (_TASK_ACTION_ERROR_MESSAGES.get(str(reason_code or "").strip()) or {}).get(surface_key)
    if not template:
        template = (_TASK_ACTION_ERROR_MESSAGES.get("task_not_found") or {}).get(surface_key, "Task not found")
    message = template.format(task_id=task_id_text)
    if include_tasks_hint and surface_key == "chat":
        return f"{message}\nUse /tasks to list queued and active tasks."
    return message


def build_task_action_error_payload(
    *,
    task_id: str,
    action: str,
    reason_code: str,
    message: Optional[str] = None,
) -> TaskActionErrorPayload:
    return {
        "error": message
        or describe_task_action_error(
            task_id=task_id,
            action=action,
            reason_code=reason_code,
            surface="http",
        ),
        "reason_code": str(reason_code or "").strip(),
    }


def record_harness_task_action(
    *,
    task_id: str,
    action: str,
    status: str,
    surface: str,
    session_key: str = "",
    platform: str = "",
    target_bucket: Any = None,
    task_context: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    task_id = str(task_id or "").strip()
    if not task_id:
        return
    try:
        from agent.harness import get_harness_manager

        manager = get_harness_manager()
        if not getattr(manager, "enabled", False):
            return
        manager.record_task_action(
            task_id=task_id,
            action=action,
            status=status,
            surface=surface,
            session_key=session_key,
            platform=platform,
            target_bucket=target_bucket,
            task_context=task_context,
            metadata=metadata,
        )
    except Exception:
        return


def humanize_task_kind(kind: Any) -> str:
    text = " ".join(str(kind or "").replace("_", " ").split()).strip()
    return text


def format_harness_control_summary(action_payload: Any) -> Optional[str]:
    if not isinstance(action_payload, dict):
        return None
    action = str(action_payload.get("action") or "").strip()
    status = str(action_payload.get("status") or "").strip()
    surface = str(action_payload.get("surface") or "").strip()
    target_bucket = str(action_payload.get("target_bucket") or "").strip()
    created_at = str(action_payload.get("created_at") or "").strip()
    parts = [part for part in [action, status] if part]
    if surface:
        parts.append(f"via {surface}")
    if target_bucket:
        parts.append(target_bucket)
    if created_at:
        parts.append(created_at)
    return " · ".join(parts) if parts else None


def format_harness_recent_control_line(task_payload: Any) -> Optional[str]:
    task = task_payload if isinstance(task_payload, dict) else {}
    harness = task.get("harness") if isinstance(task.get("harness"), dict) else {}
    harness_control = harness.get("control") if isinstance(harness.get("control"), dict) else {}
    latest_control_action = format_harness_control_summary(harness_control.get("latest_action"))
    latest_recovery = format_harness_control_summary(harness_control.get("latest_recovery"))
    if not latest_control_action and not latest_recovery:
        return None
    parts: List[str] = []
    if latest_control_action:
        parts.append(latest_control_action)
    if latest_recovery and latest_recovery != latest_control_action:
        parts.append(f"recovery: {latest_recovery}")
    return f"↳ recent: {' ; '.join(parts)}" if parts else None


def describe_task_control(detail_payload: Any) -> Optional[str]:
    payload = detail_payload if isinstance(detail_payload, dict) else {}
    task_id = str(payload.get("task_id") or "").strip()
    state = str(payload.get("state") or "").strip().lower()
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    actions = normalize_task_actions(task.get("actions"))
    control_mode = str(task.get("control_mode") or "").strip().lower()

    if state == "queued":
        bucket_options = queued_task_priority_bucket_options(task.get("priority_bucket_options")) if "reprioritize" in actions else []
        recovery_plan = queued_task_recovery_plan(task)
        controls: List[str] = []
        if recovery_plan is not None:
            controls.append(f"/task {task_id} recover")
        if bucket_options:
            controls.append(f"/task {task_id} {'|'.join(bucket_options)} to reprioritize")
        if "foreground" in actions:
            controls.append(f"/task {task_id} foreground to run next")
        if "cancel" in actions:
            controls.append(f"/task {task_id} cancel")
        if controls:
            if len(controls) == 1:
                return f"queued for this chat — use {controls[0]}."
            return f"queued for this chat — use {'; '.join(controls)}."
        return "queued for this chat — no direct control actions are exposed."

    if control_mode == "managed_runtime" or (control_mode != "read_only" and "cancel" in actions):
        if "cancel" in actions:
            return f"gateway-managed runtime task — cancel available via /task {task_id} cancel."
        return "gateway-managed runtime task — no direct control actions are exposed."

    return "read-only live turn — tracked for status only; no cancel handle is registered."


def render_gateway_task_detail_block(
    detail_payload: Any,
    *,
    preview_formatter: Optional[Callable[[Any], str]] = None,
) -> str:
    payload = detail_payload if isinstance(detail_payload, dict) else {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    task_id = str(payload.get("task_id") or "").strip()
    state = str(payload.get("state") or "").strip()
    lane = task.get("lane")
    priority = task.get("priority")
    priority_bucket = queued_task_priority_bucket(priority, task.get("priority_bucket")) if priority is not None else None
    priority_bucket_options = queued_task_priority_bucket_options(task.get("priority_bucket_options")) if task.get("priority_bucket_options") is not None else []
    reply_policy = task.get("reply_policy")
    queued_at = task.get("queued_at")
    wait_age = task.get("wait_age")
    label = task.get("label")
    kind = humanize_task_kind(task.get("kind"))
    source = task.get("source")
    started_at = task.get("started_at")
    running_age = task.get("running_age")
    preview = task.get("preview")
    actions = normalize_task_actions(task.get("actions"))
    control = describe_task_control(payload)
    harness = task.get("harness") if isinstance(task.get("harness"), dict) else {}
    harness_control = harness.get("control") if isinstance(harness.get("control"), dict) else {}
    latest_control_action = format_harness_control_summary(harness_control.get("latest_action"))
    latest_recovery = format_harness_control_summary(harness_control.get("latest_recovery"))
    starvation_alert = task.get("starvation_alert") if isinstance(task.get("starvation_alert"), dict) else None
    starvation_alert_text = format_task_starvation_alert(starvation_alert)
    suggested_command = str((starvation_alert or {}).get("suggested_command") or "").strip()

    lines = [
        "🧩 **Task Detail**",
        "",
        f"**Task ID:** `{task_id}`",
        f"**State:** {state}",
        f"**Lane:** {normalize_task_lane(lane)}",
    ]
    if priority is not None:
        lines.append(f"**Priority:** {int(priority)}")
    if priority_bucket:
        lines.append(f"**Priority Bucket:** {priority_bucket}")
    if priority_bucket_options:
        lines.append(f"**Priority Bucket Options:** {', '.join(priority_bucket_options)}")
    if queued_at:
        lines.append(f"**Queued At:** {queued_at}")
    if wait_age:
        lines.append(f"**Waited:** {wait_age}")
    if reply_policy is not None:
        lines.append(f"**Reply Policy:** {reply_policy}")
    if label:
        lines.append(f"**Label:** {label}")
    if kind:
        lines.append(f"**Kind:** {kind}")
    if source:
        lines.append(f"**Source:** {source}")
    if started_at:
        lines.append(f"**Started:** {started_at}")
    if running_age:
        lines.append(f"**Running:** {running_age}")
    if preview_formatter is not None:
        preview_text = preview_formatter(preview)
    else:
        preview_text = " ".join(str(preview or "").split()).strip()
    if preview_text:
        lines.append(f"**Preview:** {preview_text}")
    if control:
        lines.append(f"**Control:** {control}")
    if latest_control_action:
        lines.append(f"**Latest Control Action:** {latest_control_action}")
    if latest_recovery and latest_recovery != latest_control_action:
        lines.append(f"**Latest Recovery:** {latest_recovery}")
    if starvation_alert_text:
        lines.append(f"**Starvation Alert:** {starvation_alert_text}")
    if suggested_command:
        lines.append(f"**Suggested Action:** {suggested_command}")
    if actions:
        lines.append(f"**Actions:** {', '.join(actions)}")
    return "\n".join(lines)
