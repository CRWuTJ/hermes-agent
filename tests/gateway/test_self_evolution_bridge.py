from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.run import _parse_natural_dispatch
from gateway.self_evolution_bridge import parse_self_evolution_request, self_evolution_enabled


def _make_event(text: str = "") -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )
    return MessageEvent(text=text, source=source, message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._background_tasks = set()
    runner._managed_runtime_tasks = {}
    runner._last_natural_task_by_session = {}
    runner._session_db = None
    runner.session_store = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._topic_routing = {"enabled": False}
    runner._update_prompt_pending = {}
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    return runner


def test_self_evolution_enabled_reads_config():
    assert self_evolution_enabled({"self_evolution": {"enabled": True}}) is True
    assert self_evolution_enabled({"self_evolution": {"enabled": False}}) is False


def test_parse_self_evolution_request_recognizes_user_language():
    assert parse_self_evolution_request("自己反思一下系统怎么优化") == {"action": "self_evolution", "mode": "manual"}
    assert parse_self_evolution_request("系统进化部跑一轮") == {"action": "self_evolution", "mode": "manual"}
    assert parse_self_evolution_request("我们继续讨论方案") is None


def test_natural_dispatch_routes_self_evolution_request():
    dispatch = _parse_natural_dispatch(
        "你来替我规划系统优化",
        {"natural_dispatch": {"enabled": True}, "self_evolution": {"enabled": True}},
    )

    assert dispatch == {"action": "self_evolution", "mode": "manual"}


@pytest.mark.asyncio
async def test_handle_natural_self_evolution_runs_bridge_without_background_task():
    runner = _make_runner()
    runner._handle_background_command = AsyncMock(return_value="should not run")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}, "self_evolution": {"enabled": True}}), \
         patch("gateway.self_evolution_bridge.run_self_evolution_for_gateway", return_value="Result:\n系统进化部已完成一轮只读审查。") as run_review:
        result = await runner._handle_natural_dispatch(_make_event("自己反思一下系统怎么优化"))

    assert "系统进化部已完成" in result
    run_review.assert_called_once()
    runner._handle_background_command.assert_not_awaited()
