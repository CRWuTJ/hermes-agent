import asyncio
import io
import json
import sys
import threading
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
import gateway.worker_runtime as worker_runtime
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _ExplodingAgent:
    def __init__(self, *args, **kwargs):
        raise AssertionError("AIAgent should not be constructed when detached conversation worker routing is active")


class _FakeConversationWorkerHandle:
    def __init__(self, result=None):
        self._result = result or {
            "final_response": "ok from worker",
            "messages": [],
            "api_calls": 1,
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "worker-model",
        }
        self.interrupt_calls = []
        self.resolve_approval_calls = []
        self._pending_approval = True

    async def wait_for_result(self):
        return self._result

    def interrupt(self, message=None):
        self.interrupt_calls.append(message)

    def get_activity_summary(self):
        return {
            "seconds_since_activity": 0.0,
            "api_call_count": 1,
            "max_iterations": 90,
            "last_activity_desc": "worker active",
            "current_tool": "",
        }

    def has_pending_approval(self):
        return self._pending_approval

    def resolve_approval(self, choice: str) -> bool:
        self.resolve_approval_calls.append(choice)
        self._pending_approval = False
        return True


class _BlockingConversationWorkerHandle(_FakeConversationWorkerHandle):
    def __init__(self, result=None, idle_seconds: float = 0.0):
        super().__init__(result=result)
        self._released = threading.Event()
        self._idle_seconds = idle_seconds

    async def wait_for_result(self):
        self._released.wait(timeout=5)
        return self._result

    def wait_for_result_blocking(self):
        self._released.wait(timeout=5)
        return self._result

    def interrupt(self, message=None):
        super().interrupt(message)
        self._released.set()

    def get_activity_summary(self):
        data = super().get_activity_summary()
        data["seconds_since_activity"] = self._idle_seconds
        data["current_tool"] = "terminal"
        data["last_activity_desc"] = "still busy"
        return data


class _TerminatingConversationWorkerHandle(_BlockingConversationWorkerHandle):
    def __init__(self, result=None, idle_seconds: float = 0.0):
        super().__init__(result=result, idle_seconds=idle_seconds)
        self.terminate_calls = []

    def interrupt(self, message=None):
        self.interrupt_calls.append(message)

    def terminate(self, reason="cancelled"):
        self.terminate_calls.append(reason)
        self._released.set()
        return True


class _CountingApprovalHandle:
    def __init__(self, count: int):
        self._pending_count = count
        self.resolve_approval_calls = []

    def has_pending_approval(self):
        return self._pending_count > 0

    def resolve_approval(self, choice: str) -> bool:
        if self._pending_count <= 0:
            return False
        self.resolve_approval_calls.append(choice)
        self._pending_count -= 1
        return True


class _AlwaysPendingApprovalHandle:
    def __init__(self):
        self.resolve_approval_calls = []

    def has_pending_approval(self):
        return True

    def resolve_approval(self, choice: str) -> bool:
        self.resolve_approval_calls.append(choice)
        return True


class _ApprovalGapHandle:
    def has_pending_approval(self):
        return False

    def resolve_approval(self, choice: str) -> bool:
        return False


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._background_tasks = set()
    runner._managed_runtime_tasks = {}
    runner._session_db = None
    runner._voice_mode = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = MagicMock()
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._load_reasoning_config = lambda: {"enabled": True, "effort": "medium"}
    runner._load_show_reasoning = lambda: False
    runner._resolve_turn_agent_config = lambda message, model, runtime_kwargs: {
        "model": model,
        "runtime": dict(runtime_kwargs),
    }
    runner._agent_config_signature = lambda *args, **kwargs: "sig"
    runner._evict_cached_agent = lambda session_key: None
    runner._resolve_session_scoped_provider_routing = lambda *args, **kwargs: {}
    runner._session_key_for_source = lambda source: f"agent:main:{source.platform.value}:dm:{source.chat_id}"
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner.config = MagicMock()
    runner.config.streaming = MagicMock(enabled=False, transport="off", edit_interval=0.2, buffer_threshold=20, cursor="▋")
    return runner


def _make_source(platform=Platform.TELEGRAM):
    return SessionSource(
        platform=platform,
        chat_id="chat-1",
        chat_name="Chat",
        chat_type="dm",
        user_id="user-1",
        user_name="tester",
    )


def _make_real_worker_handle():
    proc = MagicMock()
    proc.stdout = io.StringIO("")
    proc.stderr = io.StringIO("")
    proc.stdin = io.StringIO()
    proc.poll.return_value = None
    return worker_runtime.GatewayConversationWorkerHandle(proc=proc, unit_name="hermes-gwconv-test")


@pytest.mark.asyncio
async def test_run_agent_uses_detached_conversation_worker_for_live_messaging_session(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    handle = _FakeConversationWorkerHandle()

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _ExplodingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    with patch.object(runner, "_should_use_detached_conversation_worker", return_value=True, create=True), \
         patch("gateway.worker_runtime.start_gateway_conversation_worker", return_value=handle) as mock_start:
        result = await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:telegram:dm:chat-1",
        )

    assert result["final_response"] == "ok from worker"
    mock_start.assert_called_once()


@pytest.mark.asyncio
async def test_run_agent_terminates_detached_worker_after_live_wall_clock_timeout(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    handle = _TerminatingConversationWorkerHandle(idle_seconds=1.0)

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "9999")
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"gateway_wall_clock_timeout": 60}, "display": {}},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda cfg: "worker-model")
    monotonic_values = iter([0.0, 61.0, 61.0, 61.0])
    monkeypatch.setattr(gateway_run, "_gateway_monotonic", lambda: next(monotonic_values, 61.0))

    real_wait = gateway_run.asyncio.wait

    async def fast_timeout_wait(tasks, timeout=None):
        if timeout == 2.0:
            return set(), set(tasks)
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(gateway_run.asyncio, "wait", fast_timeout_wait)

    with patch.object(runner, "_should_use_detached_conversation_worker", return_value=True, create=True), \
         patch("gateway.worker_runtime.start_gateway_conversation_worker", return_value=handle):
        result = await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:telegram:dm:chat-1",
        )

    assert "Live turn hit the 1 min total runtime limit" in result["final_response"]
    assert handle.interrupt_calls == ["Execution timed out (wall clock)"]
    assert handle.terminate_calls == ["wall_clock_timeout"]


def test_resolve_approval_keeps_pending_state_when_control_send_fails():
    handle = _make_real_worker_handle()
    approval = {"approval_id": "approval-1", "command": "echo hi"}
    handle._pending_approval = dict(approval)

    with patch.object(handle, "_send_control", return_value=False) as mock_send:
        assert handle.resolve_approval("approve") is False

    assert handle.get_pending_approval() == approval
    mock_send.assert_called_once_with(
        {
            "type": "approval_response",
            "approval_id": "approval-1",
            "choice": "approve",
        }
    )


def test_resolve_approval_clears_matching_pending_state_after_successful_send():
    handle = _make_real_worker_handle()
    handle._pending_approval = {"approval_id": "approval-1", "command": "echo hi"}

    with patch.object(handle, "_send_control", return_value=True):
        assert handle.resolve_approval("approve") is True

    assert handle.get_pending_approval() is None


def test_resolve_approval_preserves_newer_pending_request_created_during_send():
    handle = _make_real_worker_handle()
    handle._pending_approval = {"approval_id": "approval-1", "command": "echo hi"}
    newer = {"approval_id": "approval-2", "command": "echo later"}

    def _send_control(_payload):
        with handle._pending_approval_lock:
            handle._pending_approval = dict(newer)
        return True

    with patch.object(handle, "_send_control", side_effect=_send_control):
        assert handle.resolve_approval("approve") is True

    assert handle.get_pending_approval() == newer


@pytest.mark.asyncio
async def test_run_agent_stream_poller_waits_for_consumer_created_inside_run_sync(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    handle = _FakeConversationWorkerHandle()
    runner.config.streaming = MagicMock(enabled=True, transport="telegram", edit_interval=0.2, buffer_threshold=20, cursor="▋")

    class _FakeAdapter:
        name = "fake"

        def __init__(self):
            self.send = AsyncMock(return_value=types.SimpleNamespace(success=True, message_id="msg-1"))
            self.edit_message = AsyncMock(return_value=types.SimpleNamespace(success=True, message_id="msg-1", error=""))
            self.send_typing = AsyncMock(return_value=None)

        def has_pending_interrupt(self, session_key):
            return False

        def get_pending_message(self, session_key):
            return None

        def clear_pending_interrupt(self, session_key):
            return None

    runner.adapters[source.platform] = _FakeAdapter()

    stream_events = {"run_called": 0, "finish_called": 0}

    class _FakeGatewayStreamConsumer:
        def __init__(self, *args, **kwargs):
            self.already_sent = False

        async def run(self):
            stream_events["run_called"] += 1

        def on_delta(self, *args, **kwargs):
            return None

        def finish(self):
            stream_events["finish_called"] += 1

    class _FakeStreamConsumerConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_stream_module = types.ModuleType("gateway.stream_consumer")
    fake_stream_module.GatewayStreamConsumer = _FakeGatewayStreamConsumer
    fake_stream_module.StreamConsumerConfig = _FakeStreamConsumerConfig
    monkeypatch.setitem(sys.modules, "gateway.stream_consumer", fake_stream_module)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _ExplodingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    with patch.object(runner, "_should_use_detached_conversation_worker", return_value=True, create=True), \
         patch("gateway.worker_runtime.start_gateway_conversation_worker", return_value=handle):
        result = await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:telegram:dm:chat-1",
        )

    assert result["final_response"] == "ok from worker"
    assert stream_events["run_called"] == 1
    assert stream_events["finish_called"] == 1


@pytest.mark.asyncio
async def test_run_agent_prefers_config_max_turns_over_stale_env(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    handle = _FakeConversationWorkerHandle()

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_MAX_ITERATIONS", "90")
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"max_turns": 24}, "display": {}},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda cfg: "worker-model")

    with patch.object(runner, "_should_use_detached_conversation_worker", return_value=True, create=True), \
         patch("gateway.worker_runtime.start_gateway_conversation_worker", return_value=handle) as mock_start:
        await runner._run_agent(
            message="hello",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:telegram:dm:chat-1",
        )

    assert mock_start.call_args.kwargs["request"]["max_iterations"] == 24


def test_resolve_live_wall_clock_timeout_prefers_agent_config(monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", "999")
    timeout = gateway_run._resolve_live_wall_clock_timeout(
        {"gateway_wall_clock_timeout": 60},
        Platform.TELEGRAM,
    )
    assert timeout == 60.0


def test_resolve_live_wall_clock_timeout_defaults_for_messaging_platform(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", raising=False)
    timeout = gateway_run._resolve_live_wall_clock_timeout({}, Platform.TELEGRAM)
    assert timeout == 5400.0


def test_resolve_live_wall_clock_timeout_stays_unlimited_for_local_and_api(monkeypatch):
    monkeypatch.delenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", raising=False)
    assert gateway_run._resolve_live_wall_clock_timeout({}, Platform.LOCAL) is None
    assert gateway_run._resolve_live_wall_clock_timeout({}, Platform.API_SERVER) is None
    assert gateway_run._resolve_live_wall_clock_timeout({}, Platform.WEBHOOK) is None


@pytest.mark.asyncio
async def test_run_agent_interrupts_when_live_wall_clock_limit_is_hit(monkeypatch):
    runner = _make_runner()
    source = _make_source()

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"gateway_wall_clock_timeout": 60}, "display": {}},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda cfg: "worker-model")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "9999")
    monkeypatch.delenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", raising=False)

    class _BlockingAgent:
        instances = []

        def __init__(self, *args, **kwargs):
            self._released = threading.Event()
            self.interrupt_calls = []
            self.model = kwargs.get("model", "worker-model")
            self.session_id = kwargs.get("session_id", "session-1")
            self.context_compressor = types.SimpleNamespace(last_prompt_tokens=0)
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            type(self).instances.append(self)

        def get_activity_summary(self):
            return {
                "seconds_since_activity": 2.0,
                "api_call_count": 3,
                "max_iterations": 24,
                "last_activity_desc": "waiting on tool",
                "current_tool": "terminal",
            }

        def interrupt(self, message=None):
            self.interrupt_calls.append(message)
            self._released.set()

        def run_conversation(self, *args, **kwargs):
            self._released.wait(timeout=5)
            return {
                "final_response": "should not win",
                "messages": [],
                "api_calls": 3,
                "tools": [],
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _BlockingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    monotonic_values = iter([0.0, 61.0, 61.0, 61.0])
    monkeypatch.setattr(gateway_run, "_gateway_monotonic", lambda: next(monotonic_values, 61.0))

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:telegram:dm:chat-1",
    )

    assert "Live turn hit the 1 min total runtime limit" in result["final_response"]
    assert _BlockingAgent.instances
    assert _BlockingAgent.instances[0].interrupt_calls == ["Execution timed out (wall clock)"]


@pytest.mark.asyncio
async def test_run_agent_wall_clock_timeout_does_not_send_late_worker_response_before_followup(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = "agent:main:telegram:dm:chat-1"

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"gateway_wall_clock_timeout": 60}, "display": {}},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda cfg: "worker-model")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "9999")
    monkeypatch.delenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", raising=False)

    class _BlockingAgent:
        def __init__(self, *args, **kwargs):
            self._released = threading.Event()
            self.interrupt_calls = []
            self.model = kwargs.get("model", "worker-model")
            self.session_id = kwargs.get("session_id", "session-1")
            self.context_compressor = types.SimpleNamespace(last_prompt_tokens=0)
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0

        def get_activity_summary(self):
            return {
                "seconds_since_activity": 2.0,
                "api_call_count": 3,
                "max_iterations": 24,
                "last_activity_desc": "waiting on tool",
                "current_tool": "terminal",
            }

        def interrupt(self, message=None):
            self.interrupt_calls.append(message)
            self._released.set()

        def run_conversation(self, *args, **kwargs):
            self._released.wait(timeout=5)
            return {
                "final_response": "should not win",
                "messages": [],
                "api_calls": 3,
                "tools": [],
            }

    class _FakeAdapter:
        name = "fake"

        def __init__(self):
            self.send = AsyncMock(return_value=types.SimpleNamespace(success=True, message_id="msg-1"))
            self.send_typing = AsyncMock(return_value=None)
            self._pending_event = MessageEvent(text="followup", source=source)
            self._active_sessions = {session_key: MagicMock()}

        def has_pending_interrupt(self, session_key):
            return False

        def get_pending_message(self, session_key):
            event = self._pending_event
            self._pending_event = None
            return event

    runner.adapters[source.platform] = _FakeAdapter()

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _BlockingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    monotonic_values = iter([0.0, 61.0, 61.0, 61.0])
    monkeypatch.setattr(gateway_run, "_gateway_monotonic", lambda: next(monotonic_values, 61.0))

    original_run_agent = gateway_run.GatewayRunner._run_agent.__get__(runner, gateway_run.GatewayRunner)

    async def _run_agent_wrapper(**kwargs):
        if kwargs.get("message") == "followup":
            return {
                "final_response": "followup ok",
                "messages": [],
                "api_calls": 1,
                "tools": [],
            }
        return await original_run_agent(**kwargs)

    runner._run_agent = _run_agent_wrapper

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key=session_key,
    )

    sent_messages = [call.args[1] for call in runner.adapters[source.platform].send.await_args_list]
    assert result["final_response"] == "followup ok"
    assert "should not win" not in sent_messages


@pytest.mark.asyncio
async def test_run_agent_does_not_false_timeout_when_executor_finishes_between_polls(monkeypatch):
    runner = _make_runner()
    source = _make_source()

    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"agent": {"gateway_wall_clock_timeout": 60}, "display": {}},
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda cfg: "worker-model")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "9999")
    monkeypatch.delenv("HERMES_AGENT_WALL_CLOCK_TIMEOUT", raising=False)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _ExplodingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    loop = asyncio.get_running_loop()
    fake_future = loop.create_future()
    monkeypatch.setattr(loop, "run_in_executor", lambda executor, func: object())
    monkeypatch.setattr(gateway_run.asyncio, "ensure_future", lambda fut: fake_future)

    wait_calls = {"count": 0}

    async def _fake_wait(tasks, timeout):
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            fake_future.set_result(
                {
                    "final_response": "completed just in time",
                    "messages": [],
                    "api_calls": 1,
                    "tools": [],
                }
            )
            return set(), set()
        return {fake_future}, set()

    monkeypatch.setattr(gateway_run.asyncio, "wait", _fake_wait)
    monotonic_values = iter([0.0, 59.0, 61.0, 61.0])
    monkeypatch.setattr(gateway_run, "_gateway_monotonic", lambda: next(monotonic_values, 61.0))

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:telegram:dm:chat-1",
    )

    assert result["final_response"] == "completed just in time"
    assert wait_calls["count"] == 1


@pytest.mark.asyncio
async def test_approve_command_resolves_detached_worker_pending_approval(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    handle = _FakeConversationWorkerHandle()
    runner._running_agents[session_key] = handle
    runner._pending_approvals[session_key] = {"command": "rm -rf /tmp"}

    adapter = AsyncMock()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters[Platform.TELEGRAM] = adapter

    event = MessageEvent(text="/approve session", source=source)

    with patch("tools.approval.has_blocking_approval", return_value=False):
        result = await runner._handle_approve_command(event)

    assert "approved for this session" in result
    assert handle.resolve_approval_calls == ["session"]
    adapter.resume_typing_for_chat.assert_called_once_with(source.chat_id)


@pytest.mark.asyncio
async def test_approve_all_resolves_all_detached_worker_pending_approvals(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    handle = _CountingApprovalHandle(2)
    runner._running_agents[session_key] = handle
    runner._pending_approvals[session_key] = {"command": "rm -rf /tmp"}

    adapter = AsyncMock()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters[Platform.TELEGRAM] = adapter

    event = MessageEvent(text="/approve all", source=source)

    with patch("tools.approval.has_blocking_approval", return_value=False):
        result = await runner._handle_approve_command(event)

    assert "(2 commands)" in result
    assert handle.resolve_approval_calls == ["once", "once"]
    adapter.resume_typing_for_chat.assert_called_once_with(source.chat_id)


@pytest.mark.asyncio
async def test_approve_all_keeps_gateway_pending_state_if_worker_still_has_pending_approval(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    handle = _AlwaysPendingApprovalHandle()
    runner._running_agents[session_key] = handle
    runner._pending_approvals[session_key] = {"command": "cmd still pending"}

    adapter = AsyncMock()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters[Platform.TELEGRAM] = adapter

    event = MessageEvent(text="/approve all", source=source)

    with patch("tools.approval.has_blocking_approval", return_value=False):
        result = await runner._handle_approve_command(event)

    assert "(10 commands)" in result
    assert len(handle.resolve_approval_calls) == 10
    assert runner._pending_approvals[session_key] == {"command": "cmd still pending"}


@pytest.mark.asyncio
async def test_approve_all_preserves_pending_state_during_detached_approval_gap(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    runner._running_agents[session_key] = _ApprovalGapHandle()
    runner._pending_approvals[session_key] = {"command": "next approval not surfaced yet"}

    event = MessageEvent(text="/approve all", source=source)

    with patch("tools.approval.has_blocking_approval", return_value=False):
        result = await runner._handle_approve_command(event)

    assert "state was kept" in result
    assert runner._pending_approvals[session_key] == {"command": "next approval not surfaced yet"}


@pytest.mark.asyncio
async def test_deny_all_preserves_pending_state_during_detached_approval_gap(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    runner._running_agents[session_key] = _ApprovalGapHandle()
    runner._pending_approvals[session_key] = {"command": "next approval not surfaced yet"}

    event = MessageEvent(text="/deny all", source=source)

    with patch("tools.approval.has_blocking_approval", return_value=False):
        result = await runner._handle_deny_command(event)

    assert "state was kept" in result
    assert runner._pending_approvals[session_key] == {"command": "next approval not surfaced yet"}


@pytest.mark.asyncio
async def test_deny_command_resolves_detached_worker_pending_approval(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    handle = _FakeConversationWorkerHandle()
    runner._running_agents[session_key] = handle
    runner._pending_approvals[session_key] = {"command": "rm -rf /tmp"}

    adapter = AsyncMock()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters[Platform.TELEGRAM] = adapter

    event = MessageEvent(text="/deny", source=source)

    with patch("tools.approval.has_blocking_approval", return_value=False):
        result = await runner._handle_deny_command(event)

    assert "denied" in result.lower()
    assert handle.resolve_approval_calls == ["deny"]
    adapter.resume_typing_for_chat.assert_called_once_with(source.chat_id)


@pytest.mark.asyncio
async def test_deny_all_resolves_all_detached_worker_pending_approvals(monkeypatch):
    runner = _make_runner()
    source = _make_source()
    session_key = runner._session_key_for_source(source)
    handle = _CountingApprovalHandle(2)
    runner._running_agents[session_key] = handle
    runner._pending_approvals[session_key] = {"command": "rm -rf /tmp"}

    adapter = AsyncMock()
    adapter.resume_typing_for_chat = MagicMock()
    runner.adapters[Platform.TELEGRAM] = adapter

    event = MessageEvent(text="/deny all", source=source)

    with patch("tools.approval.has_blocking_approval", return_value=False):
        result = await runner._handle_deny_command(event)

    assert "(2 commands)" in result
    assert handle.resolve_approval_calls == ["deny", "deny"]
    adapter.resume_typing_for_chat.assert_called_once_with(source.chat_id)


def test_start_gateway_conversation_worker_passes_live_session_env(monkeypatch):
    class _DummyProc:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = []
            self.stderr = []

        def poll(self):
            return None

    class _DummyHandle:
        def __init__(self, *, proc, unit_name, runtime_dir=None, worker_run_id=None, approval_request_callback=None):
            self.proc = proc
            self.unit_name = unit_name
            self.runtime_dir = runtime_dir
            self.worker_run_id = worker_run_id
            self.approval_request_callback = approval_request_callback

    dummy_proc = _DummyProc()
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-123")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-9")
    monkeypatch.setenv("PYTHONPATH", "/tmp/test-path")

    with patch("tools.detached_runtime.popen_transient_unit", return_value=(dummy_proc, "hermes-gwconv-test")) as mock_popen, \
         patch.object(worker_runtime, "GatewayConversationWorkerHandle", _DummyHandle):
        handle = worker_runtime.start_gateway_conversation_worker(request={"message": "hello"})

    assert handle.unit_name == "hermes-gwconv-test"
    extra_env = mock_popen.call_args.kwargs["extra_env"]
    assert extra_env["HERMES_SESSION_PLATFORM"] == "telegram"
    assert extra_env["HERMES_SESSION_CHAT_ID"] == "chat-123"
    assert extra_env["HERMES_SESSION_THREAD_ID"] == "thread-9"
    assert extra_env["HERMES_GWCONV_RUN_ID"]
    assert extra_env["HERMES_GWCONV_RUN_DIR"]
    assert extra_env["PYTHONPATH"] == "/tmp/test-path"
    payload = json.loads(dummy_proc.stdin.getvalue())
    assert payload["worker_unit_name"] == "hermes-gwconv-test"
    assert payload["worker_run_id"] == extra_env["HERMES_GWCONV_RUN_ID"]
    assert payload["worker_run_dir"] == extra_env["HERMES_GWCONV_RUN_DIR"]


def test_handle_recovers_result_from_artifact_when_stdout_result_missing(tmp_path):
    runtime_dir = tmp_path / "gwconv" / "run-1"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "result.json").write_text(json.dumps({
        "final_response": "artifact success",
        "messages": [],
        "api_calls": 2,
        "tools": ["terminal"],
        "terminal_state": "completed",
    }), encoding="utf-8")

    class _Proc:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("Running as unit: hermes-bg-123.service\n")

        def poll(self):
            return 0

    handle = worker_runtime.GatewayConversationWorkerHandle(
        proc=_Proc(),
        unit_name="hermes-gwconv-test",
        runtime_dir=runtime_dir,
        worker_run_id="run-1",
    )

    result = handle.wait_for_result_blocking()

    assert result["final_response"] == "artifact success"
    assert result["failed"] is False
    assert result["terminal_state"] == "completed"


def test_handle_recovers_result_artifact_after_stdout_reader_crashes(tmp_path):
    runtime_dir = tmp_path / "gwconv" / "run-bad-stdout"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "result.json").write_text(json.dumps({
        "final_response": "artifact survived bad stdout",
        "messages": [],
        "api_calls": 3,
        "tools": ["terminal"],
        "terminal_state": "completed",
    }), encoding="utf-8")

    class _BrokenStdout:
        def __iter__(self):
            return self

        def __next__(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    class _Proc:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = _BrokenStdout()
            self.stderr = io.StringIO("")

        def poll(self):
            return None

    handle = worker_runtime.GatewayConversationWorkerHandle(
        proc=_Proc(),
        unit_name="hermes-gwconv-test",
        runtime_dir=runtime_dir,
        worker_run_id="run-bad-stdout",
    )

    result = handle.wait_for_result_blocking()

    assert result["final_response"] == "artifact survived bad stdout"
    assert result["failed"] is False
    assert result["terminal_state"] == "completed"


def test_handle_recovers_error_from_artifact_when_stdout_result_missing(tmp_path):
    runtime_dir = tmp_path / "gwconv" / "run-err"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "error.json").write_text(json.dumps({
        "final_response": "⚠️ Detached gateway worker failed: boom",
        "error": "Detached gateway worker failed: boom",
        "terminal_state": "failed",
        "failure_kind": "worker_error",
    }), encoding="utf-8")

    class _Proc:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def poll(self):
            return 0

    handle = worker_runtime.GatewayConversationWorkerHandle(
        proc=_Proc(),
        unit_name="hermes-gwconv-test",
        runtime_dir=runtime_dir,
        worker_run_id="run-err",
    )

    result = handle.wait_for_result_blocking()

    assert result["failed"] is True
    assert result["terminal_state"] == "failed"
    assert result["failure_kind"] == "worker_error"
    assert "boom" in result["final_response"]


def test_handle_reports_protocol_violation_without_stdout_or_artifact(tmp_path):
    runtime_dir = tmp_path / "gwconv" / "run-missing"
    runtime_dir.mkdir(parents=True)

    class _Proc:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("Running as unit: hermes-bg-noise.service\nreal stderr line\n")

        def poll(self):
            return 0

    handle = worker_runtime.GatewayConversationWorkerHandle(
        proc=_Proc(),
        unit_name="hermes-gwconv-test",
        runtime_dir=runtime_dir,
        worker_run_id="run-missing",
    )

    result = handle.wait_for_result_blocking()

    assert result["failed"] is True
    assert result["terminal_state"] == "protocol_violation"
    assert result["failure_kind"] == "protocol_violation"
    assert "protocol failure" in result["final_response"]
    assert "Running as unit:" not in result["final_response"]
    assert "real stderr line" in result["final_response"]


def test_worker_child_rehydrates_credential_pool_and_swaps_preferred_credential(monkeypatch, tmp_path):
    captured = {}
    emitted = []
    runtime_dir = tmp_path / "gwconv-child"

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs
            self.model = kwargs.get("model")
            self.session_prompt_tokens = 21
            self.session_completion_tokens = 9
            self.context_compressor = types.SimpleNamespace(last_prompt_tokens=34)
            self.tools = []
            self.swapped_entry = None

        def _swap_credential(self, entry):
            self.swapped_entry = entry
            captured["swapped_entry_id"] = getattr(entry, "id", None)

        def run_conversation(self, message, conversation_history=None, task_id=None):
            captured["run_message"] = message
            return {
                "final_response": "pool worker ok",
                "completed": True,
                "api_calls": 1,
                "messages": [],
            }

    class _FakePool:
        def __init__(self):
            self.provider = "custom:team-a"
            self._entry = types.SimpleNamespace(id="cred-42")
            self.release_calls = []
            self.acquire_calls = []

        def has_credentials(self):
            return True

        def acquire_lease(self, credential_id=None):
            self.acquire_calls.append(credential_id)
            return credential_id or self._entry.id

        def current(self):
            return self._entry

        def peek(self):
            return self._entry

        def release_lease(self, credential_id):
            self.release_calls.append(credential_id)
            captured["released_credential_id"] = credential_id

    fake_pool = _FakePool()
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _FakeAgent
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = lambda: object()
    fake_credential_pool = types.ModuleType("agent.credential_pool")
    fake_credential_pool.load_pool = lambda provider: fake_pool

    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_credential_pool)
    monkeypatch.setattr(worker_runtime, "_emit_message", lambda payload: emitted.append(payload))
    monkeypatch.setattr("tools.terminal_tool.set_approval_callback", lambda cb: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "message": "delegate goal",
        "conversation_history": [],
        "model": "worker-model",
        "enabled_toolsets": ["file"],
        "session_id": "delegate-0",
        "platform": "telegram",
        "with_session_db": False,
        "credential_pool_provider": "custom:team-a",
        "preferred_credential_id": "cred-42",
        "worker_run_id": "gwconv-child-test",
        "worker_run_dir": str(runtime_dir),
        "worker_unit_name": "hermes-gwconv-child-test",
        "runtime_kwargs": {
            "base_url": "https://example.invalid/v1",
            "api_key": "***",
            "provider": "custom",
            "api_mode": "chat_completions",
        },
    }) + "\n"))

    exit_code = worker_runtime._run_child()

    assert exit_code == 0
    assert captured["init_kwargs"]["credential_pool"] is fake_pool
    assert fake_pool.acquire_calls == ["cred-42"]
    assert captured["swapped_entry_id"] == "cred-42"
    assert captured["released_credential_id"] == "cred-42"
    assert emitted[-1]["type"] == "result"
    assert emitted[-1]["result"]["final_response"] == "pool worker ok"
    artifact = json.loads((runtime_dir / "result.json").read_text(encoding="utf-8"))
    assert artifact["final_response"] == "pool worker ok"
    assert artifact["worker_unit_name"] == "hermes-gwconv-child-test"
