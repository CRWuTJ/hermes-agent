import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_response(content="Done"):
    msg = SimpleNamespace(
        content=content,
        tool_calls=None,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    resp = SimpleNamespace(choices=[choice], model="test/model")
    resp.usage = None
    return resp


def test_learning_capabilities_context_is_api_only_and_not_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    registered = json.loads(
        learning_absorption(
            action="register",
            candidate={
                "finding": "Use evaluator-gated agent loops before changing default routing.",
                "source_url": "https://platform.openai.com/docs/guides/evals",
                "source_type": "official",
                "verification_level": "official",
                "impact": "medium",
                "risk": "low",
                "reusable_workflow": True,
                "applies_to": ["agent orchestration"],
            },
        )
    )
    config = {
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "skills": {"external_dirs": []},
        "compression": {"enabled": False},
    }

    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("learning_absorption")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.test/v1",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = _mock_response()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False

        with (
            patch.object(agent, "_persist_session") as mock_persist_session,
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("Plan the next agent orchestration upgrade", task_id="learning-task")

    api_messages = agent.client.chat.completions.create.call_args.kwargs["messages"]
    api_user_messages = [msg for msg in api_messages if msg.get("role") == "user"]
    assert registered["capability"]["id"] in api_user_messages[-1]["content"]
    assert "<hermes-learning-capabilities>" in api_user_messages[-1]["content"]

    assert result["messages"][0]["content"] == "Plan the next agent orchestration upgrade"
    assert "hermes-learning-capabilities" not in result["messages"][0]["content"]
    persisted_messages = mock_persist_session.call_args.args[0]
    assert persisted_messages[0]["content"] == "Plan the next agent orchestration upgrade"


def test_agent_records_speech_self_review_feedback_after_final_response(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    learning_absorption(
        action="register",
        candidate={
            "finding": "Use evaluator-gated agent loops before changing default routing.",
            "source_url": "https://platform.openai.com/docs/guides/evals",
            "source_type": "official",
            "verification_level": "official",
            "impact": "medium",
            "risk": "low",
            "reusable_workflow": True,
            "applies_to": ["agent orchestration"],
        },
    )
    config = {
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "skills": {"external_dirs": []},
        "compression": {"enabled": False},
    }

    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("learning_absorption")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.test/v1",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent.client.chat.completions.create.return_value = _mock_response(
            "我会参考这些已沉淀的 AI 使用经验：\n"
            "这对当前问题的用法：先解释关键取舍，再自己执行、验证并沉淀结果。"
        )
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            agent.run_conversation("直接修复 agent orchestration 的 bug，不要解释", task_id="learning-task")

    summary = json.loads(learning_absorption(action="speech_feedback_summary"))

    assert summary["summary"]["outcome_counts"]["too_much"] == 1
    event = summary["summary"]["recent_events"][0]
    assert event["source"] == "auto_self_review"
    assert event["actual_mode"] == "teach"
    assert event["expected_mode"] == "execute_quietly"
