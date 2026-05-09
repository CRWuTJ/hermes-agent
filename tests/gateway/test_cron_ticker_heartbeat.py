"""Regression tests for cron ticker runtime heartbeats."""

import time


def test_long_cron_tick_refreshes_running_heartbeat(monkeypatch):
    """A legitimate long cron tick must not look stale to the health guard."""
    from gateway.run import _run_cron_tick_with_heartbeat

    monkeypatch.setenv("HERMES_CRON_TICK_HEARTBEAT_MAX_SECONDS", "5")
    phases: list[str] = []

    def fake_tick(**_kwargs):
        time.sleep(0.08)
        return 3

    def fake_heartbeat(_name, *, phase, **_kwargs):
        phases.append(phase)

    result = _run_cron_tick_with_heartbeat(
        fake_tick,
        adapters=None,
        loop=None,
        interval=60,
        heartbeat_writer=fake_heartbeat,
        heartbeat_period_seconds=0.01,
    )

    assert result == 3
    assert "tick_running" in phases
