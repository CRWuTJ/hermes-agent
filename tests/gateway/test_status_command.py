"""Tests for gateway /status behavior and token persistence."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageTaskEnvelope
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(),
        message_id="m1",
    )


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._topic_routing = {}
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_status_command_reports_running_agent_without_interrupt(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    running_agent = MagicMock()
    runner._running_agents[build_session_key(_make_source())] = running_agent

    result = await runner._handle_message(_make_event("/status"))

    assert "**Session ID:** `sess-1`" in result
    assert "**Tokens:** 321" in result
    assert "**Agent Running:** Yes ⚡" in result
    assert "**Title:**" not in result
    running_agent.interrupt.assert_not_called()
    assert runner._pending_messages == {}


@pytest.mark.asyncio
async def test_status_command_includes_session_title_when_present():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    runner._session_db.get_session_title.return_value = "My titled session"

    result = await runner._handle_message(_make_event("/status"))

    assert "**Session ID:** `sess-1`" in result
    assert "**Title:** My titled session" in result


@pytest.mark.asyncio
async def test_status_command_reports_pending_task_inbox_metadata():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_task_count.return_value = 2
    adapter.peek_pending_task.return_value = MessageTaskEnvelope(
        task_id="task-123",
        session_key=session_entry.session_key,
        message_event=_make_event("next"),
        priority=20,
        lane="background",
        reply_policy="status_only",
        cancellation_policy="preserve",
    )

    result = await runner._handle_message(_make_event("/status"))

    assert "**Queued Tasks:** 2" in result
    assert "**Next Task:** `task-123`" in result
    assert "**Next Lane:** cron/scout" in result
    assert "**Next Priority:** now (20)" in result
    assert "**Reply Policy:** status_only" in result


@pytest.mark.asyncio
async def test_status_command_reports_global_lane_activity():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)

    with patch(
        "gateway.run.task_lane_registry.snapshot",
        return_value={
            "counts": {"interactive": 1, "cron/scout": 2, "housekeeping": 1},
            "tasks": [],
        },
    ):
        result = await runner._handle_message(_make_event("/status"))

    assert "**Active Lanes:** interactive=1 | cron/scout=2 | housekeeping=1" in result


@pytest.mark.asyncio
async def test_status_command_reports_cron_lane_summary():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)

    active_jobs = [{"id": "job-1", "deliver": "origin", "enabled": True}]
    due_jobs = [{"id": "job-1", "deliver": "origin", "enabled": True}]

    with patch("cron.jobs.list_jobs", return_value=active_jobs), patch(
        "cron.jobs.get_due_jobs", return_value=due_jobs
    ), patch(
        "cron.jobs.summarize_job_lanes",
        return_value={
            "active": {"interactive": 1, "cron_scout": 0, "housekeeping": 0},
            "due": {"interactive": 1, "cron_scout": 0, "housekeeping": 0},
        },
    ):
        result = await runner._handle_message(_make_event("/status"))

    assert "**Cron Jobs:** 1 active" in result
    assert "**Cron Lanes:** interactive=1 | cron/scout=0 | housekeeping=0" in result
    assert "**Cron Due Now:** interactive=1 | cron/scout=0 | housekeeping=0" in result


@pytest.mark.asyncio
async def test_status_command_uses_shared_gateway_status_payload():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)

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
                "active_jobs": 4,
                "total_jobs": 5,
                "lane_counts": {
                    "interactive": 2,
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
        result = await runner._handle_message(_make_event("/status"))

    assert "**Queued Backlog:** 3 total" in result
    assert "**Queued Lanes:** interactive=1 | cron/scout=1 | housekeeping=1" in result
    assert "**Queued Buckets:** now=1 | next=1 | later=1" in result
    assert "**Active Lanes:** interactive=1 | cron/scout=1 | housekeeping=0" in result
    assert "**Cron Jobs:** 4 active" in result
    assert "**Cron Lanes:** interactive=2 | cron/scout=1 | housekeeping=1" in result
    assert "**Cron Due Now:** interactive=1 | cron/scout=0 | housekeeping=1" in result


@pytest.mark.asyncio
async def test_status_command_includes_global_queued_backlog_summary_when_available():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_task_count.return_value = 2
    adapter.peek_pending_task.return_value = MessageTaskEnvelope(
        task_id="task-123",
        session_key=session_entry.session_key,
        message_event=_make_event("next"),
        priority=20,
        lane="background",
        reply_policy="status_only",
        cancellation_policy="preserve",
    )
    adapter.all_pending_tasks_snapshot.return_value = [
        MessageTaskEnvelope(
            task_id="task-now",
            session_key=session_entry.session_key,
            message_event=_make_event("now"),
            priority=10,
            lane="interactive",
            reply_policy="user_visible",
            cancellation_policy="preserve",
            queued_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        ),
        MessageTaskEnvelope(
            task_id="task-later",
            session_key=session_entry.session_key,
            message_event=_make_event("later"),
            priority=80,
            lane="background",
            reply_policy="status_only",
            cancellation_policy="preserve",
        ),
    ]

    with patch("gateway.task_control._queued_task_now", return_value=datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc)):
        result = await runner._handle_message(_make_event("/status"))

    assert "**Queued Backlog:** 2 total" in result
    assert "**Queued Lanes:** interactive=1 | cron/scout=1 | housekeeping=0" in result
    assert "**Queued Buckets:** now=1 | next=0 | later=1" in result
    assert "**Next Queued:** `task-now · interactive · now (10) · waiting 5m since 2026-04-19T12:00:00+00:00`" in result


@pytest.mark.asyncio
async def test_status_command_uses_shared_status_activity_block_renderer():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)

    with patch(
        "gateway.status.render_status_activity_block",
        return_value="**Active Lanes:** interactive=9 | cron/scout=0 | housekeeping=0\n**Cron Jobs:** 99 active",
    ):
        result = await runner._handle_message(_make_event("/status"))

    assert "**Active Lanes:** interactive=9 | cron/scout=0 | housekeeping=0" in result
    assert "**Cron Jobs:** 99 active" in result


@pytest.mark.asyncio
async def test_status_command_uses_shared_chat_status_block_renderer():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)

    with patch(
        "gateway.status.render_gateway_chat_status_block",
        return_value="📊 **Hermes Gateway Status**\n**Session ID:** `sess-1`\n**Next Lane:** cron/scout",
    ):
        result = await runner._handle_message(_make_event("/status"))

    assert result == "📊 **Hermes Gateway Status**\n**Session ID:** `sess-1`\n**Next Lane:** cron/scout"


@pytest.mark.asyncio
async def test_handle_message_persists_agent_token_counts(monkeypatch):
    import gateway.run as gateway_run

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "earlier"}]
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 80,
            "input_tokens": 120,
            "output_tokens": 45,
            "model": "openai/test-model",
        }
    )

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello"))

    assert result == "ok"
    runner.session_store.update_session.assert_called_once_with(
        session_entry.session_key,
        last_prompt_tokens=80,
    )



@pytest.mark.asyncio
async def test_tasks_command_reports_session_queue_and_live_runtime_tasks():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    runner._managed_runtime_tasks = {
        "bg-1": {
            "task": MagicMock(done=MagicMock(return_value=False)),
            "cancel": MagicMock(),
            "kind": "background",
        }
    }
    adapter.pending_tasks_snapshot.return_value = [
        MessageTaskEnvelope(
            task_id="task-hi",
            session_key=session_entry.session_key,
            message_event=_make_event("high priority queued follow-up"),
            priority=10,
            lane="background",
            reply_policy="status_only",
            cancellation_policy="preserve",
        ),
        MessageTaskEnvelope(
            task_id="task-lo",
            session_key=session_entry.session_key,
            message_event=_make_event("low priority queued follow-up"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
            cancellation_policy="preserve",
        ),
    ]

    with patch(
        "gateway.run.task_lane_registry.status_snapshot",
        return_value={
            "active_count": 1,
            "lane_counts": {"interactive": 0, "cron_scout": 1, "housekeeping": 0},
            "tasks": [
                {
                    "task_id": "bg-1",
                    "lane": "cron_scout",
                    "label": "background task",
                    "source": "gateway",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        },
    ):
        result = await runner._handle_message(_make_event("/tasks"))

    assert "📋 **Hermes Tasks**" in result
    assert "**Queued for this chat:** 2" in result
    assert "`task-hi` · now · cron/scout · priority=10 · reply=status_only · high priority queued follow-up" in result
    assert "↳ /task task-hi · /task task-hi now|next|later · /task task-hi foreground · /task task-hi cancel" in result
    assert "`task-lo` · next · interactive · priority=50 · reply=user_visible · low priority queued follow-up" in result
    assert "↳ /task task-lo · /task task-lo now|next|later · /task task-lo foreground · /task task-lo cancel" in result
    assert result.index("task-hi") < result.index("task-lo")
    assert "**Active runtime tasks:** 1" in result
    assert "`bg-1` · cron/scout · background task · source=gateway" in result
    assert "↳ /task bg-1 · /task bg-1 cancel" in result


@pytest.mark.asyncio
async def test_tasks_command_marks_starving_task_with_one_hop_recovery_command():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = [
        MessageTaskEnvelope(
            task_id="task-now",
            session_key=session_entry.session_key,
            message_event=_make_event("high priority queued follow-up"),
            priority=10,
            lane="interactive",
            reply_policy="status_only",
            cancellation_policy="preserve",
            queued_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        ),
        MessageTaskEnvelope(
            task_id="task-stale",
            session_key=session_entry.session_key,
            message_event=_make_event("stale queued follow-up"),
            priority=80,
            lane="housekeeping",
            reply_policy="status_only",
            cancellation_policy="preserve",
            queued_at=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
        ),
    ]

    with patch("gateway.task_control._queued_task_now", return_value=datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc)), patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/tasks"))

    assert "`task-stale` · later · housekeeping · priority=80 · reply=status_only · stale queued follow-up" in result
    assert "⚠ /task task-stale recover" in result
    assert "later bucket exceeded 2h threshold" in result


@pytest.mark.asyncio
async def test_task_command_surfaces_starving_task_recovery_command():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = [
        MessageTaskEnvelope(
            task_id="task-now",
            session_key=session_entry.session_key,
            message_event=_make_event("high priority queued follow-up"),
            priority=10,
            lane="interactive",
            reply_policy="status_only",
            cancellation_policy="preserve",
            queued_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        ),
        MessageTaskEnvelope(
            task_id="task-stale",
            session_key=session_entry.session_key,
            message_event=_make_event("stale queued follow-up"),
            priority=80,
            lane="housekeeping",
            reply_policy="status_only",
            cancellation_policy="preserve",
            queued_at=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
        ),
    ]

    with patch("gateway.task_control._queued_task_now", return_value=datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc)), patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-stale"))

    assert "**Starvation Alert:** warning · later bucket exceeded 2h threshold · waiting 2h 5m · /task task-stale recover" in result
    assert "**Suggested Action:** /task task-stale recover" in result


@pytest.mark.asyncio
async def test_tasks_command_shows_detail_hint_for_unmanaged_active_runtime_task():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = []

    with patch(
        "gateway.task_control._queued_task_now",
        return_value=datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc),
    ), patch(
        "gateway.run.task_lane_registry.status_snapshot",
        return_value={
            "active_count": 1,
            "lane_counts": {"interactive": 1, "cron_scout": 0, "housekeeping": 0},
            "tasks": [
                {
                    "task_id": "task-live",
                    "lane": "interactive",
                    "label": "regular turn",
                    "source": "gateway",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        },
    ):
        result = await runner._handle_message(_make_event("/tasks"))

    assert "`task-live` · interactive · regular turn · source=gateway · running 5m" in result
    assert "↳ /task task-live" in result
    assert "/task task-live cancel" not in result


@pytest.mark.asyncio
async def test_tasks_command_uses_live_payload_actions_without_runtime_registry_entry():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = []
    runner._managed_runtime_tasks = {}

    with patch(
        "gateway.run.task_lane_registry.status_snapshot",
        return_value={
            "active_count": 1,
            "lane_counts": {"interactive": 0, "cron_scout": 1, "housekeeping": 0},
            "tasks": [
                {
                    "task_id": "bg-payload",
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
    ):
        result = await runner._handle_message(_make_event("/tasks"))

    assert "`bg-payload` · cron/scout · background task · source=gateway" in result
    assert "↳ /task bg-payload · /task bg-payload cancel" in result


@pytest.mark.asyncio
async def test_task_command_reports_queued_task_detail():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = [
        MessageTaskEnvelope(
            task_id="task-hi",
            session_key=session_entry.session_key,
            message_event=_make_event("high priority queued follow-up"),
            priority=10,
            lane="background",
            reply_policy="status_only",
            cancellation_policy="preserve",
            queued_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        )
    ]

    with patch("gateway.task_control._queued_task_now", return_value=datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc)), patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-hi"))

    assert "🧩 **Task Detail**" in result
    assert "**Task ID:** `task-hi`" in result
    assert "**State:** queued" in result
    assert "**Lane:** cron/scout" in result
    assert "**Priority:** 10" in result
    assert "**Priority Bucket:** now" in result
    assert "**Priority Bucket Options:** now, next, later" in result
    assert "**Queued At:** 2026-04-19T12:00:00+00:00" in result
    assert "**Waited:** 5m" in result
    assert "**Reply Policy:** status_only" in result
    assert "**Kind:** queued message" in result
    assert "**Source:** telegram" in result
    assert "**Preview:** high priority queued follow-up" in result
    assert "**Control:** queued for this chat" in result
    assert "/task task-hi now|next|later" in result
    assert "/task task-hi foreground" in result
    assert "/task task-hi cancel" in result
    assert "**Actions:** foreground, reprioritize, cancel" in result


@pytest.mark.asyncio
async def test_task_command_reports_managed_active_runtime_task_detail():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = []
    runtime_task = MagicMock()
    runtime_task.done.return_value = False
    runner._managed_runtime_tasks = {
        "bg-1": {
            "task": runtime_task,
            "cancel": MagicMock(),
            "kind": "background",
        }
    }

    with patch(
        "gateway.task_control._queued_task_now",
        return_value=datetime(2026, 1, 1, 0, 5, 0, tzinfo=timezone.utc),
    ), patch(
        "gateway.run.task_lane_registry.status_snapshot",
        return_value={
            "active_count": 1,
            "lane_counts": {"interactive": 0, "cron_scout": 1, "housekeeping": 0},
            "tasks": [
                {
                    "task_id": "bg-1",
                    "lane": "cron_scout",
                    "label": "background task",
                    "source": "gateway",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        },
    ):
        result = await runner._handle_message(_make_event("/task bg-1"))

    assert "🧩 **Task Detail**" in result
    assert "**Task ID:** `bg-1`" in result
    assert "**State:** active" in result
    assert "**Lane:** cron/scout" in result
    assert "**Label:** background task" in result
    assert "**Kind:** background" in result
    assert "**Source:** gateway" in result
    assert "**Started:** 2026-01-01T00:00:00+00:00" in result
    assert "**Running:** 5m" in result
    assert "**Control:** gateway-managed runtime task" in result
    assert "/task bg-1 cancel" in result
    assert "**Actions:** cancel" in result


@pytest.mark.asyncio
async def test_task_command_uses_live_task_control_payload_without_registry_entry():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = []
    runner._managed_runtime_tasks = {}

    with patch(
        "gateway.run.task_lane_registry.status_snapshot",
        return_value={
            "active_count": 1,
            "lane_counts": {"interactive": 0, "cron_scout": 1, "housekeeping": 0},
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
    ):
        result = await runner._handle_message(_make_event("/task bg-1"))

    assert "🧩 **Task Detail**" in result
    assert "**Task ID:** `bg-1`" in result
    assert "**State:** active" in result
    assert "**Lane:** cron/scout" in result
    assert "**Label:** background task" in result
    assert "**Kind:** background" in result
    assert "**Source:** gateway" in result
    assert "**Control:** gateway-managed runtime task" in result
    assert "/task bg-1 cancel" in result
    assert "**Actions:** cancel" in result


@pytest.mark.asyncio
async def test_task_command_reports_read_only_active_runtime_task_detail():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = []

    with patch(
        "gateway.run.task_lane_registry.status_snapshot",
        return_value={
            "active_count": 1,
            "lane_counts": {"interactive": 1, "cron_scout": 0, "housekeeping": 0},
            "tasks": [
                {
                    "task_id": "turn-1",
                    "lane": "interactive",
                    "label": "regular turn",
                    "source": "gateway",
                    "started_at": "2026-01-01T00:00:01+00:00",
                }
            ],
        },
    ):
        result = await runner._handle_message(_make_event("/task turn-1"))

    assert "🧩 **Task Detail**" in result
    assert "**Task ID:** `turn-1`" in result
    assert "**State:** active" in result
    assert "**Lane:** interactive" in result
    assert "**Label:** regular turn" in result
    assert "**Kind:** live turn" in result
    assert "**Control:** read-only live turn" in result
    assert "no cancel handle" in result
    assert "**Actions:**" not in result


@pytest.mark.asyncio
async def test_task_command_reports_not_found():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = []

    with patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task missing-task"))

    assert "Task not found: `missing-task`" in result
    assert "/tasks" in result


@pytest.mark.asyncio
async def test_task_command_can_foreground_queued_task_immediately():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    queued_task = MessageTaskEnvelope(
        task_id="task-hi",
        session_key=session_entry.session_key,
        message_event=_make_event("high priority queued follow-up"),
        priority=10,
        lane="background",
        reply_policy="status_only",
        cancellation_policy="preserve",
    )
    adapter.pending_tasks_snapshot.return_value = [queued_task]
    adapter.foreground_pending_task.return_value = ("started", queued_task)

    with patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-hi foreground"))

    adapter.foreground_pending_task.assert_called_once_with(session_entry.session_key, "task-hi")
    assert "Foregrounded queued task `task-hi`" in result
    assert "starting now" in result


@pytest.mark.asyncio
async def test_task_command_can_move_queued_task_to_front_when_session_busy():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    queued_task = MessageTaskEnvelope(
        task_id="task-hi",
        session_key=session_entry.session_key,
        message_event=_make_event("high priority queued follow-up"),
        priority=10,
        lane="background",
        reply_policy="status_only",
        cancellation_policy="preserve",
    )
    adapter.pending_tasks_snapshot.return_value = [queued_task]
    adapter.foreground_pending_task.return_value = ("queued_next", queued_task)

    with patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-hi foreground"))

    adapter.foreground_pending_task.assert_called_once_with(session_entry.session_key, "task-hi")
    assert "Foregrounded queued task `task-hi`" in result
    assert "run next after the current task" in result


@pytest.mark.asyncio
async def test_task_command_can_cancel_queued_task():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    queued_task = MessageTaskEnvelope(
        task_id="task-hi",
        session_key=session_entry.session_key,
        message_event=_make_event("high priority queued follow-up"),
        priority=10,
        lane="background",
        reply_policy="status_only",
        cancellation_policy="preserve",
    )
    adapter.pending_tasks_snapshot.return_value = [queued_task]
    adapter.cancel_pending_task.return_value = queued_task

    with patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-hi cancel"))

    adapter.cancel_pending_task.assert_called_once_with(session_entry.session_key, "task-hi")
    assert "Cancelled queued task `task-hi`" in result


@pytest.mark.asyncio
async def test_task_command_can_reprioritize_queued_task_to_later_bucket():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    queued_task = MessageTaskEnvelope(
        task_id="task-hi",
        session_key=session_entry.session_key,
        message_event=_make_event("high priority queued follow-up"),
        priority=10,
        lane="background",
        reply_policy="status_only",
        cancellation_policy="preserve",
    )
    reprioritized_task = MessageTaskEnvelope(
        task_id="task-hi",
        session_key=session_entry.session_key,
        message_event=queued_task.message_event,
        priority=80,
        lane="background",
        reply_policy="status_only",
        cancellation_policy="preserve",
    )
    adapter.pending_tasks_snapshot.return_value = [queued_task]
    adapter.reprioritize_pending_task.return_value = reprioritized_task

    with patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-hi later"))

    adapter.reprioritize_pending_task.assert_called_once_with(session_entry.session_key, "task-hi", "later")
    assert "Moved queued task `task-hi` to later priority." in result


@pytest.mark.asyncio
async def test_task_command_can_recover_starving_queued_task_via_shortcut():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    queued_task = MessageTaskEnvelope(
        task_id="task-stale",
        session_key=session_entry.session_key,
        message_event=_make_event("stale queued follow-up"),
        priority=80,
        lane="housekeeping",
        reply_policy="status_only",
        cancellation_policy="preserve",
        queued_at=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
    )
    recovered_task = MessageTaskEnvelope(
        task_id="task-stale",
        session_key=session_entry.session_key,
        message_event=queued_task.message_event,
        priority=50,
        lane="housekeeping",
        reply_policy="status_only",
        cancellation_policy="preserve",
        queued_at=queued_task.queued_at,
    )
    adapter.pending_tasks_snapshot.return_value = [queued_task]
    adapter.reprioritize_pending_task.return_value = recovered_task

    with patch("gateway.task_control._queued_task_now", return_value=datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc)), patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-stale recover"))

    adapter.reprioritize_pending_task.assert_called_once_with(session_entry.session_key, "task-stale", "next")
    assert "Recovered queued task `task-stale`" in result
    assert "moved it to next priority" in result


@pytest.mark.asyncio
async def test_task_command_recover_rejects_non_starving_queued_task():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    queued_task = MessageTaskEnvelope(
        task_id="task-hi",
        session_key=session_entry.session_key,
        message_event=_make_event("high priority queued follow-up"),
        priority=10,
        lane="interactive",
        reply_policy="status_only",
        cancellation_policy="preserve",
        queued_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
    )
    adapter.pending_tasks_snapshot.return_value = [queued_task]

    with patch("gateway.task_control._queued_task_now", return_value=datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc)), patch("gateway.run.task_lane_registry.status_snapshot", return_value={"active_count": 0, "lane_counts": {"interactive": 0, "cron_scout": 0, "housekeeping": 0}, "tasks": []}):
        result = await runner._handle_message(_make_event("/task task-hi recover"))

    assert "doesn't currently have a recovery recommendation" in result


@pytest.mark.asyncio
async def test_task_command_can_cancel_managed_runtime_task():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.pending_tasks_snapshot.return_value = []
    cancel_runtime = MagicMock()
    runtime_task = MagicMock()
    runtime_task.done.return_value = False
    runner._managed_runtime_tasks = {
        "bg-1": {
            "task": runtime_task,
            "cancel": cancel_runtime,
            "kind": "background",
        }
    }

    with patch(
        "gateway.run.task_lane_registry.status_snapshot",
        return_value={
            "active_count": 1,
            "lane_counts": {"interactive": 0, "cron_scout": 1, "housekeeping": 0},
            "tasks": [
                {
                    "task_id": "bg-1",
                    "lane": "cron_scout",
                    "label": "background task",
                    "source": "gateway",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        },
    ):
        result = await runner._handle_message(_make_event("/task bg-1 cancel"))

    cancel_runtime.assert_called_once_with()
    assert "Cancellation requested for active task `bg-1`" in result


@pytest.mark.asyncio
async def test_status_command_bypasses_active_session_guard():
    """When an agent is running, /status must be dispatched immediately via
    base.handle_message — not queued or treated as an interrupt (#5046)."""
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
    from gateway.session import build_session_key
    from gateway.config import Platform, PlatformConfig, GatewayConfig

    source = _make_source()
    session_key = build_session_key(source)

    handler_called_with = []

    async def fake_handler(event):
        handler_called_with.append(event)
        return "📊 **Hermes Gateway Status**\n**Agent Running:** Yes ⚡"

    # Concrete subclass to avoid abstract method errors
    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content, **kwargs): pass
        async def get_chat_info(self, chat_id): return {}

    platform_config = PlatformConfig(enabled=True, token="***")
    adapter = _ConcreteAdapter(platform_config, Platform.TELEGRAM)
    adapter.set_message_handler(fake_handler)

    sent = []

    async def fake_send_with_retry(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)

    adapter._send_with_retry = fake_send_with_retry

    # Simulate an active session
    interrupt_event = asyncio.Event()
    adapter._active_sessions[session_key] = interrupt_event

    event = MessageEvent(
        text="/status",
        source=source,
        message_id="m1",
        message_type=MessageType.COMMAND,
    )
    await adapter.handle_message(event)

    assert handler_called_with, "/status handler was never called (event was queued or dropped)"
    assert sent, "/status response was never sent"
    assert "Agent Running" in sent[0]
    assert not interrupt_event.is_set(), "/status incorrectly triggered an agent interrupt"
    assert session_key not in adapter._pending_messages, "/status was incorrectly queued"


@pytest.mark.asyncio
async def test_tasks_command_bypasses_active_session_guard():
    """When an agent is running, /tasks must be dispatched immediately via
    base.handle_message — not queued or treated as an interrupt."""
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
    from gateway.session import build_session_key
    from gateway.config import Platform, PlatformConfig

    source = _make_source()
    session_key = build_session_key(source)

    handler_called_with = []

    async def fake_handler(event):
        handler_called_with.append(event)
        return "📋 **Hermes Tasks**\n\n**Queued for this chat:** 0\n**Active runtime tasks:** 0\nNo queued or active tasks."

    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content, **kwargs): pass
        async def get_chat_info(self, chat_id): return {}

    platform_config = PlatformConfig(enabled=True, token="***")
    adapter = _ConcreteAdapter(platform_config, Platform.TELEGRAM)
    adapter.set_message_handler(fake_handler)

    sent = []

    async def fake_send_with_retry(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)

    adapter._send_with_retry = fake_send_with_retry

    interrupt_event = asyncio.Event()
    adapter._active_sessions[session_key] = interrupt_event

    event = MessageEvent(
        text="/tasks",
        source=source,
        message_id="m1",
        message_type=MessageType.COMMAND,
    )
    await adapter.handle_message(event)

    assert handler_called_with, "/tasks handler was never called (event was queued or dropped)"
    assert sent, "/tasks response was never sent"
    assert "Hermes Tasks" in sent[0]
    assert not interrupt_event.is_set(), "/tasks incorrectly triggered an agent interrupt"
    assert session_key not in adapter._pending_messages, "/tasks was incorrectly queued"


@pytest.mark.asyncio
async def test_task_command_bypasses_active_session_guard():
    """When an agent is running, /task must be dispatched immediately via
    base.handle_message — not queued or treated as an interrupt."""
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
    from gateway.session import build_session_key
    from gateway.config import Platform, PlatformConfig

    source = _make_source()
    session_key = build_session_key(source)

    handler_called_with = []

    async def fake_handler(event):
        handler_called_with.append(event)
        return "🧩 **Task Detail**\n\n**Task ID:** `task-hi`\n**State:** queued"

    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content, **kwargs): pass
        async def get_chat_info(self, chat_id): return {}

    platform_config = PlatformConfig(enabled=True, token="***")
    adapter = _ConcreteAdapter(platform_config, Platform.TELEGRAM)
    adapter.set_message_handler(fake_handler)

    sent = []

    async def fake_send_with_retry(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)

    adapter._send_with_retry = fake_send_with_retry

    interrupt_event = asyncio.Event()
    adapter._active_sessions[session_key] = interrupt_event

    event = MessageEvent(
        text="/task task-hi",
        source=source,
        message_id="m1",
        message_type=MessageType.COMMAND,
    )
    await adapter.handle_message(event)

    assert handler_called_with, "/task handler was never called (event was queued or dropped)"
    assert sent, "/task response was never sent"
    assert "Task Detail" in sent[0]
    assert not interrupt_event.is_set(), "/task incorrectly triggered an agent interrupt"
    assert session_key not in adapter._pending_messages, "/task was incorrectly queued"


def test_tasks_and_task_commands_are_registered():
    from hermes_cli.commands import COMMAND_REGISTRY, COMMANDS, COMMANDS_BY_CATEGORY, GATEWAY_KNOWN_COMMANDS, resolve_command

    assert any(cmd.name == "tasks" and cmd.gateway_only for cmd in COMMAND_REGISTRY)
    assert any(cmd.name == "task" and cmd.gateway_only for cmd in COMMAND_REGISTRY)
    assert "/tasks" not in COMMANDS
    assert "/task" not in COMMANDS
    assert "/tasks" not in COMMANDS_BY_CATEGORY.get("Session", {})
    assert "/task" not in COMMANDS_BY_CATEGORY.get("Session", {})
    assert "tasks" in GATEWAY_KNOWN_COMMANDS
    assert "task" in GATEWAY_KNOWN_COMMANDS
    assert resolve_command("tasks").name == "tasks"
    assert resolve_command("task").name == "task"
