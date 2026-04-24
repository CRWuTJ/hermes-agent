#!/usr/bin/env python3
"""Generate the Hermes LiteLLM control-plane config.

The generated LiteLLM YAML intentionally references upstream credentials via
environment variables.  The companion env file is local-only runtime state.
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import yaml


CONTROL_MODELS = ("hermes-main", "hermes-fast", "hermes-vision", "hermes-fallback")


def _provider(config: Dict[str, Any], name: str) -> Dict[str, Any]:
    for entry in config.get("custom_providers") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return {}


def _model_context(entry: Dict[str, Any], model: str, default: int) -> int:
    models = entry.get("models") or {}
    spec = models.get(model) if isinstance(models, dict) else {}
    if isinstance(spec, dict):
        value = spec.get("context_length")
        if isinstance(value, int) and value > 0:
            return value
    return default


def _litellm_entry(
    *,
    alias: str,
    upstream_model: str,
    api_base: str,
    api_key_env: str,
    context_window: int,
    rpm: int,
    timeout: int,
) -> Dict[str, Any]:
    return {
        "model_name": alias,
        "litellm_params": {
            "model": f"openai/{upstream_model}",
            "api_base": api_base,
            "api_key": f"os.environ/{api_key_env}",
            "rpm": rpm,
            "timeout": timeout,
        },
        "model_info": {
            "mode": "chat",
            "max_input_tokens": context_window,
            "metadata": {
                "managed_by": "hermes-litellm-control",
            },
        },
    }


def build_litellm_config(config: Dict[str, Any]) -> Dict[str, Any]:
    codex = _provider(config, "gpt-mainline-codex-local")

    if not codex:
        raise ValueError("missing custom provider: gpt-mainline-codex-local")

    codex_base = str(codex.get("base_url") or "").rstrip("/")
    codex_key_env = "HERMES_EXECUTOR_GPT_SUBSCRIPTION_CODEX_KEY"
    codex_context = _model_context(codex, "gpt-5.5", 1_048_576)

    model_list = [
        _litellm_entry(
            alias="hermes-main",
            upstream_model="gpt-5.5",
            api_base=codex_base,
            api_key_env=codex_key_env,
            context_window=codex_context,
            rpm=120,
            timeout=180,
        ),
        _litellm_entry(
            alias="hermes-fast",
            upstream_model="gpt-5.5",
            api_base=codex_base,
            api_key_env=codex_key_env,
            context_window=codex_context,
            rpm=90,
            timeout=180,
        ),
        _litellm_entry(
            alias="hermes-vision",
            upstream_model="gpt-5.5",
            api_base=codex_base,
            api_key_env=codex_key_env,
            context_window=codex_context,
            rpm=60,
            timeout=180,
        ),
        _litellm_entry(
            alias="hermes-fallback",
            upstream_model="gpt-5.5",
            api_base=codex_base,
            api_key_env=codex_key_env,
            context_window=codex_context,
            rpm=60,
            timeout=180,
        ),
    ]

    return {
        "model_list": model_list,
        "router_settings": {
            "routing_strategy": "simple-shuffle",
            "num_retries": 2,
            "timeout": 180,
            "cooldown_time": 300,
            "allowed_fails": 3,
        },
        "litellm_settings": {
            "drop_params": True,
            "set_verbose": False,
        },
        "general_settings": {
            "master_key": "os.environ/HERMES_CONTROL_MASTER_KEY",
            "database_url": "os.environ/DATABASE_URL",
        },
    }


def _read_env_file(path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def build_env(config: Dict[str, Any], existing: Dict[str, str] | None = None) -> Dict[str, str]:
    existing = existing or {}
    codex = _provider(config, "gpt-mainline-codex-local")

    env = dict(existing)
    env.setdefault("HERMES_CONTROL_MASTER_KEY", f"sk-hermes-control-{secrets.token_urlsafe(24)}")
    env["HERMES_EXECUTOR_GPT_SUBSCRIPTION_CODEX_KEY"] = str(codex.get("api_key") or "")
    return env


def _write_env(path: Path, env: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in sorted(env.items()) if value]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def write_outputs(config_path: Path, out_path: Path, env_path: Path) -> Tuple[Path, Path]:
    config = yaml.safe_load(config_path.read_text()) or {}
    out = build_litellm_config(config)
    env = build_env(config, _read_env_file(env_path))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    _write_env(env_path, env)
    return out_path, env_path


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/home/wutj/.hermes/config.yaml")
    parser.add_argument("--out", default="/home/wutj/.hermes/litellm-control/config.yaml")
    parser.add_argument("--env", default="/home/wutj/.hermes/litellm-control/litellm-control.env")
    args = parser.parse_args(list(argv) if argv is not None else None)
    out, env = write_outputs(Path(args.config), Path(args.out), Path(args.env))
    print(f"wrote {out}")
    print(f"wrote {env}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
