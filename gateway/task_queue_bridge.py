"""Bridge live gateway background work into the durable task queue.

The bridge is intentionally optional and low-impact: it mirrors gateway
background tasks into a JSONL ledger so /tasks-style visibility and future
worker-pool scheduling have a durable source of truth. It does not restart the
live gateway and does not dispatch extra workers by itself.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.task_queue import QueueOperationResult, TaskQueueError, TaskQueueLedger, utc_now_iso
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_QUEUE_CFG_KEY = "task_queue"
_DEFAULT_BACKLOG = "runtime/task_queue/backlog.jsonl"
_DEFAULT_BACKGROUND_STALE_AFTER_SECONDS = 3600


def _queue_config(config: Any = None) -> dict[str, Any]:
    if isinstance(config, dict):
        raw = config.get(_QUEUE_CFG_KEY, {})
        return raw if isinstance(raw, dict) else {}
    raw = getattr(config, _QUEUE_CFG_KEY, {}) if config is not None else {}
    return raw if isinstance(raw, dict) else {}


def task_queue_enabled(config: Any = None) -> bool:
    """Return whether gateway task queue mirroring is enabled.

    Default is enabled because mirroring only writes a small local ledger and is
    the safe path toward event-driven scheduling. Operators can opt out with
    `task_queue.enabled: false`.
    """

    cfg = _queue_config(config)
    return bool(cfg.get("enabled", True))


def task_queue_path(config: Any = None) -> Path:
    cfg = _queue_config(config)
    raw_path = str(cfg.get("path") or "").strip()
    if raw_path:
        return Path(raw_path).expanduser()
    return get_hermes_home() / _DEFAULT_BACKLOG


def background_concurrency(config: Any = None) -> int:
    cfg = _queue_config(config)
    try:
        value = int(cfg.get("background_concurrency", cfg.get("concurrency", 1)))
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def cron_queue_concurrency(config: Any = None) -> int:
    cfg = _queue_config(config)
    try:
        value = int(cfg.get("cron_concurrency", 1))
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def background_stale_after_seconds(config: Any = None) -> int:
    """Return how long a queue-owned background task may run without a live owner."""

    cfg = _queue_config(config)
    raw = cfg.get("background_stale_after_seconds")
    if raw is None:
        raw = os.getenv("HERMES_TASK_QUEUE_BACKGROUND_STALE_SECONDS")
    try:
        value = int(raw) if raw is not None else _DEFAULT_BACKGROUND_STALE_AFTER_SECONDS
    except (TypeError, ValueError):
        value = _DEFAULT_BACKGROUND_STALE_AFTER_SECONDS
    return max(60, value)


def _parse_utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _source_label(source: Any) -> str:
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", None) or str(platform or "unknown")
    chat_id = getattr(source, "chat_id", None) or "unknown"
    return f"gateway:{platform_value}:{chat_id}"


def _source_payload(source: Any) -> dict[str, Any]:
    if hasattr(source, "to_dict"):
        payload = source.to_dict()
        if isinstance(payload, dict):
            return payload
    platform = getattr(source, "platform", None)
    return {
        "platform": getattr(platform, "value", None) or str(platform or "unknown"),
        "chat_id": str(getattr(source, "chat_id", "unknown") or "unknown"),
        "chat_type": str(getattr(source, "chat_type", "dm") or "dm"),
        "user_id": getattr(source, "user_id", None),
        "thread_id": getattr(source, "thread_id", None),
    }


def task_queue_status(config: Any = None) -> dict[str, Any]:
    """Return durable queue status for gateway visibility and cron watchdogs."""

    enabled = task_queue_enabled(config)
    path = task_queue_path(config)
    result: dict[str, Any] = {"enabled": enabled, "path": str(path), "exists": path.exists()}
    if not enabled:
        return result
    if not path.exists():
        result.update(
            {
                "task_count": 0,
                "open_count": 0,
                "status_counts": {},
                "lane_counts": {},
                "dispatchable_count": 0,
                "next_ready_ids": [],
            }
        )
        return result
    try:
        result.update(TaskQueueLedger(path).status_summary())
    except TaskQueueError as exc:
        result["error"] = str(exc)
    return result


def recover_abandoned_background_tasks(
    *,
    config: Any = None,
    active_task_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    stale_after_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """Block stale queue rows whose live gateway runtime task disappeared."""

    if not task_queue_enabled(config):
        return []
    active_ids = {str(item) for item in (active_task_ids or []) if str(item)}
    stale_after = int(stale_after_seconds if stale_after_seconds is not None else background_stale_after_seconds(config))
    if stale_after <= 0:
        return []
    now = datetime.now(timezone.utc)
    ledger = TaskQueueLedger(task_queue_path(config))
    try:
        with ledger._exclusive_lock():
            tasks = ledger.load(strict=True)
            recovered: list[dict[str, Any]] = []
            for task in tasks:
                task_id = str(task.get("id") or "")
                if not task_id or task_id in active_ids:
                    continue
                if task.get("status") != "running":
                    continue
                dispatch = task.get("dispatch")
                if not isinstance(dispatch, dict) or dispatch.get("kind") not in {"gateway_background", "cron_job"}:
                    continue
                last_seen = (
                    _parse_utc_timestamp(task.get("last_heartbeat_at"))
                    or _parse_utc_timestamp(task.get("updated_at"))
                    or _parse_utc_timestamp(task.get("created_at"))
                )
                age_seconds = int((now - last_seen).total_seconds()) if last_seen else stale_after
                if age_seconds < stale_after:
                    continue
                reason = (
                    "abandoned_runtime: no active gateway runtime task for "
                    f"{age_seconds}s; blocked to release queue capacity"
                )
                recovered.append(
                    ledger._apply_updates(
                        task,
                        {"status": "blocked", "blocked_by": [reason]},
                    )
                )
            if recovered:
                ledger.save(tasks)
            return recovered
    except TaskQueueError as exc:
        logger.warning("Failed to recover abandoned gateway queue tasks: %s", exc)
        return []


def infer_background_lane(prompt: str) -> str:
    text = str(prompt or "").lower()
    if any(token in text for token in ("调研", "研究", "research", "scout", "资料", "外部")):
        return "research"
    if any(token in text for token in ("验证", "测试", "test", "pytest", "smoke", "回归")):
        return "verification"
    if any(token in text for token in ("obsidian", "vault", "知识", "笔记", "落库", "wiki")):
        return "knowledge"
    if any(token in text for token in ("排查", "修复", "debug", "gateway", "模型", "auth", "凭证", "scope")):
        return "infra_debug"
    return "implementation"


def _cron_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "id",
        "name",
        "prompt",
        "skills",
        "skill",
        "model",
        "provider",
        "base_url",
        "script",
        "schedule",
        "schedule_display",
        "repeat",
        "deliver",
        "origin",
        "lane",
        "queue_first",
    }
    return {key: job.get(key) for key in allowed_keys if key in job}


def _next_cron_queue_task_id(job_id: str, existing_ids: set[str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    base = f"cron_{job_id}_{stamp}"[:54]
    candidate = base
    idx = 2
    while candidate in existing_ids:
        suffix = f"_{idx}"
        candidate = f"{base[:54 - len(suffix)]}{suffix}"
        idx += 1
    return candidate


def enqueue_cron_job_task(*, job: dict[str, Any], config: Any = None, status: str = "ready") -> dict[str, Any] | None:
    """Queue a due cron job as durable work instead of executing it in cron."""

    if not task_queue_enabled(config):
        return None
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise TaskQueueError("cron job id is required")
    ledger = TaskQueueLedger(task_queue_path(config))
    try:
        with ledger._exclusive_lock():
            tasks = ledger.load(strict=True)
            for task in tasks:
                dispatch = task.get("dispatch")
                if not isinstance(dispatch, dict) or dispatch.get("kind") != "cron_job":
                    continue
                if dispatch.get("cron_job_id") != job_id:
                    continue
                if task.get("status") in {"ready", "running", "review"}:
                    return task
            existing_ids = {str(task.get("id")) for task in tasks}
            task_id = _next_cron_queue_task_id(job_id, existing_ids)
            row = {
                "id": task_id,
                "title": f"Cron job: {str(job.get('name') or job_id).strip()}",
                "project": "Cron",
                "lane": infer_background_lane(str(job.get("prompt") or job.get("name") or "")),
                "priority": "P2",
                "status": status,
                "dependencies": [],
                "blocked_by": [],
                "worker_profile": "cron-queue-worker",
                "model_tier": "mid",
                "token_budget": 6000,
                "write_scope": "cron-runtime",
                "acceptance_check": ["Cron queue task completed or produced a visible blocker."],
                "artifact_targets": [f"cron-job:{job_id}"],
                "last_heartbeat_at": None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "dispatch": {
                    "kind": "cron_job",
                    "cron_job_id": job_id,
                    "job": _cron_job_snapshot(job),
                },
            }
            if status in {"running", "review"}:
                row["last_heartbeat_at"] = row["updated_at"]
            tasks.append(row)
            ledger.save(tasks)
            return row
    except TaskQueueError as exc:
        logger.warning("Failed to enqueue cron job %s into durable queue: %s", job_id, exc)
        return None


def enqueue_background_task(
    *,
    prompt: str,
    source: Any,
    task_id: str,
    config: Any = None,
    status: str = "running",
) -> dict[str, Any] | None:
    """Mirror a started gateway background task into the durable ledger."""

    if not task_queue_enabled(config):
        return None
    ledger = TaskQueueLedger(task_queue_path(config))
    try:
        return ledger.enqueue(
            task_id=task_id,
            title=str(prompt or "").strip()[:160] or task_id,
            project="Gateway",
            lane=infer_background_lane(prompt),
            priority="P1",
            worker_profile="gateway-background-worker",
            model_tier="mid",
            token_budget=6000,
            write_scope="gateway-runtime",
            acceptance_check=["Gateway background task completed or produced a visible blocker."],
            artifact_targets=[_source_label(source), f"gateway-task:{task_id}"],
            status=status,
            extra_fields={
                "dispatch": {
                    "kind": "gateway_background",
                    "prompt": str(prompt or ""),
                    "source": _source_payload(source),
                    "source_label": _source_label(source),
                }
            },
        )
    except TaskQueueError as exc:
        if "duplicate task id" in str(exc):
            try:
                return ledger.update_task(task_id, status=status, blocked_by=[])
            except Exception:
                logger.debug("Failed to refresh duplicate gateway queue task %s", task_id, exc_info=True)
                return None
        logger.warning("Failed to enqueue gateway task %s into durable queue: %s", task_id, exc)
        return None


def claim_ready_background_tasks(
    *,
    config: Any = None,
    max_count: int | None = None,
    task_id: str | None = None,
) -> QueueOperationResult | None:
    """Claim ready gateway-dispatchable queue tasks and mark them running."""

    if not task_queue_enabled(config):
        return None
    try:
        return TaskQueueLedger(task_queue_path(config)).claim_ready(
            predicate=lambda task: (
                task.get("dispatch", {}).get("kind") == "gateway_background"
                and (task_id is None or task.get("id") == task_id)
            ),
            max_count=max_count or background_concurrency(config),
        )
    except TaskQueueError as exc:
        logger.warning("Failed to claim gateway queue tasks: %s", exc)
        return None


def claim_ready_cron_tasks(
    *,
    config: Any = None,
    max_count: int | None = None,
) -> QueueOperationResult | None:
    """Claim ready cron queue tasks and mark them running."""

    if not task_queue_enabled(config):
        return None
    try:
        return TaskQueueLedger(task_queue_path(config)).claim_ready(
            predicate=lambda task: task.get("dispatch", {}).get("kind") == "cron_job",
            max_count=max_count or cron_queue_concurrency(config),
        )
    except TaskQueueError as exc:
        logger.warning("Failed to claim cron queue tasks: %s", exc)
        return None


def complete_background_task(*, task_id: str, evidence: str, config: Any = None) -> QueueOperationResult | None:
    """Mark a mirrored background task done and return the next ready selection."""

    if not task_queue_enabled(config):
        return None
    try:
        return TaskQueueLedger(task_queue_path(config)).complete_and_select(
            task_id,
            acceptance_evidence=[evidence],
        )
    except TaskQueueError as exc:
        if "task not found" not in str(exc):
            logger.warning("Failed to complete gateway queue task %s: %s", task_id, exc)
        return None


def complete_cron_queue_task(*, task_id: str, evidence: str, config: Any = None) -> QueueOperationResult | None:
    """Mark a queued cron task done and return the next ready selection."""

    if not task_queue_enabled(config):
        return None
    try:
        return TaskQueueLedger(task_queue_path(config)).complete_and_select(
            task_id,
            acceptance_evidence=[evidence],
        )
    except TaskQueueError as exc:
        if "task not found" not in str(exc):
            logger.warning("Failed to complete cron queue task %s: %s", task_id, exc)
        return None


def block_background_task(*, task_id: str, reason: str, config: Any = None) -> QueueOperationResult | None:
    """Mark a mirrored background task blocked and return alternate ready selection."""

    if not task_queue_enabled(config):
        return None
    try:
        return TaskQueueLedger(task_queue_path(config)).block_and_select(
            task_id,
            blocked_by=[reason or "background task failed"],
        )
    except TaskQueueError as exc:
        if "task not found" not in str(exc):
            logger.warning("Failed to block gateway queue task %s: %s", task_id, exc)
        return None


def block_cron_queue_task(*, task_id: str, reason: str, config: Any = None) -> QueueOperationResult | None:
    """Mark a queued cron task blocked and return alternate ready selection."""

    if not task_queue_enabled(config):
        return None
    try:
        return TaskQueueLedger(task_queue_path(config)).block_and_select(
            task_id,
            blocked_by=[reason or "cron queue task failed"],
        )
    except TaskQueueError as exc:
        if "task not found" not in str(exc):
            logger.warning("Failed to block cron queue task %s: %s", task_id, exc)
        return None
