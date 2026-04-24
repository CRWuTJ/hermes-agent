#!/usr/bin/env python3
"""Shared helpers for detached transient-unit launches on local WSL/systemd hosts."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from typing import Dict, Optional


_WSL_EXE = "/mnt/c/Windows/System32/wsl.exe"
_DEFAULT_DISTRO = "Ubuntu"


def _current_runtime_identity() -> dict[str, str]:
    current_user = os.getenv("USER", Path.home().name or "wutj")
    current_group = os.getenv("LOGNAME", current_user) or current_user
    return {
        "user": current_user,
        "group": current_group,
        "path": os.getenv("PATH", ""),
        "virtual_env": os.getenv("VIRTUAL_ENV", ""),
        "distro": os.getenv("WSL_DISTRO_NAME", _DEFAULT_DISTRO) or _DEFAULT_DISTRO,
    }


def _ensure_launcher_available() -> dict[str, str]:
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        raise RuntimeError("systemd-run is unavailable; cannot offload detached work")
    if not os.path.exists(_WSL_EXE):
        raise RuntimeError("WSL root bridge is unavailable; cannot offload detached work")
    return _current_runtime_identity()


def transient_unit_launcher_available() -> bool:
    try:
        _ensure_launcher_available()
        return True
    except Exception:
        return False


def launch_transient_unit(
    *,
    unit_prefix: str,
    cwd: str,
    shell_script: str,
    extra_env: Optional[Dict[str, str]] = None,
    memory_high: str = "1G",
    memory_max: str = "2G",
    tasks_max: str = "256",
    cpu_weight: str = "100",
    service_type: str = "exec",
    extra_properties: Optional[list[str]] = None,
) -> str:
    runtime = _ensure_launcher_available()
    unit_name = f"{unit_prefix}-{uuid.uuid4().hex[:12]}"

    launch_cmd = [
        _WSL_EXE, "-d", runtime["distro"], "-u", "root", "--",
        "systemd-run",
        "--quiet",
        "--unit", unit_name,
        "--collect",
        f"--service-type={service_type}",
        f"--property=User={runtime['user']}",
        f"--property=Group={runtime['group']}",
        "--property=Slice=hermes-worker.slice",
        f"--property=MemoryHigh={memory_high}",
        f"--property=MemoryMax={memory_max}",
        f"--property=TasksMax={tasks_max}",
        f"--property=CPUWeight={cpu_weight}",
    ]
    for prop in (extra_properties or []):
        if not prop:
            continue
        launch_cmd.append(f"--property={prop}")
    launch_cmd.extend([
        f"--setenv=HOME=/home/{runtime['user']}",
        f"--setenv=USER={runtime['user']}",
        f"--setenv=LOGNAME={runtime['group']}",
        f"--setenv=PATH={runtime['path']}",
        "--setenv=PYTHONUNBUFFERED=1",
    ])
    if runtime["virtual_env"]:
        launch_cmd.append(f"--setenv=VIRTUAL_ENV={runtime['virtual_env']}")
    for key, value in (extra_env or {}).items():
        if value is None:
            continue
        launch_cmd.append(f"--setenv={key}={value}")
    launch_cmd.extend([
        f"--working-directory={cwd}",
        "--no-block",
        "/bin/bash", "-lc", shell_script,
    ])
    subprocess.run(launch_cmd, check=True, timeout=30)
    return unit_name


def build_piped_transient_unit_command(
    *,
    unit_prefix: str,
    cwd: str,
    argv: list[str],
    extra_env: Optional[Dict[str, str]] = None,
    memory_high: str = "1G",
    memory_max: str = "2G",
    tasks_max: str = "256",
    cpu_weight: str = "100",
    service_type: str = "exec",
) -> tuple[list[str], str]:
    runtime = _ensure_launcher_available()
    unit_name = f"{unit_prefix}-{uuid.uuid4().hex[:12]}"

    resolved_argv = list(argv)
    if resolved_argv:
        executable = str(resolved_argv[0]).strip()
        if executable and not os.path.isabs(executable):
            resolved = shutil.which(executable, path=runtime['path'])
            if resolved:
                resolved_argv[0] = resolved

    launch_cmd = [
        _WSL_EXE, "-d", runtime["distro"], "-u", "root", "--",
        "systemd-run",
        "--pipe",
        "--quiet",
        "--collect",
        "--unit", unit_name,
        f"--service-type={service_type}",
        f"--property=User={runtime['user']}",
        f"--property=Group={runtime['group']}",
        "--property=Slice=hermes-worker.slice",
        f"--property=MemoryHigh={memory_high}",
        f"--property=MemoryMax={memory_max}",
        f"--property=TasksMax={tasks_max}",
        f"--property=CPUWeight={cpu_weight}",
        f"--setenv=HOME=/home/{runtime['user']}",
        f"--setenv=USER={runtime['user']}",
        f"--setenv=LOGNAME={runtime['group']}",
        f"--setenv=PATH={runtime['path']}",
        "--setenv=PYTHONUNBUFFERED=1",
        f"--working-directory={cwd}",
    ]
    if runtime["virtual_env"]:
        launch_cmd.append(f"--setenv=VIRTUAL_ENV={runtime['virtual_env']}")
    for key, value in (extra_env or {}).items():
        if value is None:
            continue
        launch_cmd.append(f"--setenv={key}={value}")
    launch_cmd.extend(resolved_argv)
    return launch_cmd, unit_name


def popen_transient_unit(
    *,
    unit_prefix: str,
    cwd: str,
    argv: list[str],
    extra_env: Optional[Dict[str, str]] = None,
    memory_high: str = "1G",
    memory_max: str = "2G",
    tasks_max: str = "256",
    cpu_weight: str = "100",
    service_type: str = "exec",
) -> tuple[subprocess.Popen[str], str]:
    launch_cmd, unit_name = build_piped_transient_unit_command(
        unit_prefix=unit_prefix,
        cwd=cwd,
        argv=argv,
        extra_env=extra_env,
        memory_high=memory_high,
        memory_max=memory_max,
        tasks_max=tasks_max,
        cpu_weight=cpu_weight,
        service_type=service_type,
    )
    proc = subprocess.Popen(
        launch_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    return proc, unit_name


def systemctl_show_properties(unit_name: str, *properties: str) -> Dict[str, str]:
    runtime = _current_runtime_identity()
    cmd = [_WSL_EXE, "-d", runtime["distro"], "-u", "root", "--", "systemctl", "show", unit_name]
    for prop in properties:
        cmd.extend(["--property", prop])
    result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=10)
    data: Dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def wait_for_main_pid(unit_name: str, *, retries: int = 20, delay: float = 0.25) -> Optional[int]:
    for _ in range(max(retries, 1)):
        props = systemctl_show_properties(unit_name, "MainPID")
        value = (props.get("MainPID") or "").strip()
        if value.isdigit() and int(value) > 0:
            return int(value)
        time.sleep(delay)
    return None


def signal_unit(unit_name: str, signal_name: str) -> None:
    runtime = _current_runtime_identity()
    subprocess.run(
        [_WSL_EXE, "-d", runtime["distro"], "-u", "root", "--", "systemctl", "kill", f"--signal={signal_name}", unit_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
