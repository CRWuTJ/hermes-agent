"""Tests for gateway service management helpers."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway_cli
import scripts.gateway_canonical_repair as repair_script


class TestSystemdServiceRefresh:
    def test_systemd_install_repairs_outdated_unit_without_force(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")

        calls = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.systemd_install()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert calls[:2] == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", gateway_cli.get_service_name()],
        ]

    def test_systemd_start_refreshes_outdated_unit(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")

        calls = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.systemd_start()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert calls[:2] == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "start", gateway_cli.get_service_name()],
        ]

    def test_systemd_restart_refreshes_outdated_unit(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_systemd_unit_path", lambda system=False: unit_path)
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")

        calls = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.systemd_restart()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert calls[:2] == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "restart", gateway_cli.get_service_name()],
        ]

    def test_systemd_restart_targets_active_legacy_system_unit(self, tmp_path, monkeypatch):
        unit_path = tmp_path / "hermes-gateway-17b8e69b.service"
        unit_path.write_text("old unit\n", encoding="utf-8")

        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": True,
                "scope": "system",
                "unit_name": "hermes-gateway-17b8e69b",
                "unit_path": str(unit_path),
                "drifted": True,
            },
        )
        monkeypatch.setattr(gateway_cli, "generate_systemd_unit", lambda system=False, run_as_user=None: "new unit\n")
        monkeypatch.setattr(gateway_cli, "_require_root_for_system_service", lambda action: None)

        calls = []

        def fake_run(cmd, check=True, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.systemd_restart()

        assert unit_path.read_text(encoding="utf-8") == "new unit\n"
        assert calls[:2] == [
            ["systemctl", "daemon-reload"],
            ["systemctl", "restart", "hermes-gateway-17b8e69b"],
        ]


class TestGeneratedSystemdUnits:
    def test_user_unit_avoids_recursive_execstop_and_uses_extended_stop_timeout(self):
        unit = gateway_cli.generate_systemd_unit(system=False)

        assert "ExecStart=" in unit
        assert "ExecStop=" not in unit
        assert "TimeoutStopSec=60" in unit

    def test_user_unit_includes_resolved_node_directory_in_path(self, monkeypatch):
        monkeypatch.setattr(gateway_cli.shutil, "which", lambda cmd: "/home/test/.nvm/versions/node/v24.14.0/bin/node" if cmd == "node" else None)

        unit = gateway_cli.generate_systemd_unit(system=False)

        assert "/home/test/.nvm/versions/node/v24.14.0/bin" in unit

    def test_system_unit_avoids_recursive_execstop_and_uses_extended_stop_timeout(self):
        unit = gateway_cli.generate_systemd_unit(system=True)

        assert "ExecStart=" in unit
        assert "ExecStop=" not in unit
        assert "TimeoutStopSec=60" in unit
        assert "WantedBy=multi-user.target" in unit


class TestGatewayStopCleanup:
    def test_stop_only_kills_current_profile_by_default(self, tmp_path, monkeypatch):
        """Without --all, stop uses systemd (if available) and does NOT call
        the global kill_gateway_processes()."""
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {"installed": True, "system": False, "unit_name": "hermes-gateway"},
        )

        service_calls = []
        kill_calls = []

        monkeypatch.setattr(gateway_cli, "systemd_stop", lambda system=False, user=False: service_calls.append("stop"))
        monkeypatch.setattr(
            gateway_cli,
            "kill_gateway_processes",
            lambda force=False: kill_calls.append(force) or 2,
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="stop"))

        assert service_calls == ["stop"]
        # Global kill should NOT be called without --all
        assert kill_calls == []

    def test_stop_all_sweeps_all_gateway_processes(self, tmp_path, monkeypatch):
        """With --all, stop uses systemd AND calls the global kill_gateway_processes()."""
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {"installed": True, "system": False, "unit_name": "hermes-gateway"},
        )

        service_calls = []
        kill_calls = []

        monkeypatch.setattr(gateway_cli, "systemd_stop", lambda system=False, user=False: service_calls.append("stop"))
        monkeypatch.setattr(
            gateway_cli,
            "kill_gateway_processes",
            lambda force=False: kill_calls.append(force) or 2,
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="stop", **{"all": True}))

        assert service_calls == ["stop"]
        assert kill_calls == [False]


class TestLaunchdServiceRecovery:
    def test_launchd_install_repairs_outdated_plist_without_force(self, tmp_path, monkeypatch):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text("<plist>old content</plist>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)

        calls = []

        def fake_run(cmd, check=False, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_install()

        label = gateway_cli.get_launchd_label()
        domain = gateway_cli._launchd_domain()
        assert "--replace" in plist_path.read_text(encoding="utf-8")
        assert calls[:2] == [
            ["launchctl", "bootout", f"{domain}/{label}"],
            ["launchctl", "bootstrap", domain, str(plist_path)],
        ]

    def test_launchd_start_reloads_unloaded_job_and_retries(self, tmp_path, monkeypatch):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text(gateway_cli.generate_launchd_plist(), encoding="utf-8")
        label = gateway_cli.get_launchd_label()

        calls = []
        domain = gateway_cli._launchd_domain()
        target = f"{domain}/{label}"

        def fake_run(cmd, check=False, **kwargs):
            calls.append(cmd)
            if cmd == ["launchctl", "kickstart", target] and calls.count(cmd) == 1:
                raise gateway_cli.subprocess.CalledProcessError(3, cmd, stderr="Could not find service")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_start()

        assert calls == [
            ["launchctl", "kickstart", target],
            ["launchctl", "bootstrap", domain, str(plist_path)],
            ["launchctl", "kickstart", target],
        ]

    def test_launchd_start_reloads_on_kickstart_exit_code_113(self, tmp_path, monkeypatch):
        """Exit code 113 (\"Could not find service\") should also trigger bootstrap recovery."""
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text(gateway_cli.generate_launchd_plist(), encoding="utf-8")
        label = gateway_cli.get_launchd_label()

        calls = []
        domain = gateway_cli._launchd_domain()
        target = f"{domain}/{label}"

        def fake_run(cmd, check=False, **kwargs):
            calls.append(cmd)
            if cmd == ["launchctl", "kickstart", target] and calls.count(cmd) == 1:
                raise gateway_cli.subprocess.CalledProcessError(113, cmd, stderr="Could not find service")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        gateway_cli.launchd_start()

        assert calls == [
            ["launchctl", "kickstart", target],
            ["launchctl", "bootstrap", domain, str(plist_path)],
            ["launchctl", "kickstart", target],
        ]

    def test_launchd_status_reports_local_stale_plist_when_unloaded(self, tmp_path, monkeypatch, capsys):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text("<plist>old content</plist>", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=113, stdout="", stderr="Could not find service"),
        )

        gateway_cli.launchd_status()

        output = capsys.readouterr().out
        assert str(plist_path) in output
        assert "stale" in output.lower()
        assert "not loaded" in output.lower()


class TestGatewayServiceDetection:
    def test_is_service_running_uses_matching_systemd_report(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {"installed": True, "active": True, "system": True},
        )

        assert gateway_cli._is_service_running() is True


class TestGatewaySystemServiceRouting:
    def test_gateway_install_passes_system_flags(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)

        calls = []
        monkeypatch.setattr(
            gateway_cli,
            "systemd_install",
            lambda force=False, system=False, user=False, run_as_user=None: calls.append((force, system, user, run_as_user)),
        )

        gateway_cli.gateway_command(
            SimpleNamespace(gateway_command="install", force=True, system=True, user=False, run_as_user="alice")
        )

        assert calls == [(True, True, False, "alice")]

    def test_gateway_status_prefers_system_service_when_only_system_unit_exists(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {"installed": True, "system": True, "unit_name": "hermes-gateway-legacy"},
        )

        calls = []
        monkeypatch.setattr(
            gateway_cli,
            "systemd_status",
            lambda deep=False, system=False, user=False, auto_select=False: calls.append((deep, system, user, auto_select)),
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="status", deep=False, system=False, user=False))

        assert calls == [(False, False, False, True)]

    def test_gateway_status_can_target_user_scope_explicitly(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {"installed": requested_scope == "user", "system": False, "unit_name": "hermes-gateway"},
        )

        calls = []
        monkeypatch.setattr(
            gateway_cli,
            "systemd_status",
            lambda deep=False, system=False, user=False, auto_select=False: calls.append((deep, system, user, auto_select)),
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="status", deep=False, system=False, user=True))

        assert calls == [(False, False, True, False)]

    def test_gateway_status_user_scope_does_not_fall_back_to_manual_process_view_when_user_service_missing(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {"installed": False, "system": False, "scope": requested_scope or "user"},
        )

        calls = []
        monkeypatch.setattr(
            gateway_cli,
            "systemd_status",
            lambda deep=False, system=False, user=False, auto_select=False: calls.append((deep, system, user, auto_select)),
        )
        monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [691964])

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="status", deep=False, system=False, user=True))

        assert calls == [(False, False, True, False)]

    def test_gateway_status_without_service_surfaces_shared_runtime_activity(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {"installed": False, "system": False, "scope": requested_scope or "user"},
        )
        monkeypatch.setattr(gateway_cli, "find_gateway_pids", lambda: [])
        monkeypatch.setattr(
            "gateway.status.build_gateway_status_payload",
            lambda: {"queued_tasks": {"queued_count": 2}, "live_tasks": {"lane_counts": {"interactive": 1, "cron_scout": 0, "housekeeping": 0}}},
        )
        monkeypatch.setattr(
            "gateway.status.render_status_activity_block",
            lambda payload, style="chat": "  Queued:       2 total\n  Active lanes: interactive=1 | cron/scout=0 | housekeeping=0" if style == "cli" else "",
        )

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="status", deep=False, system=False, user=False))

        output = capsys.readouterr().out
        assert "Gateway is not running" in output
        assert "Recent gateway activity:" in output
        assert "Queued:       2 total" in output
        assert "Active lanes: interactive=1 | cron/scout=0 | housekeeping=0" in output

    def test_gateway_repair_passes_flags_to_repair_script(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        calls = []
        monkeypatch.setattr(repair_script, "main", lambda argv=None: calls.append(list(argv or [])) or 0)

        gateway_cli.gateway_command(
            SimpleNamespace(
                gateway_command="repair",
                system=True,
                user=False,
                apply=True,
                dry_run=False,
                cleanup_legacy=True,
            )
        )

        assert calls == [["--system", "--apply", "--cleanup-legacy"]]

    def test_gateway_repair_can_target_user_scope(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        calls = []
        monkeypatch.setattr(repair_script, "main", lambda argv=None: calls.append(list(argv or [])) or 0)

        gateway_cli.gateway_command(
            SimpleNamespace(
                gateway_command="repair",
                system=False,
                user=True,
                apply=False,
                dry_run=False,
                cleanup_legacy=True,
            )
        )

        assert calls == [["--user", "--cleanup-legacy"]]

    def test_gateway_restart_explicit_system_scope_missing_service_does_not_restart_manual_gateway(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": False,
                "system": requested_scope == "system",
                "scope": requested_scope or "system",
            },
        )
        monkeypatch.setattr(gateway_cli, "get_systemd_linger_status", lambda: (True, ""))

        manual_calls = []
        monkeypatch.setattr(gateway_cli, "stop_profile_gateway", lambda: manual_calls.append("stop") or False)
        monkeypatch.setattr(gateway_cli, "run_gateway", lambda *args, **kwargs: manual_calls.append("run"))

        with pytest.raises(SystemExit) as exc:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="restart", system=True, user=False))

        assert exc.value.code == 1
        assert manual_calls == []
        output = capsys.readouterr().out
        assert "not installed" in output.lower()

    def test_gateway_stop_explicit_system_scope_missing_service_does_not_stop_manual_gateway(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": False,
                "system": requested_scope == "system",
                "scope": requested_scope or "system",
            },
        )

        manual_calls = []
        monkeypatch.setattr(gateway_cli, "stop_profile_gateway", lambda: manual_calls.append("stop") or False)

        with pytest.raises(SystemExit) as exc:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="stop", system=True, user=False, all=False))

        assert exc.value.code == 1
        assert manual_calls == []
        output = capsys.readouterr().out
        assert "not installed" in output.lower()

    def test_gateway_start_explicit_system_scope_missing_service_exits_cleanly(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": False,
                "system": requested_scope == "system",
                "scope": requested_scope or "system",
            },
        )

        start_calls = []
        monkeypatch.setattr(gateway_cli, "systemd_start", lambda system=False, user=False: start_calls.append((system, user)))

        with pytest.raises(SystemExit) as exc:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="start", system=True, user=False))

        assert exc.value.code == 1
        assert start_calls == []
        output = capsys.readouterr().out
        assert "not installed" in output.lower()

    def test_gateway_start_without_installed_service_exits_cleanly(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": False,
                "system": False,
                "scope": requested_scope or "user",
            },
        )

        start_calls = []
        monkeypatch.setattr(gateway_cli, "systemd_start", lambda system=False, user=False: start_calls.append((system, user)))

        with pytest.raises(SystemExit) as exc:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="start", system=False, user=False))

        assert exc.value.code == 1
        assert start_calls == []
        output = capsys.readouterr().out
        assert "not installed" in output.lower()

    def test_gateway_uninstall_explicit_system_scope_missing_service_exits_cleanly(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": False,
                "system": requested_scope == "system",
                "scope": requested_scope or "system",
            },
        )

        uninstall_calls = []
        monkeypatch.setattr(gateway_cli, "systemd_uninstall", lambda system=False, user=False: uninstall_calls.append((system, user)))

        with pytest.raises(SystemExit) as exc:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="uninstall", system=True, user=False))

        assert exc.value.code == 1
        assert uninstall_calls == []
        output = capsys.readouterr().out
        assert "not installed" in output.lower()

    def test_gateway_uninstall_without_installed_service_exits_cleanly(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": False,
                "system": False,
                "scope": requested_scope or "user",
            },
        )

        uninstall_calls = []
        monkeypatch.setattr(gateway_cli, "systemd_uninstall", lambda system=False, user=False: uninstall_calls.append((system, user)))

        with pytest.raises(SystemExit) as exc:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="uninstall", system=False, user=False))

        assert exc.value.code == 1
        assert uninstall_calls == []
        output = capsys.readouterr().out
        assert "not installed" in output.lower()

    def test_gateway_restart_from_gateway_surface_schedules_detached_system_lane(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": True,
                "scope": "system",
                "unit_name": "hermes-gateway",
            },
        )

        scheduled = []
        monkeypatch.setattr(
            gateway_cli,
            "_schedule_detached_system_gateway_cli",
            lambda action_name, hermes_args: scheduled.append((action_name, hermes_args)) or ("hermes-gateway-restart-123", Path("/tmp/restart.log")),
        )

        direct_calls = []
        monkeypatch.setattr(gateway_cli, "systemd_restart", lambda system=False, user=False: direct_calls.append((system, user)))

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="restart", system=True, user=False))

        assert direct_calls == []
        assert scheduled == [(
            "restart",
            ["gateway", "restart", "--system"],
        )]
        output = capsys.readouterr().out
        assert "detached" in output.lower()

    def test_gateway_uninstall_from_gateway_surface_schedules_detached_system_lane(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": True,
                "scope": "system",
                "unit_name": "hermes-gateway",
            },
        )

        scheduled = []
        monkeypatch.setattr(
            gateway_cli,
            "_schedule_detached_system_gateway_cli",
            lambda action_name, hermes_args: scheduled.append((action_name, hermes_args)) or ("hermes-gateway-uninstall-123", Path("/tmp/uninstall.log")),
        )

        uninstall_calls = []
        monkeypatch.setattr(gateway_cli, "systemd_uninstall", lambda system=False, user=False: uninstall_calls.append((system, user)))

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="uninstall", system=True, user=False))

        assert uninstall_calls == []
        assert scheduled == [(
            "uninstall",
            ["gateway", "uninstall", "--system"],
        )]
        output = capsys.readouterr().out
        assert "detached" in output.lower()

    def test_gateway_stop_from_gateway_surface_schedules_detached_system_lane(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: False)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": True,
                "scope": "system",
                "unit_name": "hermes-gateway",
            },
        )

        scheduled = []
        monkeypatch.setattr(
            gateway_cli,
            "_schedule_detached_system_gateway_cli",
            lambda action_name, hermes_args: scheduled.append((action_name, hermes_args)) or ("hermes-gateway-stop-123", Path("/tmp/stop.log")),
        )

        direct_calls = []
        monkeypatch.setattr(gateway_cli, "systemd_stop", lambda system=False, user=False: direct_calls.append((system, user)))

        gateway_cli.gateway_command(SimpleNamespace(gateway_command="stop", system=True, user=False, all=False))

        assert direct_calls == []
        assert scheduled == [(
            "stop",
            ["gateway", "stop", "--system"],
        )]
        output = capsys.readouterr().out
        assert "detached" in output.lower()

    def test_gateway_repair_apply_from_gateway_surface_schedules_detached_system_lane(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "is_linux", lambda: True)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": True,
                "scope": "system",
                "unit_name": "hermes-gateway",
            },
        )

        scheduled = []
        monkeypatch.setattr(
            gateway_cli,
            "_schedule_detached_system_gateway_cli",
            lambda action_name, hermes_args: scheduled.append((action_name, hermes_args)) or ("hermes-gateway-repair-123", Path("/tmp/repair.log")),
        )

        repair_calls = []
        monkeypatch.setattr(repair_script, "main", lambda argv=None: repair_calls.append(list(argv or [])) or 0)

        gateway_cli.gateway_command(
            SimpleNamespace(
                gateway_command="repair",
                system=True,
                user=False,
                apply=True,
                dry_run=False,
                cleanup_legacy=True,
            )
        )

        assert repair_calls == []
        assert scheduled == [(
            "repair",
            ["gateway", "repair", "--system", "--apply", "--cleanup-legacy"],
        )]
        output = capsys.readouterr().out
        assert "detached" in output.lower()

    def test_schedule_detached_system_gateway_cli_uses_wsl_root_bridge(self, monkeypatch, tmp_path):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("[Service]\nUser=wutj\nEnvironment=HERMES_HOME=/home/wutj/.hermes\n", encoding="utf-8")

        monkeypatch.setattr(
            gateway_cli,
            "_resolve_systemd_service_target",
            lambda system=False, user=False: {
                "scope": "system",
                "system": True,
                "unit_name": "hermes-gateway",
                "unit_path": str(unit_path),
            },
        )
        monkeypatch.setattr(gateway_cli, "_system_service_identity", lambda run_as_user=None: ("wutj", "wutj", "/home/wutj"))
        monkeypatch.setattr(gateway_cli, "get_hermes_cli_path", lambda: "/home/wutj/.local/bin/hermes")
        monkeypatch.setattr(gateway_cli, "_is_wsl", lambda: True)
        monkeypatch.setattr(gateway_cli, "_find_wsl_executable", lambda: "/mnt/c/Windows/System32/wsl.exe")
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")

        calls = []

        def fake_run(cmd, check=True, timeout=None, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        unit_name, log_path = gateway_cli._schedule_detached_system_gateway_cli(
            "restart",
            ["gateway", "restart", "--system"],
        )

        assert unit_name.startswith("hermes-gateway-restart-")
        assert log_path.name.startswith("gateway-restart-")
        assert calls and calls[0][:6] == [
            "/mnt/c/Windows/System32/wsl.exe",
            "-d",
            "Ubuntu",
            "-u",
            "root",
            "--",
        ]
        assert "systemd-run" in calls[0]
        assert "gateway restart --system" in calls[0][-1]

    def test_schedule_detached_system_gateway_cli_falls_back_to_listed_wsl_distro_when_env_missing(self, monkeypatch, tmp_path):
        unit_path = tmp_path / "hermes-gateway.service"
        unit_path.write_text("[Service]\nUser=wutj\nEnvironment=HERMES_HOME=/home/wutj/.hermes\n", encoding="utf-8")

        monkeypatch.setattr(
            gateway_cli,
            "_resolve_systemd_service_target",
            lambda system=False, user=False: {
                "scope": "system",
                "system": True,
                "unit_name": "hermes-gateway",
                "unit_path": str(unit_path),
            },
        )
        monkeypatch.setattr(gateway_cli, "_system_service_identity", lambda run_as_user=None: ("wutj", "wutj", "/home/wutj"))
        monkeypatch.setattr(gateway_cli, "get_hermes_cli_path", lambda: "/home/wutj/.local/bin/hermes")
        monkeypatch.setattr(gateway_cli, "_is_wsl", lambda: True)
        monkeypatch.setattr(gateway_cli, "_find_wsl_executable", lambda: "/mnt/c/Windows/System32/wsl.exe")
        monkeypatch.setattr(gateway_cli.os, "geteuid", lambda: 1000)
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)

        calls = []

        def fake_run(cmd, check=True, timeout=None, capture_output=False, text=False, **kwargs):
            calls.append(cmd)
            if cmd == ["/mnt/c/Windows/System32/wsl.exe", "-l", "-q"]:
                return SimpleNamespace(returncode=0, stdout="U\x00b\x00u\x00n\x00t\x00u\x00\n\x00\n\x00", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway_cli.subprocess, "run", fake_run)

        unit_name, log_path = gateway_cli._schedule_detached_system_gateway_cli(
            "restart",
            ["gateway", "restart", "--system"],
        )

        assert unit_name.startswith("hermes-gateway-restart-")
        assert log_path.name.startswith("gateway-restart-")
        assert calls[0] == ["/mnt/c/Windows/System32/wsl.exe", "-l", "-q"]
        assert calls[1][:6] == [
            "/mnt/c/Windows/System32/wsl.exe",
            "-d",
            "Ubuntu",
            "-u",
            "root",
            "--",
        ]

    def test_systemd_status_surfaces_repair_commands_for_drifted_system_unit(self, tmp_path, monkeypatch, capsys):
        unit_path = tmp_path / "hermes-gateway-17b8e69b.service"
        unit_path.write_text("[Unit]\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "has_conflicting_systemd_units", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": True,
                "scope": "system",
                "unit_name": "hermes-gateway-17b8e69b",
                "unit_path": str(unit_path),
                "drifted": True,
                "active": True,
                "reachable": True,
            },
        )
        monkeypatch.setattr(gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: True)
        monkeypatch.setattr(gateway_cli, "_read_systemd_user_from_unit", lambda path: "wutj")
        monkeypatch.setattr(gateway_cli, "get_service_name", lambda: "hermes-gateway")
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )

        gateway_cli.systemd_status(system=True)

        output = capsys.readouterr().out
        assert "Preview cutover: hermes gateway repair --system" in output
        assert "Apply when ready: sudo hermes gateway repair --system --apply --cleanup-legacy" in output

    def test_systemd_status_user_scope_surfaces_user_repair_commands(self, tmp_path, monkeypatch, capsys):
        unit_path = tmp_path / "hermes-gateway-17b8e69b.service"
        unit_path.write_text("[Unit]\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "has_conflicting_systemd_units", lambda: False)
        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": False,
                "scope": "user",
                "unit_name": "hermes-gateway-17b8e69b",
                "unit_path": str(unit_path),
                "drifted": True,
                "active": False,
                "reachable": True,
            },
        )
        monkeypatch.setattr(gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: False)
        monkeypatch.setattr(gateway_cli, "get_service_name", lambda: "hermes-gateway")
        monkeypatch.setattr(
            gateway_cli.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        monkeypatch.setattr(gateway_cli, "get_systemd_linger_status", lambda: (True, "enabled"))

        gateway_cli.systemd_status(user=True)

        output = capsys.readouterr().out
        assert "Preview cutover: hermes gateway repair --user" in output
        assert "Apply when ready: hermes gateway repair --user --apply --cleanup-legacy" in output
        assert "Run: hermes gateway restart --user  # auto-refreshes the unit" in output

    def test_resolve_systemd_service_target_can_force_user_scope(self, tmp_path, monkeypatch):
        user_unit_path = tmp_path / "hermes-gateway.service"
        user_unit_path.write_text("[Unit]\n", encoding="utf-8")
        system_unit_path = tmp_path / "hermes-gateway-legacy.service"
        system_unit_path.write_text("[Unit]\n", encoding="utf-8")

        monkeypatch.setattr(
            gateway_cli,
            "get_gateway_systemd_report",
            lambda requested_scope=None: {
                "installed": True,
                "system": requested_scope == "system",
                "scope": requested_scope or "system",
                "unit_name": "hermes-gateway-legacy" if requested_scope != "user" else "hermes-gateway",
                "unit_path": str(system_unit_path if requested_scope != "user" else user_unit_path),
            },
        )

        target = gateway_cli._resolve_systemd_service_target(user=True)

        assert target["scope"] == "user"
        assert target["system"] is False
        assert target["unit_name"] == "hermes-gateway"
        assert target["unit_path"] == str(user_unit_path)

    def test_scope_conflict_warning_mentions_explicit_user_cleanup(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway_cli, "get_installed_systemd_scopes", lambda: ["user", "system"])

        gateway_cli.print_systemd_scope_conflict_warning()

        output = capsys.readouterr().out
        assert "Pass --user or --system to force the scope explicitly." in output
        assert "hermes gateway status --user" in output
        assert "hermes gateway uninstall --user" in output
        assert "sudo hermes gateway uninstall --system" in output

    def test_gateway_restart_does_not_fallback_to_foreground_when_launchd_restart_fails(self, tmp_path, monkeypatch):
        plist_path = tmp_path / "ai.hermes.gateway.plist"
        plist_path.write_text("plist\n", encoding="utf-8")

        monkeypatch.setattr(gateway_cli, "is_linux", lambda: False)
        monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_cli, "get_launchd_plist_path", lambda: plist_path)
        monkeypatch.setattr(
            gateway_cli,
            "launchd_restart",
            lambda: (_ for _ in ()).throw(
                gateway_cli.subprocess.CalledProcessError(5, ["launchctl", "kickstart", "-k", "gui/501/ai.hermes.gateway"])
            ),
        )

        run_calls = []
        monkeypatch.setattr(gateway_cli, "run_gateway", lambda verbose=0, quiet=False, replace=False: run_calls.append((verbose, quiet, replace)))
        monkeypatch.setattr(gateway_cli, "kill_gateway_processes", lambda force=False: 0)

        try:
            gateway_cli.gateway_command(SimpleNamespace(gateway_command="restart", system=False))
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("Expected gateway_command to exit when service restart fails")

        assert run_calls == []


class TestDetectVenvDir:
    """Tests for _detect_venv_dir() virtualenv detection."""

    def test_detects_active_virtualenv_via_sys_prefix(self, tmp_path, monkeypatch):
        venv_path = tmp_path / "my-custom-venv"
        venv_path.mkdir()
        monkeypatch.setattr("sys.prefix", str(venv_path))
        monkeypatch.setattr("sys.base_prefix", "/usr")

        result = gateway_cli._detect_venv_dir()
        assert result == venv_path

    def test_falls_back_to_dot_venv_directory(self, tmp_path, monkeypatch):
        # Not inside a virtualenv
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        dot_venv = tmp_path / ".venv"
        dot_venv.mkdir()

        result = gateway_cli._detect_venv_dir()
        assert result == dot_venv

    def test_falls_back_to_venv_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        venv = tmp_path / "venv"
        venv.mkdir()

        result = gateway_cli._detect_venv_dir()
        assert result == venv

    def test_prefers_dot_venv_over_venv(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        (tmp_path / ".venv").mkdir()
        (tmp_path / "venv").mkdir()

        result = gateway_cli._detect_venv_dir()
        assert result == tmp_path / ".venv"

    def test_returns_none_when_no_virtualenv(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.prefix", "/usr")
        monkeypatch.setattr("sys.base_prefix", "/usr")
        monkeypatch.setattr(gateway_cli, "PROJECT_ROOT", tmp_path)

        result = gateway_cli._detect_venv_dir()
        assert result is None


class TestSystemUnitHermesHome:
    """HERMES_HOME in system units must reference the target user, not root."""

    def test_system_unit_uses_target_user_home_not_calling_user(self, monkeypatch):
        # Simulate sudo: Path.home() returns /root, target user is alice
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", "/home/alice"),
        )
        monkeypatch.setattr(
            gateway_cli, "_build_user_local_paths",
            lambda home, existing: [],
        )

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert 'HERMES_HOME=/home/alice/.hermes' in unit
        assert '/root/.hermes' not in unit

    def test_system_unit_remaps_profile_to_target_user(self, monkeypatch):
        # Simulate sudo with a profile: HERMES_HOME was resolved under root
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/root/.hermes/profiles/coder")
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", "/home/alice"),
        )
        monkeypatch.setattr(
            gateway_cli, "_build_user_local_paths",
            lambda home, existing: [],
        )

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert 'HERMES_HOME=/home/alice/.hermes/profiles/coder' in unit
        assert '/root/' not in unit

    def test_system_unit_preserves_custom_hermes_home(self, monkeypatch):
        # Custom HERMES_HOME not under any user's home — keep as-is
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/opt/hermes-shared")
        monkeypatch.setattr(
            gateway_cli, "_system_service_identity",
            lambda run_as_user=None: ("alice", "alice", "/home/alice"),
        )
        monkeypatch.setattr(
            gateway_cli, "_build_user_local_paths",
            lambda home, existing: [],
        )

        unit = gateway_cli.generate_systemd_unit(system=True, run_as_user="alice")

        assert 'HERMES_HOME=/opt/hermes-shared' in unit

    def test_user_unit_unaffected_by_change(self):
        # User-scope units should still use the calling user's HERMES_HOME
        unit = gateway_cli.generate_systemd_unit(system=False)

        hermes_home = str(gateway_cli.get_hermes_home().resolve())
        assert f'HERMES_HOME={hermes_home}' in unit


class TestHermesHomeForTargetUser:
    """Unit tests for _hermes_home_for_target_user()."""

    def test_remaps_default_home(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.delenv("HERMES_HOME", raising=False)

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/home/alice/.hermes"

    def test_remaps_profile_path(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/root/.hermes/profiles/coder")

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/home/alice/.hermes/profiles/coder"

    def test_keeps_custom_path(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/root")))
        monkeypatch.setenv("HERMES_HOME", "/opt/hermes")

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/opt/hermes"

    def test_noop_when_same_user(self, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/alice")))
        monkeypatch.delenv("HERMES_HOME", raising=False)

        result = gateway_cli._hermes_home_for_target_user("/home/alice")
        assert result == "/home/alice/.hermes"


class TestGeneratedUnitUsesDetectedVenv:
    def test_systemd_unit_uses_dot_venv_when_detected(self, tmp_path, monkeypatch):
        dot_venv = tmp_path / ".venv"
        dot_venv.mkdir()
        (dot_venv / "bin").mkdir()

        monkeypatch.setattr(gateway_cli, "_detect_venv_dir", lambda: dot_venv)
        monkeypatch.setattr(gateway_cli, "get_python_path", lambda: str(dot_venv / "bin" / "python"))

        unit = gateway_cli.generate_systemd_unit(system=False)

        assert f"VIRTUAL_ENV={dot_venv}" in unit
        assert f"{dot_venv}/bin" in unit
        # Must NOT contain a hardcoded /venv/ path
        assert "/venv/" not in unit or "/.venv/" in unit


class TestGeneratedUnitIncludesLocalBin:
    """~/.local/bin must be in PATH so uvx/pipx tools are discoverable."""

    def test_user_unit_includes_local_bin_in_path(self):
        unit = gateway_cli.generate_systemd_unit(system=False)
        home = str(Path.home())
        assert f"{home}/.local/bin" in unit

    def test_system_unit_includes_local_bin_in_path(self):
        unit = gateway_cli.generate_systemd_unit(system=True)
        # System unit uses the resolved home dir from _system_service_identity
        assert "/.local/bin" in unit


class TestSystemServiceIdentityRootHandling:
    """Root user handling in _system_service_identity()."""

    def test_auto_detected_root_is_rejected(self, monkeypatch):
        """When root is auto-detected (not explicitly requested), raise."""
        import pwd
        import grp

        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "root")
        monkeypatch.setenv("LOGNAME", "root")

        import pytest
        with pytest.raises(ValueError, match="pass --run-as-user root to override"):
            gateway_cli._system_service_identity(run_as_user=None)

    def test_explicit_root_is_allowed(self, monkeypatch):
        """When root is explicitly passed via --run-as-user root, allow it."""
        import pwd
        import grp

        root_info = pwd.getpwnam("root")
        root_group = grp.getgrgid(root_info.pw_gid).gr_name

        username, group, home = gateway_cli._system_service_identity(run_as_user="root")
        assert username == "root"
        assert home == root_info.pw_dir

    def test_non_root_user_passes_through(self, monkeypatch):
        """Normal non-root user works as before."""
        import pwd
        import grp

        monkeypatch.delenv("SUDO_USER", raising=False)
        monkeypatch.setenv("USER", "nobody")
        monkeypatch.setenv("LOGNAME", "nobody")

        try:
            username, group, home = gateway_cli._system_service_identity(run_as_user=None)
            assert username == "nobody"
        except ValueError as e:
            # "nobody" might not exist on all systems
            assert "Unknown user" in str(e)


class TestEnsureUserSystemdEnv:
    """Tests for _ensure_user_systemd_env() D-Bus session bus auto-detection."""

    def test_sets_xdg_runtime_dir_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 42)

        # Patch Path.exists so /run/user/42 appears to exist.
        # Using a FakePath subclass breaks on Python 3.12+ where
        # PosixPath.__new__ ignores the redirected path argument.
        _orig_exists = gateway_cli.Path.exists
        monkeypatch.setattr(
            gateway_cli.Path, "exists",
            lambda self: True if str(self) == "/run/user/42" else _orig_exists(self),
        )

        gateway_cli._ensure_user_systemd_env()

        assert os.environ.get("XDG_RUNTIME_DIR") == "/run/user/42"

    def test_sets_dbus_address_when_bus_socket_exists(self, tmp_path, monkeypatch):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        bus_socket = runtime / "bus"
        bus_socket.touch()  # simulate the socket file

        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 99)

        gateway_cli._ensure_user_systemd_env()

        assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={bus_socket}"

    def test_preserves_existing_env_vars(self, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/runtime")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/custom/bus")

        gateway_cli._ensure_user_systemd_env()

        assert os.environ["XDG_RUNTIME_DIR"] == "/custom/runtime"
        assert os.environ["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/custom/bus"

    def test_no_dbus_when_bus_socket_missing(self, tmp_path, monkeypatch):
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        # no bus socket created

        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
        monkeypatch.setattr(os, "getuid", lambda: 99)

        gateway_cli._ensure_user_systemd_env()

        assert "DBUS_SESSION_BUS_ADDRESS" not in os.environ

    def test_systemctl_cmd_calls_ensure_for_user_mode(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: calls.append("called"))

        result = gateway_cli._systemctl_cmd(system=False)
        assert result == ["systemctl", "--user"]
        assert calls == ["called"]

    def test_systemctl_cmd_skips_ensure_for_system_mode(self, monkeypatch):
        calls = []
        monkeypatch.setattr(gateway_cli, "_ensure_user_systemd_env", lambda: calls.append("called"))

        result = gateway_cli._systemctl_cmd(system=True)
        assert result == ["systemctl"]
        assert calls == []


class TestProfileArg:
    """Tests for _profile_arg — returns '--profile <name>' for named profiles."""

    def test_default_hermes_home_returns_empty(self, tmp_path, monkeypatch):
        """Default ~/.hermes should not produce a --profile flag."""
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = gateway_cli._profile_arg(str(hermes_home))
        assert result == ""

    def test_named_profile_returns_flag(self, tmp_path, monkeypatch):
        """~/.hermes/profiles/mybot should return '--profile mybot'."""
        profile_dir = tmp_path / ".hermes" / "profiles" / "mybot"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = gateway_cli._profile_arg(str(profile_dir))
        assert result == "--profile mybot"

    def test_hash_path_returns_empty(self, tmp_path, monkeypatch):
        """Arbitrary non-profile HERMES_HOME should return empty string."""
        custom_home = tmp_path / "custom" / "hermes"
        custom_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = gateway_cli._profile_arg(str(custom_home))
        assert result == ""

    def test_nested_profile_path_returns_empty(self, tmp_path, monkeypatch):
        """~/.hermes/profiles/mybot/subdir should NOT match — too deep."""
        nested = tmp_path / ".hermes" / "profiles" / "mybot" / "subdir"
        nested.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = gateway_cli._profile_arg(str(nested))
        assert result == ""

    def test_invalid_profile_name_returns_empty(self, tmp_path, monkeypatch):
        """Profile names with invalid chars should not match the regex."""
        bad_profile = tmp_path / ".hermes" / "profiles" / "My Bot!"
        bad_profile.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = gateway_cli._profile_arg(str(bad_profile))
        assert result == ""

    def test_systemd_unit_includes_profile(self, tmp_path, monkeypatch):
        """generate_systemd_unit should include --profile in ExecStart for named profiles."""
        profile_dir = tmp_path / ".hermes" / "profiles" / "mybot"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        unit = gateway_cli.generate_systemd_unit(system=False)
        assert "--profile mybot" in unit
        assert "gateway run --replace" in unit

    def test_launchd_plist_includes_profile(self, tmp_path, monkeypatch):
        """generate_launchd_plist should include --profile in ProgramArguments for named profiles."""
        profile_dir = tmp_path / ".hermes" / "profiles" / "mybot"
        profile_dir.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
        monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: profile_dir)
        plist = gateway_cli.generate_launchd_plist()
        assert "<string>--profile</string>" in plist
        assert "<string>mybot</string>" in plist
