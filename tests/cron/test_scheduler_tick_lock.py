from unittest.mock import patch

import pytest

from cron import scheduler


pytestmark = pytest.mark.skipif(getattr(scheduler, "fcntl", None) is None, reason="requires Unix file locking")


def _make_job():
    return {
        "id": "monitor-job",
        "name": "monitor",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
    }


def test_tick_uses_env_override_lock_dir_when_default_lock_is_busy(monkeypatch, tmp_path):
    default_lock_dir = tmp_path / "default-locks"
    default_lock_dir.mkdir(parents=True, exist_ok=True)
    default_lock_file = default_lock_dir / ".tick.lock"
    override_lock_dir = tmp_path / "override-locks"
    monkeypatch.setenv("HERMES_CRON_LOCK_DIR", str(override_lock_dir))

    held_lock = open(default_lock_file, "w")
    scheduler.fcntl.flock(held_lock, scheduler.fcntl.LOCK_EX | scheduler.fcntl.LOCK_NB)
    try:
        with patch("cron.scheduler._LOCK_DIR", default_lock_dir), \
             patch("cron.scheduler._LOCK_FILE", default_lock_file), \
             patch("cron.scheduler.get_due_jobs", return_value=[_make_job()]), \
             patch("cron.scheduler.run_job", return_value=(True, "# output", "Results here", None)), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md") as save_mock, \
             patch("cron.scheduler._deliver_result") as deliver_mock, \
             patch("cron.scheduler.mark_job_run") as mark_mock:
            executed = scheduler.tick(verbose=False)

        assert executed == 1
        save_mock.assert_called_once_with("monitor-job", "# output")
        deliver_mock.assert_called_once()
        mark_mock.assert_called_once()
    finally:
        scheduler.fcntl.flock(held_lock, scheduler.fcntl.LOCK_UN)
        held_lock.close()
