"""Tests for Telegram natural foreground/background admission control."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_cli.config import DEFAULT_CONFIG


def _source(platform=Platform.TELEGRAM):
    return SessionSource(
        platform=platform,
        user_id="12345",
        chat_id="67890",
        user_name="tester",
        chat_type="dm",
    )


def _event(text: str, platform=Platform.TELEGRAM):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(platform))


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = {"natural_dispatch": {"enabled": True}}
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._update_prompt_pending = {}
    runner._topic_routing = {"enabled": False}
    runner._draining = False
    runner.session_store = None
    runner._is_user_authorized = lambda _source: True
    runner._handle_background_command = AsyncMock(return_value="Background task started: natural dispatch")
    runner._handle_message_with_agent = AsyncMock(return_value="foreground response")
    return runner


class _TranscriptSessionStore:
    def __init__(self, history):
        self.history = history

    def _generate_session_key(self, _source):
        return "telegram:dm:12345"

    def get_or_create_session(self, _source):
        return SimpleNamespace(session_id="session-current", session_key="telegram:dm:12345")

    def load_transcript(self, session_id):
        assert session_id == "session-current"
        return list(self.history)


def test_default_config_defines_natural_dispatch_enabled_flag():
    assert isinstance(DEFAULT_CONFIG["natural_dispatch"]["enabled"], bool)
    assert DEFAULT_CONFIG["natural_dispatch"]["enabled"] is True


def test_gateway_config_object_enables_natural_dispatch_from_dict():
    from gateway.natural_dispatch import classify_natural_dispatch

    config = GatewayConfig.from_dict({"natural_dispatch": {"enabled": True}})
    decision = classify_natural_dispatch("继续优化这个实现", config)

    assert decision.should_background is True


def test_load_gateway_config_maps_natural_dispatch_from_config_yaml(tmp_path, monkeypatch):
    from gateway.config import load_gateway_config
    from gateway.natural_dispatch import classify_natural_dispatch

    (tmp_path / "config.yaml").write_text("natural_dispatch:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = load_gateway_config()
    decision = classify_natural_dispatch("继续优化这个实现", config)

    assert config.natural_dispatch["enabled"] is True
    assert decision.should_background is True


@pytest.mark.parametrize(
    "message",
    [
        "后台帮我跑完整测试",
        "继续优化这个实现",
        "请修复这个 bug 并运行测试",
        "实现 admission control 并验证",
        "跑一次 smoke test 并检查结果",
        "分步骤检查、修改、测试并给我结论",
    ],
)
def test_complex_messages_route_to_background(message):
    from gateway.natural_dispatch import classify_natural_dispatch

    decision = classify_natural_dispatch(message, {"natural_dispatch": {"enabled": True}})

    assert decision.should_background is True
    assert decision.reason


@pytest.mark.parametrize(
    "message",
    [
        "生成视频：猫在火星上散步",
        "生成一个机器人短视频",
        "制作 10 秒产品介绍视频",
        "生成图片：赛博朋克猫",
        "帮我做一张产品海报图",
        "draw an image of a robot assistant",
        "出图啊",
        "图呢？",
        "给我图",
    ],
)
def test_media_generation_stays_foreground_for_native_attachment_path(message):
    from gateway.natural_dispatch import classify_natural_dispatch

    decision = classify_natural_dispatch(message, {"natural_dispatch": {"enabled": True}})

    assert decision.should_background is False
    assert decision.reason == "media_generation"


def test_bare_continuation_routes_to_background_only_with_technical_context():
    from gateway.natural_dispatch import classify_natural_dispatch

    config = {"natural_dispatch": {"enabled": True}}

    without_context = classify_natural_dispatch("继续", config)
    casual_context = classify_natural_dispatch(
        "继续",
        config,
        context=["我们刚才在聊晚饭和旅行安排。"],
    )
    with_context = classify_natural_dispatch(
        "继续",
        config,
        context=[
            "Root cause: Telegram gateway stability foreground guard needs debugging.",
            "Next step: run pytest and verify gateway/natural_dispatch.py.",
        ],
    )

    assert without_context.should_background is False
    assert casual_context.should_background is False
    assert with_context.should_background is True
    assert with_context.reason == "continuation_context"


def test_execution_acceptance_routes_to_background_only_with_technical_context():
    from gateway.natural_dispatch import classify_natural_dispatch

    config = {"natural_dispatch": {"enabled": True}}
    message = "同意，按推荐方案执行"

    without_context = classify_natural_dispatch(message, config)
    casual_context = classify_natural_dispatch(
        message,
        config,
        context=["我们刚才在聊明天去哪家餐厅。"],
    )
    with_context = classify_natural_dispatch(
        message,
        config,
        context=[
            "方案：先补 gateway natural dispatch 测试，再改路由。",
            "Next step: implement the smallest code change and run pytest.",
        ],
    )

    assert without_context.should_background is False
    assert casual_context.should_background is False
    assert with_context.should_background is True
    assert with_context.reason == "accepted_execution_context"


@pytest.mark.parametrize(
    "message",
    [
        "这个方案可以执行吗？",
        "how should we proceed",
        "should we proceed",
        "can we proceed",
    ],
)
def test_execution_acceptance_question_stays_foreground_even_with_technical_context(message):
    from gateway.natural_dispatch import classify_natural_dispatch

    decision = classify_natural_dispatch(
        message,
        {"natural_dispatch": {"enabled": True}},
        context=[
            "方案：补路由测试并修改 gateway natural_dispatch。",
            "Next step: implement and run pytest.",
        ],
    )

    assert decision.should_background is False


@pytest.mark.parametrize(
    "message",
    [
        "我们继续讨论方案",
        "你先分析方案，不要执行",
        "检查一下这个方案是否合理",
        "我有个想法，想让 Hermes 帮我做 AI 老师兼助手，你觉得怎么推进？",
        "后台数据怎么同步",
        "现在能生成视频了吗",
        "你先分析怎么生成图片，不要执行",
        "这个 bug 应该怎么修复？",
        "how should we fix this bug?",
    ],
)
def test_lightweight_discussion_stays_foreground(message):
    from gateway.natural_dispatch import classify_natural_dispatch

    decision = classify_natural_dispatch(message, {"natural_dispatch": {"enabled": True}})

    assert decision.should_background is False


def test_gateway_handler_routes_bare_continuation_with_technical_transcript_to_background():
    runner = _runner()
    runner.session_store = _TranscriptSessionStore(
        [
            {"role": "user", "content": "检查 Telegram gateway stability 的 foreground guard"},
            {"role": "assistant", "content": "Root cause found; next step is pytest verification."},
        ]
    )
    event = _event("继续")

    result = asyncio.run(runner._handle_message(event))

    assert "Background task started" in result
    runner._handle_background_command.assert_awaited_once()
    background_event = runner._handle_background_command.await_args.args[0]
    assert background_event.text.startswith("/background ")
    assert "Continue the active Telegram workstream" in background_event.get_command_args()
    assert "pytest verification" in background_event.get_command_args()
    runner._handle_message_with_agent.assert_not_awaited()


def test_gateway_handler_starts_background_and_returns_immediately_for_complex_message():
    runner = _runner()
    event = _event("继续优化这个实现并运行测试")

    result = asyncio.run(runner._handle_message(event))

    assert "Background task started" in result
    runner._handle_background_command.assert_awaited_once()
    background_event = runner._handle_background_command.await_args.args[0]
    assert background_event.text.startswith("/background ")
    assert "继续优化这个实现" in background_event.get_command_args()
    runner._handle_message_with_agent.assert_not_awaited()
