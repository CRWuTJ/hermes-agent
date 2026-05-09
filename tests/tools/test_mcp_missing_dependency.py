import asyncio
import sys

import pytest


def test_stdio_server_reports_missing_mcp_sdk(monkeypatch):
    import tools.mcp_tool as mcp_mod

    monkeypatch.setattr(mcp_mod, "_MCP_AVAILABLE", False)
    monkeypatch.delattr(mcp_mod, "StdioServerParameters", raising=False)
    server = mcp_mod.MCPServerTask("obsidian")

    with pytest.raises(ImportError, match="MCP SDK is not installed"):
        asyncio.run(server._run_stdio({"command": sys.executable, "args": []}))
