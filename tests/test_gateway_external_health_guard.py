import importlib.util
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gateway_external_health_guard.py"


def load_guard_module(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    module_name = f"gateway_external_health_guard_test_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(module, "STATE_PATH", hermes_home / "gateway_state.json")
    monkeypatch.setattr(module, "LOG_DIR", hermes_home / "logs")
    monkeypatch.setattr(module, "LOG_PATH", hermes_home / "logs" / "health.log")
    monkeypatch.setattr(module, "LOCK_PATH", hermes_home / "logs" / ".health.lock")
    monkeypatch.setattr(module, "LAST_RECOVERY_PATH", hermes_home / "logs" / ".health-last-recovery.json")
    return module, hermes_home


def _state(now, *, updated_delta=timedelta(seconds=10), heartbeat_delta=timedelta(seconds=10), live_active=0, queued=0, started_delta=timedelta(seconds=0)):
    started_at = (now - started_delta).isoformat()
    tasks = []
    oldest = None
    if live_active:
        oldest = {
            "task_id": "agent:main:telegram:dm:6141351975",
            "lane": "interactive",
            "started_at": started_at,
        }
        tasks = [oldest]
    return {
        "gateway_state": "running",
        "updated_at": (now - updated_delta).isoformat(),
        "live_tasks": {
            "active_count": live_active,
            "lane_counts": {"interactive": live_active, "cron_scout": 0, "housekeeping": 0},
            "oldest_running": oldest,
            "tasks": tasks,
        },
        "queued_tasks": {
            "queued_count": queued,
            "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0},
        },
        "heartbeats": {
            "cron_ticker": {
                "updated_at": (now - heartbeat_delta).isoformat(),
                "stale_after_seconds": 180,
            }
        },
    }


def test_decision_is_healthy_when_service_and_heartbeat_are_fresh(tmp_path, monkeypatch):
    module, _ = load_guard_module(tmp_path, monkeypatch)
    now = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

    decision = module.evaluate_health(
        {"ActiveState": "active", "SubState": "running", "ExecMainPID": "123"},
        _state(now),
        now=now,
    )

    assert decision.status == "healthy"
    assert decision.recover is False
    assert decision.reason == "ok"


def test_decision_recovers_when_cron_heartbeat_is_stale_and_gateway_is_quiet(tmp_path, monkeypatch):
    module, _ = load_guard_module(tmp_path, monkeypatch)
    now = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

    decision = module.evaluate_health(
        {"ActiveState": "active", "SubState": "running", "ExecMainPID": "123"},
        _state(now, heartbeat_delta=timedelta(minutes=8), live_active=0, queued=0),
        now=now,
    )

    assert decision.status == "stale"
    assert decision.recover is True
    assert "cron_ticker heartbeat stale" in decision.reason


def test_decision_waits_when_interactive_work_is_active_but_not_old(tmp_path, monkeypatch):
    module, _ = load_guard_module(tmp_path, monkeypatch)
    now = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

    decision = module.evaluate_health(
        {"ActiveState": "active", "SubState": "running", "ExecMainPID": "123"},
        _state(
            now,
            heartbeat_delta=timedelta(minutes=8),
            live_active=1,
            started_delta=timedelta(minutes=5),
        ),
        now=now,
        active_recovery_seconds=1800,
    )

    assert decision.status == "busy_stale"
    assert decision.recover is False
    assert "interactive task still within recovery window" in decision.reason


def test_decision_recovers_when_interactive_work_is_stale_beyond_recovery_window(tmp_path, monkeypatch):
    module, _ = load_guard_module(tmp_path, monkeypatch)
    now = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)

    decision = module.evaluate_health(
        {"ActiveState": "active", "SubState": "running", "ExecMainPID": "123"},
        _state(
            now,
            heartbeat_delta=timedelta(minutes=15),
            live_active=1,
            started_delta=timedelta(minutes=45),
        ),
        now=now,
        active_recovery_seconds=1800,
    )

    assert decision.status == "wedged"
    assert decision.recover is True
    assert "interactive task exceeded recovery window" in decision.reason


def test_main_runs_restart_verify_for_recoverable_decision(tmp_path, monkeypatch):
    module, _ = load_guard_module(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(module, "acquire_lock", lambda: 10)
    monkeypatch.setattr(module, "release_lock", lambda _fd: None)
    monkeypatch.setattr(module, "service_snapshot", lambda: {"ActiveState": "inactive", "SubState": "dead", "ExecMainPID": "0"})
    monkeypatch.setattr(module, "read_runtime_status", lambda: None)
    monkeypatch.setattr(module, "cooldown_allows_recovery", lambda _now: True)

    def fake_restart_verify(reason):
        calls.append(reason)
        return 0

    monkeypatch.setattr(module, "run_restart_verify", fake_restart_verify)

    assert module.main() == 0
    assert calls == ["gateway service not running: inactive/dead"]
    assert "decision status=down recover=True" in module.LOG_PATH.read_text(encoding="utf-8")


def test_main_respects_recovery_cooldown(tmp_path, monkeypatch):
    module, _ = load_guard_module(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(module, "acquire_lock", lambda: 10)
    monkeypatch.setattr(module, "release_lock", lambda _fd: None)
    monkeypatch.setattr(module, "service_snapshot", lambda: {"ActiveState": "inactive", "SubState": "dead", "ExecMainPID": "0"})
    monkeypatch.setattr(module, "read_runtime_status", lambda: None)
    monkeypatch.setattr(module, "cooldown_allows_recovery", lambda _now: False)
    monkeypatch.setattr(module, "run_restart_verify", lambda reason: calls.append(reason) or 0)

    assert module.main() == 0
    assert calls == []
    assert "recovery suppressed by cooldown" in module.LOG_PATH.read_text(encoding="utf-8")
