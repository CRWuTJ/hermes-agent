from datetime import datetime, timezone

from gateway.task_control import (
    build_task_action_error_payload,
    build_task_action_payload,
    build_task_detail_payload,
    describe_task_action_error,
    describe_task_action_result,
    parse_task_command_args,
    queued_task_priority_bucket,
    queued_task_wait_age,
    queued_task_wait_seconds,
    render_gateway_task_detail_block,
    render_gateway_tasks_block,
    task_command_usage_text,
)


def test_queued_task_priority_bucket_maps_numeric_bands():
    assert queued_task_priority_bucket(10) == "now"
    assert queued_task_priority_bucket(20) == "now"
    assert queued_task_priority_bucket(50) == "next"
    assert queued_task_priority_bucket(80) == "later"


def test_queued_task_wait_helpers_compute_compact_age(monkeypatch):
    monkeypatch.setattr(
        "gateway.task_control._queued_task_now",
        lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
    )

    queued_at = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

    assert queued_task_wait_seconds(queued_at) == 300
    assert queued_task_wait_age(queued_at) == "5m"


def test_parse_task_command_args_normalizes_longform_reprioritize_bucket():
    assert parse_task_command_args("task-hi reprioritize later") == (
        "task-hi",
        "reprioritize",
        "later",
    )


def test_parse_task_command_args_preserves_explicit_reprioritize_without_bucket():
    assert parse_task_command_args("task-hi reprioritize") == (
        "task-hi",
        "reprioritize",
        None,
    )


def test_task_command_usage_text_reuses_shared_command_usage_line():
    from hermes_cli.commands import command_usage_line

    usage = task_command_usage_text()

    assert usage.startswith(command_usage_line("task") + "\n")
    assert "/task bg_123abc reprioritize later" in usage


def test_describe_task_action_result_formats_foreground_for_chat():
    assert (
        describe_task_action_result(
            task_id="task-hi",
            action="foreground",
            status="queued_next",
            markdown_task_id=True,
        )
        == "Foregrounded queued task `task-hi` — it will run next after the current task."
    )


def test_build_task_action_payload_normalizes_actions_and_uses_status_wording():
    payload = build_task_action_payload(
        task_id="bg-1",
        action="cancel",
        status="cancellation_requested",
        task={
            "task_id": "bg-1",
            "kind": "background",
            "actions": ["cancel", "cancel"],
            "source": "gateway",
        },
    )

    assert payload == {
        "task_id": "bg-1",
        "action": "cancel",
        "status": "cancellation_requested",
        "message": "Cancellation requested for active task bg-1.",
        "task": {
            "task_id": "bg-1",
            "lane": "interactive",
            "kind": "background",
            "control_mode": "managed_runtime",
            "actions": ["cancel"],
            "source": "gateway",
        },
    }


def test_build_task_detail_payload_normalizes_queued_lane_aliases_and_defaults(monkeypatch):
    monkeypatch.setattr(
        "gateway.task_control._queued_task_now",
        lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
    )

    payload = build_task_detail_payload(
        task_id="task-hi",
        state="queued",
        task={
            "task_id": "task-hi",
            "lane": "background",
            "priority": 10,
            "queued_at": "2026-04-19T12:00:00+00:00",
            "actions": ["foreground", "reprioritize", "cancel", "foreground"],
        },
    )

    assert payload["task"] == {
        "task_id": "task-hi",
        "state": "queued",
        "lane": "cron_scout",
        "priority": 10,
        "priority_bucket": "now",
        "queued_at": "2026-04-19T12:00:00+00:00",
        "wait_seconds": 300,
        "wait_age": "5m",
        "kind": "queued_message",
        "control_mode": "queued",
        "actions": ["foreground", "reprioritize", "cancel"],
        "priority_bucket_options": ["now", "next", "later"],
    }


def test_build_task_action_payload_normalizes_live_lane_aliases_and_runtime_defaults(monkeypatch):
    monkeypatch.setattr(
        "gateway.task_control._queued_task_now",
        lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
    )

    payload = build_task_action_payload(
        task_id="bg-1",
        action="cancel",
        status="cancellation_requested",
        task={
            "task_id": "bg-1",
            "lane": "background",
            "label": "background task",
            "source": "gateway",
            "started_at": "2026-04-19T12:00:00+00:00",
            "actions": ["cancel", "cancel"],
        },
    )

    assert payload["task"] == {
        "task_id": "bg-1",
        "lane": "cron_scout",
        "label": "background task",
        "source": "gateway",
        "started_at": "2026-04-19T12:00:00+00:00",
        "running_seconds": 300,
        "running_age": "5m",
        "kind": "background",
        "control_mode": "managed_runtime",
        "actions": ["cancel"],
    }


def test_describe_task_action_result_formats_reprioritize_for_chat():
    assert (
        describe_task_action_result(
            task_id="task-hi",
            action="reprioritize",
            status="reprioritized",
            target_bucket="later",
            markdown_task_id=True,
        )
        == "Moved queued task `task-hi` to later priority."
    )


def test_describe_task_action_result_formats_recover_for_chat():
    assert (
        describe_task_action_result(
            task_id="task-stale",
            action="recover",
            status="recovered",
            target_bucket="next",
            markdown_task_id=True,
        )
        == "Recovered queued task `task-stale` — moved it to next priority."
    )


def test_describe_task_action_error_formats_chat_not_found_with_hint():
    assert (
        describe_task_action_error(
            task_id="missing-task",
            action="cancel",
            reason_code="task_not_found",
            markdown_task_id=True,
            include_tasks_hint=True,
            surface="chat",
        )
        == "Task not found: `missing-task`\nUse /tasks to list queued and active tasks."
    )


def test_build_task_action_error_payload_adds_reason_code_and_http_wording():
    payload = build_task_action_error_payload(
        task_id="turn-1",
        action="foreground",
        reason_code="task_already_active",
    )

    assert payload == {
        "error": "Task is already active — foreground is only supported for queued tasks.",
        "reason_code": "task_already_active",
    }


def test_render_gateway_task_detail_block_includes_recover_shortcut_in_control_text():
    rendered = render_gateway_task_detail_block(
        {
            "task_id": "task-stale",
            "state": "queued",
            "task": {
                "task_id": "task-stale",
                "lane": "housekeeping",
                "priority": 80,
                "priority_bucket": "later",
                "priority_bucket_options": ["now", "next", "later"],
                "reply_policy": "status_only",
                "queued_at": "2026-04-19T10:00:00+00:00",
                "wait_seconds": 7500,
                "wait_age": "2h 5m",
                "kind": "queued_message",
                "control_mode": "queued",
                "actions": ["foreground", "reprioritize", "cancel"],
                "source": "telegram",
                "starvation_alert": {
                    "level": "warning",
                    "reason_code": "bucket_wait_threshold_exceeded",
                    "reason": "later bucket exceeded 2h threshold",
                    "bucket": "later",
                    "threshold_seconds": 7200,
                    "threshold_age": "2h",
                    "current_wait_seconds": 7500,
                    "current_wait_age": "2h 5m",
                    "oldest_task_id": "task-stale",
                    "suggested_action": "reprioritize",
                    "suggested_bucket": "next",
                    "suggested_command": "/task task-stale recover",
                },
            },
        }
    )

    assert "**Control:** queued for this chat — use /task task-stale recover; /task task-stale now|next|later to reprioritize" in rendered



def test_render_gateway_task_detail_block_includes_harness_control_summary():
    rendered = render_gateway_task_detail_block(
        {
            "task_id": "task-hi",
            "state": "queued",
            "task": {
                "task_id": "task-hi",
                "lane": "interactive",
                "priority": 50,
                "priority_bucket": "next",
                "kind": "queued_message",
                "control_mode": "queued",
                "actions": ["foreground", "reprioritize", "cancel"],
                "source": "telegram",
                "harness": {
                    "control": {
                        "latest_action": {
                            "action": "reprioritize",
                            "status": "reprioritized",
                            "surface": "chat",
                            "target_bucket": "later",
                            "session_key": "telegram:user:123",
                            "created_at": "2026-04-21T18:00:00+00:00",
                        },
                        "latest_recovery": {
                            "action": "recover",
                            "status": "recovered",
                            "surface": "api",
                            "target_bucket": "next",
                            "session_key": "telegram:user:123",
                            "created_at": "2026-04-21T17:55:00+00:00",
                        },
                    }
                },
            },
        }
    )

    assert "**Latest Control Action:** reprioritize · reprioritized · via chat · later · 2026-04-21T18:00:00+00:00" in rendered
    assert "**Latest Recovery:** recover · recovered · via api · next · 2026-04-21T17:55:00+00:00" in rendered



def test_render_gateway_tasks_block_includes_harness_control_summary():
    rendered = render_gateway_tasks_block(
        {
            "queued": {
                "queued_count": 1,
                "tasks": [
                    {
                        "task_id": "task-hi",
                        "lane": "interactive",
                        "priority": 50,
                        "priority_bucket": "next",
                        "reply_policy": "status_only",
                        "preview": "queued follow-up",
                        "actions": ["foreground", "reprioritize", "cancel"],
                        "harness": {
                            "control": {
                                "latest_action": {
                                    "action": "reprioritize",
                                    "status": "reprioritized",
                                    "surface": "chat",
                                    "target_bucket": "later",
                                    "created_at": "2026-04-21T18:00:00+00:00",
                                },
                                "latest_recovery": {
                                    "action": "recover",
                                    "status": "recovered",
                                    "surface": "api",
                                    "target_bucket": "next",
                                    "created_at": "2026-04-21T17:55:00+00:00",
                                },
                            }
                        },
                    }
                ],
            },
            "live": {"active_count": 0, "tasks": []},
        }
    )

    assert "↳ recent: reprioritize · reprioritized · via chat · later · 2026-04-21T18:00:00+00:00" in rendered
    assert "recover · recovered · via api · next · 2026-04-21T17:55:00+00:00" in rendered
