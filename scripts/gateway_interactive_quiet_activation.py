#!/usr/bin/env python3
"""One-shot detached activation that waits only for interactive gateway work to drain.

Used when gateway_state contains stale cron_scout/background entries that would
otherwise block a code reload forever. It still protects user-facing Telegram
continuity by requiring interactive lane and queued work to be zero for multiple
samples before running the standard restart+verify script.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HERMES_HOME", "/home/wutj/.hermes"))
REPO = Path(os.environ.get("HERMES_AGENT_REPO", "/home/wutj/.hermes/hermes-agent"))
STATE = HOME / "gateway_state.json"
PLAN = Path(os.environ.get("HERMES_GATEWAY_ACTIVATION_PLAN", str(HOME / ".live_activation_pending.json")))
LOG_DIR = HOME / "logs"
LOG = LOG_DIR / f"gateway-interactive-quiet-activation-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
VERIFY = REPO / "scripts" / "gateway_detached_restart_verify.sh"
SAMPLE_INTERVAL = float(os.environ.get("HERMES_GATEWAY_INTERACTIVE_SAMPLE_INTERVAL", "3"))
SAMPLES_REQUIRED = int(os.environ.get("HERMES_GATEWAY_INTERACTIVE_SAMPLES", "3"))
MAX_WAIT = int(os.environ.get("HERMES_GATEWAY_INTERACTIVE_MAX_WAIT", "900"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        f.flush()


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"json_read_failed path={path} error={exc}")
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def plan_pending() -> bool:
    plan = read_json(PLAN) or {}
    status = str(plan.get("status") or "").lower()
    if status != "pending":
        log(f"plan_not_pending status={status!r}")
        return False
    return True


def count_int(section: dict[str, Any], key: str) -> int | None:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def interactive_counts() -> tuple[int, int, int] | None:
    state = read_json(STATE)
    if not state:
        return None
    live = state.get("live_tasks")
    queued = state.get("queued_tasks")
    if not isinstance(live, dict) or not isinstance(queued, dict):
        return None
    live_lanes = live.get("lane_counts")
    queued_lanes = queued.get("lane_counts")
    if not isinstance(live_lanes, dict) or not isinstance(queued_lanes, dict):
        return None
    live_interactive = count_int(live_lanes, "interactive")
    queued_interactive = count_int(queued_lanes, "interactive")
    queued_total = count_int(queued, "queued_count")
    if live_interactive is None or queued_interactive is None or queued_total is None:
        return None
    return live_interactive, queued_interactive, queued_total


def wait_interactive_quiet() -> bool:
    deadline = time.monotonic() + MAX_WAIT
    quiet = 0
    log(f"waiting_interactive_quiet state={STATE} samples={SAMPLES_REQUIRED} interval={SAMPLE_INTERVAL} max_wait={MAX_WAIT}")
    while time.monotonic() < deadline:
        counts = interactive_counts()
        if counts is None:
            quiet = 0
            log("busy reason=unsafe_state")
        else:
            live_interactive, queued_interactive, queued_total = counts
            if live_interactive == 0 and queued_interactive == 0 and queued_total == 0:
                quiet += 1
                log(f"quiet_sample={quiet}/{SAMPLES_REQUIRED}")
                if quiet >= SAMPLES_REQUIRED:
                    return True
            else:
                quiet = 0
                log(f"busy live_interactive={live_interactive} queued_interactive={queued_interactive} queued_total={queued_total}")
        time.sleep(SAMPLE_INTERVAL)
    log("timeout_no_interactive_quiet")
    return False


def service_snapshot() -> dict[str, str]:
    proc = subprocess.run(
        ["systemctl", "show", "hermes-gateway.service", "-p", "MainPID", "-p", "ExecMainStartTimestamp", "-p", "ActiveState", "-p", "SubState", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def mark_plan(rc: int) -> None:
    plan = read_json(PLAN)
    if not plan:
        return
    commands = plan.get("commands_executed") if isinstance(plan.get("commands_executed"), list) else []
    commands.append({
        "at": now(),
        "command": str(VERIFY),
        "rc": rc,
        "log": str(LOG),
        "verify_log": str(HOME / "logs" / "gateway-restart-verify.log"),
        "service": service_snapshot(),
        "quiet_gate": "interactive_only",
    })
    plan["commands_executed"] = commands
    plan["last_activation_log"] = str(LOG)
    plan["last_verify_log"] = str(HOME / "logs" / "gateway-restart-verify.log")
    plan["updated_at"] = now()
    safety = plan.get("safety") if isinstance(plan.get("safety"), dict) else {}
    if rc == 0:
        plan["status"] = "applied"
        plan["live_restart_performed"] = True
        plan["applied_at"] = now()
        safety["no_live_restart_performed"] = False
    else:
        plan["status"] = "failed"
        plan["live_restart_performed"] = False
        plan["failed_at"] = now()
        plan["failure_rc"] = rc
    plan["safety"] = safety
    write_json(PLAN, plan)


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log(f"interactive_quiet_activation_start pid={os.getpid()} repo={REPO} hermes_home={HOME}")
    if not plan_pending():
        return 0
    if not wait_interactive_quiet():
        return 3
    if not plan_pending():
        return 0
    env = os.environ.copy()
    env["HERMES_HOME"] = str(HOME)
    env["HERMES_AGENT_REPO"] = str(REPO)
    env.setdefault("HERMES_GATEWAY_PYTHON_BIN", str(REPO / "venv" / "bin" / "python"))
    log(f"restart_verify_start script={VERIFY}")
    try:
        proc = subprocess.run([str(VERIFY)], cwd=str(REPO), env=env, text=True, timeout=180)
        rc = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        log(f"restart_verify_timeout seconds={exc.timeout}")
        rc = 124
    except Exception as exc:
        log(f"restart_verify_error error={exc}")
        rc = 2
    log(f"restart_verify_exit code={rc}")
    mark_plan(rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
