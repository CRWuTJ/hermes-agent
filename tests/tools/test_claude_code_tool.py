import json
import subprocess

import pytest

from tools import claude_code_tool


def test_claude_code_task_runs_json_lane(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, cwd, env, text, stdout, stderr, timeout):
        captured.update(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "text": text,
                "stdout": stdout,
                "stderr": stderr,
                "timeout": timeout,
            }
        )
        payload = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "CLAUDE_TOOL_OK",
            "session_id": "abc",
            "num_turns": 1,
            "duration_ms": 123,
            "modelUsage": {"gpt-5.5": {"inputTokens": 1, "outputTokens": 1}},
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload))

    monkeypatch.setenv("UNRELATED_SECRET", "do-not-pass-to-child")
    monkeypatch.setattr(claude_code_tool.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_code_tool, "_resolve_token", lambda: "secret-token")
    monkeypatch.setattr(claude_code_tool.subprocess, "run", fake_run)

    result = json.loads(
        claude_code_tool.claude_code_task(
            "review this",
            workdir=str(tmp_path),
            mode="review",
            max_turns=2,
            timeout=30,
        )
    )

    assert result["success"] is True
    assert result["result"] == "CLAUDE_TOOL_OK"
    assert result["model"] == "gpt-5.5"
    assert result["mode"] == "review"
    assert result["allowed_tools"].startswith("Read")
    assert result["env"]["ANTHROPIC_AUTH_TOKEN"] == "<redacted>"
    assert captured["cwd"] == str(tmp_path)
    assert captured["timeout"] == 30
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"
    assert captured["env"]["ANTHROPIC_MODEL"] == "gpt-5.5"
    assert captured["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "gpt-5.5"
    assert "UNRELATED_SECRET" not in captured["env"]
    allowed_tools = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
    assert "pytest" not in allowed_tools
    assert captured["cmd"][:3] == ["claude", "-p", "review this"]
    assert "--output-format" in captured["cmd"]
    assert "json" in captured["cmd"]
    assert "--no-session-persistence" in captured["cmd"]


def test_claude_code_task_rejects_allowed_tools_override(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_code_tool.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_code_tool, "_resolve_token", lambda: "secret-token")
    monkeypatch.delenv("CLAUDE_GPT_ALLOW_EDIT_TOOL_OVERRIDE", raising=False)

    result = json.loads(
        claude_code_tool.claude_code_task(
            "plan this",
            workdir=str(tmp_path),
            mode="plan",
            allowed_tools="Read,Edit",
        )
    )

    assert result["error"]
    assert "allowed_tools overrides are disabled" in result["error"]


def test_claude_code_task_reports_json_error(monkeypatch, tmp_path):
    def fake_run(cmd, cwd, env, text, stdout, stderr, timeout):
        payload = {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "terminal_reason": "max_turns",
            "errors": ["Reached maximum number of turns (1)"],
        }
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(payload))

    monkeypatch.setattr(claude_code_tool.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_code_tool, "_resolve_token", lambda: "secret-token")
    monkeypatch.setattr(claude_code_tool.subprocess, "run", fake_run)

    result = json.loads(
        claude_code_tool.claude_code_task(
            "read note",
            workdir=str(tmp_path),
            mode="review",
            max_turns=1,
        )
    )

    assert result["success"] is False
    assert result["is_error"] is True
    assert result["subtype"] == "error_max_turns"
    assert result["terminal_reason"] == "max_turns"
    assert result["process_exit"] == 1
    assert result["errors"] == ["Reached maximum number of turns (1)"]


def test_claude_code_task_rejects_result_json_without_is_error(monkeypatch, tmp_path):
    def fake_run(cmd, cwd, env, text, stdout, stderr, timeout):
        payload = {"type": "result", "subtype": "success", "result": "OK"}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload))

    monkeypatch.setattr(claude_code_tool.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_code_tool, "_resolve_token", lambda: "secret-token")
    monkeypatch.setattr(claude_code_tool.subprocess, "run", fake_run)

    result = json.loads(
        claude_code_tool.claude_code_task(
            "read note",
            workdir=str(tmp_path),
            mode="review",
            max_turns=1,
        )
    )

    assert result["success"] is False
    assert result["error"] == "Claude Code returned invalid result JSON"


def test_claude_code_task_redacts_token_from_error_output(monkeypatch, tmp_path):
    def fake_run(cmd, cwd, env, text, stdout, stderr, timeout):
        payload = {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "leaked secret-token",
            "errors": ["secret-token in error"],
        }
        return subprocess.CompletedProcess(cmd, 1, stdout="noise secret-token\n" + json.dumps(payload))

    monkeypatch.setattr(claude_code_tool.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_code_tool, "_resolve_token", lambda: "secret-token")
    monkeypatch.setattr(claude_code_tool.subprocess, "run", fake_run)

    result = json.loads(
        claude_code_tool.claude_code_task(
            "read note",
            workdir=str(tmp_path),
            mode="review",
            max_turns=1,
        )
    )

    assert result["success"] is False
    assert result["result"] == "leaked <redacted>"
    assert result["errors"] == ["<redacted> in error"]
    assert "secret-token" not in result["stdout_tail"]


def test_check_requirements_reads_hermes_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("IMAGE_OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("IMAGE_OPENAI_API_KEY=from-dotenv\n")
    monkeypatch.setattr(claude_code_tool.shutil, "which", lambda name: "/usr/bin/claude")

    assert claude_code_tool.check_claude_code_requirements() is True
