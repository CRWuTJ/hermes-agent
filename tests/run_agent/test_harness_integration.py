from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_assistant_msg(content="Hello", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = _mock_assistant_msg(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = None
    return resp


def test_run_conversation_returns_harness_metadata(tmp_path):
    config = {
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "skills": {"external_dirs": []},
        "compression": {"enabled": False},
        "harness": {
            "enabled": True,
            "db_path": str(tmp_path / "harness.sqlite3"),
            "plan": {
                "always_require_for_surfaces": [],
                "max_direct_chars": 10,
                "keywords": ["retrofit"],
                "inject_contract_context": True,
            },
            "acceptance": {
                "require_evidence_for_plan_required": True,
            },
            "drift": {"enabled": True},
            "audit": {"capture_tool_results": True},
        },
    }

    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = _mock_response(content="Done", finish_reason="stop")

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("Retrofit Hermes harness", task_id="task-1")

    assert result["task_id"] == "task-1"
    assert result["harness_plan_mode"] == "plan_required"
    assert result["harness_state"] == "needs_replan"
    assert result["plan_artifact_path"].endswith(".md")


class _FakeRateLimitError(Exception):
    status_code = 429


def test_run_conversation_pauses_high_value_rate_limit_with_harness(tmp_path):
    config = {
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "skills": {"external_dirs": []},
        "compression": {"enabled": False},
        "harness": {
            "enabled": True,
            "db_path": str(tmp_path / "harness.sqlite3"),
            "plan": {
                "always_require_for_surfaces": [],
                "max_direct_chars": 1000,
                "keywords": [],
                "inject_contract_context": True,
            },
            "rate_limits": {
                "subscription_retry_seconds": 1800,
                "pause_high_value_models": True,
                "high_value_model_patterns": ["gpt-5"],
            },
            "acceptance": {"require_evidence_for_plan_required": False},
            "drift": {"enabled": False},
            "audit": {"capture_tool_results": True},
        },
    }

    err = _FakeRateLimitError("usage limit reached")

    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
    ):
        agent = AIAgent(
            model="gpt-5.5",
            provider="custom",
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=3,
        )
        agent.client = MagicMock()
        agent.client.chat.completions.create.side_effect = err

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("Continue Hermes retrofit", task_id="quota-task")

    assert result["completed"] is False
    assert result["interrupted"] is True
    assert result["harness_state"] == "waiting_external"
    assert "30 minutes" in result["final_response"]


def test_run_conversation_does_not_downgrade_on_rate_limit_when_harness_forbids_it(tmp_path):
    config = {
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "skills": {"external_dirs": []},
        "compression": {"enabled": False},
        "harness": {
            "enabled": True,
            "db_path": str(tmp_path / "harness.sqlite3"),
            "plan": {
                "always_require_for_surfaces": [],
                "max_direct_chars": 1000,
                "keywords": [],
                "inject_contract_context": True,
            },
            "rate_limits": {
                "subscription_retry_seconds": 1800,
                "pause_high_value_models": True,
                "allow_model_downgrade": False,
                "high_value_model_patterns": ["gpt-5"],
            },
            "acceptance": {"require_evidence_for_plan_required": False},
            "drift": {"enabled": False},
            "audit": {"capture_tool_results": True},
        },
        "fallback_model": {
            "provider": "openrouter",
            "model": "gpt-4o-mini",
        },
    }

    err = _FakeRateLimitError("usage limit reached")

    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
    ):
        agent = AIAgent(
            model="gpt-5.5",
            provider="custom",
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=3,
            fallback_model=config["fallback_model"],
        )
        agent.client = MagicMock()
        agent.client.chat.completions.create.side_effect = err

        with (
            patch.object(agent, "_try_activate_fallback", wraps=agent._try_activate_fallback) as fallback_spy,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("Continue Hermes retrofit", task_id="quota-no-downgrade")

    assert result["interrupted"] is True
    assert result["harness_state"] == "waiting_external"
    fallback_spy.assert_not_called()
