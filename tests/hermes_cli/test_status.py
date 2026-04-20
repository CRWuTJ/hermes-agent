from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import hermes_cli.gateway as gateway_cli
from cron.jobs import create_job, trigger_job
from hermes_cli.status import show_status


def test_show_status_includes_tavily_key(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-1...cdef")

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Tavily" in output
    assert "tvly...cdef" in output


def test_show_status_surfaces_gateway_repair_hints_for_drifted_outdated_unit(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    unit_path = tmp_path / "hermes-gateway-17b8e69b.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda: {
            "installed": True,
            "active": True,
            "state": "running",
            "scope": "system",
            "system": True,
            "unit_name": "hermes-gateway-17b8e69b",
            "unit_path": str(unit_path),
            "drifted": True,
        },
    )
    monkeypatch.setattr(gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: False)

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Unit:         hermes-gateway-17b8e69b (legacy/non-canonical)" in output
    assert "Drift:        yes" in output
    assert "Preview:      hermes gateway repair --system" in output
    assert "Apply:        sudo hermes gateway repair --system --apply --cleanup-legacy" in output
    assert "Definition:   outdated" in output
    assert "Refresh:      sudo hermes gateway restart --system" in output


def test_show_status_skips_gateway_repair_hints_when_unit_is_canonical_and_current(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    unit_path = tmp_path / "hermes-gateway.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda: {
            "installed": True,
            "active": True,
            "state": "running",
            "scope": "system",
            "system": True,
            "unit_name": "hermes-gateway",
            "unit_path": str(unit_path),
            "drifted": False,
        },
    )
    monkeypatch.setattr(gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: True)

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Unit:         hermes-gateway" in output
    assert "Preview:" not in output
    assert "Apply:" not in output
    assert "Definition:" not in output
    assert "Refresh:" not in output


def test_show_status_reports_scheduled_job_lane_totals_and_due_counts(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda: {"installed": False, "active": False, "state": "stopped", "scope": "user"},
    )

    interactive = create_job(prompt="Notify user", schedule="every 1h", deliver="origin")
    create_job(prompt="Scout backlog", schedule="every 1h")
    housekeeping = create_job(prompt="Flush stale state", schedule="every 1h", lane="housekeeping")
    trigger_job(interactive["id"])
    trigger_job(housekeeping["id"])

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Jobs:         3 active, 3 total" in output
    assert "Lanes:        interactive=1 | cron/scout=1 | housekeeping=1" in output
    assert "Due now:      interactive=1 | cron/scout=0 | housekeeping=1" in output


def test_show_status_reports_live_task_lane_totals_from_runtime_status(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda: {"installed": False, "active": True, "state": "running", "scope": "user"},
    )
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "live_tasks": {
                "active_count": 3,
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 2,
                    "housekeeping": 0,
                },
                "tasks": [],
            },
        },
    )

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Active lanes: interactive=1 | cron/scout=2 | housekeeping=0" in output


def test_show_status_reads_persisted_queued_backlog_from_runtime_status_file(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda: {"installed": False, "active": True, "state": "running", "scope": "user"},
    )

    from gateway.status import write_runtime_status

    monkeypatch.setattr(
        "gateway.task_control._queued_task_now",
        lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
    )

    write_runtime_status(
        gateway_state="running",
        queued_tasks={
            "queued_count": 2,
            "lane_counts": {
                "interactive": 1,
                "cron_scout": 1,
                "housekeeping": 0,
            },
            "bucket_counts": {
                "now": 1,
                "next": 0,
                "later": 1,
            },
            "tasks": [
                {
                    "task_id": "task-now",
                    "lane": "interactive",
                    "priority": 10,
                    "priority_bucket": "now",
                    "queued_at": "2026-04-19T12:00:00+00:00",
                    "actions": ["foreground", "reprioritize", "cancel"],
                }
            ],
        },
    )

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Queued:       2 total" in output
    assert "Queue lanes:  interactive=1 | cron/scout=1 | housekeeping=0" in output
    assert "Buckets:      now=1 | next=0 | later=1" in output
    assert "Next queued:  task-now · interactive · now (10) · waiting 5m since 2026-04-19T12:00:00+00:00" in output


def test_show_status_uses_shared_gateway_status_payload(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda: {"installed": False, "active": True, "state": "running", "scope": "user"},
    )

    with patch(
        "gateway.status.build_gateway_status_payload",
        return_value={
            "gateway_state": "running",
            "exit_reason": None,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "platforms": {},
            "queued_tasks": {
                "queued_count": 3,
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 1,
                    "housekeeping": 1,
                },
                "bucket_counts": {
                    "now": 1,
                    "next": 1,
                    "later": 1,
                },
                "tasks": [],
            },
            "live_tasks": {
                "active_count": 2,
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 1,
                    "housekeeping": 0,
                },
                "tasks": [],
            },
            "cron": {
                "active_jobs": 3,
                "total_jobs": 4,
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 1,
                    "housekeeping": 1,
                },
                "due_now": {
                    "interactive": 1,
                    "cron_scout": 0,
                    "housekeeping": 1,
                },
            },
        },
    ):
        show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Queued:       3 total" in output
    assert "Queue lanes:  interactive=1 | cron/scout=1 | housekeeping=1" in output
    assert "Buckets:      now=1 | next=1 | later=1" in output
    assert "Active lanes: interactive=1 | cron/scout=1 | housekeeping=0" in output
    assert "Jobs:         3 active, 4 total" in output
    assert "Lanes:        interactive=1 | cron/scout=1 | housekeeping=1" in output
    assert "Due now:      interactive=1 | cron/scout=0 | housekeeping=1" in output


def test_show_status_uses_shared_status_activity_block_renderer(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        gateway_cli,
        "get_gateway_systemd_report",
        lambda: {"installed": False, "active": True, "state": "running", "scope": "user"},
    )
    with patch(
        "gateway.status.render_status_activity_block",
        return_value="  Active lanes: interactive=7 | cron/scout=0 | housekeeping=0\n  Jobs:         42 active, 42 total",
    ):
        show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Active lanes: interactive=7 | cron/scout=0 | housekeeping=0" in output
    assert "Jobs:         42 active, 42 total" in output
