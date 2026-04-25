from agent.harness import (
    HarnessManager,
    build_workspace_change_ledger,
    render_workspace_change_ledger,
)


def _config(tmp_path):
    return {
        "harness": {
            "enabled": True,
            "db_path": str(tmp_path / "harness.sqlite3"),
            "plan": {
                "always_require_for_surfaces": ["cron", "delegate"],
                "max_direct_chars": 140,
                "keywords": ["refactor", "retrofit", "redesign"],
                "inject_contract_context": True,
            },
            "acceptance": {
                "require_evidence_for_plan_required": True,
                "verification_tools": ["terminal", "read_file", "search_files"],
            },
            "drift": {
                "enabled": True,
                "blocked_tool_threshold": 2,
                "read_only_streak_threshold": 4,
            },
            "audit": {
                "capture_tool_results": True,
            },
        }
    }


def _admit_plan_required_task(manager, task_id="task-1"):
    return manager.admit_turn(
        task_id=task_id,
        session_id=f"session-{task_id}",
        surface="cron",
        platform="cron",
        user_request="Retrofit Hermes harness governance",
        workspace_root="/repo",
        max_iterations=40,
    )


def test_admit_turn_persists_plan_required_contract_for_cron(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    contract = _admit_plan_required_task(manager)
    row = manager.store.get_task("task-1")
    events = manager.store.get_events("task-1")

    assert contract.plan_mode == "plan_required"
    assert row["state"] == "planning"
    assert row["plan_artifact_path"].endswith(".md")
    assert any(event["event_type"] == "gate.plan.required" for event in events)


def test_preflight_blocks_broad_writes_until_plan_artifact_exists(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    contract = _admit_plan_required_task(manager, task_id="task-2")

    blocked = manager.preflight_tool_call(
        task_id="task-2",
        tool_name="write_file",
        args={"path": "/repo/src/app.py", "content": "print('hi')"},
        session_id="session-task-2",
        tool_call_id="call-1",
    )
    assert blocked["allowed"] is False
    assert blocked["state"] == "planning"

    manager.record_tool_complete(
        task_id="task-2",
        tool_name="write_file",
        args={"path": contract.plan_artifact_path, "content": "# plan"},
        result='{"ok": true}',
        session_id="session-task-2",
        tool_call_id="call-plan",
    )

    row = manager.store.get_task("task-2")
    events = manager.store.get_events("task-2")

    assert row["state"] == "admitted"
    assert any(event["event_type"] == "gate.plan.satisfied" for event in events)
    assert any(artifact["artifact_kind"] == "plan_artifact" for artifact in manager.store.get_artifacts("task-2"))


def test_finalize_turn_requires_verification_evidence_for_plan_required_tasks(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    contract = _admit_plan_required_task(manager, task_id="task-3")

    manager.record_tool_complete(
        task_id="task-3",
        tool_name="write_file",
        args={"path": contract.plan_artifact_path, "content": "# plan"},
        result='{"ok": true}',
        session_id="session-task-3",
        tool_call_id="call-plan",
    )

    final_state = manager.finalize_turn(
        task_id="task-3",
        final_response="done",
        completed=True,
        interrupted=False,
        api_calls=3,
        message_count=5,
    )
    events = manager.store.get_events("task-3")

    assert final_state == "needs_replan"
    assert any(event["event_type"] == "gate.replan.required" for event in events)


def test_finalize_turn_moves_to_acceptance_after_plan_and_verification(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    contract = _admit_plan_required_task(manager, task_id="task-4")

    manager.record_tool_start(
        task_id="task-4",
        tool_name="write_file",
        args={"path": contract.plan_artifact_path},
        session_id="session-task-4",
        tool_call_id="call-plan",
    )
    manager.record_tool_complete(
        task_id="task-4",
        tool_name="write_file",
        args={"path": contract.plan_artifact_path, "content": "# plan"},
        result='{"ok": true}',
        session_id="session-task-4",
        tool_call_id="call-plan",
    )
    manager.record_tool_complete(
        task_id="task-4",
        tool_name="read_file",
        args={"path": contract.plan_artifact_path},
        result="# plan",
        session_id="session-task-4",
        tool_call_id="call-read",
    )

    final_state = manager.finalize_turn(
        task_id="task-4",
        final_response="done",
        completed=True,
        interrupted=False,
        api_calls=4,
        message_count=6,
    )

    row = manager.store.get_task("task-4")
    events = manager.store.get_events("task-4")

    assert final_state == "needs_acceptance"
    assert row["state"] == "needs_acceptance"
    assert any(event["event_type"] == "tool.started" for event in events)
    assert any(event["event_type"] == "tool.completed" for event in events)
    assert any(event["event_type"] == "gate.acceptance.required" for event in events)


def test_repeated_scope_violations_trigger_drift_and_needs_replan(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.admit_turn(
        task_id="task-5",
        session_id="session-task-5",
        surface="cli",
        platform="cli",
        user_request="small fix",
        workspace_root="/repo",
        max_iterations=10,
    )

    first = manager.preflight_tool_call(
        task_id="task-5",
        tool_name="write_file",
        args={"path": "/outside/rogue.txt", "content": "x"},
        session_id="session-task-5",
        tool_call_id="call-1",
    )
    second = manager.preflight_tool_call(
        task_id="task-5",
        tool_name="write_file",
        args={"path": "/outside/rogue-2.txt", "content": "y"},
        session_id="session-task-5",
        tool_call_id="call-2",
    )

    row = manager.store.get_task("task-5")
    events = manager.store.get_events("task-5")

    assert first["allowed"] is False
    assert second["allowed"] is False
    assert row["state"] == "needs_replan"
    assert any(event["event_type"] == "drift.detected" for event in events)


def test_process_events_are_recorded_for_harnessed_tasks(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.admit_turn(
        task_id="task-6",
        session_id="session-task-6",
        surface="cli",
        platform="cli",
        user_request="Run background validation",
        workspace_root="/repo",
        max_iterations=12,
    )

    manager.record_process_spawned(
        task_id="task-6",
        process_session_id="proc_123",
        command="pytest -q",
        pid=4321,
        session_key="chat-1",
        metadata={"notify_on_complete": True},
    )
    manager.record_process_completed(
        task_id="task-6",
        process_session_id="proc_123",
        exit_code=1,
        output_preview="FAILED tests/test_example.py",
        session_key="chat-1",
        metadata={"pid_scope": "host"},
    )

    row = manager.store.get_task("task-6")
    events = manager.store.get_events("task-6")

    assert row["state"] == "active"
    assert any(event["event_type"] == "process.spawned" for event in events)
    assert any(event["event_type"] == "process.failed" for event in events)


def test_task_action_creates_gateway_stub_and_records_event(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    manager.record_task_action(
        task_id="queued-1",
        action="reprioritize",
        status="reprioritized",
        surface="api",
        session_key="telegram:user:123",
        platform="telegram",
        target_bucket="next",
        task_context={
            "preview": "Queued follow-up",
            "lane": "interactive",
            "kind": "queued_message",
            "priority": 50,
            "control_mode": "queued",
            "source": "telegram",
        },
    )

    row = manager.store.get_task("queued-1")
    events = manager.store.get_events("queued-1")

    assert row is not None
    assert row["state"] == "queued"
    assert row["surface"] == "api"
    assert row["platform"] == "telegram"
    assert row["normalized_goal"] == "Queued follow-up"
    assert any(
        event["event_type"] == "task.action"
        and event["payload"].get("action") == "reprioritize"
        and event["payload"].get("target_bucket") == "next"
        for event in events
    )


def test_task_action_marks_cancelled_when_queued_task_is_cancelled(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    manager.record_task_action(
        task_id="queued-2",
        action="cancel",
        status="cancelled",
        surface="chat",
        session_key="telegram:user:123",
        platform="telegram",
        task_context={
            "preview": "Cancel me",
            "lane": "interactive",
            "kind": "queued_message",
            "priority": 10,
            "control_mode": "queued",
            "source": "telegram",
        },
    )

    row = manager.store.get_task("queued-2")
    events = manager.store.get_events("queued-2")

    assert row is not None
    assert row["state"] == "cancelled"
    assert row["completed_at"]
    assert any(
        event["event_type"] == "task.action"
        and event["payload"].get("action") == "cancel"
        and event["payload"].get("status") == "cancelled"
        for event in events
    )


def test_task_snapshot_includes_latest_control_action_summary(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    manager.record_task_action(
        task_id="queued-3",
        action="reprioritize",
        status="reprioritized",
        surface="chat",
        session_key="telegram:user:123",
        platform="telegram",
        target_bucket="later",
        task_context={
            "preview": "Move me",
            "lane": "interactive",
            "kind": "queued_message",
            "priority": 80,
            "control_mode": "queued",
            "source": "telegram",
        },
    )
    snapshot = manager.task_snapshot("queued-3")

    assert snapshot["signals"]["action_count"] == 1
    assert snapshot["control"]["latest_action"] == {
        "action": "reprioritize",
        "status": "reprioritized",
        "surface": "chat",
        "target_bucket": "later",
        "session_key": "telegram:user:123",
        "created_at": snapshot["control"]["latest_action"]["created_at"],
    }


def test_summarize_tasks_includes_recent_control_summary(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    manager.record_task_action(
        task_id="queued-4",
        action="reprioritize",
        status="reprioritized",
        surface="chat",
        session_key="telegram:user:123",
        platform="telegram",
        target_bucket="later",
        task_context={
            "preview": "Move me later",
            "lane": "interactive",
            "kind": "queued_message",
            "priority": 80,
            "control_mode": "queued",
            "source": "telegram",
        },
    )
    manager.record_task_action(
        task_id="queued-4",
        action="recover",
        status="recovered",
        surface="api",
        session_key="api:operator",
        platform="api",
        target_bucket="next",
        task_context={
            "preview": "Move me later",
            "lane": "interactive",
            "kind": "queued_message",
            "priority": 80,
            "control_mode": "queued",
            "source": "telegram",
        },
    )

    summary = manager.summarize_tasks(limit=3)

    assert summary["recent"][0]["control"]["latest_action"] == {
        "action": "recover",
        "status": "recovered",
        "surface": "api",
        "target_bucket": "next",
        "session_key": "api:operator",
        "created_at": summary["recent"][0]["control"]["latest_action"]["created_at"],
    }
    assert summary["recent"][0]["control"]["latest_recovery"] == {
        "action": "recover",
        "status": "recovered",
        "surface": "api",
        "target_bucket": "next",
        "session_key": "api:operator",
        "created_at": summary["recent"][0]["control"]["latest_recovery"]["created_at"],
    }


def test_admit_turn_classifies_workstream_and_injects_scope_guard(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    contract = manager.admit_turn(
        task_id="model-upgrade",
        session_id="telegram-session",
        surface="telegram",
        platform="telegram",
        user_request="把 Hermes 当前接入模型升级到 gpt 5.5，不要再跑到 gateway worker 改造",
        workspace_root="/repo",
        max_iterations=40,
    )

    row = manager.store.get_task("model-upgrade")
    context = manager.build_turn_context(contract)

    assert row["metadata"]["workstream"] == "model_chain"
    assert "gateway_worker" in row["metadata"]["out_of_scope_workstreams"]
    assert "Workstream: model_chain" in context
    assert "Out of scope: gateway_worker" in context


def test_record_budget_pause_moves_task_to_waiting_external_with_retry_window(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.admit_turn(
        task_id="quota-task",
        session_id="telegram-session",
        surface="telegram",
        platform="telegram",
        user_request="继续推进 Hermes 改造",
        workspace_root="/repo",
        max_iterations=40,
    )

    pause = manager.record_budget_pause(
        task_id="quota-task",
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
        reason="usage limit reached",
    )
    row = manager.store.get_task("quota-task")
    events = manager.store.get_events("quota-task")

    assert row["state"] == "waiting_external"
    assert pause["retry_after_seconds"] == 1800
    assert pause["model"] == "gpt-5.5"
    assert any(event["event_type"] == "budget.pause" for event in events)


def test_budget_pause_queue_lists_due_and_pending_tasks(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    for task_id in ("due-task", "pending-task"):
        manager.admit_turn(
            task_id=task_id,
            session_id=f"session-{task_id}",
            surface="telegram",
            platform="telegram",
            user_request=f"Continue {task_id}",
            workspace_root="/repo",
            max_iterations=40,
        )

    manager.record_budget_pause(
        task_id="due-task",
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
        reason="usage limit reached",
        retry_after_seconds=60,
        metadata={"retry_after_at": "2026-04-24T00:00:00+00:00"},
    )
    manager.record_budget_pause(
        task_id="pending-task",
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
        reason="usage limit reached",
        retry_after_seconds=3600,
        metadata={"retry_after_at": "2026-04-24T02:00:00+00:00"},
    )

    all_paused = manager.list_budget_paused_tasks(now="2026-04-24T01:00:00+00:00")
    due_only = manager.list_budget_paused_tasks(now="2026-04-24T01:00:00+00:00", due_only=True)

    assert [item["task_id"] for item in all_paused] == ["pending-task", "due-task"]
    assert {item["task_id"]: item["due"] for item in all_paused} == {
        "due-task": True,
        "pending-task": False,
    }
    assert [item["task_id"] for item in due_only] == ["due-task"]


def test_record_budget_resume_moves_due_task_back_to_queued(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.admit_turn(
        task_id="resume-task",
        session_id="telegram-session",
        surface="telegram",
        platform="telegram",
        user_request="Continue after usage limit",
        workspace_root="/repo",
        max_iterations=40,
    )
    manager.record_budget_pause(
        task_id="resume-task",
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
        reason="usage limit reached",
        retry_after_seconds=1,
    )

    resume = manager.record_budget_resume(task_id="resume-task", status="queued")
    row = manager.store.get_task("resume-task")
    events = manager.store.get_events("resume-task")

    assert resume["status"] == "queued"
    assert row["state"] == "queued"
    assert any(event["event_type"] == "budget.resume" for event in events)


def test_budget_resume_digest_reports_due_tasks(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.admit_turn(
        task_id="due-task",
        session_id="telegram-session",
        surface="telegram",
        platform="telegram",
        user_request="继续 Hermes 改造但遇到 GPT usage limit",
        workspace_root="/repo",
        max_iterations=40,
    )
    manager.record_budget_pause(
        task_id="due-task",
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
        reason="usage limit reached",
        retry_after_seconds=60,
        metadata={"retry_after_at": "2026-04-24T00:00:00+00:00"},
    )

    digest = manager.budget_resume_digest(now="2026-04-24T01:00:00+00:00")

    assert "Ready to resume:" in digest
    assert "due-task" in digest
    assert "gpt-5.5" in digest


def test_should_pause_for_rate_limit_only_targets_high_value_models(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    assert manager.should_pause_for_rate_limit(
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
        error_text="usage limit reached",
        status_code=429,
    )
    assert not manager.should_pause_for_rate_limit(
        provider="nim-local",
        model="minimax-2.7",
        error_text="usage limit reached",
        status_code=429,
    )


def test_rate_limit_policy_never_downgrades_models(tmp_path):
    manager = HarnessManager(_config(tmp_path))

    assert manager.allow_model_downgrade_for_rate_limit(
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
    ) is False


def test_task_snapshot_exposes_company_os_contract_for_cron_worker(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.admit_turn(
        task_id="cron-pr-monitor",
        session_id="cron-pr-monitor",
        surface="cron",
        platform="cron",
        user_request="Monitor Hermes upstream PR #15649",
        workspace_root="/repo",
        max_iterations=20,
    )

    snapshot = manager.task_snapshot("cron-pr-monitor")

    assert snapshot["task_contract"] == {
        "workflow_name": "general",
        "run_id": "cron-pr-monitor",
        "object_id": "cron-pr-monitor",
        "source_pointer": "cron:cron-pr-monitor",
        "current_step": "planning",
        "owner": "Hermes",
        "worker_class": "detached_worker",
        "approval_state": "pending_plan",
        "decision_state": "needs-approval",
        "next_action": "write_or_update_plan_artifact",
        "deadline_or_sla": "",
        "evidence_pointer": "",
        "stop_reason": "",
        "canonical_work_product": "",
        "review_surface": "/task cron-pr-monitor",
        "reuse_path": "",
    }


def test_task_snapshot_maps_gateway_queue_stub_to_runtime_coordinator(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.record_task_action(
        task_id="queued-worker",
        action="reprioritize",
        status="reprioritized",
        surface="chat",
        session_key="telegram:user:123",
        platform="telegram",
        target_bucket="next",
        task_context={
            "preview": "Queued follow-up",
            "lane": "interactive",
            "kind": "queued_message",
            "priority": 50,
            "control_mode": "queued",
            "source": "telegram",
        },
    )

    snapshot = manager.task_snapshot("queued-worker")

    assert snapshot["task_contract"]["worker_class"] == "runtime_coordinator"
    assert snapshot["task_contract"]["decision_state"] == "ready"
    assert snapshot["task_contract"]["approval_state"] == "approved"
    assert snapshot["task_contract"]["source_pointer"] == "telegram:telegram:user:123"
    assert snapshot["task_contract"]["next_action"] == "take_next_queue_action"


def test_task_contract_uses_latest_artifact_as_evidence_pointer(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    manager.admit_turn(
        task_id="artifact-task",
        session_id="telegram-session",
        surface="telegram",
        platform="telegram",
        user_request="Write and verify a small artifact",
        workspace_root="/repo",
        max_iterations=20,
    )
    manager.record_tool_complete(
        task_id="artifact-task",
        tool_name="write_file",
        args={"path": "/repo/report.md", "content": "done"},
        result='{"success": true}',
        session_id="telegram-session",
        tool_call_id="call-write",
    )

    snapshot = manager.task_snapshot("artifact-task")

    assert snapshot["task_contract"]["decision_state"] == "running"
    assert snapshot["task_contract"]["evidence_pointer"] == "/repo/report.md"
    assert snapshot["task_contract"]["canonical_work_product"] == "/repo/report.md"


def test_visibility_digest_groups_completed_blocked_and_next_work(tmp_path):
    manager = HarnessManager(_config(tmp_path))
    for task_id, request in [
        ("done-1", "把 Hermes 当前接入模型升级到 gpt 5.5"),
        ("blocked-1", "继续 Hermes 改造但遇到 GPT usage limit"),
        ("next-1", "清理当前脏工作区，拆分 gateway worker 和模型升级"),
    ]:
        manager.admit_turn(
            task_id=task_id,
            session_id=f"session-{task_id}",
            surface="telegram",
            platform="telegram",
            user_request=request,
            workspace_root="/repo",
            max_iterations=40,
        )

    manager.store.transition_state("done-1", "completed", completed_at="2026-04-24T00:00:00+00:00")
    manager.record_budget_pause(
        task_id="blocked-1",
        provider="gpt-mainline-codex-local",
        model="gpt-5.5",
        reason="usage limit reached",
    )

    digest = manager.visibility_digest(limit=5)

    assert "Completed:" in digest
    assert "Blocked:" in digest
    assert "Next:" in digest
    assert "gpt 5.5" in digest
    assert "usage limit" in digest
    assert "脏工作区" in digest


def test_workspace_change_ledger_assigns_each_path_to_one_workstream():
    ledger = build_workspace_change_ledger(
        [
            " M agent/harness.py",
            " M agent/topic_router.py",
            " M gateway/run.py",
            " M hermes_cli/codex_models.py",
            "?? tests/run_agent/test_harness_integration.py",
        ],
        overrides={"gateway/run.py": "visibility"},
    )

    assert ledger["total"] == 5
    assert ledger["by_workstream"]["task_governance"][0]["path"] == "agent/harness.py"
    assert ledger["by_workstream"]["context_management"][0]["path"] == "agent/topic_router.py"
    assert ledger["by_workstream"]["visibility"][0]["path"] == "gateway/run.py"
    assert ledger["by_workstream"]["model_chain"][0]["path"] == "hermes_cli/codex_models.py"
    assert ledger["by_workstream"]["task_governance"][1]["path"] == "tests/run_agent/test_harness_integration.py"


def test_workspace_change_ledger_rendering_is_plain_and_actionable():
    ledger = build_workspace_change_ledger(
        [
            " M gateway/worker_runtime.py",
            "?? website/docs/user-guide/features/api-server.md",
        ]
    )

    rendered = render_workspace_change_ledger(ledger)

    assert "Total dirty entries: 2" in rendered
    assert "gateway_worker" in rendered
    assert "workspace_hygiene" in rendered
    assert "gateway/worker_runtime.py" in rendered
