import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.task_queue import TaskQueueError, TaskQueueLedger, select_ready_tasks


def _task(task_id, *, status="ready", lane="implementation", priority="P1", deps=None, targets=None, scope="queue-ledger-only"):
    return {
        "id": task_id,
        "title": task_id,
        "project": "Company OS",
        "lane": lane,
        "priority": priority,
        "status": status,
        "dependencies": list(deps or []),
        "blocked_by": [],
        "worker_profile": "worker",
        "model_tier": "mid",
        "token_budget": 1000,
        "write_scope": scope,
        "acceptance_check": ["verified"],
        "artifact_targets": list(targets or [f"artifact:{task_id}"]),
        "last_heartbeat_at": None,
        "created_at": "2026-04-28T00:00:00Z",
        "updated_at": "2026-04-28T00:00:00Z",
    }


def test_enqueue_persists_valid_jsonl(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")

    task = ledger.enqueue(
        title="Land queue runner",
        lane="control",
        priority="P0",
        acceptance_check=["runner can select ready tasks"],
        artifact_targets=[".hermes/task_queue/backlog.jsonl"],
    )

    rows = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert rows == [task]
    assert task["id"] == "land-queue-runner"
    assert ledger.select()["selected"][0]["id"] == task["id"]


def test_complete_selects_newly_unblocked_ready_task(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    ledger.save([
        _task("unblocker", status="ready", lane="infra_debug", priority="P0"),
        _task("downstream", status="ready", lane="knowledge", priority="P1", deps=["unblocker"]),
    ])

    before = ledger.select()
    assert [task["id"] for task in before["selected"]] == ["unblocker"]
    assert before["skipped_blocked"][0]["reasons"] == ["dependency-not-done:unblocker"]

    result = ledger.complete_and_select("unblocker", acceptance_evidence=["unblocker verified"]).to_dict()

    assert result["task"]["status"] == "done"
    assert [task["id"] for task in result["selection"]["selected"]] == ["downstream"]


def test_complete_requires_acceptance_evidence(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    ledger.save([_task("candidate")])

    with pytest.raises(TaskQueueError, match="acceptance_evidence"):
        ledger.complete_and_select("candidate")


def test_governed_task_requires_knowledge_disposition_before_completion(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    task = _task("governed", status="running")
    task["workflow_governance"] = {"mode": "adaptive", "knowledge_disposition": "required"}
    ledger.save([task])

    with pytest.raises(TaskQueueError, match="knowledge_disposition"):
        ledger.complete_and_select("governed", acceptance_evidence=["tests passed"])


def test_governed_task_completion_records_knowledge_disposition(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    task = _task("governed", status="running")
    task["workflow_governance"] = {"mode": "adaptive", "knowledge_disposition": "required"}
    ledger.save([task])

    result = ledger.complete_and_select(
        "governed",
        acceptance_evidence=["tests passed"],
        knowledge_disposition="no durable knowledge",
    ).to_dict()

    assert result["task"]["status"] == "done"
    assert result["task"]["knowledge_disposition"] == "no durable knowledge"


def test_completion_clears_heartbeat_and_records_evidence(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    running = _task("candidate", status="running")
    running["last_heartbeat_at"] = "2026-04-28T00:01:00Z"
    ledger.save([running])

    result = ledger.complete_and_select("candidate", acceptance_evidence=["tests passed"]).to_dict()

    assert result["task"]["last_heartbeat_at"] is None
    assert result["task"]["completion_evidence"] == ["tests passed"]


def test_start_next_marks_selected_tasks_running_and_consumes_capacity(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    ledger.save([
        _task("impl", lane="implementation", priority="P1"),
        _task("verify", lane="verification", priority="P1"),
    ])

    result = ledger.start_next(max_count=2).to_dict()
    started = result["task"]["started"]

    assert {task["id"] for task in started} == {"impl", "verify"}
    rows = {task["id"]: task for task in ledger.load()}
    assert rows["impl"]["status"] == "running"
    assert rows["verify"]["status"] == "running"
    assert rows["impl"]["last_heartbeat_at"] is not None
    assert result["selection"]["selected"] == []


def test_claim_ready_marks_only_matching_dispatchable_tasks_running(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    manual = _task("manual", lane="implementation", priority="P0")
    gateway_task = _task("gateway-bg", lane="implementation", priority="P1")
    gateway_task["dispatch"] = {"kind": "gateway_background", "prompt": "run queued work"}
    ledger.save([manual, gateway_task])

    result = ledger.claim_ready(
        predicate=lambda task: task.get("dispatch", {}).get("kind") == "gateway_background",
        max_count=1,
    ).to_dict()

    assert [task["id"] for task in result["task"]["started"]] == ["gateway-bg"]
    rows = {task["id"]: task for task in ledger.load()}
    assert rows["manual"]["status"] == "ready"
    assert rows["gateway-bg"]["status"] == "running"
    assert rows["gateway-bg"]["last_heartbeat_at"] is not None


def test_enqueue_accepts_safe_extra_fields_for_dispatch_metadata(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")

    task = ledger.enqueue(
        title="Queued gateway task",
        task_id="gateway-bg",
        acceptance_check=["response delivered"],
        artifact_targets=["gateway-task:gateway-bg"],
        extra_fields={"dispatch": {"kind": "gateway_background", "prompt": "run queued work"}},
    )

    assert task["dispatch"] == {"kind": "gateway_background", "prompt": "run queued work"}
    assert TaskQueueLedger(tmp_path / "backlog.jsonl").load()[0]["dispatch"] == task["dispatch"]


def test_running_read_only_task_consumes_lane_capacity():
    tasks = [
        _task("current", status="running", lane="research", scope="read-only"),
        _task("next", status="ready", lane="research", scope="read-only"),
    ]

    result = select_ready_tasks(tasks)

    assert result["selected"] == []
    assert result["skipped_lane_limit"] == [{"id": "next", "lane": "research"}]


def test_needs_user_approval_scope_is_not_selected():
    result = select_ready_tasks([_task("vault", scope="needs-user-approval")])

    assert result["selected"] == []
    assert result["skipped_blocked"][0]["reasons"] == ["write-scope-needs-user-approval"]


def test_artifact_locks_prevent_parallel_write_conflicts():
    tasks = [
        _task("first", lane="implementation", priority="P1", targets=["shared.md"]),
        _task("second", lane="verification", priority="P1", targets=["shared.md"]),
    ]

    result = select_ready_tasks(tasks)

    assert [task["id"] for task in result["selected"]] == ["first"]
    assert result["artifact_lock_conflicts"] == [
        {"id": "second", "conflicts_with": ["first"], "artifacts": ["shared.md"]}
    ]


def test_block_selects_alternate_ready_work(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    ledger.save([
        _task("blocked-candidate", lane="implementation", priority="P0"),
        _task("alternate", lane="research", priority="P2", scope="read-only"),
    ])

    result = ledger.block_and_select("blocked-candidate", blocked_by=["missing credentials"]).to_dict()

    assert result["task"]["status"] == "blocked"
    assert result["task"]["blocked_by"] == ["missing credentials"]
    assert result["task"]["last_heartbeat_at"] is None
    assert [task["id"] for task in result["selection"]["selected"]] == ["alternate"]


def test_status_summary_reports_queue_visibility_counts(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")
    ledger.save([
        _task("queued", lane="research", priority="P1"),
        _task("running", lane="implementation", status="running"),
        _task("blocked", lane="knowledge", status="blocked"),
        _task("done", lane="verification", status="done"),
    ])

    summary = ledger.status_summary()

    assert summary["task_count"] == 4
    assert summary["open_count"] == 3
    assert summary["status_counts"] == {"ready": 1, "running": 1, "blocked": 1, "done": 1}
    assert summary["dispatchable_count"] == 1
    assert summary["next_ready_ids"] == ["queued"]
    assert summary["lane_counts"]["research"]["ready"] == 1


def test_concurrent_enqueue_preserves_all_tasks(tmp_path):
    ledger = TaskQueueLedger(tmp_path / "backlog.jsonl")

    def add_task(idx):
        return ledger.enqueue(
            title=f"Concurrent task {idx}",
            acceptance_check=["queued"],
            artifact_targets=[f"artifact:{idx}"],
        )["id"]

    with ThreadPoolExecutor(max_workers=6) as executor:
        ids = list(executor.map(add_task, range(20)))

    rows = ledger.load()
    assert len(rows) == 20
    assert sorted(task["id"] for task in rows) == sorted(ids)
