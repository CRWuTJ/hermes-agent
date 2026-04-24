"""Tests for terminal stdin support."""

import json

import tools.terminal_tool as terminal_mod


def test_terminal_tool_passes_stdin_to_foreground_command(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "15")
    monkeypatch.setenv("TERMINAL_WORKDIR", str(tmp_path))

    result = json.loads(
        terminal_mod.terminal_tool(
            "bash -s",
            stdin="cat <<'H_EOF'\nterminal stdin body\nH_EOF\n",
            task_id="terminal-stdin-test",
        )
    )

    try:
        assert result["exit_code"] == 0
        assert result["output"] == "terminal stdin body"
    finally:
        terminal_mod.cleanup_vm("terminal-stdin-test")


def test_terminal_tool_rejects_stdin_for_background_command(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_WORKDIR", str(tmp_path))

    result = json.loads(
        terminal_mod.terminal_tool(
            "cat",
            background=True,
            stdin="payload",
            task_id="terminal-stdin-background-test",
        )
    )

    try:
        assert result["status"] == "blocked"
        assert result["exit_code"] == -1
        assert "foreground" in result["error"]
    finally:
        terminal_mod.cleanup_vm("terminal-stdin-background-test")
