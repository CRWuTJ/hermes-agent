"""Tests for the AI learning absorption decision tool."""

import json
from datetime import datetime, timedelta, timezone


def test_fetch_feed_retries_transient_urlopen_failure(monkeypatch):
    from tools import learning_absorption_tool as tool

    calls = []

    class Headers:
        def get_content_charset(self):
            return "utf-8"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit):
            assert limit == 1_000_000
            return b"<rss>ok</rss>"

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        if len(calls) == 1:
            raise TimeoutError("transient handshake timeout")
        return Response()

    monkeypatch.setattr(tool.urllib.request, "urlopen", fake_urlopen)

    assert tool._fetch_feed("https://example.com/feed.xml", timeout=3, attempts=2, retry_delay=0) == "<rss>ok</rss>"
    assert len(calls) == 2


def test_unverified_routing_change_goes_to_verification_backlog(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(
        learning_absorption(
            action="decide",
            candidate={
                "finding": "Route all execution approvals to a new background worker by default.",
                "source_url": "https://example.com/community-post",
                "source_type": "community",
                "verification_level": "source_checked",
                "impact": "high",
                "risk": "high",
                "affects_routing": True,
            },
        )
    )

    assert result["success"] is True
    decision = result["decision"]
    assert decision["promotion_target"] == "verification_backlog"
    assert decision["can_apply_by_default"] is False
    assert "local smoke" in " ".join(decision["required_evidence"]).lower()


def test_verified_reusable_workflow_promotes_to_playbook(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(
        learning_absorption(
            action="decide",
            candidate={
                "finding": "Use evaluator-gated agent loops before changing default routing.",
                "source_url": "https://platform.openai.com/docs/guides/evals",
                "source_type": "official",
                "verification_level": "official",
                "impact": "medium",
                "risk": "low",
                "reusable_workflow": True,
                "applies_to": ["agent orchestration", "workflow design"],
            },
        )
    )

    assert result["success"] is True
    decision = result["decision"]
    assert decision["promotion_target"] == "playbook"
    assert decision["can_apply_by_default"] is True
    assert decision["registry_status"] == "active"


def test_front_channel_allows_major_new_technique(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(
        learning_absorption(
            action="decide",
            candidate={
                "finding": "A new official agent evaluation pattern changes how Hermes should score worker reliability.",
                "source_url": "https://platform.openai.com/docs/guides/evals",
                "source_type": "official",
                "verification_level": "official",
                "impact": "high",
                "risk": "low",
                "front_channel_reason": "major_new_technique",
            },
        )
    )

    assert result["decision"]["front_channel"]["allow"] is True
    assert result["decision"]["front_channel"]["reason"] == "major_new_technique"


def test_front_channel_allows_current_project_relevance(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(
        learning_absorption(
            action="decide",
            candidate={
                "finding": "Hermes v0.13 changed cron delivery semantics used by this retrofit.",
                "source_url": "https://github.com/NousResearch/hermes-agent/releases",
                "source_type": "official",
                "verification_level": "official",
                "impact": "medium",
                "risk": "low",
                "front_channel_reason": "current_project_relevance",
            },
        )
    )

    assert result["decision"]["front_channel"]["allow"] is True
    assert result["decision"]["front_channel"]["reason"] == "current_project_relevance"


def test_front_channel_allows_execution_blocker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(
        learning_absorption(
            action="decide",
            candidate={
                "finding": "The configured provider is failing and blocks the live scout proof.",
                "source_url": "local://hermes/runtime",
                "source_type": "local",
                "verification_level": "local_smoke",
                "impact": "high",
                "risk": "medium",
                "front_channel_reason": "execution_blocker",
            },
        )
    )

    assert result["decision"]["front_channel"]["allow"] is True
    assert result["decision"]["front_channel"]["reason"] == "execution_blocker"


def test_front_channel_allows_direction_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(
        learning_absorption(
            action="decide",
            candidate={
                "finding": "The next Hermes upgrade must choose between routing defaults and a playbook-only rollout.",
                "source_url": "local://hermes/plan",
                "source_type": "local",
                "verification_level": "applied",
                "impact": "high",
                "risk": "medium",
                "front_channel_reason": "direction_decision",
            },
        )
    )

    assert result["decision"]["front_channel"]["allow"] is True
    assert result["decision"]["front_channel"]["reason"] == "direction_decision"


def test_front_channel_blocks_low_value_learning_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(
        learning_absorption(
            action="decide",
            candidate={
                "finding": "A generic AI newsletter repeated common prompt tips.",
                "source_url": "https://example.com/newsletter",
                "source_type": "community",
                "verification_level": "source_checked",
                "impact": "low",
                "risk": "low",
            },
        )
    )

    front_channel = result["decision"]["front_channel"]
    assert front_channel["allow"] is False
    assert front_channel["reason"] == "local_only"
    assert "[SILENT]" in front_channel["default_response"]


def test_register_persists_capability_idempotently(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    candidate = {
        "finding": "Use evaluator-gated agent loops before changing default routing.",
        "source_url": "https://platform.openai.com/docs/guides/evals",
        "source_type": "official",
        "verification_level": "official",
        "impact": "medium",
        "risk": "low",
        "reusable_workflow": True,
        "applies_to": ["agent orchestration"],
    }

    first = json.loads(learning_absorption(action="register", candidate=candidate))
    second = json.loads(learning_absorption(action="register", candidate=candidate))

    assert first["success"] is True
    assert second["success"] is True
    assert first["capability"]["id"] == second["capability"]["id"]

    registry_path = tmp_path / "learning" / "capabilities.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(payload["capabilities"]) == 1
    assert payload["capabilities"][0]["promotion_target"] == "playbook"
    assert payload["capabilities"][0]["status"] == "active"


def test_select_returns_relevant_active_capabilities_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    active = {
        "finding": "Use evaluator-gated agent loops before changing default routing.",
        "source_url": "https://platform.openai.com/docs/guides/evals",
        "source_type": "official",
        "verification_level": "official",
        "impact": "medium",
        "risk": "low",
        "reusable_workflow": True,
        "applies_to": ["agent orchestration", "routing"],
    }
    pending = {
        "finding": "Route every request through an unverified worker mesh.",
        "source_url": "https://example.com/community",
        "source_type": "community",
        "verification_level": "unverified",
        "impact": "high",
        "risk": "high",
        "affects_routing": True,
        "applies_to": ["agent orchestration", "routing"],
    }
    learning_absorption(action="register", candidate=active)
    learning_absorption(action="register", candidate=pending)

    result = json.loads(
        learning_absorption(
            action="select",
            query="How should Hermes handle agent orchestration and routing?",
        )
    )

    assert result["success"] is True
    assert [item["finding"] for item in result["capabilities"]] == [active["finding"]]


def test_record_use_updates_capability_usage_log(tmp_path, monkeypatch):
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

    result = json.loads(
        learning_absorption(
            action="record_use",
            capability_id=registered["capability"]["id"],
            usage={
                "task": "Discuss Hermes orchestration upgrade",
                "outcome": "used_in_plan",
            },
        )
    )

    assert result["success"] is True
    assert result["capability"]["use_count"] == 1
    assert result["capability"]["usage_log"][0]["outcome"] == "used_in_plan"


def test_build_capability_invocation_context_is_bounded_and_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import (
        build_capability_invocation_context,
        learning_absorption,
    )

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

    context = build_capability_invocation_context("agent orchestration upgrade")

    assert "<hermes-learning-capabilities>" in context
    assert registered["capability"]["id"] in context
    assert "Apply these active capabilities when they fit this turn" in context


def test_teacher_brief_explains_relevant_capabilities_in_chinese(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import LEARNING_ABSORPTION_SCHEMA, learning_absorption

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
                "applies_to": ["agent orchestration", "AI product usage"],
            },
        )
    )

    result = json.loads(
        learning_absorption(
            action="teacher_brief",
            query="教我怎么规划 AI agent 工作流的下一步",
        )
    )

    assert result["success"] is True
    assert "teacher_brief" in LEARNING_ABSORPTION_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert result["should_speak"] is True
    assert result["speech_policy"]["mode"] == "teach"
    assert registered["capability"]["id"] in result["brief"]
    assert "我会参考" in result["brief"]
    assert "下一步" in result["brief"]


def test_teacher_brief_stays_quiet_for_direct_execution_requests(tmp_path, monkeypatch):
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
            "applies_to": ["agent orchestration", "AI product usage"],
        },
    )

    result = json.loads(
        learning_absorption(
            action="teacher_brief",
            query="直接修复 agent orchestration 的 bug，不要解释，完成后报告结果",
        )
    )

    assert result["success"] is True
    assert result["should_speak"] is False
    assert result["speech_policy"]["mode"] == "execute_quietly"
    assert result["brief"] == ""
    assert result["capabilities"]


def test_teacher_brief_speaks_for_direction_discussions(tmp_path, monkeypatch):
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
            "applies_to": ["agent orchestration", "AI product usage"],
        },
    )

    result = json.loads(
        learning_absorption(
            action="teacher_brief",
            query="我们讨论一下 Hermes agent orchestration 下一步方向和取舍",
        )
    )

    assert result["success"] is True
    assert result["should_speak"] is True
    assert result["speech_policy"]["mode"] == "direction"
    assert "确认方向" in result["brief"]


def test_teacher_brief_speaks_for_user_brief_capabilities_even_without_teaching_words(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    learning_absorption(
        action="register",
        candidate={
            "finding": "New official agent runtime changes how workspace agents coordinate tasks.",
            "source_url": "https://openai.com/index/workspace-agents",
            "source_type": "official",
            "verification_level": "official",
            "impact": "high",
            "risk": "low",
            "applies_to": ["workspace agents"],
        },
    )

    result = json.loads(
        learning_absorption(
            action="teacher_brief",
            query="workspace agents 这件事对 Hermes 有影响吗",
        )
    )

    assert result["success"] is True
    assert result["should_speak"] is True
    assert result["speech_policy"]["mode"] == "decision"
    assert "需要你关注" in result["brief"]


def test_learning_context_includes_teacher_guidance_for_ai_usage_discussions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import (
        build_capability_invocation_context,
        learning_absorption,
    )

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
            "applies_to": ["agent orchestration", "AI product usage"],
        },
    )

    context = build_capability_invocation_context("我想学习怎么更好使用 AI agent")

    assert "teacher_mode" in context
    assert "用中文简短解释" in context


def test_learning_context_suppresses_teacher_mode_for_direct_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import (
        build_capability_invocation_context,
        learning_absorption,
    )

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
            "applies_to": ["agent orchestration", "AI product usage"],
        },
    )

    context = build_capability_invocation_context("直接修复 agent orchestration 的 bug，不要解释")

    assert "teacher_mode" not in context
    assert "execute_quietly" in context


def test_record_speech_feedback_persists_event_and_returns_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import LEARNING_ABSORPTION_SCHEMA, learning_absorption

    result = json.loads(
        learning_absorption(
            action="record_speech_feedback",
            usage={
                "query": "直接修复 agent orchestration 的 bug，不要解释",
                "actual_mode": "teach",
                "expected_mode": "execute_quietly",
                "outcome": "too_much",
                "note": "Hermes 讲解太多，应该直接做完后报告结果。",
                "capability_ids": ["cap-1"],
            },
        )
    )

    assert result["success"] is True
    assert "record_speech_feedback" in LEARNING_ABSORPTION_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert result["event"]["outcome"] == "too_much"
    assert result["event"]["actual_mode"] == "teach"
    assert result["event"]["expected_mode"] == "execute_quietly"
    assert result["summary"]["outcome_counts"]["too_much"] == 1
    assert any("少解释" in item for item in result["summary"]["recommendations"])

    summary = json.loads(learning_absorption(action="speech_feedback_summary"))

    assert summary["success"] is True
    assert summary["summary"]["total_events"] == 1
    assert summary["summary"]["recent_events"][0]["capability_ids"] == ["cap-1"]


def test_learning_context_includes_recent_speech_feedback_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import (
        build_capability_invocation_context,
        learning_absorption,
    )

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
            "applies_to": ["agent orchestration", "AI product usage"],
        },
    )
    learning_absorption(
        action="record_speech_feedback",
        usage={
            "query": "直接修复 agent orchestration 的 bug，不要解释",
            "actual_mode": "teach",
            "expected_mode": "execute_quietly",
            "outcome": "too_much",
        },
    )

    context = build_capability_invocation_context("agent orchestration upgrade")

    assert "<speech-feedback>" in context
    assert "少解释" in context
    assert "current user intent wins" in context


def test_natural_speech_feedback_attributes_to_latest_teacher_brief(tmp_path, monkeypatch):
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
                "applies_to": ["agent orchestration", "AI product usage"],
            },
        )
    )
    brief = json.loads(
        learning_absorption(
            action="teacher_brief",
            query="教我怎么规划 AI agent 工作流的下一步",
        )
    )

    result = json.loads(
        learning_absorption(
            action="record_speech_feedback",
            usage={"feedback_text": "你刚才讲太多了，应该直接做完后报告结果。"},
        )
    )

    assert brief["speech_policy"]["mode"] == "teach"
    assert result["success"] is True
    assert result["event"]["outcome"] == "too_much"
    assert result["event"]["actual_mode"] == "teach"
    assert result["event"]["expected_mode"] == "execute_quietly"
    assert result["event"]["query"] == "教我怎么规划 AI agent 工作流的下一步"
    assert result["event"]["capability_ids"] == [registered["capability"]["id"]]
    assert result["event"]["attribution"]["source"] == "latest_speech_policy_trace"
    assert result["event"]["attribution"]["trace_id"] == brief["speech_trace"]["trace_id"]


def test_natural_risk_feedback_attributes_to_latest_context_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import (
        build_capability_invocation_context,
        learning_absorption,
    )

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

    context = build_capability_invocation_context("直接改默认 agent orchestration 路由")
    result = json.loads(
        learning_absorption(
            action="record_speech_feedback",
            usage={"feedback_text": "这里应该先提醒我风险，再继续执行。"},
        )
    )

    assert "execute_quietly" in context
    assert result["event"]["outcome"] == "missed_decision"
    assert result["event"]["actual_mode"] == "execute_quietly"
    assert result["event"]["expected_mode"] == "decision"
    assert result["event"]["query"] == "直接改默认 agent orchestration 路由"
    assert result["event"]["capability_ids"] == [registered["capability"]["id"]]
    assert result["event"]["attribution"]["trace_source"] == "invocation_context"


def test_evaluate_speech_response_records_auto_feedback_when_direct_execution_overexplains(tmp_path, monkeypatch):
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

    result = json.loads(
        learning_absorption(
            action="evaluate_speech_response",
            query="直接修复 agent orchestration 的 bug，不要解释",
            response=(
                "我会参考这些已沉淀的 AI 使用经验：\n"
                "这对当前问题的用法：先解释关键取舍，再自己执行、验证并沉淀结果。"
            ),
        )
    )

    assert result["success"] is True
    assert result["feedback_recorded"] is True
    assert result["speech_policy"]["mode"] == "execute_quietly"
    assert result["event"]["source"] == "auto_self_review"
    assert result["event"]["outcome"] == "too_much"
    assert result["event"]["actual_mode"] == "teach"
    assert result["event"]["expected_mode"] == "execute_quietly"
    assert result["event"]["capability_ids"] == [registered["capability"]["id"]]
    assert result["summary"]["outcome_counts"]["too_much"] == 1


def test_evaluate_speech_response_records_missed_decision_for_high_attention_capability(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    registered = json.loads(
        learning_absorption(
            action="register",
            candidate={
                "finding": "New default routing behavior can affect Hermes agent execution.",
                "source_url": "https://platform.openai.com/docs/guides/agents",
                "source_type": "official",
                "verification_level": "official",
                "impact": "high",
                "risk": "low",
                "reusable_workflow": False,
                "applies_to": ["workspace agents"],
            },
        )
    )

    result = json.loads(
        learning_absorption(
            action="evaluate_speech_response",
            query="workspace agents 对 Hermes 有影响吗",
            response="我已经直接改好了。",
        )
    )

    assert result["success"] is True
    assert result["feedback_recorded"] is True
    assert result["speech_policy"]["mode"] == "decision"
    assert result["event"]["source"] == "auto_self_review"
    assert result["event"]["outcome"] == "missed_decision"
    assert result["event"]["actual_mode"] == "execute_quietly"
    assert result["event"]["expected_mode"] == "decision"
    assert result["event"]["capability_ids"] == [registered["capability"]["id"]]
    assert result["summary"]["outcome_counts"]["missed_decision"] == 1


def test_evaluate_speech_response_suppresses_recent_duplicate_auto_feedback(tmp_path, monkeypatch):
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

    first = json.loads(
        learning_absorption(
            action="evaluate_speech_response",
            query="直接修复 agent orchestration 的 bug，不要解释",
            response=(
                "我会参考这些已沉淀的 AI 使用经验：\n"
                "这对当前问题的用法：先解释关键取舍，再自己执行、验证并沉淀结果。"
            ),
        )
    )
    second = json.loads(
        learning_absorption(
            action="evaluate_speech_response",
            query="直接修复 agent orchestration 的 bug，不要解释",
            response=(
                "我会参考这些已沉淀的 AI 使用经验：\n"
                "这对当前问题的用法：先解释关键取舍，再自己执行、验证并沉淀结果。"
            ),
        )
    )

    assert first["feedback_recorded"] is True
    assert second["feedback_recorded"] is False
    assert second["reason"] == "duplicate_recent_auto_self_review"
    assert second["summary"]["outcome_counts"]["too_much"] == 1


def test_evaluate_speech_response_records_again_after_duplicate_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import learning_absorption_tool as tool

    base_time = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(tool, "_hermes_now", lambda: base_time)

    tool.learning_absorption(
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

    response = (
        "我会参考这些已沉淀的 AI 使用经验：\n"
        "这对当前问题的用法：先解释关键取舍，再自己执行、验证并沉淀结果。"
    )
    first = json.loads(
        tool.learning_absorption(
            action="evaluate_speech_response",
            query="直接修复 agent orchestration 的 bug，不要解释",
            response=response,
        )
    )
    monkeypatch.setattr(tool, "_hermes_now", lambda: base_time + timedelta(minutes=11))
    second = json.loads(
        tool.learning_absorption(
            action="evaluate_speech_response",
            query="直接修复 agent orchestration 的 bug，不要解释",
            response=response,
        )
    )

    assert first["feedback_recorded"] is True
    assert second["feedback_recorded"] is True
    assert second["summary"]["outcome_counts"]["too_much"] == 2


def test_learning_report_summarizes_recent_capabilities_and_speech_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    playbook = json.loads(
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
    user_brief = json.loads(
        learning_absorption(
            action="register",
            candidate={
                "finding": "New agent routing defaults can affect Hermes execution policy.",
                "source_url": "https://platform.openai.com/docs/guides/agents",
                "source_type": "official",
                "verification_level": "official",
                "impact": "high",
                "risk": "low",
                "reusable_workflow": False,
                "applies_to": ["workspace agents"],
            },
        )
    )
    learning_absorption(
        action="record_speech_feedback",
        usage={
            "query": "直接修复 agent orchestration 的 bug，不要解释",
            "actual_mode": "teach",
            "expected_mode": "execute_quietly",
            "outcome": "too_much",
        },
    )

    result = json.loads(learning_absorption(action="learning_report", limit=5))

    assert result["success"] is True
    assert result["report"]["capability_count"] == 2
    assert result["report"]["recent_capabilities"][0]["id"] == user_brief["capability"]["id"]
    assert result["report"]["high_attention_capabilities"][0]["id"] == user_brief["capability"]["id"]
    assert result["report"]["speech_feedback"]["outcome_counts"]["too_much"] == 1
    assert "少解释" in result["report"]["front_channel_summary"]
    assert playbook["capability"]["id"] in result["report"]["front_channel_summary"]


def test_learning_report_is_silent_without_learning_or_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import learning_absorption

    result = json.loads(learning_absorption(action="learning_report"))

    assert result["success"] is True
    assert result["report"]["capability_count"] == 0
    assert result["report"]["front_channel_summary"] == "[SILENT]"


def test_tool_is_available_in_core_toolsets():
    import model_tools  # noqa: F401 - triggers tool discovery
    from tools.registry import registry
    from toolsets import resolve_toolset

    assert "learning_absorption" in registry.get_all_tool_names()
    assert "learning_absorption" in resolve_toolset("memory")
    assert "learning_absorption" in resolve_toolset("hermes-telegram")
    assert "learning_absorption" in resolve_toolset("hermes-api-server")
    assert "learning_absorption" in resolve_toolset("hermes-acp")


def test_radar_run_registers_new_ai_usage_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import run_learning_radar

    sources = [
        {
            "name": "OpenAI News",
            "url": "https://example.com/openai.xml",
            "source_type": "official",
        }
    ]
    feed_xml = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>New agent guide for tool use and evals</title>
        <link>https://example.com/agents</link>
        <description>How to combine tool use, evals, and prompts in AI assistants.</description>
        <pubDate>Sat, 09 May 2026 10:00:00 GMT</pubDate>
      </item>
      <item>
        <title>Unrelated office update</title>
        <link>https://example.com/office</link>
        <description>Campus news.</description>
      </item>
    </channel></rss>
    """

    def fake_fetch(url):
        assert url == "https://example.com/openai.xml"
        return feed_xml

    result = run_learning_radar(sources=sources, fetcher=fake_fetch, register=True)

    assert result["success"] is True
    assert result["new_item_count"] == 1
    assert result["registered_count"] == 1
    assert result["registered"][0]["status"] == "active"
    assert "agent guide" in result["registered"][0]["finding"].lower()

    registry_path = tmp_path / "learning" / "capabilities.json"
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    assert len(payload["capabilities"]) == 1

    second = run_learning_radar(sources=sources, fetcher=fake_fetch, register=True)
    assert second["new_item_count"] == 0
    assert second["registered_count"] == 0


def test_radar_dry_run_does_not_mark_items_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools.learning_absorption_tool import run_learning_radar

    sources = [
        {
            "name": "OpenAI News",
            "url": "https://example.com/openai.xml",
            "source_type": "official",
        }
    ]
    feed_xml = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <title>New agent guide for tool use and evals</title>
        <link>https://example.com/agents</link>
        <description>How to combine tool use, evals, and prompts in AI assistants.</description>
        <pubDate>Sat, 09 May 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """

    dry_run = run_learning_radar(sources=sources, fetcher=lambda _url: feed_xml, register=False)
    assert dry_run["success"] is True
    assert dry_run["new_item_count"] == 1
    assert dry_run["registered_count"] == 0
    assert not (tmp_path / "learning" / "radar_seen.json").exists()

    real_run = run_learning_radar(sources=sources, fetcher=lambda _url: feed_xml, register=True)
    assert real_run["new_item_count"] == 1
    assert real_run["registered_count"] == 1


def test_learning_radar_prompt_runs_report_after_source_scan():
    from tools.learning_absorption_tool import _RADAR_JOB_PROMPT

    assert "action=radar_run" in _RADAR_JOB_PROMPT
    assert "action=learning_report" in _RADAR_JOB_PROMPT
    assert "front_channel_summary" in _RADAR_JOB_PROMPT
    assert "[SILENT]" in _RADAR_JOB_PROMPT


def test_radar_install_is_not_exposed_to_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    import cron.jobs as jobs_mod
    from tools.learning_absorption_tool import LEARNING_ABSORPTION_SCHEMA, learning_absorption

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", tmp_path / "cron" / "output")

    actions = LEARNING_ABSORPTION_SCHEMA["parameters"]["properties"]["action"]["enum"]
    result = json.loads(learning_absorption(action="radar_install", schedule="every 12h"))

    assert "radar_install" not in actions
    assert result["success"] is False
    assert "disabled" in result["error"].lower()
    assert jobs_mod.list_jobs(include_disabled=True) == []
