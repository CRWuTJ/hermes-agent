from pathlib import Path

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.task_queue_bridge import (
    background_concurrency,
    block_background_task,
    block_cron_queue_task,
    claim_ready_background_tasks,
    claim_ready_cron_tasks,
    complete_background_task,
    complete_cron_queue_task,
    enqueue_background_task,
    enqueue_cron_job_task,
    infer_background_lane,
    recover_abandoned_background_tasks,
    task_queue_enabled,
)
from agent.task_queue import TaskQueueLedger


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")


def _config(path: Path, enabled=True):
    return {"task_queue": {"enabled": enabled, "path": str(path)}}


def test_task_queue_enabled_defaults_on_but_allows_opt_out():
    assert task_queue_enabled({}) is True
    assert task_queue_enabled({"task_queue": {"enabled": False}}) is False


def test_infer_background_lane_from_prompt():
    assert infer_background_lane("后台调研 gateway") == "research"
    assert infer_background_lane("跑 pytest 验证") == "verification"
    assert infer_background_lane("写入 Obsidian 知识库") == "knowledge"
    assert infer_background_lane("排查 gateway auth") == "infra_debug"
    assert infer_background_lane("实现队列 runner") == "implementation"


def test_enqueue_background_task_records_running_gateway_task(tmp_path):
    backlog = tmp_path / "backlog.jsonl"

    task = enqueue_background_task(
        prompt="排查 gateway 卡顿",
        source=_source(),
        task_id="bg_123",
        config=_config(backlog),
    )

    assert task is not None
    assert task["id"] == "bg_123"
    assert task["status"] == "running"
    assert task["lane"] == "infra_debug"
    assert task["last_heartbeat_at"] is not None
    assert TaskQueueLedger(backlog).load()[0]["id"] == "bg_123"


def test_complete_background_task_closes_and_selects_next_ready(tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    cfg = _config(backlog)
    enqueue_background_task(prompt="实现 A", source=_source(), task_id="bg_123", config=cfg)
    TaskQueueLedger(backlog).enqueue(
        title="调研 B",
        task_id="research_b",
        lane="research",
        priority="P2",
        acceptance_check=["done"],
        artifact_targets=["artifact:b"],
    )

    result = complete_background_task(task_id="bg_123", evidence="response delivered", config=cfg)

    assert result is not None
    payload = result.to_dict()
    assert payload["task"]["status"] == "done"
    assert payload["task"]["last_heartbeat_at"] is None
    assert payload["task"]["completion_evidence"] == ["response delivered"]
    assert [task["id"] for task in payload["selection"]["selected"]] == ["research_b"]


def test_block_background_task_records_blocker_and_selects_alternate(tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    cfg = _config(backlog)
    enqueue_background_task(prompt="实现 A", source=_source(), task_id="bg_123", config=cfg)
    TaskQueueLedger(backlog).enqueue(
        title="只读调研 B",
        task_id="research_b",
        lane="research",
        priority="P2",
        write_scope="read-only",
        acceptance_check=["done"],
        artifact_targets=["artifact:b"],
    )

    result = block_background_task(task_id="bg_123", reason="model_auth_failure", config=cfg)

    assert result is not None
    payload = result.to_dict()
    assert payload["task"]["status"] == "blocked"
    assert payload["task"]["blocked_by"] == ["model_auth_failure"]
    assert [task["id"] for task in payload["selection"]["selected"]] == ["research_b"]


def test_enqueue_and_claim_cron_job_task(tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    cfg = _config(backlog)
    job = {"id": "cron_123", "name": "knowledge scout", "prompt": "写入 Obsidian 知识", "deliver": "local"}

    task = enqueue_cron_job_task(job=job, config=cfg)
    duplicate = enqueue_cron_job_task(job=job, config=cfg)
    claim = claim_ready_cron_tasks(config=cfg)

    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    assert task is not None
    assert duplicate["id"] == task["id"]
    assert task["dispatch"]["kind"] == "cron_job"
    assert task["dispatch"]["cron_job_id"] == "cron_123"
    assert task["lane"] == "knowledge"
    started = claim.to_dict()["task"]["started"]
    assert [item["id"] for item in started] == [task["id"]]
    assert rows[task["id"]]["status"] == "running"


def test_complete_and_block_cron_queue_task(tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    cfg = _config(backlog)
    task = enqueue_cron_job_task(job={"id": "cron_done", "name": "runner", "prompt": "run"}, config=cfg, status="running")

    completed = complete_cron_queue_task(task_id=task["id"], evidence="output saved", config=cfg)

    assert completed is not None
    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    assert rows[task["id"]]["status"] == "done"
    assert rows[task["id"]]["completion_evidence"] == ["output saved"]

    task2 = enqueue_cron_job_task(job={"id": "cron_block", "name": "runner", "prompt": "run"}, config=cfg, status="running")
    blocked = block_cron_queue_task(task_id=task2["id"], reason="provider failed", config=cfg)

    assert blocked is not None
    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    assert rows[task2["id"]]["status"] == "blocked"
    assert rows[task2["id"]]["blocked_by"] == ["provider failed"]


def test_recover_abandoned_background_tasks_blocks_stale_missing_owner(tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    cfg = _config(backlog)
    enqueue_background_task(prompt="实现 A", source=_source(), task_id="bg_old", config=cfg)
    TaskQueueLedger(backlog).update_task("bg_old", last_heartbeat_at="2000-01-01T00:00:00Z")

    recovered = recover_abandoned_background_tasks(config=cfg, active_task_ids=set(), stale_after_seconds=60)

    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    assert [task["id"] for task in recovered] == ["bg_old"]
    assert rows["bg_old"]["status"] == "blocked"
    assert "abandoned_runtime" in rows["bg_old"]["blocked_by"][0]


def test_recover_abandoned_background_tasks_preserves_active_owner(tmp_path):
    backlog = tmp_path / "backlog.jsonl"
    cfg = _config(backlog)
    enqueue_background_task(prompt="实现 A", source=_source(), task_id="bg_active", config=cfg)
    TaskQueueLedger(backlog).update_task("bg_active", last_heartbeat_at="2000-01-01T00:00:00Z")

    recovered = recover_abandoned_background_tasks(config=cfg, active_task_ids={"bg_active"}, stale_after_seconds=60)

    rows = {row["id"]: row for row in TaskQueueLedger(backlog).load()}
    assert recovered == []
    assert rows["bg_active"]["status"] == "running"


def test_disabled_queue_does_not_write(tmp_path):
    backlog = tmp_path / "backlog.jsonl"

    task = enqueue_background_task(
        prompt="排查 gateway",
        source=_source(),
        task_id="bg_123",
        config=_config(backlog, enabled=False),
    )

    assert task is None
    assert not backlog.exists()
