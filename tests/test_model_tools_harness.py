from unittest.mock import MagicMock, patch

from model_tools import handle_function_call


def test_handle_function_call_records_harness_events_when_enabled():
    manager = MagicMock()
    manager.enabled = True
    manager.preflight_tool_call.return_value = {"allowed": True}

    with (
        patch("model_tools.registry.dispatch", return_value='{"ok":true}'),
        patch("agent.harness.get_harness_manager", return_value=manager),
    ):
        result = handle_function_call(
            "web_search",
            {"q": "test"},
            task_id="task-1",
            tool_call_id="call-1",
            session_id="session-1",
        )

    assert result == '{"ok":true}'
    manager.preflight_tool_call.assert_called_once()
    manager.record_tool_start.assert_called_once()
    manager.record_tool_complete.assert_called_once()


def test_handle_function_call_returns_harness_block_when_preflight_denies():
    manager = MagicMock()
    manager.enabled = True
    manager.preflight_tool_call.return_value = {
        "allowed": False,
        "reason": "blocked by harness",
        "state": "planning",
    }

    with (
        patch("model_tools.registry.dispatch") as mock_dispatch,
        patch("agent.harness.get_harness_manager", return_value=manager),
    ):
        result = handle_function_call(
            "web_search",
            {"q": "test"},
            task_id="task-1",
            tool_call_id="call-1",
            session_id="session-1",
        )

    mock_dispatch.assert_not_called()
    manager.record_tool_start.assert_not_called()
    manager.record_tool_complete.assert_not_called()
    assert "blocked by harness" in result
    assert "planning" in result
