import asyncio
from unittest.mock import MagicMock, patch

import pytest

from agent.task_queue import TaskQueueLedger
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/background 排查 gateway 卡顿"):
    source = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", chat_type="dm")
    return MessageEvent(text=text, source=source, message_id="m1")


def _make_runner():
    from gateway.hooks import HookRegistry
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._background_tasks = set()
    runner._managed_runtime_tasks = {}
    runner.session_store = MagicMock()
    runner.hooks = HookRegistry()
    return runner


@pytest.mark.asyncio
async def test_background_command_mirrors_started_task_into_durable_queue(tmp_path):
    runner = _make_runner()
    backlog = tmp_path / "backlog.jsonl"

    def capture_task(coro, *args, **kwargs):
        coro.close()
        task = MagicMock()
        task.add_done_callback = MagicMock()
        return task

    with patch("gateway.run._load_gateway_config", return_value={"task_queue": {"enabled": True, "path": str(backlog)}}), \
         patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        result = await runner._handle_background_command(_make_event())

    task_id = next(line.split(": ", 1)[1] for line in result.splitlines() if line.startswith("Task ID:"))
    rows = TaskQueueLedger(backlog).load()
    assert [row["id"] for row in rows] == [task_id]
    assert rows[0]["status"] == "running"
    assert rows[0]["lane"] == "infra_debug"
    assert rows[0]["last_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_background_command_respects_task_queue_opt_out(tmp_path):
    runner = _make_runner()
    backlog = tmp_path / "backlog.jsonl"

    def capture_task(coro, *args, **kwargs):
        coro.close()
        task = MagicMock()
        task.add_done_callback = MagicMock()
        return task

    with patch("gateway.run._load_gateway_config", return_value={"task_queue": {"enabled": False, "path": str(backlog)}}), \
         patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        await runner._handle_background_command(_make_event())

    assert not backlog.exists()


@pytest.mark.asyncio
async def test_background_command_queues_without_starting_when_lane_capacity_is_full(tmp_path):
    runner = _make_runner()
    backlog = tmp_path / "backlog.jsonl"
    TaskQueueLedger(backlog).enqueue(
        title="already running gateway work",
        task_id="bg_running",
        lane="infra_debug",
        status="running",
        acceptance_check=["done"],
        artifact_targets=["gateway-task:bg_running"],
        extra_fields={"dispatch": {"kind": "gateway_background", "prompt": "排查 gateway"}},
    )

    created = []

    def capture_task(coro, *args, **kwargs):
        created.append(coro)
        coro.close()
        task = MagicMock()
        task.add_done_callback = MagicMock()
        return task

    with patch("gateway.run._load_gateway_config", return_value={"task_queue": {"enabled": True, "path": str(backlog)}}), \
         patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        result = await runner._handle_background_command(_make_event())

    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    queued_ids = [task_id for task_id in rows if task_id != "bg_running"]
    assert len(queued_ids) == 1
    assert rows[queued_ids[0]]["status"] == "ready"
    assert created == []
    assert "queued" in result.lower()


@pytest.mark.asyncio
async def test_dispatch_claimed_background_tasks_starts_ready_queue_item(tmp_path):
    runner = _make_runner()
    backlog = tmp_path / "backlog.jsonl"
    source = _make_event().source
    TaskQueueLedger(backlog).enqueue(
        title="queued gateway work",
        task_id="bg_ready",
        lane="infra_debug",
        status="ready",
        acceptance_check=["done"],
        artifact_targets=["gateway-task:bg_ready"],
        extra_fields={"dispatch": {"kind": "gateway_background", "prompt": "排查 gateway", "source": source.to_dict()}},
    )

    created = []

    def capture_task(coro, *args, **kwargs):
        created.append(coro)
        coro.close()
        task = MagicMock()
        task.add_done_callback = MagicMock()
        return task

    with patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        started = await runner._dispatch_claimed_background_tasks({"task_queue": {"enabled": True, "path": str(backlog)}})

    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    assert started == ["bg_ready"]
    assert rows["bg_ready"]["status"] == "running"
    assert len(created) == 1


@pytest.mark.asyncio
async def test_dispatch_claimed_background_tasks_starts_ready_cron_queue_item(tmp_path):
    runner = _make_runner()
    backlog = tmp_path / "backlog.jsonl"
    from gateway.task_queue_bridge import enqueue_cron_job_task

    task = enqueue_cron_job_task(
        job={"id": "cron_123", "name": "queued cron", "prompt": "do work", "deliver": "local"},
        config={"task_queue": {"enabled": True, "path": str(backlog)}},
    )

    created = []

    def capture_task(coro, *args, **kwargs):
        created.append(coro)
        coro.close()
        task_mock = MagicMock()
        task_mock.add_done_callback = MagicMock()
        return task_mock

    with patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        started = await runner._dispatch_claimed_background_tasks({"task_queue": {"enabled": True, "path": str(backlog)}})

    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    assert started == [task["id"]]
    assert rows[task["id"]]["status"] == "running"
    assert len(created) == 1


def test_cron_ticker_queue_wakeup_runs_async_dispatcher(monkeypatch):
    import gateway.run as gateway_run

    calls = []
    fake_loop = object()

    async def dispatcher(config):
        calls.append(config)
        return ["bg_ready"]

    class _Future:
        def __init__(self, result):
            self._result = result
            self.timeout = None

        def result(self, timeout=None):
            self.timeout = timeout
            return self._result

    def run_threadsafe(coro, loop):
        assert loop is fake_loop
        return _Future(asyncio.run(coro))

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"task_queue": {"enabled": True}})
    monkeypatch.setattr(gateway_run.asyncio, "run_coroutine_threadsafe", run_threadsafe)

    dispatched = gateway_run._dispatch_ready_queue_from_ticker(dispatcher, fake_loop)

    assert dispatched == ["bg_ready"]
    assert calls == [{"task_queue": {"enabled": True}}]


def test_cron_ticker_wakes_queue_dispatcher_once(monkeypatch):
    import gateway.run as gateway_run

    calls = []

    class _StopAfterOneTick:
        def __init__(self):
            self.wait_calls = 0

        def is_set(self):
            return self.wait_calls > 0

        def wait(self, timeout=None):
            self.wait_calls += 1
            return True

    monkeypatch.setattr("cron.scheduler.tick", lambda **kwargs: calls.append(("cron", kwargs)) or 0)
    monkeypatch.setattr(gateway_run, "_dispatch_ready_queue_from_ticker", lambda dispatcher, loop: calls.append(("queue", dispatcher, loop)) or ["bg_ready"])

    gateway_run._start_cron_ticker(
        _StopAfterOneTick(),
        adapters={},
        loop="loop-token",
        interval=0,
        queue_dispatcher="dispatcher-token",
    )

    assert calls[0][0] == "cron"
    assert calls[1] == ("queue", "dispatcher-token", "loop-token")
