#!/usr/bin/env python3
"""External Hermes gateway health guard.

Runs outside the live gateway process (normally from a systemd timer). It checks
systemd service state plus the gateway runtime heartbeat file and invokes the
standard detached restart+verify helper only when the gateway appears down or
wedged. Healthy active conversations are left alone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - standalone fallback
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


REPO = Path(os.environ.get("HERMES_AGENT_REPO", Path(__file__).resolve().parents[1]))
HERMES_HOME = get_hermes_home()
STATE_PATH = HERMES_HOME / "gateway_state.json"
LOG_DIR = HERMES_HOME / "logs"
LOG_PATH = LOG_DIR / "gateway-external-health-guard.log"
LOCK_PATH = LOG_DIR / ".gateway-external-health-guard.lock"
LAST_RECOVERY_PATH = LOG_DIR / ".gateway-external-health-last-recovery.json"
VERIFY_SCRIPT = REPO / "scripts" / "gateway_detached_restart_verify.sh"
UNIT = os.environ.get("HERMES_GATEWAY_HEALTH_UNIT", "hermes-gateway.service")

STATUS_STALE_SECONDS = int(os.environ.get("HERMES_GATEWAY_HEALTH_STATUS_STALE_SECONDS", "300"))
ACTIVE_RECOVERY_SECONDS = int(os.environ.get("HERMES_GATEWAY_HEALTH_ACTIVE_RECOVERY_SECONDS", "2700"))
STARTUP_GRACE_SECONDS = int(os.environ.get("HERMES_GATEWAY_HEALTH_STARTUP_GRACE_SECONDS", "120"))
HEARTBEAT_GRACE_SECONDS = int(os.environ.get("HERMES_GATEWAY_HEALTH_HEARTBEAT_GRACE_SECONDS", "60"))
RECOVERY_COOLDOWN_SECONDS = int(os.environ.get("HERMES_GATEWAY_HEALTH_RECOVERY_COOLDOWN_SECONDS", "900"))
STALE_LOCK_SECONDS = int(os.environ.get("HERMES_GATEWAY_HEALTH_STALE_LOCK_SECONDS", "1800"))
DRY_RUN = os.environ.get("HERMES_GATEWAY_HEALTH_DRY_RUN", "0") == "1"


@dataclass(frozen=True)
class HealthDecision:
    status: str
    recover: bool
    reason: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        log(f"json_read_error path={path} error={type(exc).__name__}: {exc}")
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_systemd_start(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text == "n/a":
        return None
    parts = text.split()
    if len(parts) >= 4 and re.match(r"^[A-Z][a-z]{2}$", parts[0]):
        parts = parts[1:]
    if len(parts) < 2:
        return None
    try:
        # Systemd prints local wall-clock time; only used for rough startup grace.
        dt = datetime.strptime(" ".join(parts[:2]), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc)


def age_seconds(value: Any, now: datetime) -> float | None:
    dt = parse_iso(value)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds())


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _lane_count(section: dict[str, Any], lane: str) -> int | None:
    lanes = section.get("lane_counts")
    if not isinstance(lanes, dict):
        return None
    return _non_negative_int(lanes.get(lane))


def _interactive_live_count(state: dict[str, Any]) -> int:
    live = state.get("live_tasks") if isinstance(state.get("live_tasks"), dict) else {}
    lane_count = _lane_count(live, "interactive")
    if lane_count is not None:
        return lane_count
    active_count = _non_negative_int(live.get("active_count"))
    return active_count or 0


def _queued_count(state: dict[str, Any]) -> int:
    queued = state.get("queued_tasks") if isinstance(state.get("queued_tasks"), dict) else {}
    queued_count = _non_negative_int(queued.get("queued_count"))
    return queued_count or 0


def _oldest_interactive_started_at(state: dict[str, Any]) -> Any:
    live = state.get("live_tasks") if isinstance(state.get("live_tasks"), dict) else {}
    oldest = live.get("oldest_running")
    if isinstance(oldest, dict) and str(oldest.get("lane") or "interactive") == "interactive":
        return oldest.get("started_at")
    tasks = live.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict) and str(task.get("lane") or "") == "interactive":
                return task.get("started_at")
    return None


def _service_age_seconds(service: dict[str, Any], now: datetime) -> float | None:
    start = parse_systemd_start(service.get("ExecMainStartTimestamp"))
    if start is None:
        return None
    return max(0.0, (now - start).total_seconds())


def _heartbeat_stale_reason(state: dict[str, Any], now: datetime) -> str | None:
    heartbeats = state.get("heartbeats")
    if not isinstance(heartbeats, dict):
        return "cron_ticker heartbeat missing"
    cron = heartbeats.get("cron_ticker")
    if not isinstance(cron, dict):
        return "cron_ticker heartbeat missing"
    updated_age = age_seconds(cron.get("updated_at"), now)
    if updated_age is None:
        return "cron_ticker heartbeat timestamp invalid"
    stale_after = _non_negative_int(cron.get("stale_after_seconds")) or 180
    threshold = stale_after + HEARTBEAT_GRACE_SECONDS
    if updated_age > threshold:
        return f"cron_ticker heartbeat stale: age={int(updated_age)}s threshold={threshold}s"
    return None


def evaluate_health(
    service: dict[str, Any],
    state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    status_stale_seconds: int = STATUS_STALE_SECONDS,
    active_recovery_seconds: int = ACTIVE_RECOVERY_SECONDS,
) -> HealthDecision:
    now = now or utc_now()
    active = str(service.get("ActiveState") or "")
    sub = str(service.get("SubState") or "")
    pid = str(service.get("ExecMainPID") or service.get("MainPID") or "").strip()
    if active != "active" or sub != "running" or pid in {"", "0"}:
        return HealthDecision("down", True, f"gateway service not running: {active or 'unknown'}/{sub or 'unknown'}")

    if not isinstance(state, dict):
        service_age = _service_age_seconds(service, now)
        if service_age is not None and service_age < STARTUP_GRACE_SECONDS:
            return HealthDecision("warming", False, f"runtime status missing during startup grace: service_age={int(service_age)}s")
        return HealthDecision("stale", True, "runtime status missing or unreadable")

    stale_reasons: list[str] = []
    status_age = age_seconds(state.get("updated_at"), now)
    if status_age is None:
        stale_reasons.append("runtime status timestamp invalid")
    elif status_age > status_stale_seconds:
        stale_reasons.append(f"runtime status stale: age={int(status_age)}s threshold={status_stale_seconds}s")

    heartbeat_reason = _heartbeat_stale_reason(state, now)
    if heartbeat_reason:
        stale_reasons.append(heartbeat_reason)

    if not stale_reasons:
        return HealthDecision("healthy", False, "ok")

    live_interactive = _interactive_live_count(state)
    queued = _queued_count(state)
    if live_interactive > 0:
        task_age = age_seconds(_oldest_interactive_started_at(state), now)
        if task_age is None:
            return HealthDecision("busy_stale", False, "; ".join(stale_reasons) + "; interactive task age unknown")
        if task_age < active_recovery_seconds:
            return HealthDecision(
                "busy_stale",
                False,
                "; ".join(stale_reasons) + f"; interactive task still within recovery window: age={int(task_age)}s threshold={active_recovery_seconds}s",
            )
        return HealthDecision(
            "wedged",
            True,
            "; ".join(stale_reasons) + f"; interactive task exceeded recovery window: age={int(task_age)}s threshold={active_recovery_seconds}s",
        )

    if queued > 0:
        return HealthDecision("stale", True, "; ".join(stale_reasons) + f"; queued work present: queued={queued}")

    return HealthDecision("stale", True, "; ".join(stale_reasons))


def service_snapshot() -> dict[str, str]:
    cmd = [
        "systemctl",
        "show",
        UNIT,
        "-p",
        "ActiveState",
        "-p",
        "SubState",
        "-p",
        "ExecMainPID",
        "-p",
        "ExecMainStartTimestamp",
        "-p",
        "NRestarts",
        "--no-pager",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    result: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    if proc.returncode != 0:
        result.setdefault("ActiveState", "unknown")
        result.setdefault("SubState", "unknown")
        result["error"] = (proc.stderr or proc.stdout or "systemctl show failed").strip()
    return result


def read_runtime_status() -> dict[str, Any] | None:
    return read_json(STATE_PATH)


def _lock_owner_pid(content: str) -> int | None:
    for part in str(content or "").replace("\n", " ").split():
        if not part.startswith("pid="):
            continue
        try:
            pid = int(part.split("=", 1)[1])
        except ValueError:
            return None
        return pid if pid > 0 else None
    return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_lock() -> int | None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                content = LOCK_PATH.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            owner = _lock_owner_pid(content)
            age = None
            try:
                age = max(0.0, time.time() - LOCK_PATH.stat().st_mtime)
            except OSError:
                pass
            if owner is not None and _pid_is_running(owner):
                log(f"lock_exists owner_pid={owner}; exiting")
                return None
            if owner is not None or (age is not None and age >= STALE_LOCK_SECONDS):
                try:
                    LOCK_PATH.unlink()
                    log(f"stale_lock_removed owner_pid={owner} age_seconds={age}")
                    continue
                except OSError as exc:
                    log(f"stale_lock_remove_failed owner_pid={owner} error={exc}")
                    return None
            log("lock_exists_without_stale_owner; exiting")
            return None
        os.write(fd, f"pid={os.getpid()} started_at={utc_now_iso()}\n".encode("utf-8"))
        os.fsync(fd)
        return fd
    return None


def release_lock(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def cooldown_allows_recovery(now: datetime) -> bool:
    data = read_json(LAST_RECOVERY_PATH)
    if not data:
        return True
    last = parse_iso(data.get("at"))
    if last is None:
        return True
    return (now - last).total_seconds() >= RECOVERY_COOLDOWN_SECONDS


def record_recovery(reason: str, rc: int) -> None:
    write_json_atomic(
        LAST_RECOVERY_PATH,
        {
            "at": utc_now_iso(),
            "reason": reason,
            "rc": rc,
        },
    )


def run_restart_verify(reason: str) -> int:
    log(f"restart_verify_requested reason={reason!r} dry_run={int(DRY_RUN)} script={VERIFY_SCRIPT}")
    if DRY_RUN:
        return 0
    if not VERIFY_SCRIPT.exists():
        log(f"restart_verify_missing script={VERIFY_SCRIPT}")
        return 2
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["HERMES_AGENT_REPO"] = str(REPO)
    env.setdefault("HERMES_GATEWAY_PYTHON_BIN", str(REPO / "venv" / "bin" / "python"))
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("USER", os.environ.get("USER") or Path.home().name)
    env.setdefault("LOGNAME", env.get("USER", Path.home().name))
    try:
        proc = subprocess.run([str(VERIFY_SCRIPT)], cwd=str(REPO), env=env, text=True, timeout=240, check=False)
    except subprocess.TimeoutExpired as exc:
        log(f"restart_verify_timeout seconds={exc.timeout}")
        return 124
    except OSError as exc:
        log(f"restart_verify_error error={type(exc).__name__}: {exc}")
        return 2
    log(f"restart_verify_exit code={proc.returncode}")
    return int(proc.returncode)


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fd = acquire_lock()
    if fd is None:
        return 0
    try:
        now = utc_now()
        service = service_snapshot()
        state = read_runtime_status()
        decision = evaluate_health(service, state, now=now)
        log(f"decision status={decision.status} recover={decision.recover} reason={decision.reason}")
        if not decision.recover:
            return 0
        if not cooldown_allows_recovery(now):
            log("recovery suppressed by cooldown")
            return 0
        rc = run_restart_verify(decision.reason)
        record_recovery(decision.reason, rc)
        return rc
    finally:
        release_lock(fd)


if __name__ == "__main__":
    raise SystemExit(main())
