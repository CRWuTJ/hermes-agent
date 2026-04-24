"""
Status command for hermes CLI.

Shows the status of all Hermes Agent components.
"""

import os
import sys
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from hermes_cli.auth import AuthError, resolve_provider
from hermes_cli.colors import Colors, color
from hermes_cli.config import get_env_path, get_env_value, get_hermes_home, load_config
from hermes_cli.models import provider_label
from hermes_cli.nous_subscription import get_nous_subscription_features
from hermes_cli.runtime_provider import resolve_requested_provider
from hermes_constants import OPENROUTER_MODELS_URL
from tools.tool_backend_helpers import managed_nous_tools_enabled


def _format_harness_control_summary(action_payload) -> str | None:
    if not isinstance(action_payload, dict):
        return None
    action = str(action_payload.get("action") or "").strip()
    status = str(action_payload.get("status") or "").strip()
    surface = str(action_payload.get("surface") or "").strip()
    target_bucket = str(action_payload.get("target_bucket") or "").strip()
    created_at = str(action_payload.get("created_at") or "").strip()
    parts = [part for part in [action, status] if part]
    if surface:
        parts.append(f"via {surface}")
    if target_bucket:
        parts.append(target_bucket)
    if created_at:
        parts.append(created_at)
    return " · ".join(parts) if parts else None


def _format_harness_recent_control(control_payload) -> str | None:
    control = control_payload if isinstance(control_payload, dict) else {}
    latest_control_action = _format_harness_control_summary(control.get("latest_action"))
    latest_recovery = _format_harness_control_summary(control.get("latest_recovery"))
    if not latest_control_action and not latest_recovery:
        return None
    parts = []
    if latest_control_action:
        parts.append(latest_control_action)
    if latest_recovery and latest_recovery != latest_control_action:
        parts.append(f"recovery: {latest_recovery}")
    return " ; ".join(parts) if parts else None


def check_mark(ok: bool) -> str:
    if ok:
        return color("✓", Colors.GREEN)
    return color("✗", Colors.RED)

def redact_key(key: str) -> str:
    """Redact an API key for display."""
    if not key:
        return "(not set)"
    if len(key) < 12:
        return "***"
    return key[:4] + "..." + key[-4:]


def _format_iso_timestamp(value) -> str:
    """Format ISO timestamps for status output, converting to local timezone."""
    if not value or not isinstance(value, str):
        return "(unknown)"
    from datetime import datetime, timezone
    text = value.strip()
    if not text:
        return "(unknown)"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _configured_model_label(config: dict) -> str:
    """Return the configured default model from config.yaml."""
    model_cfg = config.get("model")
    if isinstance(model_cfg, dict):
        model = (model_cfg.get("default") or model_cfg.get("name") or "").strip()
    elif isinstance(model_cfg, str):
        model = model_cfg.strip()
    else:
        model = ""
    return model or "(not set)"


def _effective_provider_label() -> str:
    """Return the provider label matching current CLI runtime resolution."""
    requested = resolve_requested_provider()
    try:
        effective = resolve_provider(requested)
    except AuthError:
        effective = requested or "auto"

    if effective == "openrouter" and get_env_value("OPENAI_BASE_URL"):
        effective = "custom"

    return provider_label(effective)


def _gateway_restart_command(system: bool = False, user: bool = False) -> str:
    return f"{'sudo ' if system else ''}hermes gateway restart{' --system' if system else ' --user' if user else ''}"


def _gateway_repair_preview_command(system: bool = False) -> str:
    return f"hermes gateway repair{' --system' if system else ''}"


def _gateway_repair_apply_command(system: bool = False, cleanup_legacy: bool = False) -> str:
    command = f"{'sudo ' if system else ''}hermes gateway repair{' --system' if system else ''} --apply"
    if cleanup_legacy:
        command += " --cleanup-legacy"
    return command


def show_status(args):
    """Show status of all Hermes Agent components."""
    show_all = getattr(args, 'all', False)
    deep = getattr(args, 'deep', False)
    
    print()
    print(color("┌─────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                 ⚕ Hermes Agent Status                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────┘", Colors.CYAN))
    
    # =========================================================================
    # Environment
    # =========================================================================
    print()
    print(color("◆ Environment", Colors.CYAN, Colors.BOLD))
    print(f"  Project:      {PROJECT_ROOT}")
    print(f"  Python:       {sys.version.split()[0]}")
    
    env_path = get_env_path()
    print(f"  .env file:    {check_mark(env_path.exists())} {'exists' if env_path.exists() else 'not found'}")

    try:
        config = load_config()
    except Exception:
        config = {}

    print(f"  Model:        {_configured_model_label(config)}")
    print(f"  Provider:     {_effective_provider_label()}")
    
    # =========================================================================
    # API Keys
    # =========================================================================
    print()
    print(color("◆ API Keys", Colors.CYAN, Colors.BOLD))
    
    keys = {
        "OpenRouter": "OPENROUTER_API_KEY",
        "OpenAI": "OPENAI_API_KEY",
        "Z.AI/GLM": "GLM_API_KEY",
        "Kimi": "KIMI_API_KEY",
        "MiniMax": "MINIMAX_API_KEY",
        "MiniMax-CN": "MINIMAX_CN_API_KEY",
        "Firecrawl": "FIRECRAWL_API_KEY",
        "Tavily": "TAVILY_API_KEY",
        "Browser Use": "BROWSER_USE_API_KEY",  # Optional — local browser works without this
        "Browserbase": "BROWSERBASE_API_KEY",  # Optional — direct credentials only
        "FAL": "FAL_KEY",
        "Tinker": "TINKER_API_KEY",
        "WandB": "WANDB_API_KEY",
        "ElevenLabs": "ELEVENLABS_API_KEY",
        "GitHub": "GITHUB_TOKEN",
    }
    
    for name, env_var in keys.items():
        value = get_env_value(env_var) or ""
        has_key = bool(value)
        display = redact_key(value) if not show_all else value
        print(f"  {name:<12}  {check_mark(has_key)} {display}")

    anthropic_value = (
        get_env_value("ANTHROPIC_TOKEN")
        or get_env_value("ANTHROPIC_API_KEY")
        or ""
    )
    anthropic_display = redact_key(anthropic_value) if not show_all else anthropic_value
    print(f"  {'Anthropic':<12}  {check_mark(bool(anthropic_value))} {anthropic_display}")

    # =========================================================================
    # Auth Providers (OAuth)
    # =========================================================================
    print()
    print(color("◆ Auth Providers", Colors.CYAN, Colors.BOLD))

    try:
        from hermes_cli.auth import get_nous_auth_status, get_codex_auth_status
        nous_status = get_nous_auth_status()
        codex_status = get_codex_auth_status()
    except Exception:
        nous_status = {}
        codex_status = {}

    nous_logged_in = bool(nous_status.get("logged_in"))
    print(
        f"  {'Nous Portal':<12}  {check_mark(nous_logged_in)} "
        f"{'logged in' if nous_logged_in else 'not logged in (run: hermes model)'}"
    )
    if nous_logged_in:
        portal_url = nous_status.get("portal_base_url") or "(unknown)"
        access_exp = _format_iso_timestamp(nous_status.get("access_expires_at"))
        key_exp = _format_iso_timestamp(nous_status.get("agent_key_expires_at"))
        refresh_label = "yes" if nous_status.get("has_refresh_token") else "no"
        print(f"    Portal URL: {portal_url}")
        print(f"    Access exp: {access_exp}")
        print(f"    Key exp:    {key_exp}")
        print(f"    Refresh:    {refresh_label}")

    codex_logged_in = bool(codex_status.get("logged_in"))
    print(
        f"  {'OpenAI Codex':<12}  {check_mark(codex_logged_in)} "
        f"{'logged in' if codex_logged_in else 'not logged in (run: hermes model)'}"
    )
    codex_auth_file = codex_status.get("auth_store")
    if codex_auth_file:
        print(f"    Auth file:  {codex_auth_file}")
    codex_last_refresh = _format_iso_timestamp(codex_status.get("last_refresh"))
    if codex_status.get("last_refresh"):
        print(f"    Refreshed:  {codex_last_refresh}")
    if codex_status.get("error") and not codex_logged_in:
        print(f"    Error:      {codex_status.get('error')}")

    # =========================================================================
    # Nous Subscription Features
    # =========================================================================
    if managed_nous_tools_enabled():
        features = get_nous_subscription_features(config)
        print()
        print(color("◆ Nous Subscription Features", Colors.CYAN, Colors.BOLD))
        if not features.nous_auth_present:
            print("  Nous Portal   ✗ not logged in")
        else:
            print("  Nous Portal   ✓ managed tools available")
        for feature in features.items():
            if feature.managed_by_nous:
                state = "active via Nous subscription"
            elif feature.active:
                current = feature.current_provider or "configured provider"
                state = f"active via {current}"
            elif feature.included_by_default and features.nous_auth_present:
                state = "included by subscription, not currently selected"
            elif feature.key == "modal" and features.nous_auth_present:
                state = "available via subscription (optional)"
            else:
                state = "not configured"
            print(f"  {feature.label:<15} {check_mark(feature.available or feature.active or feature.managed_by_nous)} {state}")

    # =========================================================================
    # API-Key Providers
    # =========================================================================
    print()
    print(color("◆ API-Key Providers", Colors.CYAN, Colors.BOLD))

    apikey_providers = {
        "Z.AI / GLM":       ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        "Kimi / Moonshot":  ("KIMI_API_KEY",),
        "MiniMax":          ("MINIMAX_API_KEY",),
        "MiniMax (China)":  ("MINIMAX_CN_API_KEY",),
    }
    for pname, env_vars in apikey_providers.items():
        key_val = ""
        for ev in env_vars:
            key_val = get_env_value(ev) or ""
            if key_val:
                break
        configured = bool(key_val)
        label = "configured" if configured else "not configured (run: hermes model)"
        print(f"  {pname:<16} {check_mark(configured)} {label}")

    # =========================================================================
    # Terminal Configuration
    # =========================================================================
    print()
    print(color("◆ Terminal Backend", Colors.CYAN, Colors.BOLD))
    
    terminal_env = os.getenv("TERMINAL_ENV", "")
    if not terminal_env:
        # Fall back to config file value when env var isn't set
        # (hermes status doesn't go through cli.py's config loading)
        try:
            _cfg = load_config()
            terminal_env = _cfg.get("terminal", {}).get("backend", "local")
        except Exception:
            terminal_env = "local"
    print(f"  Backend:      {terminal_env}")
    
    if terminal_env == "ssh":
        ssh_host = os.getenv("TERMINAL_SSH_HOST", "")
        ssh_user = os.getenv("TERMINAL_SSH_USER", "")
        print(f"  SSH Host:     {ssh_host or '(not set)'}")
        print(f"  SSH User:     {ssh_user or '(not set)'}")
    elif terminal_env == "docker":
        docker_image = os.getenv("TERMINAL_DOCKER_IMAGE", "python:3.11-slim")
        print(f"  Docker Image: {docker_image}")
    elif terminal_env == "daytona":
        daytona_image = os.getenv("TERMINAL_DAYTONA_IMAGE", "nikolaik/python-nodejs:python3.11-nodejs20")
        print(f"  Daytona Image: {daytona_image}")
    
    sudo_password = os.getenv("SUDO_PASSWORD", "")
    print(f"  Sudo:         {check_mark(bool(sudo_password))} {'enabled' if sudo_password else 'disabled'}")
    
    # =========================================================================
    # Messaging Platforms
    # =========================================================================
    print()
    print(color("◆ Messaging Platforms", Colors.CYAN, Colors.BOLD))
    
    platforms = {
        "Telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL"),
        "Discord": ("DISCORD_BOT_TOKEN", "DISCORD_HOME_CHANNEL"),
        "WhatsApp": ("WHATSAPP_ENABLED", None),
        "Signal": ("SIGNAL_HTTP_URL", "SIGNAL_HOME_CHANNEL"),
        "Slack": ("SLACK_BOT_TOKEN", None),
        "Email": ("EMAIL_ADDRESS", "EMAIL_HOME_ADDRESS"),
        "SMS": ("TWILIO_ACCOUNT_SID", "SMS_HOME_CHANNEL"),
        "DingTalk": ("DINGTALK_CLIENT_ID", None),
        "Feishu": ("FEISHU_APP_ID", "FEISHU_HOME_CHANNEL"),
        "WeCom": ("WECOM_BOT_ID", "WECOM_HOME_CHANNEL"),
    }
    
    for name, (token_var, home_var) in platforms.items():
        token = os.getenv(token_var, "")
        has_token = bool(token)
        
        home_channel = ""
        if home_var:
            home_channel = os.getenv(home_var, "")
        
        status = "configured" if has_token else "not configured"
        if home_channel:
            status += f" (home: {home_channel})"
        
        print(f"  {name:<12}  {check_mark(has_token)} {status}")
    
    # =========================================================================
    # Gateway Status
    # =========================================================================
    print()
    print(color("◆ Gateway Service", Colors.CYAN, Colors.BOLD))
    
    if sys.platform.startswith('linux'):
        try:
            from hermes_cli.gateway import get_gateway_systemd_report, systemd_unit_path_is_current
            gateway_report = get_gateway_systemd_report()
        except Exception:
            gateway_report = {"installed": False}
            systemd_unit_path_is_current = None

        is_active = gateway_report.get("active") is True
        state_label = "running" if is_active else (gateway_report.get("state") or "stopped")
        manager_label = f"systemd ({gateway_report.get('scope')})" if gateway_report.get("installed") else "systemd (user)"
        print(f"  Status:       {check_mark(is_active)} {state_label}")
        print(f"  Manager:      {manager_label}")
        if gateway_report.get("installed"):
            unit_name = gateway_report.get("unit_name")
            active_system = bool(gateway_report.get("system"))
            unit_path = Path(gateway_report.get("unit_path")) if gateway_report.get("unit_path") else None
            outdated = bool(
                unit_path
                and unit_path.exists()
                and systemd_unit_path_is_current is not None
                and not systemd_unit_path_is_current(unit_path, system=active_system)
            )
            if gateway_report.get("drifted"):
                print(f"  Unit:         {unit_name} (legacy/non-canonical)")
                print("  Drift:        yes")
                print(f"  Preview:      {_gateway_repair_preview_command(system=active_system)}")
                print(f"  Apply:        {_gateway_repair_apply_command(system=active_system, cleanup_legacy=True)}")
            else:
                print(f"  Unit:         {unit_name}")
            if outdated:
                print("  Definition:   outdated")
                print(f"  Refresh:      {_gateway_restart_command(system=active_system)}")
        
    elif sys.platform == 'darwin':
        from hermes_cli.gateway import get_launchd_label
        try:
            result = subprocess.run(
                ["launchctl", "list", get_launchd_label()],
                capture_output=True,
                text=True,
                timeout=5
            )
            is_loaded = result.returncode == 0
        except subprocess.TimeoutExpired:
            is_loaded = False
        print(f"  Status:       {check_mark(is_loaded)} {'loaded' if is_loaded else 'not loaded'}")
        print("  Manager:      launchd")
    else:
        print(f"  Status:       {color('N/A', Colors.DIM)}")
        print("  Manager:      (not supported on this platform)")

    try:
        from gateway.status import build_gateway_status_payload, render_status_activity_block

        status_payload = build_gateway_status_payload()
        activity_block = render_status_activity_block(status_payload, style="cli")
        activity_lines = activity_block.splitlines() if activity_block else []
    except Exception:
        status_payload = {"live_tasks": {}, "cron": None}
        activity_lines = []

    active_lane_lines = [line for line in activity_lines if line.startswith("  Active lanes:")]
    cron_activity_lines = [line for line in activity_lines if not line.startswith("  Active lanes:")]
    for line in active_lane_lines:
        print(line)
    
    # =========================================================================
    # Cron Jobs
    # =========================================================================
    print()
    print(color("◆ Scheduled Jobs", Colors.CYAN, Colors.BOLD))

    if cron_activity_lines:
        for line in cron_activity_lines:
            print(line)
    else:
        print("  Jobs:         (error reading jobs file)")
    
    # =========================================================================
    # Harness
    # =========================================================================
    print()
    print(color("◆ Harness", Colors.CYAN, Colors.BOLD))
    try:
        from agent.harness import get_harness_manager

        harness_manager = get_harness_manager(config)
        if getattr(harness_manager, "enabled", False):
            harness_summary = harness_manager.summarize_tasks(limit=3)
            print(f"  Enabled:      yes")
            print(f"  Tasks:        {int(harness_summary.get('total') or 0)} tracked")
            states = harness_summary.get("states") if isinstance(harness_summary.get("states"), dict) else {}
            if states:
                state_parts = [f"{key}={value}" for key, value in sorted(states.items())]
                print(f"  States:       {' | '.join(state_parts)}")
            recent = harness_summary.get("recent") if isinstance(harness_summary.get("recent"), list) else []
            if recent:
                top = recent[0]
                goal = " ".join(str(top.get("goal") or "").split()).strip()
                goal = goal[:72] + "…" if len(goal) > 72 else goal
                print(
                    f"  Recent:       {top.get('task_id', '')} · {top.get('state', '')} · {top.get('surface', '')}"
                    + (f" · {goal}" if goal else "")
                )
                recent_control = _format_harness_recent_control(top.get("control"))
                if recent_control:
                    print(f"  Recent Control: {recent_control}")
            else:
                print("  Recent:       none")
        else:
            print("  Enabled:      no")
    except Exception:
        print("  Enabled:      (error reading harness state)")

    # =========================================================================
    # Sessions
    # =========================================================================
    print()
    print(color("◆ Sessions", Colors.CYAN, Colors.BOLD))
    
    sessions_file = get_hermes_home() / "sessions" / "sessions.json"
    if sessions_file.exists():
        import json
        try:
            with open(sessions_file, encoding="utf-8") as f:
                data = json.load(f)
                print(f"  Active:       {len(data)} session(s)")
        except Exception:
            print("  Active:       (error reading sessions file)")
    else:
        print("  Active:       0")
    
    # =========================================================================
    # Deep checks
    # =========================================================================
    if deep:
        print()
        print(color("◆ Deep Checks", Colors.CYAN, Colors.BOLD))
        
        # Check OpenRouter connectivity
        openrouter_key = os.getenv("OPENROUTER_API_KEY", "")
        if openrouter_key:
            try:
                import httpx
                response = httpx.get(
                    OPENROUTER_MODELS_URL,
                    headers={"Authorization": f"Bearer {openrouter_key}"},
                    timeout=10
                )
                ok = response.status_code == 200
                print(f"  OpenRouter:   {check_mark(ok)} {'reachable' if ok else f'error ({response.status_code})'}")
            except Exception as e:
                print(f"  OpenRouter:   {check_mark(False)} error: {e}")
        
        # Check gateway port
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', 18789))
            sock.close()
            # Port in use = gateway likely running
            port_in_use = result == 0
            # This is informational, not necessarily bad
            print(f"  Port 18789:   {'in use' if port_in_use else 'available'}")
        except OSError:
            pass
    
    print()
    print(color("─" * 60, Colors.DIM))
    print(color("  Run 'hermes doctor' for detailed diagnostics", Colors.DIM))
    print(color("  Run 'hermes setup' to configure", Colors.DIM))
    print()
