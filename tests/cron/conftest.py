import pytest


@pytest.fixture(autouse=True)
def isolated_cron_tick_lock_dir(monkeypatch, tmp_path):
    """Keep cron tick lock tests isolated from any live Hermes runtime."""
    monkeypatch.setenv("HERMES_CRON_LOCK_DIR", str(tmp_path / "cron-locks"))
