"""Run Claude Code through the local GPT route as a bounded Hermes tool.

This is intentionally a small print-mode wrapper rather than a generic shell
adapter. Claude Code 2.1.96 on this host does not expose ``--acp --stdio``, so
Hermes cannot use it as an ACP child directly. This tool gives Hermes a safe,
JSON-returning lane for planning, review, and small code fixes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error

DEFAULT_BASE_URL = "http://127.0.0.1:8317"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MAX_TURNS = {
    "plan": 4,
    "review": 5,
    "fix": 8,
}
MAX_TURNS_LIMIT = 12
MAX_TIMEOUT_SECONDS = 900
DEFAULT_TIMEOUT_SECONDS = 300

MODE_ALLOWED_TOOLS = {
    "plan": "Read",
    "review": "Read,Bash(git status *),Bash(git diff *),Bash(git diff --stat *)",
    "fix": "Read,Edit,Bash(python3 -m pytest *),Bash(pytest *),Bash(python -m pytest *)",
}

SENSITIVE_ENV_KEYS = (
    "CLAUDE_GPT_AUTH_TOKEN",
    "IMAGE_OPENAI_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)

CHILD_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "HERMES_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
)


def _read_dotenv_value(key: str, env_file: Optional[Path] = None) -> str:
    path = env_file or (get_hermes_home() / ".env")
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def _resolve_token() -> str:
    for key in SENSITIVE_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return _read_dotenv_value("IMAGE_OPENAI_API_KEY")


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def check_claude_code_requirements() -> bool:
    return bool(shutil.which(os.getenv("CLAUDE_GPT_CLAUDE_BIN", "claude"))) and bool(_resolve_token())


def _coerce_mode(mode: Optional[str]) -> str:
    normalized = str(mode or "review").strip().lower()
    if normalized not in MODE_ALLOWED_TOOLS:
        raise ValueError("mode must be one of: plan, review, fix")
    return normalized


def _clamp_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _resolve_workdir(workdir: Optional[str]) -> Path:
    raw = str(workdir or ".").strip() or "."
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"workdir does not exist or is not a directory: {path}")
    return path


def _parse_last_json(text: str) -> Optional[Dict[str, Any]]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _redacted_env(env: Dict[str, str]) -> Dict[str, str]:
    visible = {}
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "NO_PROXY"):
        if env.get(key):
            visible[key] = env[key]
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        visible["ANTHROPIC_AUTH_TOKEN"] = "<redacted>"
    return visible


def _secret_values(extra: Optional[Iterable[str]] = None) -> list[str]:
    values = set()
    for value in extra or []:
        text = str(value or "").strip()
        if len(text) >= 4:
            values.add(text)
    for key in SENSITIVE_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if len(value) >= 4:
            values.add(value)
    dotenv_value = _read_dotenv_value("IMAGE_OPENAI_API_KEY").strip()
    if len(dotenv_value) >= 4:
        values.add(dotenv_value)
    return sorted(values, key=len, reverse=True)


def _redact_text(text: Any, extra_secrets: Optional[Iterable[str]] = None) -> str:
    redacted = str(text or "")
    for secret in _secret_values(extra_secrets):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _redact_value(value: Any, extra_secrets: Optional[Iterable[str]] = None) -> Any:
    if isinstance(value, str):
        return _redact_text(value, extra_secrets)
    if isinstance(value, list):
        return [_redact_value(item, extra_secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, extra_secrets) for key, item in value.items()}
    return value


def _build_env(model: str, base_url: str, token: str) -> Dict[str, str]:
    env = {key: value for key in CHILD_ENV_ALLOWLIST if (value := os.getenv(key))}
    env.setdefault("HOME", str(Path.home()))
    default_user = os.getenv("USER") or os.getenv("LOGNAME") or Path.home().name
    env.setdefault("USER", default_user)
    env.setdefault("LOGNAME", env.get("USER", default_user))
    env.update(
        {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_API_KEY": token,
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        }
    )
    no_proxy = os.getenv("NO_PROXY", "")
    prefix = "127.0.0.1,localhost"
    env["NO_PROXY"] = prefix if not no_proxy else f"{prefix},{no_proxy}"
    return env


def _command_for(
    prompt: str,
    *,
    model: str,
    max_turns: int,
    allowed_tools: str,
    extra_args: Optional[Iterable[str]] = None,
) -> list[str]:
    claude_bin = os.getenv("CLAUDE_GPT_CLAUDE_BIN", "claude")
    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        "--no-session-persistence",
        "--allowedTools",
        allowed_tools,
    ]
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args if str(arg).strip())
    return cmd


def claude_code_task(
    prompt: str,
    workdir: Optional[str] = None,
    mode: str = "review",
    max_turns: Optional[int] = None,
    timeout: Optional[int] = None,
    allowed_tools: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> str:
    """Run a bounded Claude Code print-mode task through local GPT."""
    if not isinstance(prompt, str) or not prompt.strip():
        return tool_error("prompt is required")

    try:
        effective_mode = _coerce_mode(mode)
        cwd = _resolve_workdir(workdir)
    except ValueError as exc:
        return tool_error(str(exc))

    effective_model = str(model or os.getenv("CLAUDE_GPT_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    effective_base_url = str(base_url or os.getenv("CLAUDE_GPT_BASE_URL") or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    token = _resolve_token()
    if not token:
        return tool_error("Claude GPT route is missing an auth token. Set CLAUDE_GPT_AUTH_TOKEN or IMAGE_OPENAI_API_KEY.")

    if not shutil.which(os.getenv("CLAUDE_GPT_CLAUDE_BIN", "claude")):
        return tool_error("Claude Code CLI not found on PATH")

    default_turns = DEFAULT_MAX_TURNS[effective_mode]
    turns = _clamp_int(max_turns, default_turns, minimum=1, maximum=MAX_TURNS_LIMIT)
    effective_timeout = _clamp_int(timeout, DEFAULT_TIMEOUT_SECONDS, minimum=5, maximum=MAX_TIMEOUT_SECONDS)
    default_allowed_tools = MODE_ALLOWED_TOOLS[effective_mode]
    requested_allowed_tools = str(allowed_tools or "").strip()
    if requested_allowed_tools and requested_allowed_tools != default_allowed_tools:
        return tool_error("allowed_tools overrides are disabled; choose mode='plan', 'review', or 'fix' instead")
    effective_allowed_tools = default_allowed_tools

    env = _build_env(effective_model, effective_base_url, token)
    cmd = _command_for(
        prompt.strip(),
        model=effective_model,
        max_turns=turns,
        allowed_tools=effective_allowed_tools,
    )

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        return json.dumps(
            {
                "success": False,
                "error": "timeout",
                "timeout_seconds": effective_timeout,
                "stdout_tail": _redact_text(str(output)[-2000:], [token]),
                "workdir": str(cwd),
                "mode": effective_mode,
            },
            ensure_ascii=False,
        )
    except OSError as exc:
        return tool_error(f"failed to run Claude Code: {exc}")

    output = proc.stdout or ""
    payload = _parse_last_json(output)
    if payload is None:
        return json.dumps(
            {
                "success": False,
                "error": "Claude Code did not return parseable JSON",
                "process_exit": proc.returncode,
                "stdout_tail": _redact_text(output[-4000:], [token]),
                "workdir": str(cwd),
                "mode": effective_mode,
            },
            ensure_ascii=False,
        )

    if payload.get("type") != "result" or "is_error" not in payload:
        return json.dumps(
            {
                "success": False,
                "error": "Claude Code returned invalid result JSON",
                "process_exit": proc.returncode,
                "stdout_tail": _redact_text(output[-4000:], [token]),
                "workdir": str(cwd),
                "mode": effective_mode,
            },
            ensure_ascii=False,
        )

    is_error = payload.get("is_error") is not False
    result: Dict[str, Any] = {
        "success": not is_error,
        "is_error": is_error,
        "result": _redact_value(payload.get("result"), [token]),
        "subtype": _redact_value(payload.get("subtype"), [token]),
        "terminal_reason": _redact_value(payload.get("terminal_reason"), [token]),
        "stop_reason": _redact_value(payload.get("stop_reason"), [token]),
        "session_id": _redact_value(payload.get("session_id"), [token]),
        "num_turns": payload.get("num_turns"),
        "duration_ms": payload.get("duration_ms"),
        "process_exit": proc.returncode,
        "model_usage": payload.get("modelUsage"),
        "permission_denials": _redact_value(payload.get("permission_denials"), [token]),
        "errors": _redact_value(payload.get("errors"), [token]),
        "workdir": str(cwd),
        "mode": effective_mode,
        "model": effective_model,
        "allowed_tools": effective_allowed_tools,
        "env": _redacted_env(env),
    }
    if is_error or proc.returncode != 0:
        result["stdout_tail"] = _redact_text(output[-4000:], [token])
    return json.dumps(result, ensure_ascii=False)


CLAUDE_CODE_TASK_SCHEMA = {
    "name": "claude_code_task",
    "description": (
        "Run Claude Code in bounded print/json mode through the local GPT route. "
        "Use for code review, planning, and small fixes when Claude Code's file/code workflow is useful. "
        "This does not require a Claude subscription on this host; it pins Claude Code to gpt-5.5 via the local gateway."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Self-contained task for Claude Code. Include exact files, constraints, and expected output.",
            },
            "workdir": {
                "type": "string",
                "description": "Project directory to run in. Defaults to the current process directory.",
            },
            "mode": {
                "type": "string",
                "enum": ["plan", "review", "fix"],
                "description": "plan/review are read-focused. fix allows Edit plus common pytest commands for small patches.",
            },
            "max_turns": {
                "type": "integer",
                "description": "Claude Code turn cap. Clamped to 1-12.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Clamped to 5-900.",
            },
            "allowed_tools": {
                "type": "string",
                "description": "Advanced/internal only. Overrides are rejected by default; use mode to select the safe tool profile.",
            },
            "model": {
                "type": "string",
                "description": "Model to pin inside Claude Code. Defaults to gpt-5.5.",
            },
        },
        "required": ["prompt"],
    },
}


registry.register(
    name="claude_code_task",
    toolset="delegation",
    schema=CLAUDE_CODE_TASK_SCHEMA,
    handler=lambda args, **kw: claude_code_task(
        prompt=args.get("prompt", ""),
        workdir=args.get("workdir"),
        mode=args.get("mode", "review"),
        max_turns=args.get("max_turns"),
        timeout=args.get("timeout"),
        allowed_tools=args.get("allowed_tools"),
        model=args.get("model"),
    ),
    check_fn=check_claude_code_requirements,
    emoji="🧠",
)
