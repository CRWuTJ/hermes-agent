"""Tests for gateway runtime status tracking."""

import json
import os
from datetime import datetime, timezone

from gateway import status


class TestGatewayPidState:
    def test_write_pid_file_records_gateway_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_pid_file()

        payload = json.loads((tmp_path / "gateway.pid").read_text())
        assert payload["pid"] == os.getpid()
        assert payload["kind"] == "hermes-gateway"
        assert isinstance(payload["argv"], list)
        assert payload["argv"]

    def test_get_running_pid_rejects_live_non_gateway_pid(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text(str(os.getpid()))

        assert status.get_running_pid() is None
        assert not pid_path.exists()

    def test_get_running_pid_accepts_gateway_metadata_when_cmdline_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text(json.dumps({
            "pid": os.getpid(),
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway"],
            "start_time": 123,
        }))

        monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(status, "_read_process_cmdline", lambda pid: None)

        assert status.get_running_pid() == os.getpid()

    def test_get_running_pid_accepts_script_style_gateway_cmdline(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text(json.dumps({
            "pid": os.getpid(),
            "kind": "hermes-gateway",
            "argv": ["/venv/bin/python", "/repo/hermes_cli/main.py", "gateway", "run", "--replace"],
            "start_time": 123,
        }))

        monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(
            status,
            "_read_process_cmdline",
            lambda pid: "/venv/bin/python /repo/hermes_cli/main.py gateway run --replace",
        )

        assert status.get_running_pid() == os.getpid()


class TestGatewayRuntimeStatus:
    def test_write_runtime_status_overwrites_stale_pid_on_restart(self, tmp_path, monkeypatch):
        """Regression: setdefault() preserved stale PID from previous process (#1631)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        # Simulate a previous gateway run that left a state file with a stale PID
        state_path = tmp_path / "gateway_state.json"
        state_path.write_text(json.dumps({
            "pid": 99999,
            "start_time": 1000.0,
            "kind": "hermes-gateway",
            "platforms": {},
            "updated_at": "2025-01-01T00:00:00Z",
        }))

        status.write_runtime_status(gateway_state="running")

        payload = status.read_runtime_status()
        assert payload["pid"] == os.getpid(), "PID should be overwritten, not preserved via setdefault"
        assert payload["start_time"] != 1000.0, "start_time should be overwritten on restart"

    def test_write_runtime_status_records_platform_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_runtime_status(
            gateway_state="startup_failed",
            exit_reason="telegram conflict",
            platform="telegram",
            platform_state="fatal",
            error_code="telegram_polling_conflict",
            error_message="another poller is active",
        )

        payload = status.read_runtime_status()
        assert payload["gateway_state"] == "startup_failed"
        assert payload["exit_reason"] == "telegram conflict"
        assert payload["platforms"]["telegram"]["state"] == "fatal"
        assert payload["platforms"]["telegram"]["error_code"] == "telegram_polling_conflict"
        assert payload["platforms"]["telegram"]["error_message"] == "another poller is active"

    def test_write_runtime_status_explicit_none_clears_stale_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_runtime_status(
            gateway_state="startup_failed",
            exit_reason="telegram conflict",
            platform="telegram",
            platform_state="fatal",
            error_code="telegram_polling_conflict",
            error_message="another poller is active",
        )

        status.write_runtime_status(
            gateway_state="running",
            exit_reason=None,
            platform="telegram",
            platform_state="connected",
            error_code=None,
            error_message=None,
        )

        payload = status.read_runtime_status()
        assert payload["gateway_state"] == "running"
        assert payload["exit_reason"] is None
        assert payload["platforms"]["telegram"]["state"] == "connected"
        assert payload["platforms"]["telegram"]["error_code"] is None
        assert payload["platforms"]["telegram"]["error_message"] is None

    def test_write_runtime_status_records_live_task_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_runtime_status(
            gateway_state="running",
            live_tasks={
                "active_count": 2,
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 1,
                    "housekeeping": 0,
                },
                "tasks": [
                    {
                        "task_id": "bg-1",
                        "lane": "cron_scout",
                        "label": "background task",
                        "kind": "background",
                        "control_mode": "managed_runtime",
                        "actions": ["cancel"],
                        "source": "gateway",
                        "started_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            },
        )

        payload = status.read_runtime_status()
        assert payload["live_tasks"]["active_count"] == 2
        assert payload["live_tasks"]["lane_counts"] == {
            "interactive": 1,
            "cron_scout": 1,
            "housekeeping": 0,
        }
        assert payload["live_tasks"]["tasks"][0]["lane"] == "cron_scout"
        assert payload["live_tasks"]["tasks"][0]["kind"] == "background"
        assert payload["live_tasks"]["tasks"][0]["control_mode"] == "managed_runtime"
        assert payload["live_tasks"]["tasks"][0]["actions"] == ["cancel"]

    def test_write_runtime_status_records_queued_task_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_runtime_status(
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
                    "next": 1,
                    "later": 0,
                },
                "tasks": [
                    {
                        "task_id": "task-now",
                        "lane": "interactive",
                        "priority": 10,
                        "priority_bucket": "now",
                        "actions": ["foreground", "reprioritize", "cancel"],
                        "priority_bucket_options": ["now", "next", "later"],
                    },
                    {
                        "task_id": "task-next",
                        "lane": "cron_scout",
                        "priority": 50,
                        "priority_bucket": "next",
                        "actions": ["foreground", "reprioritize", "cancel"],
                        "priority_bucket_options": ["now", "next", "later"],
                    },
                ],
            },
        )

        payload = status.read_runtime_status()
        assert payload["queued_tasks"]["queued_count"] == 2
        assert payload["queued_tasks"]["lane_counts"] == {
            "interactive": 1,
            "cron_scout": 1,
            "housekeeping": 0,
        }
        assert payload["queued_tasks"]["bucket_counts"] == {
            "now": 1,
            "next": 1,
            "later": 0,
        }
        assert payload["queued_tasks"]["tasks"][0]["task_id"] == "task-now"
        assert payload["queued_tasks"]["tasks"][0]["priority_bucket_options"] == ["now", "next", "later"]

    def test_write_runtime_status_safe_persists_current_queued_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        from gateway import run as gateway_run
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent, MessageTaskEnvelope
        from gateway.session import SessionSource

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            user_name="tester",
            chat_type="dm",
        )
        queued_task = MessageTaskEnvelope(
            task_id="task-now",
            session_key="telegram:dm:c1:u1",
            message_event=MessageEvent(text="queued", source=source, message_id="m1"),
            priority=10,
            lane="interactive",
            reply_policy="status_only",
            cancellation_policy="preserve",
        )

        class _Adapter:
            def all_pending_tasks_snapshot(self):
                return [queued_task]

            def foreground_pending_task(self, *args, **kwargs):
                return None

            def reprioritize_pending_task(self, *args, **kwargs):
                return None

            def cancel_pending_task(self, *args, **kwargs):
                return None

        gateway_run._set_runtime_status_adapters({Platform.TELEGRAM: _Adapter()})
        try:
            gateway_run._write_runtime_status_safe(gateway_state="running")
        finally:
            gateway_run._set_runtime_status_adapters(None)

        payload = status.read_runtime_status()
        assert payload["queued_tasks"]["queued_count"] == 1
        assert payload["queued_tasks"]["bucket_counts"] == {
            "now": 1,
            "next": 0,
            "later": 0,
        }
        assert payload["queued_tasks"]["tasks"][0]["task_id"] == "task-now"
        assert payload["queued_tasks"]["tasks"][0]["actions"] == ["foreground", "reprioritize", "cancel"]

    def test_build_gateway_status_payload_normalizes_legacy_lane_keys(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc),
        )

        payload = status.build_gateway_status_payload(
            runtime_status={
                "gateway_state": "running",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "platforms": None,
                "live_tasks": {
                    "active_count": 3,
                    "lane_counts": {
                        "interactive": 1,
                        "cron/scout": 2,
                    },
                    "tasks": [
                        {
                            "task_id": "bg-1",
                            "lane": "background",
                            "label": "background task",
                            "source": "gateway",
                            "started_at": "2026-01-01T00:00:00+00:00",
                        },
                        {
                            "task_id": "turn-1",
                            "lane": "interactive",
                            "label": "message turn",
                            "source": "gateway",
                            "started_at": "2026-01-01T00:00:01+00:00",
                        }
                    ],
                },
            },
            cron_payload={
                "active_jobs": 2,
                "total_jobs": 3,
                "lane_counts": {
                    "cron/scout": 1,
                },
                "due_now": {
                    "background": 2,
                    "housekeeping": 1,
                },
            },
        )

        assert payload == {
            "gateway_state": "running",
            "exit_reason": None,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "platforms": {},
            "live_tasks": {
                "active_count": 3,
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 2,
                    "housekeeping": 0,
                },
                "tasks": [
                    {
                        "task_id": "bg-1",
                        "lane": "cron_scout",
                        "label": "background task",
                        "kind": "background",
                        "control_mode": "managed_runtime",
                        "actions": ["cancel"],
                        "source": "gateway",
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "running_seconds": 300,
                        "running_age": "5m",
                    },
                    {
                        "task_id": "turn-1",
                        "lane": "interactive",
                        "label": "message turn",
                        "kind": "live_turn",
                        "control_mode": "read_only",
                        "actions": [],
                        "source": "gateway",
                        "started_at": "2026-01-01T00:00:01+00:00",
                        "running_seconds": 299,
                        "running_age": "4m",
                    }
                ],
            },
            "cron": {
                "active_jobs": 2,
                "total_jobs": 3,
                "lane_counts": {
                    "interactive": 0,
                    "cron_scout": 1,
                    "housekeeping": 0,
                },
                "due_now": {
                    "interactive": 0,
                    "cron_scout": 2,
                    "housekeeping": 1,
                },
            },
        }

    def test_build_gateway_status_payload_derives_live_task_running_age(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc),
        )

        payload = status.build_gateway_status_payload(
            runtime_status={
                "gateway_state": "running",
                "updated_at": "2026-01-01T00:05:00+00:00",
                "platforms": {},
                "live_tasks": {
                    "active_count": 1,
                    "lane_counts": {
                        "interactive": 1,
                        "cron_scout": 0,
                        "housekeeping": 0,
                    },
                    "tasks": [
                        {
                            "task_id": "turn-1",
                            "lane": "interactive",
                            "label": "message turn",
                            "source": "gateway",
                            "started_at": "2026-01-01T00:00:00+00:00",
                        }
                    ],
                },
            },
            cron_payload=None,
        )

        assert payload["live_tasks"]["tasks"][0]["running_seconds"] == 300
        assert payload["live_tasks"]["tasks"][0]["running_age"] == "5m"

    def test_render_status_activity_lines_supports_chat_and_cli_styles(self):
        payload = {
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
        }

        assert status.render_status_activity_lines(payload, style="chat") == [
            "**Queued Backlog:** 3 total",
            "**Queued Lanes:** interactive=1 | cron/scout=1 | housekeeping=1",
            "**Queued Buckets:** now=1 | next=1 | later=1",
            "**Active Lanes:** interactive=1 | cron/scout=1 | housekeeping=0",
            "**Cron Jobs:** 3 active",
            "**Cron Lanes:** interactive=1 | cron/scout=1 | housekeeping=1",
            "**Cron Due Now:** interactive=1 | cron/scout=0 | housekeeping=1",
        ]
        assert status.render_status_activity_lines(payload, style="cli") == [
            "  Queued:       3 total",
            "  Queue lanes:  interactive=1 | cron/scout=1 | housekeeping=1",
            "  Buckets:      now=1 | next=1 | later=1",
            "  Active lanes: interactive=1 | cron/scout=1 | housekeeping=0",
            "  Jobs:         3 active, 4 total",
            "  Lanes:        interactive=1 | cron/scout=1 | housekeeping=1",
            "  Due now:      interactive=1 | cron/scout=0 | housekeeping=1",
        ]

    def test_render_status_activity_lines_include_next_and_starvation_summaries(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        payload = {
            "queued_tasks": {
                "queued_count": 2,
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 0,
                    "housekeeping": 1,
                },
                "bucket_counts": {
                    "now": 1,
                    "next": 0,
                    "later": 1,
                },
                "next_task": {
                    "task_id": "task-now",
                    "lane": "interactive",
                    "priority": 10,
                    "priority_bucket": "now",
                    "queued_at": "2026-04-19T12:00:00+00:00",
                },
                "tasks": [
                    {
                        "task_id": "task-now",
                        "lane": "interactive",
                        "priority": 10,
                        "priority_bucket": "now",
                        "queued_at": "2026-04-19T12:00:00+00:00",
                    },
                    {
                        "task_id": "task-later",
                        "lane": "housekeeping",
                        "priority": 80,
                        "priority_bucket": "later",
                        "queued_at": "2026-04-19T10:00:00+00:00",
                    },
                ],
            },
            "live_tasks": {
                "active_count": 0,
                "lane_counts": {
                    "interactive": 0,
                    "cron_scout": 0,
                    "housekeeping": 0,
                },
                "tasks": [],
            },
        }

        assert status.render_status_activity_lines(payload, style="chat") == [
            "**Queued Backlog:** 2 total",
            "**Queued Lanes:** interactive=1 | cron/scout=0 | housekeeping=1",
            "**Queued Buckets:** now=1 | next=0 | later=1",
            "**Next Queued:** `task-now · interactive · now (10) · waiting 5m since 2026-04-19T12:00:00+00:00`",
            "**Oldest Waiting:** `task-later · housekeeping · later (80) · waiting 2h 5m since 2026-04-19T10:00:00+00:00`",
            "**Starving Bucket:** `later · 1 queued · oldest wait 2h 5m · task-later`",
            "**Starvation Alert:** `warning · later bucket exceeded 2h threshold · waiting 2h 5m · /task task-later recover`",
        ]
        assert status.render_status_activity_lines(payload, style="cli") == [
            "  Queued:       2 total",
            "  Queue lanes:  interactive=1 | cron/scout=0 | housekeeping=1",
            "  Buckets:      now=1 | next=0 | later=1",
            "  Next queued:  task-now · interactive · now (10) · waiting 5m since 2026-04-19T12:00:00+00:00",
            "  Oldest wait:  task-later · housekeeping · later (80) · waiting 2h 5m since 2026-04-19T10:00:00+00:00",
            "  Starving:     later · 1 queued · oldest wait 2h 5m · task-later",
            "  Alert:        warning · later bucket exceeded 2h threshold · waiting 2h 5m · /task task-later recover",
        ]

    def test_render_status_activity_block_supports_chat_and_cli_styles(self):
        payload = {
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
            },
            "live_tasks": {
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 1,
                    "housekeeping": 0,
                },
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
        }

        assert status.render_status_activity_block(payload, style="chat") == (
            "**Queued Backlog:** 3 total\n"
            "**Queued Lanes:** interactive=1 | cron/scout=1 | housekeeping=1\n"
            "**Queued Buckets:** now=1 | next=1 | later=1\n"
            "**Active Lanes:** interactive=1 | cron/scout=1 | housekeeping=0\n"
            "**Cron Jobs:** 3 active\n"
            "**Cron Lanes:** interactive=1 | cron/scout=1 | housekeeping=1\n"
            "**Cron Due Now:** interactive=1 | cron/scout=0 | housekeeping=1"
        )
        assert status.render_status_activity_block(payload, style="cli") == (
            "  Queued:       3 total\n"
            "  Queue lanes:  interactive=1 | cron/scout=1 | housekeeping=1\n"
            "  Buckets:      now=1 | next=1 | later=1\n"
            "  Active lanes: interactive=1 | cron/scout=1 | housekeeping=0\n"
            "  Jobs:         3 active, 4 total\n"
            "  Lanes:        interactive=1 | cron/scout=1 | housekeeping=1\n"
            "  Due now:      interactive=1 | cron/scout=0 | housekeeping=1"
        )

    def test_render_gateway_chat_status_block_uses_shared_activity_block_and_normalizes_next_lane(self):
        payload = {
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
            },
            "live_tasks": {
                "lane_counts": {
                    "interactive": 1,
                    "cron_scout": 1,
                    "housekeeping": 0,
                },
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
        }

        block = status.render_gateway_chat_status_block(
            session_id="sess-1",
            created_at="2026-01-01 10:00",
            updated_at="2026-01-01 10:05",
            total_tokens=321,
            agent_running=True,
            queued_tasks=2,
            connected_platforms=["telegram", "local"],
            status_payload=payload,
            title="My titled session",
            next_task={
                "task_id": "task-123",
                "lane": "background",
                "reply_policy": "status_only",
            },
        )

        assert block == (
            "📊 **Hermes Gateway Status**\n\n"
            "**Session ID:** `sess-1`\n"
            "**Title:** My titled session\n"
            "**Created:** 2026-01-01 10:00\n"
            "**Last Activity:** 2026-01-01 10:05\n"
            "**Tokens:** 321\n"
            "**Agent Running:** Yes ⚡\n"
            "**Queued Tasks:** 2\n"
            "**Next Task:** `task-123`\n"
            "**Next Lane:** cron/scout\n"
            "**Reply Policy:** status_only\n"
            "**Queued Backlog:** 3 total\n"
            "**Queued Lanes:** interactive=1 | cron/scout=1 | housekeeping=1\n"
            "**Queued Buckets:** now=1 | next=1 | later=1\n"
            "**Active Lanes:** interactive=1 | cron/scout=1 | housekeeping=0\n"
            "**Cron Jobs:** 3 active\n"
            "**Cron Lanes:** interactive=1 | cron/scout=1 | housekeeping=1\n"
            "**Cron Due Now:** interactive=1 | cron/scout=0 | housekeeping=1\n\n"
            "**Connected Platforms:** telegram, local"
        )

    def test_build_gateway_status_payload_includes_normalized_queued_task_summary(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        payload = status.build_gateway_status_payload(
            runtime_status={
                "gateway_state": "running",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "platforms": {},
            },
            queued_tasks={
                "queued_count": 3,
                "lane_counts": {
                    "background": 2,
                    "interactive": 1,
                },
                "tasks": [
                    {
                        "task_id": "task-now",
                        "lane": "background",
                        "priority": 10,
                        "queued_at": "2026-04-19T12:00:00+00:00",
                        "actions": ["foreground", "reprioritize", "cancel"],
                    },
                    {
                        "task_id": "task-next",
                        "lane": "interactive",
                        "priority": 50,
                        "actions": ["foreground", "reprioritize", "cancel"],
                    },
                    {
                        "task_id": "task-later",
                        "lane": "housekeeping",
                        "priority": 80,
                        "queued_at": "2026-04-19T10:00:00+00:00",
                        "actions": ["foreground", "reprioritize", "cancel"],
                    },
                ],
            },
            cron_payload=None,
        )

        assert payload["queued_tasks"]["queued_count"] == 3
        assert payload["queued_tasks"]["lane_counts"] == {
            "interactive": 1,
            "cron_scout": 2,
            "housekeeping": 0,
        }
        assert payload["queued_tasks"]["bucket_counts"] == {
            "now": 1,
            "next": 1,
            "later": 1,
        }
        assert payload["queued_tasks"]["next_task"]["task_id"] == "task-now"
        assert payload["queued_tasks"]["next_task"]["priority_bucket"] == "now"
        assert payload["queued_tasks"]["next_task"]["wait_seconds"] == 300
        assert payload["queued_tasks"]["next_task"]["wait_age"] == "5m"
        assert payload["queued_tasks"]["oldest_waiting"]["task_id"] == "task-later"
        assert payload["queued_tasks"]["oldest_waiting"]["priority_bucket"] == "later"
        assert payload["queued_tasks"]["oldest_waiting"]["wait_seconds"] == 7500
        assert payload["queued_tasks"]["oldest_waiting"]["wait_age"] == "2h 5m"
        assert payload["queued_tasks"]["starving_bucket"] == {
            "bucket": "later",
            "queued_count": 1,
            "oldest_wait_seconds": 7500,
            "oldest_wait_age": "2h 5m",
            "oldest_task_id": "task-later",
        }
        assert payload["queued_tasks"]["starvation_alert"] == {
            "level": "warning",
            "reason_code": "bucket_wait_threshold_exceeded",
            "reason": "later bucket exceeded starvation threshold",
            "bucket": "later",
            "threshold_seconds": 7200,
            "threshold_age": "2h",
            "current_wait_seconds": 7500,
            "current_wait_age": "2h 5m",
            "oldest_task_id": "task-later",
            "suggested_action": "reprioritize",
            "suggested_bucket": "next",
            "suggested_command": "/task task-later recover",
        }
        assert payload["queued_tasks"]["tasks"][0]["priority_bucket"] == "now"
        assert payload["queued_tasks"]["tasks"][0]["wait_seconds"] == 300
        assert payload["queued_tasks"]["tasks"][0]["wait_age"] == "5m"


class TestScopedLocks:
    def test_acquire_scoped_lock_rejects_live_other_process(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = tmp_path / "locks" / "telegram-bot-token-2bb80d537b1da3e3.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            "pid": 99999,
            "start_time": 123,
            "kind": "hermes-gateway",
        }))

        monkeypatch.setattr(status.os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)

        acquired, existing = status.acquire_scoped_lock("telegram-bot-token", "secret", metadata={"platform": "telegram"})

        assert acquired is False
        assert existing["pid"] == 99999

    def test_acquire_scoped_lock_replaces_stale_record(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
        lock_path = tmp_path / "locks" / "telegram-bot-token-2bb80d537b1da3e3.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            "pid": 99999,
            "start_time": 123,
            "kind": "hermes-gateway",
        }))

        def fake_kill(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(status.os, "kill", fake_kill)

        acquired, existing = status.acquire_scoped_lock("telegram-bot-token", "secret", metadata={"platform": "telegram"})

        assert acquired is True
        payload = json.loads(lock_path.read_text())
        assert payload["pid"] == os.getpid()
        assert payload["metadata"]["platform"] == "telegram"

    def test_release_scoped_lock_only_removes_current_owner(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))

        acquired, _ = status.acquire_scoped_lock("telegram-bot-token", "secret", metadata={"platform": "telegram"})
        assert acquired is True
        lock_path = tmp_path / "locks" / "telegram-bot-token-2bb80d537b1da3e3.lock"
        assert lock_path.exists()

        status.release_scoped_lock("telegram-bot-token", "secret")
        assert not lock_path.exists()
