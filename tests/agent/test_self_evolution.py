import json

from agent.self_evolution import (
    AuthorityLevel,
    SelfEvolutionEngine,
    SystemImprovementOpportunity,
    classify_authority_level,
    read_report,
    write_report,
)


def test_authority_classifier_keeps_risky_system_changes_approval_gated():
    assert classify_authority_level(risk_tags=["live_gateway", "restart"]) == AuthorityLevel.USER_APPROVAL
    assert classify_authority_level(risk_tags=["model_routing", "account_pool"]) == AuthorityLevel.USER_APPROVAL
    assert classify_authority_level(risk_tags=["delete", "memory"]) == AuthorityLevel.USER_APPROVAL
    assert classify_authority_level(risk_tags=["paid_cost"]) == AuthorityLevel.USER_APPROVAL
    assert classify_authority_level(risk_tags=["tests", "read_only"]) == AuthorityLevel.LOW_RISK_EXECUTION


def test_memory_pressure_becomes_state_placement_upgrade_opportunity():
    engine = SelfEvolutionEngine(config={"self_evolution": {"memory_pressure_threshold": 0.8}})

    opportunities = engine.evaluate(
        {
            "memory": {
                "usage_ratio": 0.96,
                "procedural_entries": ["gateway restart workflow"],
                "long_entries": ["large research note"],
            }
        }
    )

    memory = next(item for item in opportunities if item.area == "memory")
    assert memory.recommended_direction.startswith("Rebalance durable state")
    assert "skill" in memory.options[0].lower()
    assert memory.authority_level_required == AuthorityLevel.TASK_DRAFT
    assert memory.priority_score >= 80


def test_worker_strategy_prefers_stronger_external_worker_over_custom_wheel():
    engine = SelfEvolutionEngine()

    opportunities = engine.evaluate(
        {
            "workers": {
                "custom_worker_count": 3,
                "external_workers": {"claude_code": "available", "opencode": "available"},
                "recent_custom_worker_failures": 4,
            }
        }
    )

    worker = next(item for item in opportunities if item.area == "workers")
    assert "Claude Code" in worker.recommended_direction
    assert worker.worker_strategy == "prefer_external_worker"
    assert worker.authority_level_required == AuthorityLevel.TASK_DRAFT


def test_self_evolution_run_produces_decisions_not_only_report():
    engine = SelfEvolutionEngine(config={"self_evolution": {"auto_create_tasks": True}})

    run = engine.run(
        observations={
            "memory": {"usage_ratio": 0.95, "procedural_entries": ["procedure"], "long_entries": []},
            "queue_cron": {"cron_main_driver_count": 2, "queue_first": False},
        },
        trigger="manual",
    )

    assert run.decisions
    assert {decision.decision for decision in run.decisions} <= {"create_task_draft", "ask_approval", "defer", "no_action"}
    assert any(decision.decision == "create_task_draft" for decision in run.decisions)
    assert run.task_drafts
    assert all(draft.plan_required for draft in run.task_drafts)
    assert {draft.source_opportunity_id for draft in run.task_drafts}
    assert run.summary["opportunity_count"] >= 2


def test_report_writer_persists_self_evolution_evidence(tmp_path):
    opportunity = SystemImprovementOpportunity(
        id="memory-placement",
        area="memory",
        symptom="Memory is near capacity",
        root_cause_hypothesis="Procedures are stored in global memory",
        evidence=["usage_ratio=0.96"],
        global_impact="Preferences become less effective",
        recommended_direction="Move procedures to skills",
        options=["Extract skills", "Keep as memory"],
        priority_score=90,
        authority_level_required=AuthorityLevel.TASK_DRAFT,
        can_execute_now=False,
        verification_plan=["memory usage lower"],
        rollback_plan=["keep original memory until approved"],
    )
    run = SelfEvolutionEngine().build_run(
        trigger="manual",
        observations={"memory": {"usage_ratio": 0.96}},
        opportunities=[opportunity],
    )

    report_path = write_report(run, report_dir=tmp_path)
    loaded = read_report(report_path)

    assert report_path.exists()
    assert loaded["run_id"] == run.run_id
    assert loaded["opportunities"][0]["id"] == "memory-placement"
    assert loaded["decisions"][0]["opportunity_id"] == "memory-placement"
    assert json.loads(report_path.read_text(encoding="utf-8"))["summary"]["opportunity_count"] == 1
