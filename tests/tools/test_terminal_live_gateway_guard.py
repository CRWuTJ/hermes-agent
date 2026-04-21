"""Tests for blocking live gateway service control from messaging/origin sessions."""

import json
from unittest.mock import patch

import tools.terminal_tool as terminal_mod


class TestBlockLiveGatewayServiceControl:
    def test_blocks_systemctl_restart_in_messaging_session(self):
        with patch.dict("os.environ", {"HERMES_SESSION_PLATFORM": "telegram"}, clear=False):
            message = terminal_mod._block_live_gateway_service_control(
                "systemctl restart hermes-gateway.service"
            )
        assert message is not None
        assert "Blocked" in message
        assert "live Hermes gateway service control" in message

    def test_blocks_cli_gateway_restart_for_origin_cron_delivery(self):
        with patch.dict(
            "os.environ",
            {
                "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
                "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "6141351975",
            },
            clear=False,
        ):
            message = terminal_mod._block_live_gateway_service_control(
                "python -m hermes_cli.main gateway restart --system"
            )
        assert message is not None
        assert "pending live reload" in message

    def test_does_not_block_gateway_status(self):
        with patch.dict("os.environ", {"HERMES_SESSION_PLATFORM": "telegram"}, clear=False):
            message = terminal_mod._block_live_gateway_service_control(
                "systemctl status hermes-gateway --no-pager"
            )
        assert message is None

    def test_does_not_block_when_not_in_messaging_or_origin_context(self):
        with patch.dict("os.environ", {}, clear=True):
            message = terminal_mod._block_live_gateway_service_control(
                "systemctl restart hermes-gateway.service"
            )
        assert message is None

    def test_terminal_tool_returns_blocked_before_env_setup(self):
        with patch.dict("os.environ", {"HERMES_SESSION_PLATFORM": "telegram"}, clear=False), \
             patch.object(terminal_mod, "_get_env_config", side_effect=AssertionError("should not load env config")):
            result = json.loads(
                terminal_mod.terminal_tool("python -m hermes_cli.main gateway restart --system")
            )
        assert result["status"] == "blocked"
        assert result["exit_code"] == -1
        assert "live Hermes gateway service control" in result["error"]
