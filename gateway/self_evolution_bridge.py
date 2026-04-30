"""Gateway-safe bridge for Hermes self-evolution runs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.self_evolution import render_concise_summary, run_self_evolution


_SELF_EVOLUTION_PATTERNS = (
    "全局审查一下系统",
    "自己反思一下系统",
    "系统进化部",
    "自我进化部",
    "看看 hermes 哪些地方该升级",
    "看看 Hermes 哪些地方该升级",
    "你来替我规划系统优化",
    "替我规划系统优化",
    "系统怎么优化",
)


def _self_evolution_config(config: Any = None) -> dict[str, Any]:
    raw = config.get("self_evolution", {}) if isinstance(config, dict) else {}
    return raw if isinstance(raw, dict) else {}


def self_evolution_enabled(config: Any = None) -> bool:
    cfg = _self_evolution_config(config)
    return bool(cfg.get("enabled", True))


def parse_self_evolution_request(text: str, config: Any = None) -> dict[str, str] | None:
    raw = str(text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    lowered = raw.lower()
    if any(pattern.lower() in lowered for pattern in _SELF_EVOLUTION_PATTERNS):
        return {"action": "self_evolution", "mode": "manual"}
    if re.search(r"(全局|整体).*(优化|升级|改造|审查)", raw):
        return {"action": "self_evolution", "mode": "manual"}
    return None


def run_self_evolution_for_gateway(config: Any = None, *, report_dir: str | Path | None = None) -> str:
    cfg = config if isinstance(config, dict) else {}
    if not self_evolution_enabled(cfg):
        return "Result:\n系统进化部当前未启用。\n\nBlocker:\nself_evolution.enabled=false。\n\nNext step:\n开启后可以先跑只读审查。"
    run, report_path = run_self_evolution(config=cfg, report_dir=report_dir, trigger="manual")
    return render_concise_summary(run, report_path=report_path)
