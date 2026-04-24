"""Tests for notify_on_complete background process feature.

Covers:
  - ProcessSession.notify_on_complete field
  - ProcessRegistry.completion_queue population on _move_to_finished()
  - Checkpoint persistence of notify_on_complete
  - Terminal tool schema includes notify_on_complete
  - Terminal tool handler passes notify_on_complete through
"""

import json
import os
import queue
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.process_registry import (
    ProcessRegistry,
    ProcessSession,
)


@pytest.fixture()
def registry():
    """Create a fresh ProcessRegistry."""
    return ProcessRegistry()


def _make_session(
    sid="proc_test_notify",
    command="echo hello",
    task_id="t1",
    exited=False,
    exit_code=None,
    output="",
    notify_on_complete=False,
) -> ProcessSession:
    s = ProcessSession(
        id=sid,
        command=command,
        task_id=task_id,
        started_at=time.time(),
        exited=exited,
        exit_code=exit_code,
        output_buffer=output,
        notify_on_complete=notify_on_complete,
    )
    return s


# =========================================================================
# ProcessSession field
# =========================================================================

class TestProcessSessionField:
    def test_default_false(self):
        s = ProcessSession(id="proc_1", command="echo hi")
        assert s.notify_on_complete is False

    def test_set_true(self):
        s = ProcessSession(id="proc_1", command="echo hi", notify_on_complete=True)
        assert s.notify_on_complete is True


# =========================================================================
# Completion queue
# =========================================================================

class TestCompletionQueue:
    def test_queue_exists(self, registry):
        assert hasattr(registry, "completion_queue")
        assert registry.completion_queue.empty()

    def test_move_to_finished_no_notify(self, registry):
        """Processes without notify_on_complete don't enqueue."""
        s = _make_session(notify_on_complete=False, output="done")
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)
        assert registry.completion_queue.empty()

    def test_move_to_finished_with_notify(self, registry):
        """Processes with notify_on_complete push to queue."""
        s = _make_session(
            notify_on_complete=True,
            output="build succeeded",
            exit_code=0,
        )
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        fake_harness = MagicMock(enabled=True)
        with patch.object(registry, "_write_checkpoint"), patch(
            "agent.harness.get_harness_manager",
            return_value=fake_harness,
        ):
            registry._move_to_finished(s)

        assert not registry.completion_queue.empty()
        completion = registry.completion_queue.get_nowait()
        assert completion["session_id"] == s.id
        assert completion["command"] == "echo hello"
        assert completion["exit_code"] == 0
        assert "build succeeded" in completion["output"]
        fake_harness.record_process_completed.assert_called_once()
        kwargs = fake_harness.record_process_completed.call_args.kwargs
        assert kwargs["task_id"] == "t1"
        assert kwargs["process_session_id"] == s.id
        assert kwargs["exit_code"] == 0

    def test_move_to_finished_nonzero_exit(self, registry):
        """Nonzero exit codes are captured correctly."""
        s = _make_session(
            notify_on_complete=True,
            output="FAILED",
            exit_code=1,
        )
        s.exited = True
        s.exit_code = 1
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        completion = registry.completion_queue.get_nowait()
        assert completion["exit_code"] == 1
        assert "FAILED" in completion["output"]

    def test_output_truncated_to_2000(self, registry):
        """Long output is truncated to last 2000 chars."""
        long_output = "x" * 5000
        s = _make_session(
            notify_on_complete=True,
            output=long_output,
        )
        s.exited = True
        s.exit_code = 0
        registry._running[s.id] = s
        with patch.object(registry, "_write_checkpoint"):
            registry._move_to_finished(s)

        completion = registry.completion_queue.get_nowait()
        assert len(completion["output"]) == 2000

    def test_multiple_completions_queued(self, registry):
        """Multiple notify processes all push to the same queue."""
        for i in range(3):
            s = _make_session(
                sid=f"proc_{i}",
                notify_on_complete=True,
                output=f"output_{i}",
            )
            s.exited = True
            s.exit_code = 0
            registry._running[s.id] = s
            with patch.object(registry, "_write_checkpoint"):
                registry._move_to_finished(s)

        completions = []
        while not registry.completion_queue.empty():
            completions.append(registry.completion_queue.get_nowait())
        assert len(completions) == 3
        ids = {c["session_id"] for c in completions}
        assert ids == {"proc_0", "proc_1", "proc_2"}


# =========================================================================
# Checkpoint persistence
# =========================================================================

class TestCheckpointNotify:
    def test_checkpoint_includes_notify(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "procs.json"):
            s = _make_session(notify_on_complete=True)
            registry._running[s.id] = s
            registry._write_checkpoint()

            data = json.loads((tmp_path / "procs.json").read_text())
            assert len(data) == 1
            assert data[0]["notify_on_complete"] is True

    def test_checkpoint_without_notify(self, registry, tmp_path):
        with patch("tools.process_registry.CHECKPOINT_PATH", tmp_path / "procs.json"):
            s = _make_session(notify_on_complete=False)
            registry._running[s.id] = s
            registry._write_checkpoint()

            data = json.loads((tmp_path / "procs.json").read_text())
            assert data[0]["notify_on_complete"] is False

    def test_recover_preserves_notify(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "notify_on_complete": True,
        }]))
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            s = registry.get("proc_live")
            assert s.notify_on_complete is True

    def test_recover_requeues_notify_watchers(self, registry, tmp_path):
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
            "session_key": "sk1",
            "watcher_platform": "telegram",
            "watcher_chat_id": "123",
            "watcher_thread_id": "42",
            "watcher_interval": 5,
            "notify_on_complete": True,
        }]))
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            assert len(registry.pending_watchers) == 1
            assert registry.pending_watchers[0]["notify_on_complete"] is True

    def test_recover_defaults_false(self, registry, tmp_path):
        """Old checkpoint entries without the field default to False."""
        checkpoint = tmp_path / "procs.json"
        checkpoint.write_text(json.dumps([{
            "session_id": "proc_live",
            "command": "sleep 999",
            "pid": os.getpid(),
            "task_id": "t1",
        }]))
        with patch("tools.process_registry.CHECKPOINT_PATH", checkpoint):
            recovered = registry.recover_from_checkpoint()
            assert recovered == 1
            s = registry.get("proc_live")
            assert s.notify_on_complete is False


# =========================================================================
# Terminal tool schema
# =========================================================================

class TestTerminalSchema:
    def test_schema_has_notify_on_complete(self):
        from tools.terminal_tool import TERMINAL_SCHEMA
        props = TERMINAL_SCHEMA["parameters"]["properties"]
        assert "notify_on_complete" in props
        assert props["notify_on_complete"]["type"] == "boolean"
        assert props["notify_on_complete"]["default"] is False

    def test_handler_passes_notify(self):
        """_handle_terminal passes notify_on_complete to terminal_tool."""
        from tools.terminal_tool import _handle_terminal
        with patch("tools.terminal_tool.terminal_tool", return_value='{"ok":true}') as mock_tt:
            _handle_terminal(
                {"command": "echo hi", "background": True, "notify_on_complete": True},
                task_id="t1",
            )
            _, kwargs = mock_tt.call_args
            assert kwargs["notify_on_complete"] is True

    def test_background_terminal_records_harness_process_spawn(self, monkeypatch):
        import tools.terminal_tool as terminal_mod

        fake_env = MagicMock()
        fake_env.env = {"PATH": "/usr/bin:/bin"}
        fake_process_session = MagicMock(id="proc_test", pid=4321)
        fake_harness = MagicMock(enabled=True)

        monkeypatch.setattr(terminal_mod, "_active_environments", {})
        monkeypatch.setattr(terminal_mod, "_last_activity", {})
        monkeypatch.setattr(terminal_mod, "_creation_locks", {})
        monkeypatch.setattr(terminal_mod, "_get_env_config", lambda: {"env_type": "local", "cwd": "/tmp", "timeout": 30})
        monkeypatch.setattr(terminal_mod, "_create_environment", lambda **kwargs: fake_env)
        monkeypatch.setattr(terminal_mod, "_check_all_guards", lambda *args, **kwargs: {"approved": True})
        monkeypatch.setattr(terminal_mod, "_validate_workdir", lambda workdir: None)
        monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="": "chat-1")
        monkeypatch.setattr("tools.process_registry.process_registry.spawn_local", lambda **kwargs: fake_process_session)
        monkeypatch.setattr("agent.harness.get_harness_manager", lambda *args, **kwargs: fake_harness)

        result = json.loads(
            terminal_mod.terminal_tool(
                "echo hi",
                task_id="task-h1",
                background=True,
                notify_on_complete=True,
                check_interval=45,
            )
        )

        assert result["session_id"] == "proc_test"
        fake_harness.record_process_spawned.assert_called_once()
        kwargs = fake_harness.record_process_spawned.call_args.kwargs
        assert kwargs["task_id"] == "task-h1"
        assert kwargs["process_session_id"] == "proc_test"
        assert kwargs["session_key"] == "chat-1"
        assert kwargs["metadata"]["notify_on_complete"] is True
        assert kwargs["metadata"]["check_interval"] == 45


# =========================================================================
# Code execution blocked params
# =========================================================================

class TestCodeExecutionBlocked:
    def test_notify_on_complete_blocked_in_sandbox(self):
        from tools.code_execution_tool import _TERMINAL_BLOCKED_PARAMS
        assert "notify_on_complete" in _TERMINAL_BLOCKED_PARAMS


class TestHeavyBackgroundOffload:
    def test_live_heavy_background_command_uses_detached_launcher(self, monkeypatch):
        import tools.terminal_tool as terminal_mod

        fake_env = MagicMock()
        fake_env.env = {"PATH": "/usr/bin:/bin"}
        fake_process_session = MagicMock(id="proc_detached", pid=9876, detached=True)
        fake_process_session.unit_name = "hermes-bg-123"
        fake_harness = MagicMock(enabled=True)
        launch_calls = []

        monkeypatch.setattr(terminal_mod, "_active_environments", {})
        monkeypatch.setattr(terminal_mod, "_last_activity", {})
        monkeypatch.setattr(terminal_mod, "_creation_locks", {})
        monkeypatch.setattr(terminal_mod, "_get_env_config", lambda: {"env_type": "local", "cwd": "/tmp", "timeout": 30})
        monkeypatch.setattr(terminal_mod, "_create_environment", lambda **kwargs: fake_env)
        monkeypatch.setattr(terminal_mod, "_check_all_guards", lambda *args, **kwargs: {"approved": True})
        monkeypatch.setattr(terminal_mod, "_validate_workdir", lambda workdir: None)
        monkeypatch.setattr("tools.approval.get_current_session_key", lambda default="": "chat-1")
        monkeypatch.setattr("tools.process_registry.process_registry.spawn_local", lambda **kwargs: (_ for _ in ()).throw(AssertionError("spawn_local should not be used for heavy live background commands")))
        monkeypatch.setattr("agent.harness.get_harness_manager", lambda *args, **kwargs: fake_harness)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "123")

        def fake_launch(**kwargs):
            launch_calls.append(kwargs)
            return fake_process_session

        monkeypatch.setattr(terminal_mod, "_launch_detached_heavy_background_process", fake_launch)

        result = json.loads(
            terminal_mod.terminal_tool(
                "pytest tests/gateway/test_status.py -q",
                task_id="task-heavy-1",
                background=True,
                notify_on_complete=True,
            )
        )

        assert result["session_id"] == "proc_detached"
        assert result["detached"] is True
        assert launch_calls and launch_calls[0]["command"] == "pytest tests/gateway/test_status.py -q"
        fake_harness.record_process_spawned.assert_called_once()
        kwargs = fake_harness.record_process_spawned.call_args.kwargs
        assert kwargs["process_session_id"] == "proc_detached"
        assert kwargs["metadata"]["notify_on_complete"] is True
        assert kwargs["metadata"]["detached"] is True
        assert kwargs["metadata"]["launcher"] == "systemd_transient"
