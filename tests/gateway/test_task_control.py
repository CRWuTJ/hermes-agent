from datetime import datetime, timezone

from gateway.task_control import (
    build_task_action_error_payload,
    build_task_action_payload,
    describe_task_action_error,
    describe_task_action_result,
    queued_task_priority_bucket,
    queued_task_wait_age,
    queued_task_wait_seconds,
    render_gateway_task_detail_block,
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
            "kind": "background",
            "actions": ["cancel"],
            "source": "gateway",
        },
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
