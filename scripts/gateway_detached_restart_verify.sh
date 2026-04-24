#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TARGET_USER="${USER:-$(id -un)}"
TARGET_LOGNAME="${LOGNAME:-$TARGET_USER}"
TARGET_HOME="${HOME:-$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6)}"
if [[ -z "$TARGET_HOME" ]]; then
  TARGET_HOME="$(python3 - <<'PY'
from pathlib import Path
print(Path.home())
PY
)"
fi
TARGET_HERMES_HOME="${HERMES_HOME:-$TARGET_HOME/.hermes}"
LOG="$TARGET_HERMES_HOME/logs/gateway-restart-verify.log"
UNIT=hermes-gateway.service
UNIT_NAME=${UNIT%.service}
DRY_RUN=${HERMES_GATEWAY_RESTART_DRY_RUN:-0}
EXPECTED_MEMORY_HIGH_BYTES=402653184
EXPECTED_MEMORY_MAX_BYTES=805306368
EXPECTED_TASKS_MAX=128
EXPECTED_CPU_WEIGHT=50
ROOT_BRIDGE=${HERMES_GATEWAY_ROOT_BRIDGE:-/mnt/c/Windows/System32/wsl.exe}
WSL_DISTRO=${HERMES_GATEWAY_WSL_DISTRO:-${WSL_DISTRO_NAME:-Ubuntu}}
PYTHON_BIN=${HERMES_GATEWAY_PYTHON_BIN:-$REPO/venv/bin/python}
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Unable to locate python interpreter for gateway restart verify" >&2
  exit 2
fi
PYTHONPATH_VALUE="$REPO${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$(dirname "$LOG")"

current_pid() {
  systemctl show "$UNIT" --property=ExecMainPID --value 2>/dev/null | tr -d '[:space:]'
}

current_start_text() {
  local pid="$1"
  if [[ -z "$pid" || "$pid" == "0" ]]; then
    return 0
  fi
  ps -p "$pid" -o lstart= --no-headers 2>/dev/null | sed 's/^ *//'
}

current_start_epoch() {
  local pid="$1"
  local start_text
  start_text="$(current_start_text "$pid")"
  if [[ -z "$start_text" ]]; then
    return 0
  fi
  date -d "$start_text" +%s 2>/dev/null || true
}

file_mtime_epoch() {
  local path="$1"
  stat -c %Y "$path"
}

_run_hermes_gateway() {
  (
    cd "$REPO"
    env \
      HOME="$TARGET_HOME" \
      USER="$TARGET_USER" \
      LOGNAME="$TARGET_LOGNAME" \
      HERMES_HOME="$TARGET_HERMES_HOME" \
      PYTHONPATH="$PYTHONPATH_VALUE" \
      "$PYTHON_BIN" -m hermes_cli.main "$@"
  )
}

_build_hermes_gateway_shell_command() {
  local shell_cmd
  printf -v shell_cmd 'cd %q && env HOME=%q USER=%q LOGNAME=%q HERMES_HOME=%q PYTHONPATH=%q %q -m hermes_cli.main' \
    "$REPO" "$TARGET_HOME" "$TARGET_USER" "$TARGET_LOGNAME" "$TARGET_HERMES_HOME" "$PYTHONPATH_VALUE" "$PYTHON_BIN"
  local arg
  for arg in "$@"; do
    local quoted
    printf -v quoted ' %q' "$arg"
    shell_cmd+="$quoted"
  done
  printf '%s\n' "$shell_cmd"
}

print_hermes_gateway_command() {
  local shell_cmd
  shell_cmd="$(_build_hermes_gateway_shell_command "$@")"
  echo "+ $shell_cmd"
}

run_hermes_gateway() {
  if [[ "$DRY_RUN" == "1" ]]; then
    print_hermes_gateway_command "$@"
    return 0
  fi
  _run_hermes_gateway "$@"
}

restart_gateway_unit() {
  if [[ "$DRY_RUN" == "1" ]]; then
    print_hermes_gateway_command gateway restart --system
    return 0
  fi
  if [[ "$(id -u)" == "0" ]]; then
    _run_hermes_gateway gateway restart --system
    return 0
  fi
  if [[ -x "$ROOT_BRIDGE" ]]; then
    local shell_cmd
    shell_cmd="$(_build_hermes_gateway_shell_command gateway restart --system)"
    "$ROOT_BRIDGE" -d "$WSL_DISTRO" -u root -- /bin/bash -lc "$shell_cmd"
    return 0
  fi
  _run_hermes_gateway gateway restart --system
}

print_status_block() {
  echo "-- systemctl show --"
  systemctl show "$UNIT" --property=ActiveState,SubState,ExecMainPID,FragmentPath,Slice,Delegate || true
  echo
  echo "-- systemctl status $UNIT_NAME --"
  systemctl status "$UNIT" --no-pager --lines=12 || true
  echo
}

verify_code_freshness() {
  local pid="$1"
  local start_epoch
  start_epoch="$(current_start_epoch "$pid")"
  local run_mtime
  local worker_mtime
  run_mtime="$(file_mtime_epoch "$REPO/gateway/run.py")"
  worker_mtime="$(file_mtime_epoch "$REPO/gateway/worker_runtime.py")"
  echo "gateway/run.py mtime=$run_mtime"
  echo "gateway/worker_runtime.py mtime=$worker_mtime"
  echo "gateway pid start_epoch=${start_epoch:-unknown}"
  if [[ -z "$pid" || "$pid" == "0" ]]; then
    echo "freshness_check=fail missing gateway pid after restart"
    return 31
  fi
  if [[ -z "$start_epoch" ]]; then
    echo "freshness_check=fail unable to resolve gateway pid start time"
    return 32
  fi
  if (( start_epoch >= run_mtime && start_epoch >= worker_mtime )); then
    echo "freshness_check=pass"
    return 0
  fi
  echo "freshness_check=fail process start predates at least one gateway file mtime"
  return 33
}

verify_restart_effect() {
  local pre_pid="$1"
  local post_pid="$2"
  if ! systemctl is-active --quiet "$UNIT"; then
    echo "restart_state=fail service not active after restart"
    return 41
  fi
  if [[ -z "$post_pid" || "$post_pid" == "0" ]]; then
    echo "restart_state=fail missing post_pid after restart"
    return 42
  fi
  if [[ -n "$pre_pid" && "$pre_pid" == "$post_pid" ]]; then
    echo "restart_state=fail post_pid matches pre_pid"
    return 43
  fi
  echo "restart_state=pass"
  return 0
}

verify_service_definition_and_profile() {
  REPO="$REPO" \
  UNIT="$UNIT" \
  EXPECTED_MEMORY_HIGH_BYTES="$EXPECTED_MEMORY_HIGH_BYTES" \
  EXPECTED_MEMORY_MAX_BYTES="$EXPECTED_MEMORY_MAX_BYTES" \
  EXPECTED_TASKS_MAX="$EXPECTED_TASKS_MAX" \
  EXPECTED_CPU_WEIGHT="$EXPECTED_CPU_WEIGHT" \
  env \
    HOME="$TARGET_HOME" \
    USER="$TARGET_USER" \
    LOGNAME="$TARGET_LOGNAME" \
    HERMES_HOME="$TARGET_HERMES_HOME" \
    PYTHONPATH="$PYTHONPATH_VALUE" \
    "$PYTHON_BIN" - <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

repo = Path(os.environ["REPO"])
unit = os.environ["UNIT"]
sys.path.insert(0, str(repo))

import hermes_cli.gateway as gateway_cli

report = gateway_cli.get_gateway_systemd_report(requested_scope="system")
unit_definition_current = None
unit_path = report.get("unit_path")
if report.get("installed") and report.get("system") and unit_path:
    unit_definition_current = gateway_cli.systemd_unit_path_is_current(Path(unit_path), system=True)

show_cmd = ["systemctl", "show", unit]
for prop in ("Slice", "Delegate", "TasksMax", "OOMPolicy", "CPUWeight", "MemoryHigh", "MemoryMax"):
    show_cmd.extend(["--property", prop])
result = subprocess.run(show_cmd, capture_output=True, text=True, check=False)
props = {}
for line in (result.stdout or "").splitlines():
    if "=" not in line:
        continue
    key, value = line.split("=", 1)
    props[key] = value.strip()

expected = {
    "Slice": {"system.slice"},
    "Delegate": {"no"},
    "TasksMax": {os.environ["EXPECTED_TASKS_MAX"]},
    "OOMPolicy": {"stop"},
    "CPUWeight": {os.environ["EXPECTED_CPU_WEIGHT"]},
    "MemoryHigh": {"384M", os.environ["EXPECTED_MEMORY_HIGH_BYTES"]},
    "MemoryMax": {"768M", os.environ["EXPECTED_MEMORY_MAX_BYTES"]},
}
property_pass = {key: str(props.get(key, "")) in allowed for key, allowed in expected.items()}
payload = {
    "unit_name": report.get("unit_name"),
    "unit_path": unit_path,
    "unit_drifted": bool(report.get("drifted")) if report else False,
    "unit_definition_current": unit_definition_current,
    "properties": props,
    "property_pass": property_pass,
}
print(json.dumps(payload, ensure_ascii=False))

ok = (
    report.get("installed")
    and report.get("system")
    and not bool(report.get("drifted"))
    and unit_definition_current is True
    and all(property_pass.values())
)
raise SystemExit(0 if ok else 12)
PY
}

VERIFY_RC=0
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') detached gateway restart verify start ==="
  echo "dry_run=$DRY_RUN"
  export HOME="$TARGET_HOME"
  export USER="$TARGET_USER"
  export LOGNAME="$TARGET_LOGNAME"
  export HERMES_HOME="$TARGET_HERMES_HOME"

  pre_pid="$(current_pid)"
  echo "pre_pid=${pre_pid:-0}"
  echo "pre_start=$(current_start_text "$pre_pid")"
  print_status_block

  echo "-- restart canonical --"
  restart_gateway_unit
  if [[ "$DRY_RUN" != "1" ]]; then
    echo "restart issued"
    for _ in $(seq 1 60); do
      post_pid="$(current_pid)"
      if systemctl is-active --quiet "$UNIT" && [[ -n "$post_pid" && "$post_pid" != "0" ]]; then
        break
      fi
      sleep 1
    done
  fi
  echo

  post_pid="$(current_pid)"
  echo "post_pid=${post_pid:-0}"
  echo "post_start=$(current_start_text "$post_pid")"
  echo

  print_status_block

  echo "-- restart effect check --"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "restart_state_check=skipped dry-run"
  elif verify_restart_effect "$pre_pid" "$post_pid"; then
    echo "restart_state_check=pass"
  else
    rc=$?
    if [[ "$VERIFY_RC" == "0" ]]; then
      VERIFY_RC=$rc
    fi
    echo "restart_state_check=fail rc=$rc"
  fi
  echo

  echo "-- freshness check --"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "freshness_check=skipped dry-run"
  elif verify_code_freshness "$post_pid"; then
    :
  else
    rc=$?
    if [[ "$VERIFY_RC" == "0" ]]; then
      VERIFY_RC=$rc
    fi
  fi
  echo

  echo "-- service definition/profile check --"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "service_profile_check=skipped dry-run"
  elif verify_service_definition_and_profile; then
    echo "service_profile_check=pass"
  else
    rc=$?
    if [[ "$VERIFY_RC" == "0" ]]; then
      VERIFY_RC=$rc
    fi
    echo "service_profile_check=fail rc=$rc"
  fi
  echo

  echo "-- hermes gateway status --system --"
  run_hermes_gateway gateway status --system || true
  echo
  echo "-- hermes gateway status --user --"
  run_hermes_gateway gateway status --user || true
  echo
  echo "=== detached gateway restart verify done ==="
} >>"$LOG" 2>&1

exit "$VERIFY_RC"
