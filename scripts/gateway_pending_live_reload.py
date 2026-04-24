#!/usr/bin/env python3
import fcntl
import getpass
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
HOME_DIR = Path(os.getenv("HOME") or Path.home()).expanduser()
USER_NAME = os.getenv("USER") or getpass.getuser()
LOGNAME = os.getenv("LOGNAME") or USER_NAME
HERMES_HOME = Path(os.getenv("HERMES_HOME") or (HOME_DIR / ".hermes")).expanduser()
LOG_PATH = HERMES_HOME / "logs" / "gateway-pending-live-reload.log"
LOCK_PATH = HERMES_HOME / "logs" / "gateway-pending-live-reload.lock"
UNIT = "hermes-gateway.service"
MAX_WAIT_SECONDS = int(os.getenv("HERMES_GATEWAY_MAX_WAIT_SECONDS", "21600"))
POLL_INTERVAL = int(os.getenv("HERMES_GATEWAY_POLL_INTERVAL", "30"))
DRY_RUN = os.getenv("HERMES_GATEWAY_RESTART_DRY_RUN", "0") == "1"
ENV = {
    **os.environ,
    "HOME": str(HOME_DIR),
    "USER": USER_NAME,
    "LOGNAME": LOGNAME,
    "HERMES_HOME": str(HERMES_HOME),
}


def log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


@contextmanager
def singleton_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another pending live reload watcher is already running")
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            try:
                handle.seek(0)
                handle.truncate()
            except Exception:
                pass
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, env=ENV, cwd=str(REPO), check=False)


def get_gateway_pid() -> int:
    proc = _run(["systemctl", "show", UNIT, "--property=ExecMainPID", "--value"])
    text = (proc.stdout or "").strip() or "0"
    try:
        return int(text)
    except ValueError:
        return 0


def get_gateway_start_epoch(pid: int) -> int | None:
    if pid <= 0:
        return None
    proc = _run(["ps", "-p", str(pid), "-o", "lstart=", "--no-headers"])
    start_text = (proc.stdout or "").strip()
    if not start_text:
        return None
    try:
        dt = datetime.strptime(start_text, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return int(dt.timestamp())


def _file_mtime(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime)
    except FileNotFoundError:
        return None


def build_probe() -> dict:
    sys.path.insert(0, str(REPO))
    from gateway.status import build_gateway_status_payload, format_status_oldest_running_task
    import hermes_cli.gateway as gateway_cli

    payload = build_gateway_status_payload()
    live = payload.get("live_tasks") if isinstance(payload.get("live_tasks"), dict) else {}
    queued = payload.get("queued_tasks") if isinstance(payload.get("queued_tasks"), dict) else {}
    oldest = live.get("oldest_running") if isinstance(live.get("oldest_running"), dict) else None
    pid = get_gateway_pid()
    start_epoch = get_gateway_start_epoch(pid)
    run_mtime = _file_mtime(REPO / "gateway/run.py")
    worker_mtime = _file_mtime(REPO / "gateway/worker_runtime.py")
    watched_mtimes = [mtime for mtime in (run_mtime, worker_mtime) if mtime is not None]
    code_stale = start_epoch is None or any(start_epoch < mtime for mtime in watched_mtimes)

    report = gateway_cli.get_gateway_systemd_report(requested_scope="system")
    unit_definition_current = None
    service_definition_stale = False
    unit_path = report.get("unit_path")
    if report.get("installed") and report.get("system") and unit_path:
        path_obj = Path(unit_path)
        unit_definition_current = path_obj.exists() and gateway_cli.systemd_unit_path_is_current(path_obj, system=True)
        service_definition_stale = bool(report.get("drifted")) or unit_definition_current is False

    stale = code_stale or service_definition_stale
    return {
        "pid": pid,
        "start_epoch": start_epoch,
        "run_mtime": run_mtime,
        "worker_mtime": worker_mtime,
        "code_stale": code_stale,
        "unit_name": report.get("unit_name"),
        "unit_path": unit_path,
        "unit_drifted": bool(report.get("drifted")) if report else False,
        "unit_definition_current": unit_definition_current,
        "service_definition_stale": service_definition_stale,
        "stale": stale,
        "live_active": int(live.get("active_count") or 0),
        "live_lanes": dict(live.get("lane_counts") or {}),
        "queued_count": int(queued.get("queued_count") or 0),
        "queued_lanes": dict(queued.get("lane_counts") or {}),
        "oldest_running": format_status_oldest_running_task(oldest) if oldest else None,
    }


def run_restart_verify() -> int:
    cmd = ["bash", "scripts/gateway_detached_restart_verify.sh"]
    proc = subprocess.run(cmd, cwd=str(REPO), env=ENV, check=False)
    return proc.returncode


def main() -> int:
    try:
        with singleton_lock(LOCK_PATH):
            log(
                "pending live reload watcher start "
                + json.dumps(
                    {
                        "dry_run": DRY_RUN,
                        "max_wait_seconds": MAX_WAIT_SECONDS,
                        "poll_interval": POLL_INTERVAL,
                    },
                    ensure_ascii=False,
                )
            )
            initial = build_probe()
            log("initial probe " + json.dumps(initial, ensure_ascii=False))
            if not initial.get("stale"):
                log("gateway already fresh; nothing to do")
                return 0

            deadline = time.time() + max(0, MAX_WAIT_SECONDS)
            while True:
                probe = build_probe()
                if not probe.get("stale"):
                    log("gateway became fresh before restart; exiting")
                    return 0
                if int(probe.get("live_active") or 0) == 0:
                    log("safe window reached; invoking detached restart verify")
                    rc = run_restart_verify()
                    post = build_probe()
                    log("restart verify exit rc=%s post=%s" % (rc, json.dumps(post, ensure_ascii=False)))
                    if DRY_RUN:
                        log("dry-run completed; gateway freshness is unchanged because no restart was performed")
                        return rc
                    if rc == 0 and post.get("stale"):
                        log("restart verify reported success but gateway still stale after reload")
                        return 11
                    return rc
                oldest = probe.get("oldest_running") or "unknown"
                log(
                    "waiting for quiet live window "
                    + json.dumps(
                        {
                            "live_active": probe.get("live_active"),
                            "live_lanes": probe.get("live_lanes"),
                            "queued_count": probe.get("queued_count"),
                            "queued_lanes": probe.get("queued_lanes"),
                            "oldest_running": oldest,
                        },
                        ensure_ascii=False,
                    )
                )
                if time.time() >= deadline:
                    log("pending live reload timed out before a quiet window appeared")
                    return 10
                time.sleep(max(1, POLL_INTERVAL))
    except RuntimeError as exc:
        log(str(exc))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
