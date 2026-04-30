from types import SimpleNamespace
import time

import pytest

from run_agent import AIAgent


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def close(self):
        pass


def _make_agent(monkeypatch):
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", _FakeOpenAI)
    return AIAgent(
        api_key="test",
        base_url="http://127.0.0.1:9999/v1",
        provider="custom",
        model="test-model",
        max_iterations=1,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def test_interruptible_api_call_enforces_hard_wall_timeout(monkeypatch):
    """A stuck non-streaming SDK call should be cut off by Hermes' own watchdog."""
    agent = _make_agent(monkeypatch)
    monkeypatch.setenv("HERMES_API_HARD_TIMEOUT", "0.05")

    class _BlockingCompletions:
        def create(self, **kwargs):
            time.sleep(0.2)
            return SimpleNamespace(choices=[])

    request_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_BlockingCompletions())
    )
    close_reasons = []
    replaced_reasons = []

    monkeypatch.setattr(
        agent,
        "_create_request_openai_client",
        lambda *, reason: request_client,
    )
    monkeypatch.setattr(
        agent,
        "_close_request_openai_client",
        lambda client, *, reason: close_reasons.append(reason),
    )
    monkeypatch.setattr(
        agent,
        "_replace_primary_openai_client",
        lambda *, reason: replaced_reasons.append(reason) or True,
    )

    start = time.monotonic()
    with pytest.raises(TimeoutError, match="exceeded hard timeout"):
        agent._interruptible_api_call({"model": "test-model", "messages": [], "timeout": 30.0})
    elapsed = time.monotonic() - start

    assert elapsed < 0.18
    assert "hard_timeout_abort" in close_reasons
    assert "api_hard_timeout" in replaced_reasons


def test_interruptible_streaming_api_call_enforces_hard_wall_timeout(monkeypatch):
    """A stuck streaming SDK call should stop even if socket close cannot unblock it."""
    agent = _make_agent(monkeypatch)
    monkeypatch.setenv("HERMES_API_HARD_TIMEOUT", "0.05")
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "30")

    class _BlockingCompletions:
        def create(self, **kwargs):
            time.sleep(0.2)
            return iter(())

    request_client = SimpleNamespace(
        chat=SimpleNamespace(completions=_BlockingCompletions())
    )
    close_reasons = []
    replaced_reasons = []

    monkeypatch.setattr(
        agent,
        "_create_request_openai_client",
        lambda *, reason: request_client,
    )
    monkeypatch.setattr(
        agent,
        "_close_request_openai_client",
        lambda client, *, reason: close_reasons.append(reason),
    )
    monkeypatch.setattr(
        agent,
        "_replace_primary_openai_client",
        lambda *, reason: replaced_reasons.append(reason) or True,
    )
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda api_kwargs: SimpleNamespace(choices=[]),
    )

    start = time.monotonic()
    with pytest.raises(TimeoutError, match="exceeded hard timeout"):
        agent._interruptible_streaming_api_call({"model": "test-model", "messages": [], "timeout": 30.0})
    elapsed = time.monotonic() - start

    assert elapsed < 0.18
    assert "hard_timeout_abort" in close_reasons
    assert "api_hard_timeout" in replaced_reasons
