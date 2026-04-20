"""Track live task activity by scheduler lane for status surfaces."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Iterator, Optional

LANE_ORDER = ("interactive", "cron/scout", "housekeeping")
STATUS_LANE_ORDER = ("interactive", "cron_scout", "housekeeping")
_LANE_ALIASES = {
    "interactive": "interactive",
    "cron_scout": "cron/scout",
    "cron/scout": "cron/scout",
    "background": "cron/scout",
    "housekeeping": "housekeeping",
}
_STATUS_LANE_ALIASES = {
    "interactive": "interactive",
    "cron_scout": "cron_scout",
    "cron/scout": "cron_scout",
    "background": "cron_scout",
    "housekeeping": "housekeeping",
}


def normalize_task_lane(lane: Optional[str]) -> str:
    """Normalize internal/legacy lane names to the shared status display names."""
    if lane is None:
        return "interactive"
    raw = str(lane).strip().lower()
    if not raw:
        return "interactive"
    lookup = raw.replace("-", "_")
    return _LANE_ALIASES.get(lookup, _LANE_ALIASES.get(raw, raw))


def normalize_status_task_lane(lane: Optional[str]) -> str:
    """Normalize task lanes for machine-readable status payloads."""
    if lane is None:
        return "interactive"
    raw = str(lane).strip().lower()
    if not raw:
        return "interactive"
    lookup = raw.replace("-", "_")
    normalized = _STATUS_LANE_ALIASES.get(lookup, _STATUS_LANE_ALIASES.get(raw))
    if normalized:
        return normalized
    display_lane = normalize_task_lane(raw)
    return _STATUS_LANE_ALIASES.get(display_lane, display_lane.replace("/", "_"))


@dataclass
class _ActiveTask:
    task_id: str
    lane: str
    label: Optional[str]
    source: Optional[str]
    started_at: str
    kind: Optional[str] = None
    control_mode: Optional[str] = None
    actions: tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "lane": self.lane,
            "label": self.label,
            "source": self.source,
            "started_at": self.started_at,
            "kind": self.kind,
            "control_mode": self.control_mode,
            "actions": list(self.actions),
        }


class TaskLaneRegistry:
    """Small thread-safe registry for live task lane activity."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._tasks: Dict[str, _ActiveTask] = {}

    def start_task(
        self,
        *,
        task_id: str,
        lane: Optional[str],
        label: Optional[str] = None,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        control_mode: Optional[str] = None,
        actions: Optional[list[str] | tuple[str, ...]] = None,
    ) -> Dict[str, Any]:
        normalized_lane = normalize_task_lane(lane)
        normalized_actions = tuple(str(action).strip() for action in (actions or []) if str(action).strip())
        task = _ActiveTask(
            task_id=task_id,
            lane=normalized_lane,
            label=label,
            source=source,
            started_at=datetime.now(timezone.utc).isoformat(),
            kind=str(kind).strip() if kind is not None and str(kind).strip() else None,
            control_mode=str(control_mode).strip() if control_mode is not None and str(control_mode).strip() else None,
            actions=normalized_actions,
        )
        with self._lock:
            self._tasks[task_id] = task
        return task.to_dict()

    def finish_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    @contextmanager
    def track(
        self,
        *,
        task_id: str,
        lane: Optional[str],
        label: Optional[str] = None,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        control_mode: Optional[str] = None,
        actions: Optional[list[str] | tuple[str, ...]] = None,
    ) -> Iterator[Dict[str, Any]]:
        task = self.start_task(
            task_id=task_id,
            lane=lane,
            label=label,
            source=source,
            kind=kind,
            control_mode=control_mode,
            actions=actions,
        )
        try:
            yield task
        finally:
            self.finish_task(task_id)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            tasks = [task.to_dict() for task in self._tasks.values()]

        counts = {lane: 0 for lane in LANE_ORDER}
        for task in tasks:
            lane = task.get("lane")
            if lane in counts:
                counts[lane] += 1

        tasks.sort(key=lambda item: item.get("started_at") or "")
        return {
            "counts": counts,
            "tasks": tasks,
        }

    def status_snapshot(self) -> Dict[str, Any]:
        snapshot = self.snapshot()
        counts = {lane: 0 for lane in STATUS_LANE_ORDER}
        tasks = []

        for task in snapshot.get("tasks", []):
            status_task = dict(task)
            status_task["lane"] = normalize_status_task_lane(task.get("lane"))
            tasks.append(status_task)
            if status_task["lane"] in counts:
                counts[status_task["lane"]] += 1

        oldest_running = dict(tasks[0]) if tasks else None
        return {
            "active_count": len(tasks),
            "lane_counts": counts,
            "oldest_running": oldest_running,
            "tasks": tasks,
        }
