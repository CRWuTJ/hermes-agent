#!/usr/bin/env python3
"""Wait for a quiet Hermes gateway window, then activate a pending live reload.

This script is intentionally launched as a detached transient unit, not as a
child of the live gateway conversation. It waits until gateway_state reports no
active or queued work for multiple samples before invoking the existing
restart+verify helper.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(os.environ.get("HERMES_AGENT_REPO", Path(__file__).resolve().parents[1]))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - defensive fallback for standalone launch
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

HERMES_HOME = get_hermes_home()
STATE_PATH = HERMES_HOME / "gateway_state.json"
PLAN_PATH = HERMES_HOME / ".live_activation_pending.json"
LOG_DIR = HERMES_HOME / "logs"
LOG_PATH = LOG_DIR / f"gateway-quiet-activation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
LOCK_PATH = LOG_DIR / ".gateway-quiet-activation.lock"
VERIFY_SCRIPT = REPO / "scripts" / "gateway_detached_restart_verify.sh"

SAMPLE_INTERVAL = float(os.environ.get("HERMES_GATEWAY_QUIET_SAMPLE_INTERVAL", "3"))
QUIET_SAMPLES_REQUIRED = int(os.environ.get("HERMES_GATEWAY_QUIET_SAMPLES", "3"))
MAX_WAIT_SECONDS = int(os.environ.get("HERMES_GATEWAY_QUIET_MAX_WAIT", "900"))
DRY_RUN = os.environ.get("HERMES_GATEWAY_QUIET_ACTIVATION_DRY_RUN", "0") == "1"
STALE_LOCK_SECONDS = int(os.environ.get("HERMES_GATEWAY_QUIET_STALE_LOCK_SECONDS", "1800"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        log(f"json_missing path={path}")
        return None
    except Exception as exc:
        log(f"json_read_error path={path} error={exc}")
        return None
    if not isinstance(data, dict):
        log(f"json_invalid_type path={path} type={type(data).__name__}")
        return None
    return data


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def _lock_age_seconds() -> float | None:
    try:
        return max(0.0, time.time() - LOCK_PATH.stat().st_mtime)
    except OSError:
        return None


def _read_lock_content() -> str:
    try:
        return LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def acquire_lock() -> int | None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = _read_lock_content()
            owner_pid = _lock_owner_pid(existing)
            lock_age = _lock_age_seconds()
            if owner_pid is not None and _pid_is_running(owner_pid):
                log(f"lock_exists path={LOCK_PATH} owner_pid={owner_pid} content={existing!r}; exiting")
                return None
            if owner_pid is not None or (lock_age is not None and lock_age >= STALE_LOCK_SECONDS):
                try:
                    LOCK_PATH.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    log(f"stale_lock_remove_failed path={LOCK_PATH} owner_pid={owner_pid} error={exc}; exiting")
                    return None
                log(f"stale_lock_removed path={LOCK_PATH} owner_pid={owner_pid} age_seconds={lock_age} content={existing!r}")
                continue
            log(f"lock_exists path={LOCK_PATH} owner_pid={owner_pid} content={existing!r}; exiting")
            return None
        os.write(fd, f"pid={os.getpid()} started_at={utc_now()} log={LOG_PATH}\n".encode("utf-8"))
        os.fsync(fd)
        return fd
    log(f"lock_acquire_race path={LOCK_PATH}; exiting")
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


def plan_is_pending() -> bool:
    plan = read_json(PLAN_PATH) or {}
    status = str(plan.get("status") or "").lower()
    if status != "pending":
        log(f"no_pending_plan status={status!r}; exiting")
        return False
    return True


def _non_negative_int_count(section: dict[str, Any], field: str) -> int | None:
    value = section.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def state_counts() -> tuple[int, int, str] | None:
    state = read_json(STATE_PATH)
    if not isinstance(state, dict):
        log("unsafe_state unavailable; treating gateway as busy")
        return None
    live = state.get("live_tasks")
    queued = state.get("queued_tasks")
    if not isinstance(live, dict) or not isinstance(queued, dict):
        log("unsafe_state missing_task_sections; treating gateway as busy")
        return None
    if "active_count" not in live or "queued_count" not in queued:
        log("unsafe_state missing_count_fields; treating gateway as busy")
        return None
    active_count = _non_negative_int_count(live, "active_count")
    queued_count = _non_negative_int_count(queued, "queued_count")
    if active_count is None or queued_count is None:
        log("unsafe_state invalid_counts; treating gateway as busy")
        return None
    oldest = live.get("oldest_running") if isinstance(live.get("oldest_running"), dict) else {}
    task_id = str(oldest.get("task_id") or "")
    return active_count, queued_count, task_id


def wait_for_quiet() -> bool:
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    quiet_samples = 0
    log(
        "waiting_for_quiet "
        f"state={STATE_PATH} required_samples={QUIET_SAMPLES_REQUIRED} "
        f"sample_interval={SAMPLE_INTERVAL}s max_wait={MAX_WAIT_SECONDS}s"
    )
    while True:
        counts = state_counts()
        if counts is None:
            quiet_samples = 0
            log("busy reason=unsafe_or_missing_state")
        else:
            active_count, queued_count, task_id = counts
            busy = active_count > 0 or queued_count > 0
            if busy:
                quiet_samples = 0
                log(f"busy active_count={active_count} queued_count={queued_count} oldest_task={task_id}")
            else:
                quiet_samples += 1
                log(f"quiet_sample={quiet_samples}/{QUIET_SAMPLES_REQUIRED}")
                if quiet_samples >= QUIET_SAMPLES_REQUIRED:
                    return True
        if time.monotonic() >= deadline:
            break
        time.sleep(SAMPLE_INTERVAL)
    log("quiet_wait_timeout; no restart attempted")
    return False


def run_restart_verify() -> int:
    if not VERIFY_SCRIPT.exists():
        log(f"missing_verify_script path={VERIFY_SCRIPT}")
        return 2
    env = os.environ.copy()
    default_user = os.environ.get("USER") or os.environ.get("LOGNAME") or Path.home().name
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("USER", default_user)
    env.setdefault("LOGNAME", env.get("USER", default_user))
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["HERMES_AGENT_REPO"] = str(REPO)
    env.setdefault("HERMES_GATEWAY_PYTHON_BIN", str(REPO / "venv" / "bin" / "python"))
    if DRY_RUN:
        env["HERMES_GATEWAY_RESTART_DRY_RUN"] = "1"
    log(f"restart_verify_start dry_run={int(DRY_RUN)} script={VERIFY_SCRIPT}")
    try:
        proc = subprocess.run([str(VERIFY_SCRIPT)], cwd=str(REPO), env=env, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        log(f"restart_verify_timeout seconds={exc.timeout}")
        return 124
    except OSError as exc:
        log(f"restart_verify_error error={exc}")
        return 2
    log(f"restart_verify_exit code={proc.returncode}")
    return int(proc.returncode)


def current_service_snapshot() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        proc = subprocess.run(
            [
                "systemctl",
                "show",
                "hermes-gateway.service",
                "--property",
                "MainPID",
                "--property",
                "ExecMainStartTimestamp",
                "--property",
                "ActiveState",
                "--property",
                "SubState",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key] = value
    except Exception as exc:
        result["error"] = str(exc)
    return result


def mark_plan(rc: int) -> None:
    plan = read_json(PLAN_PATH)
    if not plan:
        return
    commands = plan.get("commands_executed")
    if not isinstance(commands, list):
        commands = []
    commands.append(
        {
            "at": utc_now(),
            "command": str(VERIFY_SCRIPT),
            "rc": rc,
            "log": str(LOG_PATH),
            "verify_log": str(HERMES_HOME / "logs" / "gateway-restart-verify.log"),
            "service": current_service_snapshot(),
        }
    )
    plan["commands_executed"] = commands
    plan["last_activation_log"] = str(LOG_PATH)
    plan["last_verify_log"] = str(HERMES_HOME / "logs" / "gateway-restart-verify.log")
    plan["updated_at"] = utc_now()
    if rc == 0:
        plan["status"] = "applied"
        plan["live_restart_performed"] = True
        plan["applied_at"] = utc_now()
        safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
        safety["no_live_restart_performed"] = False
        plan["safety"] = safety
    else:
        plan["status"] = "failed"
        plan["live_restart_performed"] = False
        plan["failed_at"] = utc_now()
        plan["failure_rc"] = rc
    write_json_atomic(PLAN_PATH, plan)


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log(f"quiet_activation_start pid={os.getpid()} repo={REPO} hermes_home={HERMES_HOME}")
    fd = acquire_lock()
    if fd is None:
        return 0
    try:
        if not plan_is_pending():
            return 0
        if not wait_for_quiet():
            return 3
        if not plan_is_pending():
            log("pending_plan_cleared_after_quiet; no restart attempted")
            return 0
        rc = run_restart_verify()
        mark_plan(rc)
        return rc
    finally:
        release_lock(fd)
        log("quiet_activation_exit")


if __name__ == "__main__":
    raise SystemExit(main())
