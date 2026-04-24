import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.rl_training_tool as rl_training_tool


@pytest.mark.asyncio
async def test_spawn_training_run_uses_detached_units_for_live_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("TINKER_API_KEY", "test-tinker")
    monkeypatch.setenv("WANDB_API_KEY", "test-wandb")

    logs_dir = tmp_path / "logs"
    root_dir = tmp_path / "tinker-atropos"
    root_dir.mkdir()
    config_path = tmp_path / "run.yaml"
    config_path.write_text("env: {}\n", encoding="utf-8")
    env_file = tmp_path / "fake_env.py"
    env_file.write_text("print('fake env')\n", encoding="utf-8")

    monkeypatch.setattr(rl_training_tool, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(rl_training_tool, "TINKER_ATROPOS_ROOT", root_dir)
    monkeypatch.setattr(rl_training_tool, "_ensure_logs_dir", lambda: logs_dir.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(
        rl_training_tool,
        "_environments",
        [
            rl_training_tool.EnvironmentInfo(
                name="fake-env",
                class_name="FakeEnv",
                file_path=str(env_file),
            )
        ],
    )

    run_state = rl_training_tool.RunState(
        run_id="run12345",
        environment="fake-env",
        config={},
        status="starting",
    )

    launched = []

    async def fast_sleep(_seconds):
        return None

    def fake_launch_transient_unit(**kwargs):
        launched.append(kwargs)
        return f"unit-{len(launched)}"

    def fake_systemctl_show_properties(unit_name: str, *properties: str):
        return {"MainPID": "4321", "ExecMainStatus": ""}

    with (
        patch("tools.detached_runtime.transient_unit_launcher_available", return_value=True),
        patch("tools.detached_runtime.launch_transient_unit", side_effect=fake_launch_transient_unit),
        patch("tools.detached_runtime.systemctl_show_properties", side_effect=fake_systemctl_show_properties),
        patch("tools.rl_training_tool.asyncio.sleep", side_effect=fast_sleep),
        patch("tools.rl_training_tool.asyncio.create_task", side_effect=lambda coro: coro.close()) as mock_create_task,
        patch("tools.rl_training_tool.subprocess.Popen") as mock_popen,
    ):
        await rl_training_tool._spawn_training_run(run_state, config_path)

    assert run_state.status == "running"
    assert run_state.api_unit_name == "unit-1"
    assert run_state.trainer_unit_name == "unit-2"
    assert run_state.env_unit_name == "unit-3"
    assert run_state.api_exit_path.endswith("api_run12345.exit")
    assert run_state.trainer_exit_path.endswith("trainer_run12345.exit")
    assert run_state.env_exit_path.endswith("env_run12345.exit")
    assert len(launched) == 3
    assert "run-api" in launched[0]["shell_script"]
    assert "launch_training.py" in launched[1]["shell_script"]
    assert str(env_file) in launched[2]["shell_script"]
    mock_create_task.assert_called_once()
    mock_popen.assert_not_called()


def test_stop_training_run_signals_detached_units(tmp_path):
    run_state = rl_training_tool.RunState(
        run_id="run54321",
        environment="fake-env",
        config={},
        status="running",
        api_unit_name="api-unit",
        trainer_unit_name="trainer-unit",
        env_unit_name="env-unit",
        api_exit_path=str(tmp_path / "api.exit"),
        trainer_exit_path=str(tmp_path / "trainer.exit"),
        env_exit_path=str(tmp_path / "env.exit"),
    )

    unit_to_exit = {
        "api-unit": Path(run_state.api_exit_path),
        "trainer-unit": Path(run_state.trainer_exit_path),
        "env-unit": Path(run_state.env_exit_path),
    }
    signals = []

    def fake_signal_unit(unit_name: str, signal_name: str):
        signals.append((unit_name, signal_name))
        if signal_name == "TERM":
            unit_to_exit[unit_name].write_text("0", encoding="utf-8")

    def fake_systemctl_show_properties(unit_name: str, *properties: str):
        if unit_to_exit[unit_name].exists():
            return {"MainPID": "0", "ExecMainStatus": unit_to_exit[unit_name].read_text(encoding="utf-8")}
        return {"MainPID": "4321", "ExecMainStatus": ""}

    with (
        patch("tools.detached_runtime.signal_unit", side_effect=fake_signal_unit),
        patch("tools.detached_runtime.systemctl_show_properties", side_effect=fake_systemctl_show_properties),
        patch("tools.rl_training_tool.time.sleep", return_value=None),
    ):
        rl_training_tool._stop_training_run(run_state)

    assert signals == [
        ("env-unit", "TERM"),
        ("trainer-unit", "TERM"),
        ("api-unit", "TERM"),
    ]
    assert run_state.status == "stopped"


@pytest.mark.asyncio
async def test_rl_check_status_reports_detached_unit_processes(tmp_path, monkeypatch):
    run_state = rl_training_tool.RunState(
        run_id="run99999",
        environment="fake-env",
        config={},
        status="running",
        start_time=time.time() - 120,
        api_unit_name="api-unit",
        trainer_unit_name="trainer-unit",
        env_unit_name="env-unit",
        api_exit_path=str(tmp_path / "api.exit"),
        trainer_exit_path=str(tmp_path / "trainer.exit"),
        env_exit_path=str(tmp_path / "env.exit"),
        wandb_project="proj",
        wandb_run_name="run-name",
    )
    Path(run_state.trainer_exit_path).write_text("2", encoding="utf-8")

    monkeypatch.setattr(rl_training_tool, "_active_runs", {run_state.run_id: run_state})
    monkeypatch.setattr(rl_training_tool, "_last_status_check", {})

    def fake_systemctl_show_properties(unit_name: str, *properties: str):
        return {"MainPID": "9876", "ExecMainStatus": ""}

    with patch("tools.detached_runtime.systemctl_show_properties", side_effect=fake_systemctl_show_properties):
        result = json.loads(await rl_training_tool.rl_check_status(run_state.run_id))

    assert result["status"] == "running"
    assert result["processes"]["api"] == "running"
    assert result["processes"]["trainer"] == "exited (2)"
    assert result["processes"]["env"] == "running"
