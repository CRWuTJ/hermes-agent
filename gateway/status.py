"""
Gateway runtime status helpers.

Provides PID-file based detection of whether the gateway daemon is running,
used by send_message's check_fn to gate availability in the CLI.

The PID file lives at ``{HERMES_HOME}/gateway.pid``.  HERMES_HOME defaults to
``~/.hermes`` but can be overridden via the environment variable.  This means
separate HERMES_HOME directories naturally get separate PID files — a property
that will be useful when we add named profiles (multiple agents running
concurrently under distinct configurations).
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from hermes_constants import get_hermes_home
from gateway.task_control import (
    normalize_priority_bucket,
    queued_task_iso,
    queued_task_priority_bucket,
    queued_task_priority_bucket_options,
    queued_task_wait_age,
    queued_task_wait_seconds,
)
from task_lanes import normalize_status_task_lane, normalize_task_lane
from typing import Any, Optional

_GATEWAY_KIND = "hermes-gateway"
_RUNTIME_STATUS_FILE = "gateway_state.json"
_LOCKS_DIRNAME = "gateway-locks"
_UNSET = object()
_STARVATION_ALERT_THRESHOLDS = {
    "now": 300,
    "next": 1800,
    "later": 7200,
}
_STARVATION_ALERT_REPRIORITIZE_TARGETS = {
    "later": "next",
    "next": "now",
}


def _starvation_alert_recommendation(bucket: Any, oldest_task_id: Any) -> dict[str, Any]:
    bucket_name = str(bucket or "").strip().lower()
    task_id = str(oldest_task_id or "").strip()
    recover_command = f"/task {task_id} recover" if task_id else None
    target_bucket = _STARVATION_ALERT_REPRIORITIZE_TARGETS.get(bucket_name)
    if target_bucket:
        return {
            "suggested_action": "reprioritize",
            "suggested_bucket": target_bucket,
            "suggested_command": recover_command,
        }
    return {
        "suggested_action": "foreground",
        "suggested_bucket": None,
        "suggested_command": recover_command,
    }


def _get_pid_path() -> Path:
    """Return the path to the gateway PID file, respecting HERMES_HOME."""
    home = get_hermes_home()
    return home / "gateway.pid"


def _get_runtime_status_path() -> Path:
    """Return the persisted runtime health/status file path."""
    return _get_pid_path().with_name(_RUNTIME_STATUS_FILE)


def _get_lock_dir() -> Path:
    """Return the machine-local directory for token-scoped gateway locks."""
    override = os.getenv("HERMES_GATEWAY_LOCK_DIR")
    if override:
        return Path(override)
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "hermes" / _LOCKS_DIRNAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


def _get_process_start_time(pid: int) -> Optional[int]:
    """Return the kernel start time for a process when available."""
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 in /proc/<pid>/stat is process start time (clock ticks).
        return int(stat_path.read_text().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def _read_process_cmdline(pid: int) -> Optional[str]:
    """Return the process command line as a space-separated string."""
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        raw = cmdline_path.read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return None

    if not raw:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()


def _looks_like_gateway_process(pid: int) -> bool:
    """Return True when the live PID still looks like the Hermes gateway."""
    cmdline = _read_process_cmdline(pid)
    if not cmdline:
        return False

    patterns = (
        "hermes_cli.main gateway",
        "hermes_cli/main.py gateway",
        "hermes gateway",
        "gateway/run.py",
    )
    return any(pattern in cmdline for pattern in patterns)


def _record_looks_like_gateway(record: dict[str, Any]) -> bool:
    """Validate gateway identity from PID-file metadata when cmdline is unavailable."""
    if record.get("kind") != _GATEWAY_KIND:
        return False

    argv = record.get("argv")
    if not isinstance(argv, list) or not argv:
        return False

    cmdline = " ".join(str(part) for part in argv)
    patterns = (
        "hermes_cli.main gateway",
        "hermes_cli/main.py gateway",
        "hermes gateway",
        "gateway/run.py",
    )
    return any(pattern in cmdline for pattern in patterns)


def _build_pid_record() -> dict:
    return {
        "pid": os.getpid(),
        "kind": _GATEWAY_KIND,
        "argv": list(sys.argv),
        "start_time": _get_process_start_time(os.getpid()),
    }


_STATUS_LANE_ORDER = ("interactive", "cron_scout", "housekeeping")
_STATUS_BUCKET_ORDER = ("now", "next", "later")
_LIVE_TASK_CONTROL_MODES = ("managed_runtime", "read_only")
_QUEUED_TASK_CONTROL_MODES = ("queued",)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default



def _empty_live_tasks_payload() -> dict[str, Any]:
    return {
        "active_count": 0,
        "lane_counts": {lane: 0 for lane in _STATUS_LANE_ORDER},
        "tasks": [],
    }



def _empty_queued_tasks_payload() -> dict[str, Any]:
    return {
        "queued_count": 0,
        "lane_counts": {lane: 0 for lane in _STATUS_LANE_ORDER},
        "bucket_counts": {bucket: 0 for bucket in _STATUS_BUCKET_ORDER},
        "next_task": None,
        "oldest_waiting": None,
        "starving_bucket": None,
        "starvation_alert": None,
        "tasks": [],
    }



def _empty_cron_status_payload() -> dict[str, Any]:
    return {
        "active_jobs": 0,
        "total_jobs": 0,
        "lane_counts": {lane: 0 for lane in _STATUS_LANE_ORDER},
        "due_now": {lane: 0 for lane in _STATUS_LANE_ORDER},
    }



def _normalize_lane_counts(raw_counts: Any) -> dict[str, int]:
    counts = {lane: 0 for lane in _STATUS_LANE_ORDER}
    if not isinstance(raw_counts, dict):
        return counts

    for raw_lane, raw_value in raw_counts.items():
        lane = normalize_status_task_lane(raw_lane)
        if lane not in counts:
            continue
        counts[lane] += _coerce_int(raw_value, 0)
    return counts



def _normalize_bucket_counts(raw_counts: Any) -> dict[str, int]:
    counts = {bucket: 0 for bucket in _STATUS_BUCKET_ORDER}
    if not isinstance(raw_counts, dict):
        return counts

    for raw_bucket, raw_value in raw_counts.items():
        bucket = str(raw_bucket or "").strip().lower()
        if bucket not in counts:
            continue
        counts[bucket] += _coerce_int(raw_value, 0)
    return counts



def _normalize_live_task_actions(raw_actions: Any) -> list[str]:
    if not isinstance(raw_actions, (list, tuple)):
        return []
    actions: list[str] = []
    for raw_action in raw_actions:
        action = str(raw_action or "").strip().lower()
        if action and action not in actions:
            actions.append(action)
    return actions



def _infer_live_task_kind(task: dict[str, Any]) -> str:
    raw_kind = str(task.get("kind") or "").strip()
    if raw_kind:
        return raw_kind

    label = " ".join(str(task.get("label") or "").split()).strip().lower()
    task_id = str(task.get("task_id") or "").strip().lower()
    if label == "background task" or task_id.startswith("bg_") or task_id.startswith("bg-"):
        return "background"
    if label == "btw task" or task_id.startswith("btw_") or task_id.startswith("btw-"):
        return "btw"
    if label == "message turn":
        return "live_turn"
    if label == "memory flush" or task_id.startswith("flush:"):
        return "memory_flush"
    return "runtime_task"



def _normalize_live_task_control_mode(raw_mode: Any, *, kind: str, actions: list[str]) -> str:
    mode = str(raw_mode or "").strip().lower()
    if mode in _LIVE_TASK_CONTROL_MODES:
        return mode
    if actions or kind in {"background", "btw"}:
        return "managed_runtime"
    return "read_only"



def normalize_live_tasks_payload(live_tasks: Any) -> dict[str, Any]:
    payload = live_tasks if isinstance(live_tasks, dict) else {}
    counts = _normalize_lane_counts(payload.get("lane_counts"))

    tasks = []
    raw_tasks = payload.get("tasks")
    if isinstance(raw_tasks, list):
        for task in raw_tasks:
            if not isinstance(task, dict):
                continue
            task_payload = dict(task)
            task_payload["lane"] = normalize_status_task_lane(task.get("lane"))
            kind = _infer_live_task_kind(task_payload)
            actions = _normalize_live_task_actions(task_payload.get("actions"))
            if not actions and kind in {"background", "btw"}:
                actions = ["cancel"]
            task_payload["kind"] = kind
            task_payload["control_mode"] = _normalize_live_task_control_mode(
                task_payload.get("control_mode"),
                kind=kind,
                actions=actions,
            )
            task_payload["actions"] = actions
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
            tasks.append(task_payload)

    if not any(counts.values()) and tasks:
        for task in tasks:
            lane = task.get("lane")
            if lane in counts:
                counts[lane] += 1

    active_count = _coerce_int(payload.get("active_count"), 0)
    if active_count <= 0:
        active_count = sum(counts.values()) or len(tasks)

    return {
        "active_count": active_count,
        "lane_counts": counts,
        "tasks": tasks,
    }



def _normalize_queued_task_control_mode(raw_mode: Any) -> str:
    mode = str(raw_mode or "").strip().lower()
    if mode in _QUEUED_TASK_CONTROL_MODES:
        return mode
    return "queued"



def _normalize_single_queued_task(task: Any) -> Optional[dict[str, Any]]:
    if not isinstance(task, dict):
        return None
    task_payload = dict(task)
    task_payload["state"] = "queued"
    task_payload["lane"] = normalize_status_task_lane(task.get("lane"))
    task_payload["priority"] = _coerce_int(task_payload.get("priority"), 50)
    task_payload["priority_bucket"] = queued_task_priority_bucket(
        task_payload.get("priority"),
        task_payload.get("priority_bucket"),
    )
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
    actions = _normalize_live_task_actions(task_payload.get("actions"))
    if not actions:
        actions = ["foreground", "reprioritize", "cancel"]
    task_payload["kind"] = str(task_payload.get("kind") or "queued_message")
    task_payload["control_mode"] = _normalize_queued_task_control_mode(task_payload.get("control_mode"))
    task_payload["actions"] = actions
    task_payload["priority_bucket_options"] = (
        queued_task_priority_bucket_options(task_payload.get("priority_bucket_options"))
        if "reprioritize" in actions or task_payload.get("priority_bucket_options") is not None
        else []
    )
    return task_payload



def _status_bucket_starvation_rank(bucket: Any) -> int:
    bucket_name = str(bucket or "").strip().lower()
    if bucket_name == "later":
        return 2
    if bucket_name == "next":
        return 1
    if bucket_name == "now":
        return 0
    return -1



def _select_oldest_waiting_task(tasks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    oldest_task: Optional[dict[str, Any]] = None
    oldest_key: Optional[tuple[int, int]] = None

    for task in tasks:
        wait_seconds = task.get("wait_seconds")
        if wait_seconds is None:
            continue
        wait_value = _coerce_int(wait_seconds, -1)
        if wait_value < 0:
            continue
        candidate_key = (
            wait_value,
            _status_bucket_starvation_rank(task.get("priority_bucket")),
        )
        if oldest_key is None or candidate_key > oldest_key:
            oldest_task = dict(task)
            oldest_key = candidate_key

    if oldest_task is not None:
        return oldest_task
    if tasks:
        return dict(tasks[0])
    return None



def _normalize_starving_bucket_summary(
    summary: Any,
    *,
    bucket_counts: Any = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(summary, dict):
        return None

    normalized_bucket_counts = _normalize_bucket_counts(bucket_counts)
    bucket = str(summary.get("bucket") or "").strip().lower()
    if bucket not in normalized_bucket_counts:
        return None

    oldest_wait_seconds_raw = summary.get("oldest_wait_seconds")
    oldest_wait_seconds: Optional[int]
    if oldest_wait_seconds_raw is None:
        oldest_wait_seconds = None
    else:
        value = _coerce_int(oldest_wait_seconds_raw, -1)
        oldest_wait_seconds = value if value >= 0 else None

    oldest_wait_age = queued_task_wait_age(
        None,
        raw_wait_seconds=oldest_wait_seconds,
        raw_wait_age=summary.get("oldest_wait_age"),
    )
    oldest_task_id = str(summary.get("oldest_task_id") or "").strip()
    queued_count = _coerce_int(summary.get("queued_count"), normalized_bucket_counts.get(bucket, 0))

    if queued_count <= 0 and oldest_wait_seconds is None and not oldest_wait_age and not oldest_task_id:
        return None

    return {
        "bucket": bucket,
        "queued_count": queued_count,
        "oldest_wait_seconds": oldest_wait_seconds,
        "oldest_wait_age": oldest_wait_age,
        "oldest_task_id": oldest_task_id,
    }



def _build_starving_bucket_summary(
    oldest_waiting: Any,
    *,
    bucket_counts: dict[str, int],
    raw_summary: Any = None,
) -> Optional[dict[str, Any]]:
    explicit = _normalize_starving_bucket_summary(raw_summary, bucket_counts=bucket_counts)
    if explicit is not None:
        return explicit

    normalized_task = _normalize_single_queued_task(oldest_waiting)
    if normalized_task is None:
        return None

    bucket = str(normalized_task.get("priority_bucket") or "").strip().lower()
    if bucket not in bucket_counts:
        return None

    wait_seconds = normalized_task.get("wait_seconds")
    return {
        "bucket": bucket,
        "queued_count": _coerce_int(bucket_counts.get(bucket), 0),
        "oldest_wait_seconds": wait_seconds,
        "oldest_wait_age": queued_task_wait_age(
            None,
            raw_wait_seconds=wait_seconds,
            raw_wait_age=normalized_task.get("wait_age"),
        ),
        "oldest_task_id": str(normalized_task.get("task_id") or "").strip(),
    }



def _normalize_starvation_alert_summary(summary: Any) -> Optional[dict[str, Any]]:
    if not isinstance(summary, dict):
        return None

    bucket = str(summary.get("bucket") or "").strip().lower()
    if bucket not in _STARVATION_ALERT_THRESHOLDS:
        return None

    level = str(summary.get("level") or "warning").strip().lower() or "warning"
    reason_code = str(summary.get("reason_code") or "bucket_wait_threshold_exceeded").strip() or "bucket_wait_threshold_exceeded"
    reason = str(summary.get("reason") or f"{bucket} bucket exceeded starvation threshold").strip()
    oldest_task_id = str(summary.get("oldest_task_id") or "").strip()
    recommendation = _starvation_alert_recommendation(bucket, oldest_task_id)

    suggested_action = str(summary.get("suggested_action") or recommendation.get("suggested_action") or "reprioritize").strip().lower()
    if suggested_action not in {"foreground", "reprioritize"}:
        suggested_action = str(recommendation.get("suggested_action") or "reprioritize")
    if suggested_action == "reprioritize":
        suggested_bucket = normalize_priority_bucket(summary.get("suggested_bucket")) or recommendation.get("suggested_bucket")
    else:
        suggested_bucket = None
    suggested_command = str(summary.get("suggested_command") or recommendation.get("suggested_command") or "").strip() or None

    threshold_seconds_raw = summary.get("threshold_seconds")
    threshold_seconds = _coerce_int(
        threshold_seconds_raw,
        _STARVATION_ALERT_THRESHOLDS.get(bucket, 0),
    )
    threshold_age = queued_task_wait_age(
        None,
        raw_wait_seconds=threshold_seconds,
        raw_wait_age=summary.get("threshold_age"),
    )

    current_wait_seconds_raw = summary.get("current_wait_seconds")
    if current_wait_seconds_raw is None:
        current_wait_seconds = None
    else:
        value = _coerce_int(current_wait_seconds_raw, -1)
        current_wait_seconds = value if value >= 0 else None
    current_wait_age = queued_task_wait_age(
        None,
        raw_wait_seconds=current_wait_seconds,
        raw_wait_age=summary.get("current_wait_age"),
    )

    if current_wait_seconds is not None and current_wait_seconds <= threshold_seconds:
        return None

    return {
        "level": level,
        "reason_code": reason_code,
        "reason": reason,
        "bucket": bucket,
        "threshold_seconds": threshold_seconds,
        "threshold_age": threshold_age,
        "current_wait_seconds": current_wait_seconds,
        "current_wait_age": current_wait_age,
        "oldest_task_id": oldest_task_id,
        "suggested_action": suggested_action,
        "suggested_bucket": suggested_bucket,
        "suggested_command": suggested_command,
    }



def _build_starvation_alert(summary: Any, *, raw_alert: Any = None) -> Optional[dict[str, Any]]:
    explicit = _normalize_starvation_alert_summary(raw_alert)
    if explicit is not None:
        return explicit

    starving_bucket = _normalize_starving_bucket_summary(summary)
    if starving_bucket is None:
        return None

    bucket = str(starving_bucket.get("bucket") or "").strip().lower()
    threshold_seconds = _STARVATION_ALERT_THRESHOLDS.get(bucket)
    current_wait_seconds = starving_bucket.get("oldest_wait_seconds")
    if threshold_seconds is None or current_wait_seconds is None:
        return None
    if _coerce_int(current_wait_seconds, -1) <= threshold_seconds:
        return None

    oldest_task_id = str(starving_bucket.get("oldest_task_id") or "").strip()
    recommendation = _starvation_alert_recommendation(bucket, oldest_task_id)
    return {
        "level": "warning",
        "reason_code": "bucket_wait_threshold_exceeded",
        "reason": f"{bucket} bucket exceeded starvation threshold",
        "bucket": bucket,
        "threshold_seconds": threshold_seconds,
        "threshold_age": queued_task_wait_age(None, raw_wait_seconds=threshold_seconds),
        "current_wait_seconds": _coerce_int(current_wait_seconds, 0),
        "current_wait_age": queued_task_wait_age(
            None,
            raw_wait_seconds=current_wait_seconds,
            raw_wait_age=starving_bucket.get("oldest_wait_age"),
        ),
        "oldest_task_id": oldest_task_id,
        "suggested_action": recommendation.get("suggested_action"),
        "suggested_bucket": recommendation.get("suggested_bucket"),
        "suggested_command": recommendation.get("suggested_command"),
    }



def _attach_starvation_alert_to_task(task: Optional[dict[str, Any]], alert: Any) -> Optional[dict[str, Any]]:
    if task is None:
        return None
    normalized_alert = _normalize_starvation_alert_summary(alert)
    if normalized_alert is None:
        task.pop("starvation_alert", None)
        return task
    task_id = str(task.get("task_id") or "").strip()
    if task_id and task_id == str(normalized_alert.get("oldest_task_id") or "").strip():
        task["starvation_alert"] = dict(normalized_alert)
    else:
        task.pop("starvation_alert", None)
    return task


def normalize_queued_tasks_payload(queued_tasks: Any) -> dict[str, Any]:
    payload = queued_tasks if isinstance(queued_tasks, dict) else {}
    counts = _normalize_lane_counts(payload.get("lane_counts"))
    bucket_counts = _normalize_bucket_counts(payload.get("bucket_counts"))

    tasks = []
    raw_tasks = payload.get("tasks")
    if isinstance(raw_tasks, list):
        for task in raw_tasks:
            task_payload = _normalize_single_queued_task(task)
            if task_payload is None:
                continue
            tasks.append(task_payload)

    if not any(counts.values()) and tasks:
        for task in tasks:
            lane = task.get("lane")
            if lane in counts:
                counts[lane] += 1

    if not any(bucket_counts.values()) and tasks:
        for task in tasks:
            bucket = task.get("priority_bucket")
            if bucket in bucket_counts:
                bucket_counts[bucket] += 1

    queued_count = _coerce_int(payload.get("queued_count"), 0)
    if queued_count <= 0:
        queued_count = sum(counts.values()) or len(tasks)

    next_task = _normalize_single_queued_task(payload.get("next_task"))
    if next_task is None and tasks:
        next_task = dict(tasks[0])

    oldest_waiting = _normalize_single_queued_task(payload.get("oldest_waiting"))
    if oldest_waiting is None:
        oldest_waiting = _select_oldest_waiting_task(tasks)

    starving_bucket = _build_starving_bucket_summary(
        oldest_waiting,
        bucket_counts=bucket_counts,
        raw_summary=payload.get("starving_bucket"),
    )
    starvation_alert = _build_starvation_alert(
        starving_bucket,
        raw_alert=payload.get("starvation_alert"),
    )
    next_task = _attach_starvation_alert_to_task(next_task, starvation_alert)
    oldest_waiting = _attach_starvation_alert_to_task(oldest_waiting, starvation_alert)
    tasks = [_attach_starvation_alert_to_task(dict(task), starvation_alert) or dict(task) for task in tasks]

    return {
        "queued_count": queued_count,
        "lane_counts": counts,
        "bucket_counts": bucket_counts,
        "next_task": next_task,
        "oldest_waiting": oldest_waiting,
        "starving_bucket": starving_bucket,
        "starvation_alert": starvation_alert,
        "tasks": tasks,
    }



def normalize_cron_status_payload(cron_payload: Any) -> Optional[dict[str, Any]]:
    if cron_payload is None:
        return None

    payload = cron_payload if isinstance(cron_payload, dict) else {}
    active_jobs = _coerce_int(payload.get("active_jobs"), 0)
    total_jobs = _coerce_int(payload.get("total_jobs"), active_jobs)

    return {
        "active_jobs": active_jobs,
        "total_jobs": total_jobs,
        "lane_counts": _normalize_lane_counts(payload.get("lane_counts")),
        "due_now": _normalize_lane_counts(payload.get("due_now")),
    }



def build_cron_status_payload() -> Optional[dict[str, Any]]:
    try:
        from cron.jobs import get_due_jobs, list_jobs, summarize_job_lanes

        jobs = list_jobs(include_disabled=True)
        active_jobs = [job for job in jobs if job.get("enabled", True)]
        lane_summary = summarize_job_lanes(active_jobs, due_jobs=get_due_jobs())
        return normalize_cron_status_payload(
            {
                "active_jobs": len(active_jobs),
                "total_jobs": len(jobs),
                "lane_counts": lane_summary.get("active"),
                "due_now": lane_summary.get("due"),
            }
        )
    except Exception:
        return None



def build_gateway_status_payload(
    *,
    runtime_status: Optional[dict[str, Any]] = None,
    queued_tasks: Any = _UNSET,
    live_tasks: Any = _UNSET,
    cron_payload: Any = _UNSET,
) -> dict[str, Any]:
    payload = runtime_status if isinstance(runtime_status, dict) else (read_runtime_status() or {})
    raw_queued_tasks = (
        payload.get("queued_tasks")
        if queued_tasks is _UNSET and "queued_tasks" in payload
        else queued_tasks
    )
    raw_live_tasks = payload.get("live_tasks") if live_tasks is _UNSET else live_tasks
    raw_cron_payload = build_cron_status_payload() if cron_payload is _UNSET else cron_payload
    platforms = payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {}

    result = {
        "gateway_state": payload.get("gateway_state") or "unknown",
        "exit_reason": payload.get("exit_reason"),
        "updated_at": payload.get("updated_at"),
        "platforms": platforms,
        "live_tasks": normalize_live_tasks_payload(raw_live_tasks),
        "cron": normalize_cron_status_payload(raw_cron_payload),
    }
    if raw_queued_tasks is not _UNSET:
        result["queued_tasks"] = normalize_queued_tasks_payload(raw_queued_tasks)
    return result



def format_status_lane_counts(counts: Any) -> str:
    normalized = _normalize_lane_counts(counts)
    return " | ".join(
        f"{label}={normalized.get(key, 0)}"
        for key, label in (
            ("interactive", "interactive"),
            ("cron_scout", "cron/scout"),
            ("housekeeping", "housekeeping"),
        )
    )



def format_status_bucket_counts(counts: Any) -> str:
    normalized = _normalize_bucket_counts(counts)
    return " | ".join(f"{bucket}={normalized.get(bucket, 0)}" for bucket in _STATUS_BUCKET_ORDER)



def format_status_next_queued_task(task: Any) -> Optional[str]:
    normalized = _normalize_single_queued_task(task)
    if normalized is None:
        return None
    task_id = str(normalized.get("task_id") or "").strip()
    if not task_id:
        return None
    lane = normalize_task_lane(normalized.get("lane"))
    bucket = queued_task_priority_bucket(normalized.get("priority"), normalized.get("priority_bucket"))
    priority = _coerce_int(normalized.get("priority"), 50)
    parts = [f"{task_id} · {lane} · {bucket} ({priority})"]
    wait_age = str(normalized.get("wait_age") or "").strip()
    queued_at = str(normalized.get("queued_at") or "").strip()
    if wait_age and queued_at:
        parts.append(f"waiting {wait_age} since {queued_at}")
    elif wait_age:
        parts.append(f"waiting {wait_age}")
    elif queued_at:
        parts.append(f"queued at {queued_at}")
    return " · ".join(parts)



def format_status_starving_bucket(summary: Any) -> Optional[str]:
    normalized = _normalize_starving_bucket_summary(summary)
    if normalized is None:
        return None

    bucket = str(normalized.get("bucket") or "").strip()
    if not bucket:
        return None

    parts = [bucket, f"{_coerce_int(normalized.get('queued_count'), 0)} queued"]
    wait_age = str(normalized.get("oldest_wait_age") or "").strip()
    if wait_age:
        parts.append(f"oldest wait {wait_age}")
    oldest_task_id = str(normalized.get("oldest_task_id") or "").strip()
    if oldest_task_id:
        parts.append(oldest_task_id)
    return " · ".join(parts)



def format_status_starvation_alert(summary: Any) -> Optional[str]:
    normalized = _normalize_starvation_alert_summary(summary)
    if normalized is None:
        return None

    level = str(normalized.get("level") or "warning").strip()
    bucket = str(normalized.get("bucket") or "").strip()
    threshold_age = str(normalized.get("threshold_age") or "").strip()
    current_wait_age = str(normalized.get("current_wait_age") or "").strip()
    oldest_task_id = str(normalized.get("oldest_task_id") or "").strip()
    suggested_action = str(normalized.get("suggested_action") or "").strip()
    suggested_bucket = normalize_priority_bucket(normalized.get("suggested_bucket"))
    suggested_command = str(normalized.get("suggested_command") or "").strip()

    if not level or not bucket:
        return None

    parts = [level]
    if threshold_age:
        parts.append(f"{bucket} bucket exceeded {threshold_age} threshold")
    else:
        parts.append(f"{bucket} bucket exceeded threshold")
    if current_wait_age:
        parts.append(f"waiting {current_wait_age}")
    if suggested_command:
        parts.append(suggested_command)
    elif suggested_action == "reprioritize" and oldest_task_id and suggested_bucket:
        parts.append(f"/task {oldest_task_id} {suggested_bucket}")
    elif suggested_action and oldest_task_id:
        parts.append(f"{suggested_action} {oldest_task_id}")
    elif suggested_action:
        parts.append(suggested_action)
    elif oldest_task_id:
        parts.append(oldest_task_id)
    return " · ".join(parts)



def render_status_activity_lines(status_payload: Any, *, style: str = "chat") -> list[str]:
    payload = status_payload if isinstance(status_payload, dict) else {}
    queued_status = normalize_queued_tasks_payload(payload.get("queued_tasks")) if payload.get("queued_tasks") is not None else None
    live_tasks = normalize_live_tasks_payload(payload.get("live_tasks"))
    cron_status = normalize_cron_status_payload(payload.get("cron"))
    lines: list[str] = []

    if style == "chat":
        if queued_status is not None and int(queued_status.get("queued_count", 0) or 0) > 0:
            lines.extend([
                f"**Queued Backlog:** {int(queued_status.get('queued_count', 0) or 0)} total",
                f"**Queued Lanes:** {format_status_lane_counts(queued_status.get('lane_counts'))}",
                f"**Queued Buckets:** {format_status_bucket_counts(queued_status.get('bucket_counts'))}",
            ])
            next_queued = format_status_next_queued_task(queued_status.get("next_task"))
            if next_queued:
                lines.append(f"**Next Queued:** `{next_queued}`")
            oldest_waiting = format_status_next_queued_task(queued_status.get("oldest_waiting"))
            if oldest_waiting:
                lines.append(f"**Oldest Waiting:** `{oldest_waiting}`")
            starving_bucket = format_status_starving_bucket(queued_status.get("starving_bucket"))
            if starving_bucket:
                lines.append(f"**Starving Bucket:** `{starving_bucket}`")
            starvation_alert = format_status_starvation_alert(queued_status.get("starvation_alert"))
            if starvation_alert:
                lines.append(f"**Starvation Alert:** `{starvation_alert}`")
        if any(live_tasks["lane_counts"].values()):
            lines.append(f"**Active Lanes:** {format_status_lane_counts(live_tasks['lane_counts'])}")
        if cron_status is not None:
            lines.extend([
                f"**Cron Jobs:** {int(cron_status.get('active_jobs', 0) or 0)} active",
                f"**Cron Lanes:** {format_status_lane_counts(cron_status.get('lane_counts'))}",
                f"**Cron Due Now:** {format_status_lane_counts(cron_status.get('due_now'))}",
            ])
        return lines

    if style == "cli":
        if queued_status is not None and int(queued_status.get("queued_count", 0) or 0) > 0:
            lines.extend([
                f"  Queued:       {int(queued_status.get('queued_count', 0) or 0)} total",
                f"  Queue lanes:  {format_status_lane_counts(queued_status.get('lane_counts'))}",
                f"  Buckets:      {format_status_bucket_counts(queued_status.get('bucket_counts'))}",
            ])
            next_queued = format_status_next_queued_task(queued_status.get("next_task"))
            if next_queued:
                lines.append(f"  Next queued:  {next_queued}")
            oldest_waiting = format_status_next_queued_task(queued_status.get("oldest_waiting"))
            if oldest_waiting:
                lines.append(f"  Oldest wait:  {oldest_waiting}")
            starving_bucket = format_status_starving_bucket(queued_status.get("starving_bucket"))
            if starving_bucket:
                lines.append(f"  Starving:     {starving_bucket}")
            starvation_alert = format_status_starvation_alert(queued_status.get("starvation_alert"))
            if starvation_alert:
                lines.append(f"  Alert:        {starvation_alert}")
        if any(live_tasks["lane_counts"].values()):
            lines.append(f"  Active lanes: {format_status_lane_counts(live_tasks['lane_counts'])}")
        if cron_status is not None:
            lines.extend([
                f"  Jobs:         {int(cron_status.get('active_jobs', 0) or 0)} active, {int(cron_status.get('total_jobs', 0) or 0)} total",
                f"  Lanes:        {format_status_lane_counts(cron_status.get('lane_counts'))}",
                f"  Due now:      {format_status_lane_counts(cron_status.get('due_now'))}",
            ])
        return lines

    raise ValueError(f"Unsupported status activity style: {style}")



def render_status_activity_block(status_payload: Any, *, style: str = "chat") -> str:
    return "\n".join(render_status_activity_lines(status_payload, style=style))



def _format_status_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return "" if value is None else str(value)



def _status_task_field(task: Any, field: str) -> Any:
    if isinstance(task, dict):
        return task.get(field)
    return getattr(task, field, None)



def render_gateway_chat_status_block(
    *,
    session_id: str,
    created_at: Any,
    updated_at: Any,
    total_tokens: Any,
    agent_running: bool,
    queued_tasks: Any,
    connected_platforms: Any,
    status_payload: Any,
    title: Optional[str] = None,
    next_task: Any = None,
) -> str:
    lines = [
        "📊 **Hermes Gateway Status**",
        "",
        f"**Session ID:** `{session_id}`",
    ]
    if title:
        lines.append(f"**Title:** {title}")
    lines.extend(
        [
            f"**Created:** {_format_status_datetime(created_at)}",
            f"**Last Activity:** {_format_status_datetime(updated_at)}",
            f"**Tokens:** {_coerce_int(total_tokens):,}",
            f"**Agent Running:** {'Yes ⚡' if agent_running else 'No'}",
            f"**Queued Tasks:** {_coerce_int(queued_tasks)}",
        ]
    )
    if next_task:
        task_id = _status_task_field(next_task, "task_id")
        lines.append(f"**Next Task:** `{task_id}`")
        lines.append(f"**Next Lane:** {normalize_task_lane(_status_task_field(next_task, 'lane'))}")
        priority = _status_task_field(next_task, "priority")
        if priority is not None:
            bucket = queued_task_priority_bucket(priority, _status_task_field(next_task, "priority_bucket"))
            lines.append(f"**Next Priority:** {bucket} ({int(priority)})")
        reply_policy = _status_task_field(next_task, "reply_policy")
        if reply_policy is not None:
            lines.append(f"**Reply Policy:** {reply_policy}")
    activity_block = render_status_activity_block(status_payload, style="chat")
    if activity_block:
        lines.extend(activity_block.splitlines())
    platforms = [str(platform) for platform in connected_platforms] if isinstance(connected_platforms, (list, tuple, set)) else []
    lines.extend([
        "",
        f"**Connected Platforms:** {', '.join(platforms)}",
    ])
    return "\n".join(lines)



def _build_runtime_status_record() -> dict[str, Any]:
    payload = _build_pid_record()
    payload.update({
        "gateway_state": "starting",
        "exit_reason": None,
        "platforms": {},
        "queued_tasks": _empty_queued_tasks_payload(),
        "live_tasks": _empty_live_tasks_payload(),
        "updated_at": _utc_now_iso(),
    })
    return payload


def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _read_pid_record() -> Optional[dict]:
    pid_path = _get_pid_path()
    if not pid_path.exists():
        return None

    raw = pid_path.read_text().strip()
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            return {"pid": int(raw)}
        except ValueError:
            return None

    if isinstance(payload, int):
        return {"pid": payload}
    if isinstance(payload, dict):
        return payload
    return None


def write_pid_file() -> None:
    """Write the current process PID and metadata to the gateway PID file."""
    _write_json_file(_get_pid_path(), _build_pid_record())


def write_runtime_status(
    *,
    gateway_state: Any = _UNSET,
    exit_reason: Any = _UNSET,
    platform: Optional[str] = None,
    platform_state: Any = _UNSET,
    error_code: Any = _UNSET,
    error_message: Any = _UNSET,
    queued_tasks: Any = _UNSET,
    live_tasks: Any = _UNSET,
) -> None:
    """Persist gateway runtime health information for diagnostics/status."""
    path = _get_runtime_status_path()
    payload = _read_json_file(path) or _build_runtime_status_record()
    payload.setdefault("platforms", {})
    payload.setdefault("queued_tasks", _empty_queued_tasks_payload())
    payload.setdefault("live_tasks", _empty_live_tasks_payload())
    payload.setdefault("kind", _GATEWAY_KIND)
    payload["pid"] = os.getpid()
    payload["start_time"] = _get_process_start_time(os.getpid())
    payload["updated_at"] = _utc_now_iso()

    if gateway_state is not _UNSET:
        payload["gateway_state"] = gateway_state
    if exit_reason is not _UNSET:
        payload["exit_reason"] = exit_reason
    if queued_tasks is not _UNSET:
        payload["queued_tasks"] = queued_tasks
    if live_tasks is not _UNSET:
        payload["live_tasks"] = live_tasks

    if platform is not None:
        platform_payload = payload["platforms"].get(platform, {})
        if platform_state is not _UNSET:
            platform_payload["state"] = platform_state
        if error_code is not _UNSET:
            platform_payload["error_code"] = error_code
        if error_message is not _UNSET:
            platform_payload["error_message"] = error_message
        platform_payload["updated_at"] = _utc_now_iso()
        payload["platforms"][platform] = platform_payload

    _write_json_file(path, payload)


def read_runtime_status() -> Optional[dict[str, Any]]:
    """Read the persisted gateway runtime health/status information."""
    return _read_json_file(_get_runtime_status_path())


def remove_pid_file() -> None:
    """Remove the gateway PID file if it exists."""
    try:
        _get_pid_path().unlink(missing_ok=True)
    except Exception:
        pass


def acquire_scoped_lock(scope: str, identity: str, metadata: Optional[dict[str, Any]] = None) -> tuple[bool, Optional[dict[str, Any]]]:
    """Acquire a machine-local lock keyed by scope + identity.

    Used to prevent multiple local gateways from using the same external identity
    at once (e.g. the same Telegram bot token across different HERMES_HOME dirs).
    """
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }

    existing = _read_json_file(lock_path)
    if existing:
        try:
            existing_pid = int(existing["pid"])
        except (KeyError, TypeError, ValueError):
            existing_pid = None

        if existing_pid == os.getpid() and existing.get("start_time") == record.get("start_time"):
            _write_json_file(lock_path, record)
            return True, existing

        stale = existing_pid is None
        if not stale:
            try:
                os.kill(existing_pid, 0)
            except (ProcessLookupError, PermissionError):
                stale = True
            else:
                current_start = _get_process_start_time(existing_pid)
                if (
                    existing.get("start_time") is not None
                    and current_start is not None
                    and current_start != existing.get("start_time")
                ):
                    stale = True
                # Check if process is stopped (Ctrl+Z / SIGTSTP) — stopped
                # processes still respond to os.kill(pid, 0) but are not
                # actually running. Treat them as stale so --replace works.
                if not stale:
                    try:
                        _proc_status = Path(f"/proc/{existing_pid}/status")
                        if _proc_status.exists():
                            for _line in _proc_status.read_text().splitlines():
                                if _line.startswith("State:"):
                                    _state = _line.split()[1]
                                    if _state in ("T", "t"):  # stopped or tracing stop
                                        stale = True
                                    break
                    except (OSError, PermissionError):
                        pass
        if stale:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            return False, existing

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, _read_json_file(lock_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True, None


def release_scoped_lock(scope: str, identity: str) -> None:
    """Release a previously-acquired scope lock when owned by this process."""
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_json_file(lock_path)
    if not existing:
        return
    if existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def release_all_scoped_locks() -> int:
    """Remove all scoped lock files in the lock directory.

    Called during --replace to clean up stale locks left by stopped/killed
    gateway processes that did not release their locks gracefully.
    Returns the number of lock files removed.
    """
    lock_dir = _get_lock_dir()
    removed = 0
    if lock_dir.exists():
        for lock_file in lock_dir.glob("*.lock"):
            try:
                lock_file.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def get_running_pid() -> Optional[int]:
    """Return the PID of a running gateway instance, or ``None``.

    Checks the PID file and verifies the process is actually alive.
    Cleans up stale PID files automatically.
    """
    record = _read_pid_record()
    if not record:
        remove_pid_file()
        return None

    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        remove_pid_file()
        return None

    try:
        os.kill(pid, 0)  # signal 0 = existence check, no actual signal sent
    except (ProcessLookupError, PermissionError):
        remove_pid_file()
        return None

    recorded_start = record.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and current_start != recorded_start:
        remove_pid_file()
        return None

    if not _looks_like_gateway_process(pid):
        if not _record_looks_like_gateway(record):
            remove_pid_file()
            return None

    return pid


def is_gateway_running() -> bool:
    """Check if the gateway daemon is currently running."""
    return get_running_pid() is not None
