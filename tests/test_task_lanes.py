import pytest


def test_registry_tracks_and_releases_lane_counts():
    from task_lanes import TaskLaneRegistry

    registry = TaskLaneRegistry()

    with registry.track(task_id="interactive-1", lane="interactive", label="chat", source="gateway"):
        with registry.track(task_id="cron-1", lane="cron/scout", label="scout", source="cron"):
            snapshot = registry.snapshot()
            assert snapshot["counts"] == {
                "interactive": 1,
                "cron/scout": 1,
                "housekeeping": 0,
            }
            assert {task["task_id"] for task in snapshot["tasks"]} == {"interactive-1", "cron-1"}

    assert registry.snapshot()["counts"] == {
        "interactive": 0,
        "cron/scout": 0,
        "housekeeping": 0,
    }


def test_registry_finish_task_is_idempotent():
    from task_lanes import TaskLaneRegistry

    registry = TaskLaneRegistry()
    registry.start_task(task_id="housekeeping-1", lane="housekeeping", label="flush", source="gateway")

    registry.finish_task("housekeeping-1")
    registry.finish_task("housekeeping-1")

    assert registry.snapshot()["counts"]["housekeeping"] == 0


def test_registry_normalizes_background_lane_to_cron_scout():
    from task_lanes import TaskLaneRegistry

    registry = TaskLaneRegistry()

    with registry.track(task_id="bg-1", lane="background", label="background task", source="gateway"):
        snapshot = registry.snapshot()
        assert snapshot["counts"] == {
            "interactive": 0,
            "cron/scout": 1,
            "housekeeping": 0,
        }
        assert snapshot["tasks"][0]["lane"] == "cron/scout"


def test_registry_status_snapshot_uses_machine_readable_lanes():
    from task_lanes import TaskLaneRegistry

    registry = TaskLaneRegistry()

    with registry.track(task_id="bg-1", lane="background", label="background task", source="gateway"):
        snapshot = registry.status_snapshot()
        assert snapshot["active_count"] == 1
        assert snapshot["lane_counts"] == {
            "interactive": 0,
            "cron_scout": 1,
            "housekeeping": 0,
        }
        assert snapshot["tasks"][0]["lane"] == "cron_scout"


def test_registry_status_snapshot_includes_oldest_running_task():
    from task_lanes import TaskLaneRegistry

    registry = TaskLaneRegistry()

    with registry.track(task_id="bg-1", lane="background", label="background task", source="gateway"):
        with registry.track(task_id="turn-1", lane="interactive", label="message turn", source="gateway"):
            snapshot = registry.status_snapshot()

    assert snapshot["oldest_running"]["task_id"] == "bg-1"
    assert snapshot["oldest_running"]["lane"] == "cron_scout"
    assert snapshot["oldest_running"]["label"] == "background task"