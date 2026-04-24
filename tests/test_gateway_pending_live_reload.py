import importlib.util
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import gateway.status as gateway_status
import hermes_cli.gateway as gateway_cli
import scripts.gateway_pending_live_reload as pending_reload


def _write_gateway_runtime_files(repo_root: Path, *, mtime: int) -> None:
    gateway_dir = repo_root / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    for name in ("run.py", "worker_runtime.py"):
        path = gateway_dir / name
        path.write_text("# test\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))


def _stub_status_payload():
    return {
        "live_tasks": {"active_count": 0, "lane_counts": {}, "oldest_running": None},
        "queued_tasks": {"queued_count": 0, "lane_counts": {}},
    }


def _load_pending_reload_module():
    script_path = Path(pending_reload.__file__)
    module_name = f"pending_reload_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pending_reload_module_uses_current_env_for_profile_paths(monkeypatch, tmp_path):
    home = tmp_path / "home"
    hermes_home = tmp_path / "profile-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("LOGNAME", "alice-log")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    reloaded = _load_pending_reload_module()

    assert reloaded.REPO == Path(reloaded.__file__).resolve().parent.parent
    assert reloaded.LOG_PATH == hermes_home / "logs" / "gateway-pending-live-reload.log"
    assert reloaded.LOCK_PATH == hermes_home / "logs" / "gateway-pending-live-reload.lock"
    assert reloaded.ENV["HOME"] == str(home)
    assert reloaded.ENV["USER"] == "alice"
    assert reloaded.ENV["LOGNAME"] == "alice-log"
    assert reloaded.ENV["HERMES_HOME"] == str(hermes_home)


def test_build_probe_marks_service_definition_drift_as_stale(tmp_path, monkeypatch):
    now = int(time.time())
    _write_gateway_runtime_files(tmp_path, mtime=now - 5)
    unit_path = tmp_path / "hermes-gateway.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(pending_reload, "REPO", tmp_path)
    monkeypatch.setattr(pending_reload, "get_gateway_pid", lambda: 2755210)
    monkeypatch.setattr(pending_reload, "get_gateway_start_epoch", lambda pid: now)
    monkeypatch.setattr(gateway_status, "build_gateway_status_payload", _stub_status_payload)
    monkeypatch.setattr(gateway_status, "format_status_oldest_running_task", lambda oldest: "n/a")
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda requested_scope=None: {
            "installed": True,
            "system": True,
            "unit_name": "hermes-gateway",
            "unit_path": str(unit_path),
            "drifted": False,
        },
    )
    monkeypatch.setattr(gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: False)

    probe = pending_reload.build_probe()

    assert probe["code_stale"] is False
    assert probe["unit_definition_current"] is False
    assert probe["service_definition_stale"] is True
    assert probe["stale"] is True


def test_build_probe_stays_fresh_when_code_and_service_definition_are_current(tmp_path, monkeypatch):
    now = int(time.time())
    _write_gateway_runtime_files(tmp_path, mtime=now - 5)
    unit_path = tmp_path / "hermes-gateway.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(pending_reload, "REPO", tmp_path)
    monkeypatch.setattr(pending_reload, "get_gateway_pid", lambda: 2755210)
    monkeypatch.setattr(pending_reload, "get_gateway_start_epoch", lambda pid: now)
    monkeypatch.setattr(gateway_status, "build_gateway_status_payload", _stub_status_payload)
    monkeypatch.setattr(gateway_status, "format_status_oldest_running_task", lambda oldest: "n/a")
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda requested_scope=None: {
            "installed": True,
            "system": True,
            "unit_name": "hermes-gateway",
            "unit_path": str(unit_path),
            "drifted": False,
        },
    )
    monkeypatch.setattr(gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: True)

    probe = pending_reload.build_probe()

    assert probe["code_stale"] is False
    assert probe["unit_definition_current"] is True
    assert probe["service_definition_stale"] is False
    assert probe["stale"] is False


def test_build_probe_tolerates_missing_worker_runtime_before_worker_commit(tmp_path, monkeypatch):
    now = int(time.time())
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir(parents=True)
    run_path = gateway_dir / "run.py"
    run_path.write_text("# test\n", encoding="utf-8")
    os.utime(run_path, (now - 5, now - 5))
    unit_path = tmp_path / "hermes-gateway.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(pending_reload, "REPO", tmp_path)
    monkeypatch.setattr(pending_reload, "get_gateway_pid", lambda: 2755210)
    monkeypatch.setattr(pending_reload, "get_gateway_start_epoch", lambda pid: now)
    monkeypatch.setattr(gateway_status, "build_gateway_status_payload", _stub_status_payload)
    monkeypatch.setattr(gateway_status, "format_status_oldest_running_task", lambda oldest: "n/a")
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda requested_scope=None: {
            "installed": True,
            "system": True,
            "unit_name": "hermes-gateway",
            "unit_path": str(unit_path),
            "drifted": False,
        },
    )
    monkeypatch.setattr(gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: True)

    probe = pending_reload.build_probe()

    assert probe["run_mtime"] == now - 5
    assert probe["worker_mtime"] is None
    assert probe["code_stale"] is False
    assert probe["stale"] is False


def test_main_returns_nonzero_when_restart_verify_reports_success_but_gateway_stays_stale(monkeypatch):
    @contextmanager
    def fake_lock(path):
        yield

    probe_sequence = iter(
        [
            {
                "stale": True,
                "live_active": 1,
                "live_lanes": {"interactive": 1},
                "queued_count": 0,
                "queued_lanes": {},
                "oldest_running": "turn-1",
            },
            {
                "stale": True,
                "live_active": 0,
                "live_lanes": {},
                "queued_count": 0,
                "queued_lanes": {},
                "oldest_running": None,
            },
            {
                "stale": True,
                "live_active": 0,
                "live_lanes": {},
                "queued_count": 0,
                "queued_lanes": {},
                "oldest_running": None,
            },
        ]
    )
    logs = []

    monkeypatch.setattr(pending_reload, "singleton_lock", fake_lock)
    monkeypatch.setattr(pending_reload, "build_probe", lambda: next(probe_sequence))
    monkeypatch.setattr(pending_reload, "run_restart_verify", lambda: 0)
    monkeypatch.setattr(pending_reload, "log", logs.append)
    monkeypatch.setattr(pending_reload.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(pending_reload.time, "time", lambda: 0)
    monkeypatch.setattr(pending_reload, "MAX_WAIT_SECONDS", 60)

    rc = pending_reload.main()

    assert rc == 11
    assert any("restart verify reported success but gateway still stale after reload" in entry for entry in logs)
