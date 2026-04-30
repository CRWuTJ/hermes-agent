import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.run import _natural_dispatch_enabled, _parse_natural_dispatch


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


def test_natural_dispatch_enabled_reads_config():
    assert _natural_dispatch_enabled({"natural_dispatch": {"enabled": True}}) is True
    assert _natural_dispatch_enabled({"natural_dispatch": {"enabled": False}}) is False


def test_parse_background_prefix_with_colon():
    dispatch = _parse_natural_dispatch("后台执行：检查 Hermes 状态")

    assert dispatch == {"action": "background", "prompt": "检查 Hermes 状态"}


def test_parse_background_verb_keeps_user_intent():
    dispatch = _parse_natural_dispatch("后台调研一下 gateway 队列")

    assert dispatch == {"action": "background", "prompt": "调研一下 gateway 队列"}


def test_parse_tasks_status_request():
    dispatch = _parse_natural_dispatch("看一下后台任务")

    assert dispatch == {"action": "tasks"}


def test_parse_image_generation_request_routes_to_image_tool():
    dispatch = _parse_natural_dispatch("生成一张详细介绍这个系统具体情况的图片，用小白容易理解的方式描述。")

    assert dispatch == {
        "action": "image_generate",
        "prompt": "生成一张详细介绍这个系统具体情况的图片，用小白容易理解的方式描述。",
    }


def test_parse_video_generation_request_routes_to_video_tool():
    dispatch = _parse_natural_dispatch("生成一个机器人穿过雨夜街道的短视频")

    assert dispatch == {
        "action": "video_generate",
        "prompt": "生成一个机器人穿过雨夜街道的短视频",
    }


def test_parse_cancel_task_request():
    dispatch = _parse_natural_dispatch("取消任务 bg_123abc")

    assert dispatch == {"action": "task", "task_id": "bg_123abc", "task_action": "cancel"}


def test_parse_self_arranged_background_request():
    dispatch = _parse_natural_dispatch("你自己安排后台执行：排查 gateway 卡顿")

    assert dispatch == {"action": "background", "prompt": "排查 gateway 卡顿"}


def test_parse_configured_background_prefix():
    dispatch = _parse_natural_dispatch(
        "异步处理：检查模型链路",
        {"natural_dispatch": {"background_prefixes": ["异步处理"]}},
    )

    assert dispatch == {"action": "background", "prompt": "检查模型链路"}


def test_parse_clear_work_request_defaults_to_background():
    dispatch = _parse_natural_dispatch("帮我排查为什么 gateway 偶尔卡住，别断当前会话")

    assert dispatch == {"action": "background", "prompt": "排查为什么 gateway 偶尔卡住，别断当前会话"}


def test_parse_read_only_work_request_adds_guardrail():
    dispatch = _parse_natural_dispatch("这个先别改，只查 Hermes gateway 队列")

    assert dispatch == {
        "action": "background",
        "prompt": "只读检查 Hermes gateway 队列；不要改文件，不要重启服务。",
    }


def test_parse_foreground_handoff_continues_work_in_background():
    dispatch = _parse_natural_dispatch("先简单回复，然后后台继续检查这个问题")

    assert dispatch == {"action": "background", "prompt": "继续检查这个问题"}


@pytest.mark.parametrize(
    ("text", "prompt"),
    [
        ("继续检查这个问题", "继续检查这个问题"),
        ("继续优化", "继续优化"),
        ("继续优化这个流程", "继续优化这个流程"),
        ("继续补完", "继续补完"),
    ],
)
def test_parse_direct_continuation_work_defaults_to_background(text, prompt):
    dispatch = _parse_natural_dispatch(text)

    assert dispatch == {"action": "background", "prompt": prompt}


@pytest.mark.parametrize(
    ("text", "prompt"),
    [
        ("帮我改代码并跑测试", "改代码并跑测试"),
        ("请你补完这个调度模式", "补完这个调度模式"),
        ("把这个功能补完", "把这个功能补完"),
        ("修一下这个问题并验证", "修一下这个问题并验证"),
        ("修复失败用例并跑相关测试", "修复失败用例并跑相关测试"),
        ("处理一下这个问题并验证", "处理一下这个问题并验证"),
        ("按刚才的方案落地并验证", "按刚才的方案落地并验证"),
        ("根据上面的方案实现这个能力并跑测试", "根据上面的方案实现这个能力并跑测试"),
        ("把剩下的收尾做完", "把剩下的收尾做完"),
    ],
)
def test_parse_ordinary_complex_work_defaults_to_background(text, prompt):
    dispatch = _parse_natural_dispatch(text)

    assert dispatch == {"action": "background", "prompt": prompt}


@pytest.mark.parametrize(
    "text",
    [
        "帮我处理一下这个",
        "弄一下这个",
        "做一下这个",
        "继续处理",
    ],
)
def test_parse_ambiguous_work_request_asks_for_clarification(text):
    dispatch = _parse_natural_dispatch(text)

    assert dispatch == {"action": "clarify", "prompt": text}


def test_parse_task_detail_request():
    dispatch = _parse_natural_dispatch("看任务 bg_123abc 的情况")

    assert dispatch == {"action": "task", "task_id": "bg_123abc", "task_action": ""}


def test_parse_task_foreground_request():
    dispatch = _parse_natural_dispatch("把任务 bg_123abc 优先做")

    assert dispatch == {"action": "task", "task_id": "bg_123abc", "task_action": "foreground"}


def test_parse_task_later_request():
    dispatch = _parse_natural_dispatch("任务 bg_123abc 晚点做")

    assert dispatch == {"action": "task", "task_id": "bg_123abc", "task_action": "later"}


def test_parse_latest_task_cancel_request():
    dispatch = _parse_natural_dispatch("刚才那个先停")

    assert dispatch == {"action": "task_latest", "task_action": "cancel"}


def test_parse_plain_chat_returns_none():
    assert _parse_natural_dispatch("我们继续讨论方案") is None
    assert _parse_natural_dispatch("你先分析方案，不要执行") is None
    assert _parse_natural_dispatch("检查一下这个方案是否合理") is None
    assert _parse_natural_dispatch("后台数据怎么同步") is None


@pytest.mark.asyncio
async def test_handle_natural_image_dispatch_returns_media_path():
    runner = _make_runner()
    event = _make_event("生成一张蓝色机器人图片")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}), \
         patch("tools.image_generation_tool.image_generate_tool", return_value='{"success": true, "media_path": "/tmp/hermes-image.png", "image": "/tmp/hermes-image.png", "provider": "openai"}'):
        result = await runner._handle_natural_dispatch(event)

    assert "Result:" in result
    assert "MEDIA:/tmp/hermes-image.png" in result


@pytest.mark.asyncio
async def test_handle_natural_video_dispatch_returns_media_path():
    runner = _make_runner()
    event = _make_event("生成一个蓝色机器人短视频")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}), \
         patch("tools.video_generation_tool.video_generate_tool", return_value='{"success": true, "media_path": "/tmp/hermes-video.mp4", "video": "/tmp/hermes-video.mp4", "provider": "fal"}'):
        result = await runner._handle_natural_dispatch(event)

    assert "Result:" in result
    assert "MEDIA:/tmp/hermes-video.mp4" in result


@pytest.mark.asyncio
async def test_handle_message_routes_direct_continuation_work_to_background_without_agent():
    runner = _make_runner()
    runner._handle_background_command = AsyncMock(
        return_value='🔄 Background task started: "继续检查这个问题"\nTask ID: bg_direct123\nYou can keep chatting.'
    )
    runner._handle_message_with_agent = AsyncMock(return_value="foreground agent ran")
    event = _make_event("继续检查这个问题")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}):
        result = await runner._handle_message(event)

    assert "已放到后台" in result
    assert "bg_direct123" in result
    assert event.text == "继续检查这个问题"
    runner._handle_background_command.assert_awaited_once()
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_natural_background_starts_background_task():
    runner = _make_runner()
    created_tasks = []

    def capture_task(coro, *args, **kwargs):
        coro.close()
        task = MagicMock()
        created_tasks.append(task)
        return task

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}), \
         patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        result = await runner._handle_natural_dispatch(_make_event("后台执行：检查模型链路"))

    assert "已放到后台" in result
    assert "检查模型链路" in result
    assert "bg_" in result
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_handle_natural_background_returns_delegate_failure_without_success_ack():
    runner = _make_runner()
    runner._handle_background_command = AsyncMock(return_value="Usage: /background <prompt>")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}):
        result = await runner._handle_natural_dispatch(_make_event("后台执行：检查模型链路"))

    assert result == "Usage: /background <prompt>"
    assert "已放到后台" not in result


@pytest.mark.asyncio
async def test_handle_natural_background_uses_configured_prefix():
    runner = _make_runner()
    created_tasks = []

    def capture_task(coro, *args, **kwargs):
        coro.close()
        task = MagicMock()
        created_tasks.append(task)
        return task

    config = {"natural_dispatch": {"enabled": True, "background_prefixes": ["异步处理"]}}
    with patch("gateway.run._load_gateway_config", return_value=config), \
         patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        result = await runner._handle_natural_dispatch(_make_event("异步处理：检查模型链路"))

    assert "已放到后台" in result
    assert "检查模型链路" in result
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_handle_natural_foreground_handoff_starts_background_task():
    runner = _make_runner()
    created_tasks = []

    def capture_task(coro, *args, **kwargs):
        coro.close()
        task = MagicMock()
        created_tasks.append(task)
        return task

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}), \
         patch("gateway.run.asyncio.create_task", side_effect=capture_task):
        result = await runner._handle_natural_dispatch(_make_event("先简单回复，然后后台继续检查这个问题"))

    assert "已放到后台" in result
    assert "继续检查这个问题" in result
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_handle_natural_tasks_calls_tasks_handler():
    runner = _make_runner()
    runner._handle_tasks_command = AsyncMock(return_value="任务列表")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}):
        result = await runner._handle_natural_dispatch(_make_event("现在有哪些任务"))

    assert result == "任务列表"
    runner._handle_tasks_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_ambiguous_natural_work_asks_clarification_without_background():
    runner = _make_runner()
    runner._handle_background_command = AsyncMock(return_value="should not run")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}):
        result = await runner._handle_natural_dispatch(_make_event("帮我处理一下这个"))

    assert "我理解你想让我处理一个任务" in result
    assert "要处理哪个对象" in result
    assert "能不能改" in result
    runner._handle_background_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_natural_cancel_rewrites_to_task_command():
    runner = _make_runner()
    rewritten_texts = []

    async def capture_task_command(event):
        rewritten_texts.append(event.text)
        return "已取消"

    runner._handle_task_command = AsyncMock(side_effect=capture_task_command)
    event = _make_event("停掉任务 bg_456def")

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}):
        result = await runner._handle_natural_dispatch(event)

    assert result == "已取消"
    assert rewritten_texts == ["/task bg_456def cancel"]
    assert event.text == "停掉任务 bg_456def"
    runner._handle_task_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_natural_latest_cancel_uses_last_background_task():
    runner = _make_runner()
    rewritten_texts = []

    async def capture_task_command(event):
        rewritten_texts.append(event.text)
        return "已请求取消"

    runner._handle_task_command = AsyncMock(side_effect=capture_task_command)
    event = _make_event("刚才那个先停")
    runner._last_natural_task_by_session = {runner._natural_task_session_key(event.source): "bg_last123"}

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}):
        result = await runner._handle_natural_dispatch(event)

    assert result == "已请求取消"
    assert rewritten_texts == ["/task bg_last123 cancel"]
    assert event.text == "刚才那个先停"
    runner._handle_task_command.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_natural_latest_cancel_without_memory_asks_for_task_id():
    runner = _make_runner()

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": True}}):
        result = await runner._handle_natural_dispatch(_make_event("刚才那个先停"))

    assert "不知道你指哪个任务" in result
    assert "任务号" in result


@pytest.mark.asyncio
async def test_handle_natural_dispatch_disabled_returns_none():
    runner = _make_runner()

    with patch("gateway.run._load_gateway_config", return_value={"natural_dispatch": {"enabled": False}}):
        result = await runner._handle_natural_dispatch(_make_event("后台执行：检查模型链路"))

    assert result is None
