"""Durable task queue primitives for Hermes worker-pool pilots.

This module is intentionally small and dependency-free: it manages a JSONL
ledger, selects ready tasks with lane/artifact locks, and provides the state
transitions needed by a queue runner. Runtime dispatch stays injectable so the
live gateway can adopt it without replacing cron or active sessions blindly.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # pragma: no cover - Windows fallback is exercised by not raising.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


REQUIRED_FIELDS = {
    "id",
    "title",
    "project",
    "lane",
    "priority",
    "status",
    "dependencies",
    "blocked_by",
    "worker_profile",
    "model_tier",
    "token_budget",
    "write_scope",
    "acceptance_check",
    "artifact_targets",
    "last_heartbeat_at",
    "created_at",
    "updated_at",
}

DEFAULT_LANE_LIMITS: dict[str, int] = {
    "control": 1,
    "implementation": 1,
    "research": 1,
    "verification": 1,
    "knowledge": 1,
    "infra_debug": 1,
    "watchdog": 1,
}

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
LANE_ORDER = {
    "control": 0,
    "infra_debug": 1,
    "implementation": 2,
    "verification": 3,
    "knowledge": 4,
    "research": 5,
    "watchdog": 6,
}
CLOSED_STATUSES = {"done", "cancelled"}
LOCKING_STATUSES = {"running", "review"}


class TaskQueueError(RuntimeError):
    """Raised when a durable queue operation cannot be completed safely."""


@dataclass(frozen=True)
class QueueOperationResult:
    """Machine-readable result for runner operations."""

    task: dict[str, Any] | None
    selection: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"task": self.task, "selection": self.selection}


def utc_now_iso() -> str:
    """Return a compact UTC ISO timestamp suitable for ledger rows."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    """Drop parser-only metadata before returning or saving a task."""

    return {key: value for key, value in task.items() if not str(key).startswith("_")}


def _as_list(value: Any, *, default: list[Any] | None = None) -> list[Any]:
    if value is None:
        return list(default or [])
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "task"


def _next_task_id(title: str, existing_ids: set[str]) -> str:
    base = _slugify(title)[:54]
    candidate = base
    idx = 2
    while candidate in existing_ids:
        suffix = f"-{idx}"
        candidate = f"{base[:54 - len(suffix)]}{suffix}"
        idx += 1
    return candidate


def load_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load a JSONL ledger without raising on row-level validation errors."""

    tasks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], []

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append({"line": line_number, "error": exc.msg})
            continue
        if not isinstance(row, dict):
            errors.append({"line": line_number, "error": "row is not a JSON object"})
            continue
        row["_line"] = line_number
        tasks.append(row)
    return tasks, errors


def validate_tasks(tasks: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Validate schema and dependency references for a task list."""

    by_id: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    materialized = list(tasks)

    for task in materialized:
        task_id = task.get("id")
        line = task.get("_line")
        missing = sorted(REQUIRED_FIELDS.difference(task))
        if missing:
            errors.append({"id": task_id, "line": line, "error": "missing required fields", "fields": missing})
        if not isinstance(task_id, str) or not task_id:
            errors.append({"id": task_id, "line": line, "error": "id must be a non-empty string"})
            continue
        if task_id in by_id:
            errors.append({"id": task_id, "line": line, "error": "duplicate id"})
        by_id[task_id] = task

    for task in materialized:
        task_id = task.get("id")
        dependencies = task.get("dependencies", [])
        if isinstance(dependencies, list):
            for dep_id in dependencies:
                if dep_id not in by_id:
                    errors.append({"id": task_id, "line": task.get("_line"), "error": "unknown dependency", "dependency": dep_id})
        else:
            errors.append({"id": task_id, "line": task.get("_line"), "error": "dependencies must be an array"})

        for field in ("blocked_by", "acceptance_check", "artifact_targets"):
            if field in task and not isinstance(task[field], list):
                errors.append({"id": task_id, "line": task.get("_line"), "error": f"{field} must be an array"})

    return by_id, errors


def _sort_key(task: dict[str, Any]) -> tuple[int, int, str, str]:
    return (
        PRIORITY_ORDER.get(str(task.get("priority") or "P3"), 99),
        LANE_ORDER.get(str(task.get("lane") or ""), 99),
        str(task.get("updated_at") or ""),
        str(task.get("id") or ""),
    )


def _is_write_capable(task: dict[str, Any]) -> bool:
    return task.get("write_scope") != "read-only"


def _targets(task: dict[str, Any]) -> list[str]:
    raw_targets = task.get("artifact_targets", [])
    if not isinstance(raw_targets, list):
        return []
    return [str(item) for item in raw_targets if str(item)]


def _non_empty_list(task: dict[str, Any], field: str) -> bool:
    value = task.get(field)
    return isinstance(value, list) and bool(value)


def _requires_knowledge_disposition(task: dict[str, Any]) -> bool:
    governance = task.get("workflow_governance")
    return isinstance(governance, dict) and governance.get("knowledge_disposition") == "required"


def _blocker_reasons(task: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[str]:
    reasons: list[str] = []

    if task.get("status") == "blocked":
        reasons.append("status:blocked")

    blocked_by = task.get("blocked_by", [])
    if isinstance(blocked_by, list) and blocked_by:
        reasons.extend(f"blocked_by:{item}" for item in blocked_by)
    elif not isinstance(blocked_by, list):
        reasons.append("blocked_by:not-array")

    dependencies = task.get("dependencies", [])
    if isinstance(dependencies, list):
        for dep_id in dependencies:
            dep = by_id.get(dep_id)
            if dep is None:
                reasons.append(f"dependency-missing:{dep_id}")
            elif dep.get("status") != "done":
                reasons.append(f"dependency-not-done:{dep_id}")
    else:
        reasons.append("dependencies:not-array")

    if not _non_empty_list(task, "acceptance_check"):
        reasons.append("missing-acceptance-check")
    if not _non_empty_list(task, "artifact_targets"):
        reasons.append("missing-artifact-targets")
    if str(task.get("write_scope") or "").strip().lower() == "needs-user-approval":
        reasons.append("write-scope-needs-user-approval")

    return reasons


def _add_lock(
    locks: dict[str, str],
    conflicts: list[dict[str, Any]],
    owner: str,
    artifacts: Iterable[str],
) -> None:
    for artifact in artifacts:
        existing_owner = locks.get(artifact)
        if existing_owner and existing_owner != owner:
            conflicts.append({"id": owner, "conflicts_with": existing_owner, "artifacts": [artifact]})
        else:
            locks[artifact] = owner


def select_ready_tasks(
    tasks: list[dict[str, Any]],
    *,
    lane_limits: dict[str, int] | None = None,
    max_count: int | None = None,
) -> dict[str, Any]:
    """Select dispatchable tasks while respecting dependencies and locks.

    The selector is deterministic and side-effect free. Running/review tasks
    consume lane capacity and hold write locks; ready tasks are selected in
    priority/lane/update order until capacity, lock, or max_count stops them.
    """

    limits = dict(DEFAULT_LANE_LIMITS)
    if lane_limits:
        limits.update({str(key): int(value) for key, value in lane_limits.items()})

    by_id, task_errors = validate_tasks(tasks)
    validation_errors = list(task_errors)
    status_counts = Counter(str(task.get("status", "missing")) for task in tasks)

    selected: list[dict[str, Any]] = []
    skipped_done: list[str] = []
    skipped_blocked: list[dict[str, Any]] = []
    skipped_not_ready: list[dict[str, str]] = []
    skipped_lane_limit: list[dict[str, str]] = []
    artifact_lock_conflicts: list[dict[str, Any]] = []

    lane_usage: defaultdict[str, int] = defaultdict(int)
    locks: dict[str, str] = {}

    for task in tasks:
        task_id = str(task.get("id") or "")
        status = task.get("status")
        if status in CLOSED_STATUSES:
            skipped_done.append(task_id)
        if status in LOCKING_STATUSES:
            lane_usage[str(task.get("lane") or "")] += 1
            if _is_write_capable(task):
                _add_lock(locks, artifact_lock_conflicts, task_id, _targets(task))

    for task in sorted(tasks, key=_sort_key):
        task_id = str(task.get("id") or "")
        status = task.get("status")

        if status in CLOSED_STATUSES:
            continue
        if status != "ready":
            if status == "blocked" or task.get("blocked_by"):
                skipped_blocked.append({"id": task_id, "reasons": _blocker_reasons(task, by_id)})
            elif status not in LOCKING_STATUSES:
                skipped_not_ready.append({"id": task_id, "status": str(status)})
            continue

        reasons = _blocker_reasons(task, by_id)
        if reasons:
            skipped_blocked.append({"id": task_id, "reasons": reasons})
            continue

        lane = str(task.get("lane") or "")
        if lane_usage[lane] >= limits.get(lane, 0):
            skipped_lane_limit.append({"id": task_id, "lane": lane})
            continue

        conflict_artifacts: list[str] = []
        conflict_owners: list[str] = []
        if _is_write_capable(task):
            for artifact in _targets(task):
                owner = locks.get(artifact)
                if owner and owner != task_id:
                    conflict_artifacts.append(artifact)
                    conflict_owners.append(owner)
        if conflict_artifacts:
            artifact_lock_conflicts.append(
                {
                    "id": task_id,
                    "conflicts_with": sorted(set(conflict_owners)),
                    "artifacts": sorted(set(conflict_artifacts)),
                }
            )
            continue

        selected.append(_public_task(task))
        lane_usage[lane] += 1
        if _is_write_capable(task):
            _add_lock(locks, artifact_lock_conflicts, task_id, _targets(task))
        if max_count is not None and len(selected) >= max_count:
            break

    return {
        "task_count": len(tasks),
        "status_counts": dict(status_counts),
        "selected": selected,
        "skipped_done": skipped_done,
        "skipped_blocked": skipped_blocked,
        "skipped_not_ready": skipped_not_ready,
        "skipped_lane_limit": skipped_lane_limit,
        "artifact_lock_conflicts": artifact_lock_conflicts,
        "validation_errors": validation_errors,
    }


def summarize_task_queue(
    tasks: list[dict[str, Any]],
    *,
    lane_limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Return a compact visibility summary for watchdogs and status UIs."""

    selection = select_ready_tasks(tasks, lane_limits=lane_limits)
    lane_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    open_count = 0
    for task in tasks:
        status = str(task.get("status") or "missing")
        lane = str(task.get("lane") or "unknown")
        lane_counts[lane][status] += 1
        if status not in CLOSED_STATUSES:
            open_count += 1

    selected = list(selection.get("selected") or [])
    return {
        "task_count": len(tasks),
        "open_count": open_count,
        "status_counts": dict(selection.get("status_counts") or {}),
        "lane_counts": {lane: dict(counts) for lane, counts in sorted(lane_counts.items())},
        "dispatchable_count": len(selected),
        "next_ready_ids": [str(task.get("id")) for task in selected],
        "blocked_count": len(selection.get("skipped_blocked") or []),
        "validation_error_count": len(selection.get("validation_errors") or []),
    }


class TaskQueueLedger:
    """Small JSONL-backed durable queue ledger."""

    def __init__(self, path: str | os.PathLike[str] | Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _exclusive_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def load(self, *, strict: bool = True) -> list[dict[str, Any]]:
        tasks, parse_errors = load_jsonl(self.path)
        _, schema_errors = validate_tasks(tasks)
        errors = parse_errors + schema_errors
        if strict and errors:
            raise TaskQueueError(json.dumps(errors, ensure_ascii=False))
        return tasks

    def save(self, tasks: Iterable[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        public_rows = [_public_task(dict(task)) for task in tasks]
        content = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in public_rows)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _find_task(self, tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
        for task in tasks:
            if task.get("id") == task_id:
                return task
        raise TaskQueueError(f"task not found: {task_id}")

    def _apply_updates(self, task: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        for key, value in updates.items():
            if key in {"dependencies", "blocked_by", "acceptance_check", "artifact_targets", "completion_evidence"}:
                task[key] = [str(item) for item in _as_list(value)]
            else:
                task[key] = value
        task["updated_at"] = now
        if "status" in updates:
            task["last_heartbeat_at"] = now if updates.get("status") in LOCKING_STATUSES else None
        return _public_task(task)

    def select(self, *, lane_limits: dict[str, int] | None = None, max_count: int | None = None) -> dict[str, Any]:
        return select_ready_tasks(self.load(strict=True), lane_limits=lane_limits, max_count=max_count)

    def status_summary(self, *, lane_limits: dict[str, int] | None = None) -> dict[str, Any]:
        return summarize_task_queue(self.load(strict=True), lane_limits=lane_limits)

    def enqueue(
        self,
        *,
        title: str,
        project: str = "General",
        lane: str = "control",
        priority: str = "P2",
        dependencies: Iterable[str] | None = None,
        blocked_by: Iterable[str] | None = None,
        worker_profile: str = "general-worker",
        model_tier: str = "mid",
        token_budget: int = 4000,
        write_scope: str = "queue-ledger-only",
        acceptance_check: Iterable[str] | None = None,
        artifact_targets: Iterable[str] | None = None,
        task_id: str | None = None,
        status: str = "ready",
        extra_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not str(title).strip():
            raise TaskQueueError("title is required")
        with self._exclusive_lock():
            tasks = self.load(strict=True)
            existing_ids = {str(task.get("id")) for task in tasks}
            chosen_id = str(task_id).strip() if task_id else _next_task_id(title, existing_ids)
            if chosen_id in existing_ids:
                raise TaskQueueError(f"duplicate task id: {chosen_id}")
            now = utc_now_iso()
            row = {
                "id": chosen_id,
                "title": str(title).strip(),
                "project": str(project or "General").strip(),
                "lane": str(lane or "control").strip(),
                "priority": str(priority or "P2").strip(),
                "status": str(status or "ready").strip(),
                "dependencies": [str(item) for item in _as_list(dependencies)],
                "blocked_by": [str(item) for item in _as_list(blocked_by)],
                "worker_profile": str(worker_profile or "general-worker").strip(),
                "model_tier": str(model_tier or "mid").strip(),
                "token_budget": int(token_budget),
                "write_scope": str(write_scope or "queue-ledger-only").strip(),
                "acceptance_check": [str(item) for item in _as_list(acceptance_check, default=["Manual verification required."])],
                "artifact_targets": [str(item) for item in _as_list(artifact_targets, default=[".hermes/task_queue/backlog.jsonl"])],
                "last_heartbeat_at": now if str(status or "ready").strip() in LOCKING_STATUSES else None,
                "created_at": now,
                "updated_at": now,
            }
            if extra_fields:
                if not isinstance(extra_fields, dict):
                    raise TaskQueueError("extra_fields must be a dictionary")
                protected = set(REQUIRED_FIELDS) | {"_line", "completion_evidence"}
                for key, value in extra_fields.items():
                    field = str(key)
                    if field in protected or field.startswith("_"):
                        raise TaskQueueError(f"extra field is reserved: {field}")
                    row[field] = value
            tasks.append(row)
            self.save(tasks)
            return row

    def update_task(self, task_id: str, **updates: Any) -> dict[str, Any]:
        with self._exclusive_lock():
            tasks = self.load(strict=True)
            task = self._apply_updates(self._find_task(tasks, task_id), updates)
            self.save(tasks)
            return task

    def complete_and_select(
        self,
        task_id: str,
        *,
        acceptance_evidence: Iterable[str] | None = None,
        knowledge_disposition: str | None = None,
        lane_limits: dict[str, int] | None = None,
        max_count: int | None = None,
    ) -> QueueOperationResult:
        evidence = [str(item) for item in _as_list(acceptance_evidence)]
        if not evidence:
            raise TaskQueueError("acceptance_evidence is required before completing a task")
        with self._exclusive_lock():
            tasks = self.load(strict=True)
            target = self._find_task(tasks, task_id)
            if target.get("status") in CLOSED_STATUSES:
                raise TaskQueueError(f"task already closed: {task_id}")
            if target.get("status") == "blocked" or target.get("blocked_by"):
                raise TaskQueueError(f"cannot complete blocked task without unblocking first: {task_id}")
            disposition = str(knowledge_disposition or target.get("knowledge_disposition") or "").strip()
            if _requires_knowledge_disposition(target) and not disposition:
                raise TaskQueueError("knowledge_disposition is required before completing a governed task")
            updates: dict[str, Any] = {"status": "done", "blocked_by": [], "completion_evidence": evidence}
            if disposition:
                updates["knowledge_disposition"] = disposition
            task = self._apply_updates(target, updates)
            self.save(tasks)
            selection = select_ready_tasks(tasks, lane_limits=lane_limits, max_count=max_count)
            return QueueOperationResult(task=task, selection=selection)

    def block_and_select(
        self,
        task_id: str,
        *,
        blocked_by: Iterable[str],
        lane_limits: dict[str, int] | None = None,
        max_count: int | None = None,
    ) -> QueueOperationResult:
        blockers = [str(item) for item in _as_list(blocked_by)]
        if not blockers:
            raise TaskQueueError("blocked_by is required when blocking a task")
        with self._exclusive_lock():
            tasks = self.load(strict=True)
            task = self._apply_updates(self._find_task(tasks, task_id), {"status": "blocked", "blocked_by": blockers})
            self.save(tasks)
            selection = select_ready_tasks(tasks, lane_limits=lane_limits, max_count=max_count)
            return QueueOperationResult(task=task, selection=selection)

    def _claim_candidates(
        self,
        tasks: list[dict[str, Any]],
        predicate: Any | None,
    ) -> list[dict[str, Any]]:
        if predicate is None:
            return tasks

        candidates: list[dict[str, Any]] = []
        for task in tasks:
            if task.get("status") != "ready" or predicate(_public_task(task)):
                candidates.append(task)
        return candidates

    def claim_ready(
        self,
        *,
        predicate: Any | None = None,
        lane_limits: dict[str, int] | None = None,
        max_count: int | None = None,
    ) -> QueueOperationResult:
        with self._exclusive_lock():
            tasks = self.load(strict=True)
            candidates = self._claim_candidates(tasks, predicate)
            selection = select_ready_tasks(candidates, lane_limits=lane_limits, max_count=max_count)
            selected = list(selection.get("selected") or [])
            started: list[dict[str, Any]] = []
            for item in selected:
                started.append(self._apply_updates(self._find_task(tasks, str(item["id"])), {"status": "running"}))
            self.save(tasks)
            return QueueOperationResult(
                task={"started": started},
                selection=select_ready_tasks(candidates, lane_limits=lane_limits, max_count=max_count),
            )

    def start_next(
        self,
        *,
        lane_limits: dict[str, int] | None = None,
        max_count: int | None = None,
    ) -> QueueOperationResult:
        return self.claim_ready(lane_limits=lane_limits, max_count=max_count)
