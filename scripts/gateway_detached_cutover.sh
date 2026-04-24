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
LOG="$TARGET_HERMES_HOME/logs/gateway-cutover.log"
UNIT=hermes-gateway.service
UNIT_NAME=${UNIT%.service}
DRY_RUN=${HERMES_GATEWAY_RESTART_DRY_RUN:-0}
PYTHON_BIN=${HERMES_GATEWAY_PYTHON_BIN:-$REPO/venv/bin/python}
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Unable to locate python interpreter for gateway cutover" >&2
  exit 2
fi
PYTHONPATH_VALUE="$REPO${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$(dirname "$LOG")"

run_cmd() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ $*"
    return 0
  fi
  "$@"
}

run_cmd_allow_fail() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ $* || true"
    return 0
  fi
  "$@" || true
}

run_hermes_gateway() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "+ cd $REPO && env HOME=$TARGET_HOME USER=$TARGET_USER LOGNAME=$TARGET_LOGNAME HERMES_HOME=$TARGET_HERMES_HOME PYTHONPATH=$PYTHONPATH_VALUE $PYTHON_BIN -m hermes_cli.main $*"
    return 0
  fi
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

list_gateway_units() {
  systemctl list-units --all 'hermes-gateway*' --no-pager || true
  systemctl list-unit-files 'hermes-gateway*' --no-pager || true
}

collect_legacy_units() {
  systemctl list-unit-files 'hermes-gateway-*.service' --no-legend --no-pager 2>/dev/null \
    | awk '{print $1}' \
    | grep '^hermes-gateway-.*\.service$' \
    | grep -v '^hermes-gateway.service$' || true
}

verify_canonical_status() {
  systemctl status "$UNIT" --no-pager --lines=12 || true
  run_hermes_gateway gateway status --system || true
}

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') detached gateway cutover start ==="
  echo "dry_run=$DRY_RUN"
  export HOME="$TARGET_HOME"
  export USER="$TARGET_USER"
  export LOGNAME="$TARGET_LOGNAME"
  export HERMES_HOME="$TARGET_HERMES_HOME"

  echo "-- pre units --"
  list_gateway_units
  echo

  mapfile -t LEGACY_UNITS < <(collect_legacy_units)
  if (( ${#LEGACY_UNITS[@]} == 0 )); then
    echo "-- legacy units --"
    echo "no legacy hermes-gateway-*.service units detected"
  else
    echo "-- legacy units --"
    printf 'detected legacy units: %s\n' "${LEGACY_UNITS[*]}"
    for legacy_unit in "${LEGACY_UNITS[@]}"; do
      legacy_name=${legacy_unit%.service}
      echo "stopping legacy unit: $legacy_name"
      run_cmd_allow_fail systemctl stop "$legacy_name"
      legacy_path="/etc/systemd/system/$legacy_unit"
      if [[ -f "$legacy_path" ]]; then
        echo "removing legacy unit file: $legacy_path"
        run_cmd rm -f "$legacy_path"
      else
        echo "legacy unit file already absent: $legacy_path"
      fi
    done
    echo "reloading systemd after legacy cleanup"
    run_cmd systemctl daemon-reload
  fi
  echo

  echo "-- ensure canonical service is running --"
  if systemctl is-active --quiet "$UNIT"; then
    echo "canonical service already active"
  else
    echo "starting canonical service"
    run_cmd systemctl start "$UNIT_NAME"
  fi
  echo

  echo "-- verify canonical status --"
  verify_canonical_status
  echo

  echo "-- post units --"
  list_gateway_units
  echo "=== detached gateway cutover done ==="
} >>"$LOG" 2>&1
