"""Tests for /queue message consumption after normal agent completion.

Verifies that messages queued via /queue (which store in
adapter._pending_messages WITHOUT triggering an interrupt) are consumed
after the agent finishes its current task — not silently dropped.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageTaskEnvelope,
    MessageType,
    PlatformConfig,
    Platform,
)


# ---------------------------------------------------------------------------
# Minimal adapter for testing pending message storage
# ---------------------------------------------------------------------------

class _StubAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult
        return SendResult(success=True, message_id="msg-1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestQueueMessageStorage:
    """Verify /queue stores messages correctly in adapter._pending_messages."""

    def test_queue_returns_task_envelope_with_default_metadata(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"
        event = MessageEvent(
            text="do this next",
            message_type=MessageType.TEXT,
            source=MagicMock(chat_id="123", platform=Platform.TELEGRAM),
            message_id="q-meta-1",
        )

        envelope = adapter.queue_message(session_key, event)

        assert isinstance(envelope, MessageTaskEnvelope)
        assert envelope.message_event is event
        assert envelope.session_key == session_key
        assert envelope.priority == 50
        assert envelope.lane == "interactive"
        assert envelope.reply_policy == "user_visible"
        assert envelope.cancellation_policy == "preserve"
        assert envelope.task_id

    def test_queue_stores_message_in_pending(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"
        event = MessageEvent(
            text="do this next",
            message_type=MessageType.TEXT,
            source=MagicMock(chat_id="123", platform=Platform.TELEGRAM),
            message_id="q1",
        )
        adapter.queue_message(session_key, event)

        assert session_key in adapter._pending_messages
        assert adapter._pending_messages[session_key] is event

    def test_get_pending_message_consumes_and_clears(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"
        event = MessageEvent(
            text="queued prompt",
            message_type=MessageType.TEXT,
            source=MagicMock(chat_id="123", platform=Platform.TELEGRAM),
            message_id="q2",
        )
        adapter.queue_message(session_key, event)

        retrieved = adapter.get_pending_message(session_key)
        assert retrieved is not None
        assert retrieved.text == "queued prompt"
        # Should be consumed (cleared)
        assert adapter.get_pending_message(session_key) is None

    def test_queue_does_not_set_interrupt_event(self):
        """The whole point of /queue — no interrupt signal."""
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        # Simulate an active session (agent running)
        adapter._active_sessions[session_key] = asyncio.Event()

        # Store a queued message (what /queue does)
        event = MessageEvent(
            text="queued",
            message_type=MessageType.TEXT,
            source=MagicMock(),
            message_id="q3",
        )
        adapter.queue_message(session_key, event)

        # The interrupt event should NOT be set
        assert not adapter._active_sessions[session_key].is_set()
        assert not adapter.has_pending_interrupt(session_key)

    def test_regular_message_sets_interrupt_event(self):
        """Contrast: regular messages DO trigger interrupt."""
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        adapter._active_sessions[session_key] = asyncio.Event()

        # Simulate regular message arrival (what handle_message does)
        event = MessageEvent(
            text="new message",
            message_type=MessageType.TEXT,
            source=MagicMock(),
            message_id="m1",
        )
        adapter.queue_message(session_key, event)
        adapter._active_sessions[session_key].set()  # this is what handle_message does

        assert adapter.has_pending_interrupt(session_key)

    @pytest.mark.asyncio
    async def test_queue_mode_busy_followup_does_not_interrupt(self, monkeypatch):
        monkeypatch.setenv("HERMES_BUSY_INPUT_MODE", "queue")
        adapter = _StubAdapter()
        adapter.set_message_handler(lambda event: asyncio.sleep(0, result=None))

        source = MagicMock(chat_id="123", platform=Platform.TELEGRAM, chat_type="dm")
        session_key = "agent:main:telegram:dm:123"
        interrupt_event = asyncio.Event()
        adapter._active_sessions[session_key] = interrupt_event

        event = MessageEvent(text="follow up", message_type=MessageType.TEXT, source=source, message_id="q4")
        with patch("gateway.platforms.base.build_session_key", return_value=session_key):
            await adapter.handle_message(event)

        assert adapter._pending_messages[session_key] is event
        assert interrupt_event.is_set() is False

    @pytest.mark.asyncio
    async def test_interrupt_mode_busy_followup_sets_interrupt(self, monkeypatch):
        monkeypatch.setenv("HERMES_BUSY_INPUT_MODE", "interrupt")
        adapter = _StubAdapter()
        adapter.set_message_handler(lambda event: asyncio.sleep(0, result=None))

        source = MagicMock(chat_id="123", platform=Platform.TELEGRAM, chat_type="dm")
        session_key = "agent:main:telegram:dm:123"
        interrupt_event = asyncio.Event()
        adapter._active_sessions[session_key] = interrupt_event

        event = MessageEvent(text="interrupt me", message_type=MessageType.TEXT, source=source, message_id="q5")
        with patch("gateway.platforms.base.build_session_key", return_value=session_key):
            await adapter.handle_message(event)

        assert adapter._pending_messages[session_key] is event
        assert interrupt_event.is_set() is True


class TestQueueConsumptionAfterCompletion:
    """Verify that pending messages are consumed after normal completion."""

    @pytest.mark.asyncio
    async def test_foreground_pending_task_starts_selected_task_immediately_when_session_idle(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        low_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="low", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-low"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
        )
        high_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="high", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-high"),
            priority=10,
            lane="background",
            reply_policy="status_only",
        )

        created_tasks = []

        def capture_task(coro, *args, **kwargs):
            coro.close()
            task = MagicMock()
            created_tasks.append(task)
            return task

        with patch("gateway.platforms.base.asyncio.create_task", side_effect=capture_task):
            state, foregrounded = adapter.foreground_pending_task(session_key, low_priority.task_id)

        assert state == "started"
        assert foregrounded.task_id == low_priority.task_id
        assert len(created_tasks) == 1
        assert session_key in adapter._active_sessions
        assert adapter.peek_pending_task(session_key).task_id == high_priority.task_id
        assert adapter._pending_messages[session_key] is high_priority.message_event

    def test_foreground_pending_task_moves_selected_task_to_front_when_session_active(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"
        adapter._active_sessions[session_key] = asyncio.Event()

        low_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="low", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-low"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
        )
        high_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="high", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-high"),
            priority=10,
            lane="background",
            reply_policy="status_only",
        )

        with patch("gateway.platforms.base.asyncio.create_task") as create_task:
            state, foregrounded = adapter.foreground_pending_task(session_key, low_priority.task_id)

        assert state == "queued_next"
        assert foregrounded.task_id == low_priority.task_id
        assert adapter.peek_pending_task(session_key).task_id == low_priority.task_id
        assert adapter.pending_tasks_snapshot(session_key)[1].task_id == high_priority.task_id
        assert adapter._pending_messages[session_key] is low_priority.message_event
        create_task.assert_not_called()

    def test_cancel_pending_task_removes_target_and_promotes_next_task(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        low_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="low", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-low"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
        )
        high_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="high", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-high"),
            priority=10,
            lane="background",
            reply_policy="status_only",
        )

        removed = adapter.cancel_pending_task(session_key, high_priority.task_id)

        assert removed is not None
        assert removed.task_id == high_priority.task_id
        assert adapter.pending_task_count(session_key) == 1
        assert adapter.peek_pending_task(session_key).task_id == low_priority.task_id
        assert adapter._pending_messages[session_key] is low_priority.message_event
        assert adapter.cancel_pending_task(session_key, "missing-task") is None
        assert adapter.get_pending_task(session_key).task_id == low_priority.task_id
        assert adapter.get_pending_task(session_key) is None

    def test_reprioritize_pending_task_reorders_queue_and_updates_priority(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        high_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="high", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-high"),
            priority=10,
            lane="background",
            reply_policy="status_only",
        )
        next_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="next", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-next"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
        )

        updated = adapter.reprioritize_pending_task(session_key, high_priority.task_id, "later")

        assert updated is not None
        assert updated.task_id == high_priority.task_id
        assert updated.priority == 80
        assert updated.reply_policy == "status_only"
        assert [task.task_id for task in adapter.pending_tasks_snapshot(session_key)] == [
            next_priority.task_id,
            high_priority.task_id,
        ]
        assert adapter._pending_messages[session_key] is next_priority.message_event

    def test_get_pending_task_prefers_higher_priority_item_even_if_it_arrives_later(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        low_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="low", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-low"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
        )
        high_priority = adapter.queue_message(
            session_key,
            MessageEvent(text="high", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-high"),
            priority=10,
            lane="background",
            reply_policy="status_only",
        )

        assert adapter.peek_pending_task(session_key).task_id == high_priority.task_id
        assert adapter._pending_messages[session_key] is high_priority.message_event

        fetched_first = adapter.get_pending_task(session_key)
        fetched_second = adapter.get_pending_task(session_key)

        assert fetched_first.task_id == high_priority.task_id
        assert fetched_first.priority == 10
        assert fetched_first.lane == "background"
        assert fetched_first.reply_policy == "status_only"
        assert fetched_second.task_id == low_priority.task_id
        assert fetched_second.priority == 50
        assert fetched_second.lane == "interactive"
        assert fetched_second.reply_policy == "user_visible"
        assert adapter.get_pending_task(session_key) is None

    def test_pending_tasks_snapshot_preserves_priority_order_and_metadata(self):
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        adapter.queue_message(
            session_key,
            MessageEvent(text="low", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-low"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
        )
        adapter.queue_message(
            session_key,
            MessageEvent(text="high", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-high"),
            priority=10,
            lane="background",
            reply_policy="status_only",
        )

        snapshot = adapter.pending_tasks_snapshot(session_key)

        assert [task.task_id for task in snapshot] == ["task-q-high", "task-q-low"]
        assert snapshot[0].priority == 10
        assert snapshot[0].lane == "background"
        assert snapshot[0].reply_policy == "status_only"
        assert snapshot[1].priority == 50
        assert snapshot[1].lane == "interactive"
        assert snapshot[1].reply_policy == "user_visible"

    def test_all_pending_tasks_snapshot_flattens_all_sessions(self):
        adapter = _StubAdapter()

        adapter.queue_message(
            "telegram:user:123",
            MessageEvent(text="chat one", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-one"),
            priority=50,
            lane="interactive",
            reply_policy="user_visible",
        )
        adapter.queue_message(
            "telegram:user:456",
            MessageEvent(text="chat two", message_type=MessageType.TEXT, source=MagicMock(), message_id="q-two"),
            priority=10,
            lane="background",
            reply_policy="status_only",
        )

        snapshot = adapter.all_pending_tasks_snapshot()

        assert [task.task_id for task in snapshot] == ["task-q-one", "task-q-two"]
        assert [task.session_key for task in snapshot] == ["telegram:user:123", "telegram:user:456"]
        assert snapshot[0].message_event.text == "chat one"
        assert snapshot[1].message_event.text == "chat two"

    def test_pending_message_available_after_normal_completion(self):
        """After agent finishes without interrupt, pending message should
        still be retrievable from adapter._pending_messages."""
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        # Simulate: agent starts, /queue stores a message, agent finishes
        adapter._active_sessions[session_key] = asyncio.Event()
        event = MessageEvent(
            text="process this after",
            message_type=MessageType.TEXT,
            source=MagicMock(),
            message_id="q4",
        )
        adapter.queue_message(session_key, event)

        # Agent finishes (no interrupt)
        del adapter._active_sessions[session_key]

        # The queued message should still be retrievable
        retrieved = adapter.get_pending_message(session_key)
        assert retrieved is not None
        assert retrieved.text == "process this after"

    def test_multiple_queues_preserve_fifo_order(self):
        """Queued follow-ups should be consumed in arrival order."""
        adapter = _StubAdapter()
        session_key = "telegram:user:123"

        for text in ["first", "second", "third"]:
            event = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=MagicMock(),
                message_id=f"q-{text}",
            )
            adapter.queue_message(session_key, event)

        assert adapter.get_pending_message(session_key).text == "first"
        assert adapter.get_pending_message(session_key).text == "second"
        assert adapter.get_pending_message(session_key).text == "third"
        assert adapter.get_pending_message(session_key) is None
