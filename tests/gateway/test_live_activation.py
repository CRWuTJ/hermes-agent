import json

from gateway.live_activation import (
    LIVE_ACTIVATION_PENDING_FILE,
    build_live_activation_plan,
    read_live_activation_plan,
    render_live_activation_digest,
    write_live_activation_plan,
)


def test_build_live_activation_plan_is_pending_and_never_auto_applies():
    plan = build_live_activation_plan(
        title="Hermes self-retrofit",
        reason="Load tested gateway and harness changes",
        service_name="hermes-gateway.service",
        current_pid="2766381",
        current_started_at="Fri 2026-04-24 00:25:55 CST",
        changed_files=["gateway/run.py", "agent/harness.py"],
        verification=["73 passed"],
    )

    assert plan["status"] == "pending"
    assert plan["auto_apply"] is False
    assert plan["live_restart_performed"] is False
    assert plan["safety"]["requires_explicit_apply"] is True
    assert plan["service"]["name"] == "hermes-gateway.service"
    assert plan["service"]["current_pid"] == "2766381"
    assert "gateway/run.py" in plan["changed_files"]
    assert "73 passed" in plan["verification"]
    assert "restart" not in json.dumps(plan.get("commands_executed", []), ensure_ascii=False).lower()


def test_write_read_and_render_live_activation_plan(tmp_path):
    plan = build_live_activation_plan(
        title="Hermes safe activation",
        service_name="hermes-gateway.service",
        current_pid="2766381",
        verification=["py_compile passed"],
    )

    path = write_live_activation_plan(plan, hermes_home=tmp_path)
    loaded = read_live_activation_plan(hermes_home=tmp_path)
    digest = render_live_activation_digest(hermes_home=tmp_path)

    assert path == tmp_path / LIVE_ACTIVATION_PENDING_FILE
    assert loaded["title"] == "Hermes safe activation"
    assert "Pending live activation" in digest
    assert "hermes-gateway.service" in digest
    assert "2766381" in digest
    assert "No live restart has been performed" in digest


def test_render_live_activation_digest_is_empty_after_apply(tmp_path):
    plan = build_live_activation_plan(title="Hermes safe activation")
    plan["status"] = "applied"
    plan["live_restart_performed"] = True

    write_live_activation_plan(plan, hermes_home=tmp_path)

    assert render_live_activation_digest(hermes_home=tmp_path) == ""
