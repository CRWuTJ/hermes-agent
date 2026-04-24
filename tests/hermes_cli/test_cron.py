"""Tests for hermes_cli.cron command handling."""

from argparse import Namespace
from unittest.mock import patch

import pytest

from cron.jobs import create_job, get_job, list_jobs, trigger_job
from hermes_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestCronCommandLifecycle:
    def test_pause_resume_run(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        paused = get_job(job["id"])
        assert paused["state"] == "paused"

        cron_command(Namespace(cron_command="resume", job_id=job["id"]))
        resumed = get_job(job["id"])
        assert resumed["state"] == "scheduled"

        cron_command(Namespace(cron_command="run", job_id=job["id"]))
        triggered = get_job(job["id"])
        assert triggered["state"] == "scheduled"

        out = capsys.readouterr().out
        assert "Paused job" in out
        assert "Resumed job" in out
        assert "Triggered job" in out

    def test_edit_can_replace_and_clear_skills(self, tmp_cron_dir, capsys):
        job = create_job(
            prompt="Combine skill outputs",
            schedule="every 1h",
            skill="blogwatcher",
        )

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule="every 2h",
                prompt="Revised prompt",
                name="Edited Job",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["find-nearby", "blogwatcher"],
                clear_skills=False,
            )
        )
        updated = get_job(job["id"])
        assert updated["skills"] == ["find-nearby", "blogwatcher"]
        assert updated["name"] == "Edited Job"
        assert updated["prompt"] == "Revised prompt"
        assert updated["schedule_display"] == "every 120m"

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                clear_skills=True,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["skills"] == []
        assert cleared["skill"] is None

        out = capsys.readouterr().out
        assert "Updated job" in out

    def test_create_with_multiple_skills(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="Use both skills",
                name="Skill combo",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["blogwatcher", "find-nearby"],
                lane=None,
                script=None,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["skills"] == ["blogwatcher", "find-nearby"]
        assert jobs[0]["name"] == "Skill combo"

    def test_create_accepts_explicit_lane_and_reports_it(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="Flush stale state",
                name="Housekeeping job",
                deliver="local",
                repeat=None,
                skill=None,
                skills=None,
                lane="housekeeping",
                script=None,
            )
        )

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["lane"] == "housekeeping"

        out = capsys.readouterr().out
        assert "Lane: housekeeping (explicit)" in out

    def test_create_uses_shared_job_lane_describer(self, tmp_cron_dir, capsys):
        with patch("cron.jobs.describe_job_lane", return_value="shared-lane"):
            cron_command(
                Namespace(
                    cron_command="create",
                    schedule="every 1h",
                    prompt="Notify the user",
                    name="Shared lane job",
                    deliver="origin",
                    repeat=None,
                    skill=None,
                    skills=None,
                    lane=None,
                    script=None,
                )
            )

        out = capsys.readouterr().out
        assert "Lane: shared-lane" in out

    def test_edit_can_set_and_clear_lane(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Scout backlog", schedule="every 1h")

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                add_skills=None,
                remove_skills=None,
                clear_skills=False,
                lane="housekeeping",
                clear_lane=False,
                script=None,
            )
        )
        updated = get_job(job["id"])
        assert updated["lane"] == "housekeeping"

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                add_skills=None,
                remove_skills=None,
                clear_skills=False,
                lane=None,
                clear_lane=True,
                script=None,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["lane"] is None

        out = capsys.readouterr().out
        assert "Lane: housekeeping (explicit)" in out
        assert "Lane: cron/scout (default)" in out

    def test_list_shows_lane_summary_and_effective_lane(self, tmp_cron_dir, capsys):
        interactive = create_job(prompt="Notify user", schedule="every 1h", deliver="origin")
        create_job(prompt="Scout backlog", schedule="every 1h")
        housekeeping = create_job(prompt="Flush stale state", schedule="every 1h", lane="housekeeping")

        trigger_job(interactive["id"])
        trigger_job(housekeeping["id"])

        cron_command(Namespace(cron_command="list", all=False))

        out = capsys.readouterr().out
        assert "Active lanes: interactive=1 | cron/scout=1 | housekeeping=1" in out
        assert "Due now:      interactive=1 | cron/scout=0 | housekeeping=1" in out
        assert "Lane:      interactive (default)" in out
        assert "Lane:      housekeeping (explicit)" in out

    def test_list_uses_shared_status_lane_formatter(self, tmp_cron_dir, capsys):
        create_job(prompt="Notify user", schedule="every 1h", deliver="origin")

        with patch(
            "gateway.status.format_status_lane_counts",
            side_effect=["shared-active", "shared-due"],
        ):
            cron_command(Namespace(cron_command="list", all=False))

        out = capsys.readouterr().out
        assert "Active lanes: shared-active" in out
        assert "Due now:      shared-due" in out

    def test_status_uses_shared_status_lane_formatter(self, tmp_cron_dir, capsys, monkeypatch):
        create_job(prompt="Notify user", schedule="every 1h", deliver="origin")
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [12345])

        with patch(
            "gateway.status.format_status_lane_counts",
            side_effect=["shared-active", "shared-due"],
        ):
            cron_command(Namespace(cron_command="status"))

        out = capsys.readouterr().out
        assert "Active lanes: shared-active" in out
        assert "Due now:      shared-due" in out
