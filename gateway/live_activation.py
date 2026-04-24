"""Pending live-service activation plans.

This module records activation intent for changes that require the live
gateway process to reload code. It deliberately does not execute restarts.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


LIVE_ACTIVATION_PENDING_FILE = ".live_activation_pending.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values] if values.strip() else []
    if isinstance(values, (list, tuple, set)):
        return [str(item).strip() for item in values if str(item).strip()]
    return [str(values).strip()] if str(values).strip() else []


def build_live_activation_plan(
    *,
    title: str,
    reason: str = "",
    service_name: str = "hermes-gateway.service",
    current_pid: Any = "",
    current_started_at: str = "",
    changed_files: Any = None,
    verification: Any = None,
    created_at: str = "",
) -> dict[str, Any]:
    """Build a pending activation record without applying it."""
    safe_title = str(title or "Hermes live activation").strip() or "Hermes live activation"
    return {
        "schema_version": 1,
        "plan_id": f"live-activation-{(created_at or _utc_now()).replace(':', '').replace('.', '-')}",
        "title": safe_title,
        "reason": str(reason or "").strip(),
        "status": "pending",
        "auto_apply": False,
        "live_restart_performed": False,
        "commands_executed": [],
        "created_at": created_at or _utc_now(),
        "service": {
            "name": str(service_name or "hermes-gateway.service").strip() or "hermes-gateway.service",
            "current_pid": str(current_pid or "").strip(),
            "current_started_at": str(current_started_at or "").strip(),
        },
        "changed_files": _as_list(changed_files),
        "verification": _as_list(verification),
        "safety": {
            "requires_explicit_apply": True,
            "no_live_restart_performed": True,
            "protect_telegram_continuity": True,
        },
        "apply_steps": [
            "Confirm Telegram is reachable and /tasks shows no urgent active work.",
            "Capture current service status and logs.",
            "Restart the gateway only during an explicit quiet-window activation.",
            "Verify Telegram /status and /tasks after the reload.",
        ],
        "rollback_steps": [
            "Restore the previous code revision or saved patch set.",
            "Restart the gateway back to the previous revision during a quiet window.",
            "Verify Telegram /status and the active DM session after rollback.",
        ],
    }


def _plan_path(hermes_home: str | Path | None = None) -> Path:
    root = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return root / LIVE_ACTIVATION_PENDING_FILE


def write_live_activation_plan(plan: dict[str, Any], *, hermes_home: str | Path | None = None) -> Path:
    """Persist a pending activation plan atomically."""
    path = _plan_path(hermes_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".live_activation_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def read_live_activation_plan(*, hermes_home: str | Path | None = None) -> dict[str, Any] | None:
    path = _plan_path(hermes_home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def render_live_activation_digest(*, hermes_home: str | Path | None = None) -> str:
    plan = read_live_activation_plan(hermes_home=hermes_home)
    if not plan or str(plan.get("status") or "").lower() != "pending":
        return ""
    service = plan.get("service") if isinstance(plan.get("service"), dict) else {}
    lines = [
        f"Pending live activation: {plan.get('title') or 'Hermes live activation'}",
        f"Service: {service.get('name') or 'hermes-gateway.service'}",
    ]
    if service.get("current_pid"):
        lines.append(f"Current live PID: {service.get('current_pid')}")
    if service.get("current_started_at"):
        lines.append(f"Current live start: {service.get('current_started_at')}")
    verification = _as_list(plan.get("verification"))
    if verification:
        lines.append(f"Verified: {verification[0]}")
    lines.append("No live restart has been performed.")
    lines.append("Apply requires an explicit quiet-window activation.")
    return "\n".join(lines)
