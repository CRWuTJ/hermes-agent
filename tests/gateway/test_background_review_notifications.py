"""Tests for gateway background review delivery controls."""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.session import SessionSource


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class BackgroundReviewAgent:
    def __init__(self, **kwargs):
        self.background_review_callback = None
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if self.background_review_callback:
            self.background_review_callback("💾 User profile updated")
        return {
            "final_response": "done",
            "messages": [{"role": "assistant", "content": "done"}],
            "api_calls": 1,
            "interrupted": False,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    return runner


def _install_fakes(monkeypatch):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = BackgroundReviewAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


@pytest.mark.asyncio
async def test_background_review_notifications_default_to_off(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "display:\n  tool_progress: off\n",
        encoding="utf-8",
    )
    _install_fakes(monkeypatch)

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-bg-review-default",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "done"
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_background_review_notifications_send_when_enabled(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "display:\n  tool_progress: off\n  background_review_notifications: true\n",
        encoding="utf-8",
    )
    _install_fakes(monkeypatch)

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
        thread_id=None,
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-bg-review-enabled",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["final_response"] == "done"
    assert adapter.sent == [
        {
            "chat_id": "12345",
            "content": "💾 User profile updated",
            "reply_to": None,
            "metadata": None,
        }
    ]
