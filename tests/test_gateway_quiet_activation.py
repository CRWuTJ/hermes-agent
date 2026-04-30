import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gateway_quiet_activation.py"


def load_quiet_activation_module(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    module_name = f"gateway_quiet_activation_test_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    logs = hermes_home / "logs"
    monkeypatch.setattr(module, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(module, "STATE_PATH", hermes_home / "gateway_state.json")
    monkeypatch.setattr(module, "PLAN_PATH", hermes_home / ".live_activation_pending.json")
    monkeypatch.setattr(module, "LOG_DIR", logs)
    monkeypatch.setattr(module, "LOG_PATH", logs / "quiet.log")
    monkeypatch.setattr(module, "LOCK_PATH", logs / ".quiet.lock")
    monkeypatch.setattr(module, "SAMPLE_INTERVAL", 0.001)
    monkeypatch.setattr(module, "QUIET_SAMPLES_REQUIRED", 1)
    monkeypatch.setattr(module, "MAX_WAIT_SECONDS", 0.2)
    return module, hermes_home


def test_wait_for_quiet_treats_missing_gateway_state_as_busy(tmp_path, monkeypatch):
    module, _ = load_quiet_activation_module(tmp_path, monkeypatch)

    assert module.wait_for_quiet() is False
    assert "unsafe_or_missing_state" in module.LOG_PATH.read_text(encoding="utf-8")


def test_wait_for_quiet_treats_malformed_gateway_state_as_busy(tmp_path, monkeypatch):
    module, hermes_home = load_quiet_activation_module(tmp_path, monkeypatch)
    (hermes_home / "gateway_state.json").write_text("not json", encoding="utf-8")

    assert module.wait_for_quiet() is False
    log_text = module.LOG_PATH.read_text(encoding="utf-8")
    assert "json_read_error" in log_text
    assert "unsafe_or_missing_state" in log_text


@pytest.mark.parametrize(
    "payload, expected_log",
    [
        ({}, "missing_task_sections"),
        ({"live_tasks": {}, "queued_tasks": {}}, "missing_count_fields"),
        ({"live_tasks": {"active_count": 0}}, "missing_task_sections"),
        ({"queued_tasks": {"queued_count": 0}}, "missing_task_sections"),
        ({"live_tasks": {"active_count": 0}, "queued_tasks": {}}, "missing_count_fields"),
        ({"live_tasks": {}, "queued_tasks": {"queued_count": 0}}, "missing_count_fields"),
        ({"live_tasks": {"active_count": -1}, "queued_tasks": {"queued_count": 0}}, "invalid_counts"),
        ({"live_tasks": {"active_count": 0}, "queued_tasks": {"queued_count": -1}}, "invalid_counts"),
        ({"live_tasks": {"active_count": True}, "queued_tasks": {"queued_count": 0}}, "invalid_counts"),
        ({"live_tasks": {"active_count": 0}, "queued_tasks": {"queued_count": False}}, "invalid_counts"),
        ({"live_tasks": {"active_count": 0.0}, "queued_tasks": {"queued_count": 0}}, "invalid_counts"),
        ({"live_tasks": {"active_count": 0}, "queued_tasks": {"queued_count": "0"}}, "invalid_counts"),
    ],
)
def test_wait_for_quiet_treats_schema_incomplete_gateway_state_as_busy(
    tmp_path, monkeypatch, payload, expected_log
):
    module, hermes_home = load_quiet_activation_module(tmp_path, monkeypatch)
    (hermes_home / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")

    assert module.wait_for_quiet() is False
    log_text = module.LOG_PATH.read_text(encoding="utf-8")
    assert expected_log in log_text
    assert "unsafe_or_missing_state" in log_text


def test_wait_for_quiet_accepts_explicit_zero_active_and_queued_counts(tmp_path, monkeypatch):
    module, hermes_home = load_quiet_activation_module(tmp_path, monkeypatch)
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"live_tasks": {"active_count": 0}, "queued_tasks": {"queued_count": 0}}),
        encoding="utf-8",
    )

    assert module.wait_for_quiet() is True


def test_wait_for_quiet_samples_once_even_when_deadline_is_already_expired(tmp_path, monkeypatch):
    module, hermes_home = load_quiet_activation_module(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "MAX_WAIT_SECONDS", 0)
    (hermes_home / "gateway_state.json").write_text(
        json.dumps({"live_tasks": {"active_count": True}, "queued_tasks": {"queued_count": 0}}),
        encoding="utf-8",
    )

    assert module.wait_for_quiet() is False
    log_text = module.LOG_PATH.read_text(encoding="utf-8")
    assert "invalid_counts" in log_text
    assert "unsafe_or_missing_state" in log_text


def test_acquire_lock_removes_dead_owner_stale_lock(tmp_path, monkeypatch):
    module, _ = load_quiet_activation_module(tmp_path, monkeypatch)
    module.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    module.LOCK_PATH.write_text("pid=424242 started_at=old\n", encoding="utf-8")
    monkeypatch.setattr(module, "_pid_is_running", lambda _pid: False)

    fd = module.acquire_lock()
    try:
        assert isinstance(fd, int)
        lock_text = module.LOCK_PATH.read_text(encoding="utf-8")
        assert f"pid={module.os.getpid()}" in lock_text
        assert "stale_lock_removed" in module.LOG_PATH.read_text(encoding="utf-8")
    finally:
        module.release_lock(fd)


def test_acquire_lock_keeps_live_owner_lock(tmp_path, monkeypatch):
    module, _ = load_quiet_activation_module(tmp_path, monkeypatch)
    module.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    module.LOCK_PATH.write_text("pid=424242 started_at=now\n", encoding="utf-8")
    monkeypatch.setattr(module, "_pid_is_running", lambda _pid: True)

    assert module.acquire_lock() is None
    assert module.LOCK_PATH.exists()
    assert "lock_exists" in module.LOG_PATH.read_text(encoding="utf-8")


def test_main_rechecks_pending_plan_after_quiet_before_restart(tmp_path, monkeypatch):
    module, _ = load_quiet_activation_module(tmp_path, monkeypatch)
    pending_checks = iter([True, False])
    monkeypatch.setattr(module, "acquire_lock", lambda: 10)
    monkeypatch.setattr(module, "release_lock", lambda _fd: None)
    monkeypatch.setattr(module, "plan_is_pending", lambda: next(pending_checks))
    monkeypatch.setattr(module, "wait_for_quiet", lambda: True)
    restart_verify = SimpleNamespace(called=False)

    def fake_restart_verify():
        restart_verify.called = True
        return 0

    monkeypatch.setattr(module, "run_restart_verify", fake_restart_verify)

    assert module.main() == 0
    assert restart_verify.called is False


def test_restart_verify_timeout_returns_failure_code(tmp_path, monkeypatch):
    module, hermes_home = load_quiet_activation_module(tmp_path, monkeypatch)
    verify_script = hermes_home / "verify.sh"
    verify_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(module, "VERIFY_SCRIPT", verify_script)

    def raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="verify", timeout=180)

    monkeypatch.setattr(module.subprocess, "run", raise_timeout)

    assert module.run_restart_verify() == 124
    assert "restart_verify_timeout" in module.LOG_PATH.read_text(encoding="utf-8")
