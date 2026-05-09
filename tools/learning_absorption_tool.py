#!/usr/bin/env python3
"""AI learning absorption tool for candidate cards and capability registry."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib.parse import urljoin

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now


LEARNING_DIR_NAME = "learning"
CAPABILITIES_FILE_NAME = "capabilities.json"
RADAR_SOURCES_FILE_NAME = "radar_sources.json"
RADAR_SEEN_FILE_NAME = "radar_seen.json"
RADAR_RUNS_FILE_NAME = "radar_runs.jsonl"
SPEECH_FEEDBACK_FILE_NAME = "speech_feedback.jsonl"
SPEECH_POLICY_TRACE_FILE_NAME = "speech_policy_trace.jsonl"

_ACTIVE_VERIFICATION_LEVELS = {
    "official",
    "local_smoke",
    "local_test",
    "applied",
    "user_validated",
}
_HIGH_CONFIDENCE_VERIFICATION_LEVELS = {
    "local_smoke",
    "local_test",
    "applied",
    "user_validated",
}
_HIGH_CHANGE_FLAGS = (
    "affects_routing",
    "affects_code",
    "affects_execution",
    "affects_default_behavior",
    "requires_new_dependency",
)
_FRONT_CHANNEL_REASONS = {
    "major_new_technique",
    "current_project_relevance",
    "execution_blocker",
    "direction_decision",
}
_WORD_RE = re.compile(r"[a-zA-Z0-9_]{3,}|[\u4e00-\u9fff]{2,}")
_MAX_CONTEXT_FINDING_CHARS = 260
_MAX_USAGE_LOG_ENTRIES = 20
_MAX_SPEECH_FEEDBACK_ENTRIES = 50
_MAX_SPEECH_POLICY_TRACE_ENTRIES = 50
_MAX_SPEECH_FEEDBACK_QUERY_CHARS = 240
_MAX_SPEECH_FEEDBACK_NOTE_CHARS = 240
_MAX_RADAR_SUMMARY_CHARS = 420
_MAX_TEACHER_BRIEF_FINDING_CHARS = 190
_AUTO_SELF_REVIEW_DUPLICATE_COOLDOWN_SECONDS = 10 * 60
_RADAR_JOB_NAME = "Hermes AI learning radar"
_RADAR_JOB_PROMPT = (
    "Run learning_absorption with action=radar_run, register=true, max_items=8. "
    "Then run learning_absorption with action=learning_report, limit=8. "
    "Use the report.front_channel_summary as the final reply when it is not [SILENT]. "
    "The summary should cover newly registered findings when they affect AI product usage, "
    "agent workflows, prompt/tool practices, evaluations, model routing, Hermes operation, "
    "or recent speech_feedback recommendations. "
    "For any non-silent finding, include front_channel_reason only when it is a major_new_technique, "
    "current_project_relevance, execution_blocker, or direction_decision. "
    "If report.front_channel_summary is [SILENT], reply exactly [SILENT]. Do not change code or configuration."
)
_DEFAULT_RADAR_SOURCES = [
    {
        "name": "OpenAI News",
        "url": "https://openai.com/news/rss.xml",
        "source_type": "official",
        "applies_to": ["OpenAI", "ChatGPT", "Codex", "agents", "AI product usage"],
    },
    {
        "name": "Anthropic News",
        "url": "https://www.anthropic.com/news",
        "source_type": "official",
        "format": "html",
        "item_path_contains": "/news/",
        "applies_to": ["Claude", "agents", "AI product usage"],
    },
    {
        "name": "Google AI Blog",
        "url": "https://blog.google/technology/ai/rss/",
        "source_type": "official",
        "applies_to": ["Gemini", "AI product usage", "model capabilities"],
    },
    {
        "name": "Microsoft AI Blog",
        "url": "https://blogs.microsoft.com/ai/feed/",
        "source_type": "official",
        "applies_to": ["Copilot", "AI product usage", "workflows"],
    },
]
_RADAR_KEYWORDS = {
    "agent": 5,
    "agents": 5,
    "assistant": 4,
    "assistants": 4,
    "tool": 4,
    "tools": 4,
    "tool use": 8,
    "eval": 5,
    "evals": 5,
    "evaluation": 4,
    "prompt": 4,
    "prompts": 4,
    "workflow": 4,
    "workflows": 4,
    "automation": 4,
    "mcp": 4,
    "api": 3,
    "model": 3,
    "models": 3,
    "reasoning": 3,
    "memory": 3,
    "browser": 3,
    "codex": 5,
    "chatgpt": 5,
    "claude": 4,
    "gemini": 4,
    "copilot": 4,
    "guide": 4,
    "cookbook": 4,
    "best practice": 5,
    "release": 2,
}
_TEACHER_QUERY_MARKERS = (
    "ai",
    "agent",
    "agents",
    "assistant",
    "chatgpt",
    "claude",
    "gemini",
    "copilot",
    "codex",
    "prompt",
    "tool",
    "workflow",
    "模型",
    "助手",
    "老师",
    "学习",
    "技巧",
    "用法",
    "怎么",
    "如何",
    "为什么",
    "方案",
    "下一步",
    "工作流",
    "工具",
    "提示词",
)
_QUIET_EXECUTION_MARKERS = (
    "直接",
    "执行",
    "修复",
    "实现",
    "改完",
    "完成后",
    "跑测试",
    "部署",
    "不要解释",
    "无需解释",
    "别解释",
    "少说",
    "安静",
    "只做",
    "直接做",
    "fix",
    "repair",
    "implement",
    "run tests",
    "no explanation",
    "quiet",
)
_DIRECTION_DISCUSSION_MARKERS = (
    "讨论",
    "方向",
    "取舍",
    "方案",
    "规划",
    "下一步",
    "要不要",
    "是否",
    "选择",
    "路线",
    "tradeoff",
    "plan",
    "strategy",
    "direction",
)
_TEACHING_INTENT_MARKERS = (
    "教我",
    "学习",
    "解释",
    "为什么",
    "怎么",
    "如何",
    "原理",
    "技巧",
    "用法",
    "帮我理解",
    "teach",
    "learn",
    "explain",
    "why",
    "how",
)
_SPEECH_FEEDBACK_OUTCOMES = {
    "right",
    "too_much",
    "too_little",
    "wrong_mode",
    "missed_decision",
    "unclear",
}
_SPEECH_TOO_MUCH_MARKERS = (
    "讲太多",
    "说太多",
    "太多了",
    "啰嗦",
    "不用解释",
    "不要解释",
    "应该直接",
    "直接做",
    "直接执行",
    "too much",
    "verbose",
)
_SPEECH_TOO_LITTLE_MARKERS = (
    "没讲清楚",
    "没有讲清楚",
    "应该解释",
    "多解释",
    "讲一下",
    "解释一下",
    "too little",
    "explain more",
)
_SPEECH_MISSED_DECISION_MARKERS = (
    "提醒我风险",
    "提醒风险",
    "先提醒",
    "应该提醒",
    "没提醒",
    "没有提醒",
    "影响方向",
    "影响默认",
    "risk first",
    "warn me",
)
_RESPONSE_TEACHING_MARKERS = (
    "我会参考这些已沉淀",
    "已沉淀的 ai 使用经验",
    "这对当前问题的用法",
    "关键取舍",
    "先解释",
    "解释",
    "为什么",
    "原理",
    "技巧",
    "用法",
    "teach",
    "explain",
)
_RESPONSE_DECISION_MARKERS = (
    "风险",
    "影响",
    "取舍",
    "选择",
    "路线",
    "默认",
    "路由",
    "验证",
    "可选",
    "回滚",
    "阻塞",
    "先提醒",
    "decision",
    "risk",
    "tradeoff",
)
_RESPONSE_ACTION_MARKERS = (
    "已",
    "完成",
    "改好",
    "修复",
    "实现",
    "部署",
    "测试",
    "验证",
    "done",
    "fixed",
    "implemented",
)


def _learning_dir() -> Path:
    return get_hermes_home() / LEARNING_DIR_NAME


def _capabilities_path() -> Path:
    return _learning_dir() / CAPABILITIES_FILE_NAME


def _radar_sources_path() -> Path:
    return _learning_dir() / RADAR_SOURCES_FILE_NAME


def _radar_seen_path() -> Path:
    return _learning_dir() / RADAR_SEEN_FILE_NAME


def _radar_runs_path() -> Path:
    return _learning_dir() / RADAR_RUNS_FILE_NAME


def _speech_feedback_path() -> Path:
    return _learning_dir() / SPEECH_FEEDBACK_FILE_NAME


def _speech_policy_trace_path() -> Path:
    return _learning_dir() / SPEECH_POLICY_TRACE_FILE_NAME


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, parsed)


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _plain_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokens(value: Any) -> set[str]:
    text = str(value or "").lower()
    return {match.group(0) for match in _WORD_RE.finditer(text)}


def _candidate_id(candidate: Dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "finding": str(candidate.get("finding") or "").strip(),
            "source_url": str(candidate.get("source_url") or "").strip(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _candidate_card(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _candidate_id(candidate),
        "finding": str(candidate.get("finding") or "").strip(),
        "source_url": str(candidate.get("source_url") or "").strip(),
        "source_type": _norm(candidate.get("source_type")) or "unknown",
        "verification_level": _norm(candidate.get("verification_level")) or "unverified",
        "impact": _norm(candidate.get("impact")) or "medium",
        "risk": _norm(candidate.get("risk")) or "medium",
        "applies_to": _as_list(candidate.get("applies_to")),
        "reusable_workflow": _as_bool(candidate.get("reusable_workflow")),
        "affects_routing": _as_bool(candidate.get("affects_routing")),
        "affects_code": _as_bool(candidate.get("affects_code")),
        "affects_execution": _as_bool(candidate.get("affects_execution")),
        "affects_default_behavior": _as_bool(candidate.get("affects_default_behavior")),
        "requires_new_dependency": _as_bool(candidate.get("requires_new_dependency")),
        "front_channel_reason": _norm(candidate.get("front_channel_reason")),
        "notes": str(candidate.get("notes") or "").strip(),
    }


def _required_evidence(card: Dict[str, Any]) -> List[str]:
    evidence = []
    if card["source_type"] != "official" and card["verification_level"] not in _ACTIVE_VERIFICATION_LEVELS:
        evidence.append("official source confirmation or second independent source")
    if any(card.get(flag) for flag in _HIGH_CHANGE_FLAGS) or card["risk"] == "high":
        evidence.append("local smoke test on the real Hermes path")
        evidence.append("rollback or disable path documented")
    if not evidence:
        evidence.append("short rationale and usage example")
    return evidence


def _can_apply_by_default(card: Dict[str, Any]) -> bool:
    high_change = any(card.get(flag) for flag in _HIGH_CHANGE_FLAGS)
    if high_change or card["risk"] == "high":
        return card["verification_level"] in _HIGH_CONFIDENCE_VERIFICATION_LEVELS
    if card["risk"] == "medium" and card["verification_level"] not in _ACTIVE_VERIFICATION_LEVELS:
        return False
    return card["verification_level"] in _ACTIVE_VERIFICATION_LEVELS or card["source_type"] == "official"


def _front_channel_gate(card: Dict[str, Any]) -> Dict[str, Any]:
    reason = card.get("front_channel_reason") or ""
    if reason in _FRONT_CHANNEL_REASONS:
        return {
            "allow": True,
            "reason": reason,
            "default_response": "brief_user",
            "policy": "Only deliver major techniques, current-project relevance, execution blockers, or direction decisions.",
        }
    return {
        "allow": False,
        "reason": "local_only",
        "default_response": "[SILENT]",
        "policy": "Keep ordinary source notes, generic news, and weak candidates local.",
    }


def decide_learning_absorption(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Return the absorption decision for one learning candidate."""
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be an object")
    card = _candidate_card(candidate)
    if not card["finding"]:
        raise ValueError("candidate.finding is required")

    can_apply = _can_apply_by_default(card)
    high_change = any(card.get(flag) for flag in _HIGH_CHANGE_FLAGS)

    if not can_apply and (high_change or card["risk"] == "high"):
        promotion_target = "verification_backlog"
    elif card["reusable_workflow"]:
        promotion_target = "playbook"
    elif high_change:
        promotion_target = "routing_default"
    elif card["impact"] == "high":
        promotion_target = "user_brief"
    elif card["source_type"] == "official":
        promotion_target = "knowledge_base"
    else:
        promotion_target = "source_note"

    registry_status = "active" if can_apply else "pending_verification"
    if promotion_target == "source_note":
        registry_status = "recorded"

    return {
        "candidate_card": card,
        "decision": {
            "promotion_target": promotion_target,
            "can_apply_by_default": can_apply,
            "registry_status": registry_status,
            "required_evidence": _required_evidence(card),
            "next_action": _next_action(promotion_target, registry_status),
            "front_channel": _front_channel_gate(card),
        },
    }


def _next_action(promotion_target: str, registry_status: str) -> str:
    if promotion_target == "verification_backlog":
        return "Verify locally before changing default Hermes behavior."
    if promotion_target == "playbook":
        return "Register as an active reusable workflow and reference it in relevant discussions."
    if promotion_target == "routing_default":
        return "Register only after route-specific smoke tests pass."
    if promotion_target == "user_brief":
        return "Summarize to the user when it affects current direction."
    if promotion_target == "knowledge_base":
        return "Store as durable reference material."
    if registry_status == "pending_verification":
        return "Keep in backlog until evidence improves."
    return "Record for later retrieval."


def _load_registry() -> Dict[str, Any]:
    path = _capabilities_path()
    if not path.exists():
        return {"capabilities": [], "updated_at": None}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"capabilities": [], "updated_at": None}
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = []
    return {"capabilities": capabilities, "updated_at": data.get("updated_at")}


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_radar_sources() -> List[Dict[str, Any]]:
    path = _radar_sources_path()
    if not path.exists():
        return [dict(source) for source in _DEFAULT_RADAR_SOURCES]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(source) for source in _DEFAULT_RADAR_SOURCES]
    sources = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(sources, list):
        return [dict(source) for source in _DEFAULT_RADAR_SOURCES]
    return [dict(source) for source in sources if isinstance(source, dict) and str(source.get("url") or "").strip()]


def _load_seen_items() -> Dict[str, Dict[str, Any]]:
    path = _radar_seen_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    seen = data.get("seen") if isinstance(data, dict) else data
    if isinstance(seen, dict):
        return {str(key): value for key, value in seen.items() if isinstance(value, dict)}
    if isinstance(seen, list):
        return {
            str(item.get("id")): item
            for item in seen
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
    return {}


def _save_seen_items(seen: Dict[str, Dict[str, Any]]) -> None:
    payload = {
        "seen": seen,
        "updated_at": _hermes_now().isoformat(),
    }
    _atomic_write_json(_radar_seen_path(), payload)


def _append_radar_run(record: Dict[str, Any]) -> None:
    path = _radar_runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _active_default_capability(capability: Dict[str, Any]) -> bool:
    return _norm(capability.get("status")) == "active" and _as_bool(capability.get("can_apply_by_default"))


def _capability_score(capability: Dict[str, Any], query: str) -> int:
    query_text = str(query or "").strip().lower()
    if not query_text:
        return 1

    score = 0
    query_tokens = _tokens(query_text)
    finding_tokens = _tokens(capability.get("finding"))
    score += len(query_tokens.intersection(finding_tokens)) * 2

    for applies_to in _as_list(capability.get("applies_to")):
        phrase = applies_to.lower()
        if phrase and phrase in query_text:
            score += 10
        score += len(query_tokens.intersection(_tokens(phrase))) * 3

    if str(capability.get("promotion_target") or "") == "playbook":
        score += 1
    return score


def select_applicable_capabilities(query: str, *, limit: int = 3) -> List[Dict[str, Any]]:
    """Return active, default-safe capabilities relevant to a user turn."""

    safe_limit = _positive_int(limit, default=3)
    selected: list[tuple[int, Dict[str, Any]]] = []
    for capability in _load_registry().get("capabilities", []):
        if not isinstance(capability, dict) or not _active_default_capability(capability):
            continue
        score = _capability_score(capability, query)
        if query and score <= 0:
            continue
        item = dict(capability)
        item["score"] = score
        selected.append((score, item))

    selected.sort(
        key=lambda pair: (
            pair[0],
            str(pair[1].get("last_used_at") or ""),
            str(pair[1].get("updated_at") or ""),
        ),
        reverse=True,
    )
    return [item for _, item in selected[:safe_limit]]


def build_capability_invocation_context(query: str, *, limit: int = 3) -> str:
    """Build an API-only context block for active learning capabilities."""

    capabilities = select_applicable_capabilities(query, limit=limit)
    if not capabilities:
        return ""
    speech_policy = decide_teacher_speech_policy(query, capabilities)
    if not _looks_like_speech_feedback(query):
        _record_speech_policy_trace(query, speech_policy, capabilities, source="invocation_context")

    lines = [
        "<hermes-learning-capabilities>",
        "[System note: Apply these active capabilities when they fit this turn. "
        "Do not force irrelevant capabilities. If one materially shapes the answer "
        "or execution path, call learning_absorption action=record_use after use.]",
    ]
    for capability in capabilities:
        applies_to = ", ".join(_as_list(capability.get("applies_to"))) or "general"
        lines.append(
            "- "
            f"id={capability.get('id')} "
            f"target={capability.get('promotion_target')} "
            f"verification={capability.get('verification_level')} "
            f"applies_to={applies_to}\n"
            f"  finding: {_clip(capability.get('finding'), _MAX_CONTEXT_FINDING_CHARS)}"
        )
        next_action = str(capability.get("next_action") or "").strip()
        if next_action:
            lines.append(f"  use: {_clip(next_action, 180)}")
    if speech_policy["should_speak"]:
        lines.append(
            f"[teacher_mode: mode={speech_policy['mode']} reason={speech_policy['reason']}. "
            "用中文简短解释哪些已沉淀经验适用于当前问题，再给出下一步建议；不要长篇授课。]"
        )
    elif speech_policy["mode"] == "execute_quietly":
        lines.append(
            "[speech_policy: mode=execute_quietly. The user asked for direct execution; "
            "use relevant learned capabilities silently and report only result, blocker, and verification.]"
        )
    lines.extend(_speech_feedback_context_lines())
    lines.append("</hermes-learning-capabilities>")
    return "\n".join(lines)


def _query_wants_teacher_mode(query: str) -> bool:
    text = str(query or "").lower()
    return any(marker in text for marker in _TEACHER_QUERY_MARKERS)


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _needs_user_attention(capabilities: List[Dict[str, Any]]) -> bool:
    for capability in capabilities:
        target = _norm(capability.get("promotion_target"))
        if target in {"user_brief", "routing_default", "verification_backlog"}:
            return True
    return False


def decide_teacher_speech_policy(query: str, capabilities: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """Decide whether Hermes should teach, discuss direction, or stay quiet."""

    selected = capabilities if capabilities is not None else select_applicable_capabilities(query)
    text = str(query or "").lower()
    explicit_quiet = _contains_marker(text, _QUIET_EXECUTION_MARKERS)
    explicit_teaching = _contains_marker(text, _TEACHING_INTENT_MARKERS)
    needs_attention = _needs_user_attention(selected)

    if not selected:
        return {
            "mode": "silent",
            "should_speak": False,
            "reason": "no_relevant_capability",
        }
    if explicit_quiet and not needs_attention:
        return {
            "mode": "execute_quietly",
            "should_speak": False,
            "reason": "direct_execution_request",
        }
    if needs_attention:
        return {
            "mode": "decision",
            "should_speak": True,
            "reason": "learned_capability_may_affect_user_direction",
        }
    if explicit_teaching:
        return {
            "mode": "teach",
            "should_speak": True,
            "reason": "learning_or_ai_usage_request",
        }
    if _contains_marker(text, _DIRECTION_DISCUSSION_MARKERS):
        return {
            "mode": "direction",
            "should_speak": True,
            "reason": "direction_discussion",
        }
    return {
        "mode": "execute_quietly",
        "should_speak": False,
        "reason": "task_can_use_capability_without_front_channel_teaching",
    }


def _teacher_target_label(capability: Dict[str, Any]) -> str:
    target = _norm(capability.get("promotion_target"))
    if target == "playbook":
        return "可复用做法"
    if target == "user_brief":
        return "需要主动说明的变化"
    if target == "knowledge_base":
        return "背景知识"
    if target == "routing_default":
        return "执行策略候选"
    return "参考经验"


def _recent_capabilities(limit: int) -> List[Dict[str, Any]]:
    safe_limit = _positive_int(limit, default=5)
    capabilities = [
        dict(item)
        for item in _load_registry().get("capabilities", [])
        if isinstance(item, dict)
    ]
    capabilities.sort(
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
        reverse=True,
    )
    return capabilities[:safe_limit]


def _high_attention_capabilities(capabilities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        item
        for item in capabilities
        if _norm(item.get("promotion_target")) in {"user_brief", "routing_default", "verification_backlog"}
    ]


def build_learning_report(*, limit: int = 5) -> Dict[str, Any]:
    """Build a concise report of recently absorbed AI-use knowledge."""

    registry = _load_registry()
    safe_limit = _positive_int(limit, default=5)
    recent = _recent_capabilities(safe_limit)
    high_attention = _high_attention_capabilities(recent)
    speech_feedback = summarize_speech_feedback(limit=20)

    lines: List[str] = []
    if recent:
        lines.append("最近学习沉淀：")
        for capability in recent:
            label = _teacher_target_label(capability)
            finding = _clip(capability.get("finding"), 140)
            lines.append(f"- {capability.get('id')} [{label}] {finding}")
    if high_attention:
        lines.append("需要主动提醒你的发现：")
        for capability in high_attention:
            finding = _clip(capability.get("finding"), 120)
            lines.append(f"- {capability.get('id')}：{finding}")
    if speech_feedback.get("problem_events"):
        lines.append("表达方式调整：")
        for recommendation in speech_feedback.get("recommendations", [])[:3]:
            lines.append(f"- {recommendation}")

    return {
        "generated_at": _hermes_now().isoformat(),
        "capability_count": len(registry.get("capabilities", [])),
        "recent_capabilities": recent,
        "high_attention_capabilities": high_attention,
        "speech_feedback": speech_feedback,
        "front_channel_summary": "\n".join(lines) if lines else "[SILENT]",
    }


def build_teacher_brief(query: str, *, limit: int = 3) -> Dict[str, Any]:
    """Build a concise Chinese teaching brief from relevant learned capabilities."""

    capabilities = select_applicable_capabilities(query, limit=limit)
    speech_policy = decide_teacher_speech_policy(query, capabilities)
    speech_trace = _record_speech_policy_trace(query, speech_policy, capabilities, source="teacher_brief")
    if not speech_policy["should_speak"]:
        return {
            "should_speak": False,
            "brief": "",
            "speech_policy": speech_policy,
            "speech_trace": speech_trace,
            "capabilities": capabilities,
        }

    if speech_policy["mode"] == "decision":
        heading = "这条已沉淀经验可能影响当前方向，需要你关注："
        use_line = "这对当前问题的用法：先说明它会影响什么，再决定是否调整方案；涉及默认行为、路由或代码时先验证。"
        next_line = "下一步：我会先把影响和可选路线讲清楚，再继续执行。"
    elif speech_policy["mode"] == "direction":
        heading = "我会用这些已沉淀经验来帮助确认方向："
        use_line = "这对当前问题的用法：把经验转成可比较的路线、风险和验证方式。"
        next_line = "下一步：我会先和你确认方向与取舍，方向清楚后再自己执行、验证并沉淀结果。"
    else:
        heading = "我会参考这些已沉淀的 AI 使用经验："
        use_line = "这对当前问题的用法：先把相关技巧转成可执行方案；如果会影响默认行为、路由或代码，再先验证后落地。"
        next_line = "下一步：我会先解释关键取舍，再自己执行、验证并把结果沉淀回学习库。"

    lines = [heading]
    for index, capability in enumerate(capabilities, 1):
        label = _teacher_target_label(capability)
        finding = _clip(capability.get("finding"), _MAX_TEACHER_BRIEF_FINDING_CHARS)
        lines.append(f"{index}. [{label}] id={capability.get('id')}：{finding}")

    lines.extend([use_line, next_line])
    return {
        "should_speak": True,
        "brief": "\n".join(lines),
        "speech_policy": speech_policy,
        "speech_trace": speech_trace,
        "capabilities": capabilities,
    }


def _load_speech_feedback_events(*, limit: int = _MAX_SPEECH_FEEDBACK_ENTRIES) -> List[Dict[str, Any]]:
    path = _speech_feedback_path()
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    safe_limit = _positive_int(limit, default=_MAX_SPEECH_FEEDBACK_ENTRIES)
    return events[-safe_limit:]


def _append_speech_feedback_event(event: Dict[str, Any]) -> None:
    path = _speech_feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(event, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _append_jsonl(path: Path, event: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(event, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_jsonl(path: Path, *, limit: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    safe_limit = _positive_int(limit, default=50)
    return events[-safe_limit:]


def _speech_trace_id(event: Dict[str, Any]) -> str:
    seed = json.dumps(event, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _record_speech_policy_trace(
    query: str,
    speech_policy: Dict[str, Any],
    capabilities: List[Dict[str, Any]],
    *,
    source: str,
) -> Dict[str, Any]:
    event = {
        "recorded_at": _hermes_now().isoformat(),
        "source": source,
        "query": _clip(query, _MAX_SPEECH_FEEDBACK_QUERY_CHARS),
        "speech_policy": {
            "mode": _norm(speech_policy.get("mode")) or "unknown",
            "should_speak": bool(speech_policy.get("should_speak")),
            "reason": _norm(speech_policy.get("reason")) or "unknown",
        },
        "capability_ids": [str(item.get("id")) for item in capabilities if item.get("id")],
        "capability_targets": [
            _norm(item.get("promotion_target")) or "unknown"
            for item in capabilities
            if item.get("promotion_target")
        ],
    }
    event["trace_id"] = _speech_trace_id(event)
    _append_jsonl(_speech_policy_trace_path(), event)
    return event


def _latest_speech_policy_trace() -> Dict[str, Any] | None:
    traces = _load_jsonl(_speech_policy_trace_path(), limit=_MAX_SPEECH_POLICY_TRACE_ENTRIES)
    return traces[-1] if traces else None


def _looks_like_speech_feedback(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            *_SPEECH_TOO_MUCH_MARKERS,
            *_SPEECH_TOO_LITTLE_MARKERS,
            *_SPEECH_MISSED_DECISION_MARKERS,
            "刚才",
            "上次",
            "你应该",
        )
    )


def _infer_speech_feedback_from_text(text: str) -> Dict[str, str]:
    lowered = text.lower()
    if _contains_marker(lowered, _SPEECH_MISSED_DECISION_MARKERS) or (
        "风险" in lowered and ("提醒" in lowered or "先" in lowered or "应该" in lowered)
    ):
        return {"outcome": "missed_decision", "expected_mode": "decision"}
    if _contains_marker(lowered, _SPEECH_TOO_MUCH_MARKERS):
        return {"outcome": "too_much", "expected_mode": "execute_quietly"}
    if _contains_marker(lowered, _SPEECH_TOO_LITTLE_MARKERS):
        return {"outcome": "too_little", "expected_mode": "teach"}
    if "方向" in lowered and ("应该" in lowered or "不是" in lowered):
        return {"outcome": "wrong_mode", "expected_mode": "direction"}
    return {}


def summarize_speech_feedback(*, limit: int = _MAX_SPEECH_FEEDBACK_ENTRIES) -> Dict[str, Any]:
    events = _load_speech_feedback_events(limit=limit)
    outcome_counts: Dict[str, int] = {}
    mode_pairs: Dict[str, int] = {}
    problem_events = 0
    for event in events:
        outcome = _norm(event.get("outcome")) or "unclear"
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        if outcome != "right":
            problem_events += 1
        actual = _norm(event.get("actual_mode")) or "unknown"
        expected = _norm(event.get("expected_mode")) or "unknown"
        pair = f"{actual}->{expected}"
        mode_pairs[pair] = mode_pairs.get(pair, 0) + 1

    recommendations: List[str] = []
    if outcome_counts.get("too_much", 0) > 0:
        recommendations.append("少解释：直接执行类请求优先静默使用经验，只报告结果、阻塞和验证。")
    if outcome_counts.get("too_little", 0) > 0:
        recommendations.append("该讲时要讲：用户询问学习、理解、方向或取舍时，先简短说明相关经验。")
    if outcome_counts.get("wrong_mode", 0) > 0:
        recommendations.append("复查模式：对照 actual_mode 与 expected_mode，避免把方向讨论当成教学或把执行请求当成讲解。")
    if outcome_counts.get("missed_decision", 0) > 0:
        recommendations.append("不要漏掉决策提醒：影响默认行为、路由或高风险执行时，先提示影响和可选路线。")
    if not recommendations and events:
        recommendations.append("继续沿用当前 speech_policy；只在用户反馈出现偏差时调整。")

    return {
        "total_events": len(events),
        "problem_events": problem_events,
        "outcome_counts": outcome_counts,
        "mode_pairs": mode_pairs,
        "recommendations": recommendations,
        "recent_events": events[-5:],
    }


def _response_looks_like_teaching(response: str) -> bool:
    text = str(response or "").lower()
    plain = _plain_text(text)
    return _contains_marker(text, _RESPONSE_TEACHING_MARKERS) or (
        len(plain) >= 260 and _contains_marker(text, _TEACHING_INTENT_MARKERS)
    )


def _response_has_decision_context(response: str) -> bool:
    text = str(response or "").lower()
    return _contains_marker(text, _RESPONSE_DECISION_MARKERS)


def _response_is_too_thin_for_teaching(response: str) -> bool:
    text = str(response or "").lower()
    plain = _plain_text(text)
    if len(plain) >= 70:
        return False
    return not (
        _contains_marker(text, _RESPONSE_TEACHING_MARKERS)
        or _contains_marker(text, _RESPONSE_DECISION_MARKERS)
        or _contains_marker(text, _TEACHING_INTENT_MARKERS)
    )


def _parse_recorded_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _events_have_same_capabilities(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return sorted(_as_list(left.get("capability_ids"))) == sorted(_as_list(right.get("capability_ids")))


def _queries_are_similar(left: Any, right: Any) -> bool:
    left_text = re.sub(r"\s+", " ", str(left or "").strip().lower())
    right_text = re.sub(r"\s+", " ", str(right or "").strip().lower())
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True

    left_tokens = _tokens(left_text)
    right_tokens = _tokens(right_text)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens.intersection(right_tokens))
    return overlap / max(1, min(len(left_tokens), len(right_tokens))) >= 0.75


def _find_recent_auto_self_review_duplicate(candidate: Dict[str, Any]) -> Dict[str, Any] | None:
    now = _hermes_now()
    for event in reversed(_load_speech_feedback_events(limit=20)):
        if event.get("source") != "auto_self_review":
            continue
        if _norm(event.get("outcome")) != _norm(candidate.get("outcome")):
            continue
        if _norm(event.get("actual_mode")) != _norm(candidate.get("actual_mode")):
            continue
        if _norm(event.get("expected_mode")) != _norm(candidate.get("expected_mode")):
            continue
        if not _events_have_same_capabilities(event, candidate):
            continue
        if not _queries_are_similar(event.get("query"), candidate.get("query")):
            continue

        recorded_at = _parse_recorded_at(event.get("recorded_at"))
        if recorded_at is None:
            continue
        if recorded_at.tzinfo is None and now.tzinfo is not None:
            recorded_at = recorded_at.replace(tzinfo=now.tzinfo)
        age_seconds = (now - recorded_at).total_seconds()
        if age_seconds < 0:
            age_seconds = 0
        if age_seconds <= _AUTO_SELF_REVIEW_DUPLICATE_COOLDOWN_SECONDS:
            return event
    return None


def evaluate_speech_response(query: str, response: str, *, limit: int = 3) -> Dict[str, Any]:
    """Evaluate Hermes's final response against the learned speech policy."""

    capabilities = select_applicable_capabilities(query, limit=limit)
    speech_policy = decide_teacher_speech_policy(query, capabilities)
    mode = _norm(speech_policy.get("mode")) or "unknown"
    result: Dict[str, Any] = {
        "feedback_recorded": False,
        "reason": "aligned_or_unclear",
        "speech_policy": speech_policy,
        "capabilities": capabilities,
    }
    if not capabilities:
        result["reason"] = "no_relevant_capability"
        return result

    actual_mode = ""
    expected_mode = ""
    outcome = ""
    note = ""
    response_text = str(response or "")
    if mode == "execute_quietly" and _response_looks_like_teaching(response_text):
        actual_mode = "teach"
        expected_mode = "execute_quietly"
        outcome = "too_much"
        note = "auto_self_review: direct execution response over-explained learned capabilities."
    elif mode == "decision" and not _response_has_decision_context(response_text):
        actual_mode = "execute_quietly" if _contains_marker(response_text.lower(), _RESPONSE_ACTION_MARKERS) else "unknown"
        expected_mode = "decision"
        outcome = "missed_decision"
        note = "auto_self_review: response missed a decision or risk reminder for a high-attention capability."
    elif mode == "teach" and _response_is_too_thin_for_teaching(response_text):
        actual_mode = "execute_quietly"
        expected_mode = "teach"
        outcome = "too_little"
        note = "auto_self_review: teaching response was too thin for the user's learning intent."

    if not outcome:
        return result

    feedback_usage = {
        "source": "auto_self_review",
        "query": query,
        "actual_mode": actual_mode,
        "expected_mode": expected_mode,
        "outcome": outcome,
        "note": note,
        "capability_ids": [item.get("id") for item in capabilities if item.get("id")],
        "should_speak": speech_policy.get("should_speak"),
    }
    duplicate = _find_recent_auto_self_review_duplicate(feedback_usage)
    if duplicate:
        result.update(
            {
                "feedback_recorded": False,
                "reason": "duplicate_recent_auto_self_review",
                "duplicate_event": duplicate,
                "summary": summarize_speech_feedback(),
            }
        )
        return result

    feedback = _record_speech_feedback(feedback_usage)
    result.update(
        {
            "feedback_recorded": True,
            "reason": outcome,
            "event": feedback["event"],
            "summary": feedback["summary"],
        }
    )
    return result


def _record_speech_feedback(usage: Dict[str, Any] | None = None) -> Dict[str, Any]:
    usage = usage if isinstance(usage, dict) else {}
    latest_trace = _latest_speech_policy_trace()
    feedback_text = _clip(
        usage.get("feedback_text") or usage.get("feedback") or usage.get("note"),
        _MAX_SPEECH_FEEDBACK_NOTE_CHARS,
    )
    inferred = _infer_speech_feedback_from_text(feedback_text)
    feedback_value = _norm(usage.get("feedback"))
    outcome = _norm(usage.get("outcome")) or (
        feedback_value if feedback_value in _SPEECH_FEEDBACK_OUTCOMES else ""
    ) or inferred.get("outcome") or "unclear"
    if outcome not in _SPEECH_FEEDBACK_OUTCOMES:
        outcome = "unclear"
    latest_policy = latest_trace.get("speech_policy", {}) if latest_trace else {}
    actual_mode = _norm(usage.get("actual_mode") or usage.get("mode")) or _norm(latest_policy.get("mode")) or "unknown"
    expected_mode = _norm(usage.get("expected_mode")) or inferred.get("expected_mode") or "unknown"
    if outcome == "unclear" and actual_mode != "unknown" and expected_mode != "unknown":
        outcome = "right" if actual_mode == expected_mode else "wrong_mode"
    query = _clip(
        usage.get("query") or usage.get("task") or (latest_trace or {}).get("query"),
        _MAX_SPEECH_FEEDBACK_QUERY_CHARS,
    )
    event = {
        "recorded_at": _hermes_now().isoformat(),
        "query": query,
        "actual_mode": actual_mode,
        "expected_mode": expected_mode,
        "outcome": outcome,
    }
    source = _clip(usage.get("source"), 80)
    if source:
        event["source"] = source
    if feedback_text:
        event["feedback_text"] = feedback_text
    note = _clip(usage.get("note"), _MAX_SPEECH_FEEDBACK_NOTE_CHARS)
    if note:
        event["note"] = note
    capability_ids = _as_list(
        usage.get("capability_ids")
        or usage.get("capability_id")
        or ((latest_trace or {}).get("capability_ids") if latest_trace else None)
    )
    if capability_ids:
        event["capability_ids"] = capability_ids[:8]
    if "should_speak" in usage:
        event["should_speak"] = _as_bool(usage.get("should_speak"))
    elif latest_policy:
        event["should_speak"] = bool(latest_policy.get("should_speak"))
    if latest_trace:
        event["attribution"] = {
            "source": "latest_speech_policy_trace",
            "trace_id": latest_trace.get("trace_id"),
            "trace_source": latest_trace.get("source"),
            "trace_recorded_at": latest_trace.get("recorded_at"),
            "confidence": "medium" if feedback_text else "low",
        }

    _append_speech_feedback_event(event)
    return {"event": event, "summary": summarize_speech_feedback()}


def _speech_feedback_context_lines() -> List[str]:
    summary = summarize_speech_feedback(limit=20)
    if not summary["total_events"] or not summary["problem_events"]:
        return []
    lines = [
        "<speech-feedback>",
        "[System note: Recent user feedback about Hermes speech policy. Apply as soft guidance; current user intent wins.]",
        f"- recent_events={summary['total_events']} problem_events={summary['problem_events']}",
    ]
    for recommendation in summary["recommendations"][:3]:
        lines.append(f"- {recommendation}")
    lines.append("</speech-feedback>")
    return lines


def _save_registry(payload: Dict[str, Any]) -> None:
    path = _capabilities_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _hermes_now().isoformat()
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".capabilities_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _register_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    result = decide_learning_absorption(candidate)
    card = result["candidate_card"]
    decision = result["decision"]
    now = _hermes_now().isoformat()
    capability = {
        "id": card["id"],
        "finding": card["finding"],
        "source_url": card["source_url"],
        "source_type": card["source_type"],
        "verification_level": card["verification_level"],
        "promotion_target": decision["promotion_target"],
        "status": decision["registry_status"],
        "can_apply_by_default": decision["can_apply_by_default"],
        "applies_to": card["applies_to"],
        "required_evidence": decision["required_evidence"],
        "next_action": decision["next_action"],
        "updated_at": now,
    }

    registry_payload = _load_registry()
    capabilities = registry_payload["capabilities"]
    for index, existing in enumerate(capabilities):
        if existing.get("id") == capability["id"]:
            capability["created_at"] = existing.get("created_at") or now
            capabilities[index] = {**existing, **capability}
            _save_registry(registry_payload)
            return capability

    capability["created_at"] = now
    capabilities.append(capability)
    _save_registry(registry_payload)
    return capability


def _record_capability_use(capability_id: str, usage: Dict[str, Any] | None = None) -> Dict[str, Any]:
    capability_id = str(capability_id or "").strip()
    if not capability_id:
        raise ValueError("capability_id is required")

    usage = usage if isinstance(usage, dict) else {}
    registry_payload = _load_registry()
    capabilities = registry_payload["capabilities"]
    now = _hermes_now().isoformat()
    entry = {
        "used_at": now,
        "task": _clip(usage.get("task"), 240),
        "outcome": _clip(usage.get("outcome") or "used", 120),
    }
    evidence = _as_list(usage.get("evidence"))
    if evidence:
        entry["evidence"] = evidence[:5]

    for index, existing in enumerate(capabilities):
        if not isinstance(existing, dict) or existing.get("id") != capability_id:
            continue
        updated = dict(existing)
        try:
            use_count = int(updated.get("use_count") or 0)
        except (TypeError, ValueError):
            use_count = 0
        usage_log = updated.get("usage_log")
        if not isinstance(usage_log, list):
            usage_log = []
        updated["use_count"] = use_count + 1
        updated["last_used_at"] = now
        updated["usage_log"] = (usage_log + [entry])[-_MAX_USAGE_LOG_ENTRIES:]
        updated["updated_at"] = now
        capabilities[index] = updated
        _save_registry(registry_payload)
        return updated

    raise ValueError(f"capability not found: {capability_id}")


def _fetch_feed(url: str, timeout: int = 20, attempts: int = 3, retry_delay: float = 0.5) -> str:
    last_error: BaseException | None = None
    safe_attempts = max(1, attempts)
    for attempt in range(safe_attempts):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "HermesLearningRadar/1.0 (+https://hermes-agent.local)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "Connection": "close",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(1_000_000)
                charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= safe_attempts:
                raise
            if retry_delay > 0:
                time.sleep(retry_delay)
    if last_error is not None:
        raise last_error
    raise RuntimeError("feed fetch failed without an error")


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: List[Dict[str, str]] = []
        self._href: str | None = None
        self._text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {name.lower(): value for name, value in attrs}
        href = str(attrs_dict.get("href") or "").strip()
        if href:
            self._href = href
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        text = _plain_text(" ".join(self._text_parts))
        if text:
            self.links.append({"href": self._href, "text": text})
        self._href = None
        self._text_parts = []


def _parse_html_items(source: Dict[str, Any], html_text: str) -> List[Dict[str, Any]]:
    collector = _LinkCollector()
    collector.feed(html_text)
    base_url = str(source.get("url") or "")
    path_filter = str(source.get("item_path_contains") or "").strip()
    seen_links: set[str] = set()
    items: List[Dict[str, Any]] = []
    for link in collector.links:
        href = urljoin(base_url, link["href"])
        if path_filter and path_filter not in href:
            continue
        if href in seen_links:
            continue
        seen_links.add(href)
        title = _plain_text(link["text"])
        if len(title) < 8:
            continue
        item_id = hashlib.sha256(
            json.dumps(
                {
                    "source": source.get("url"),
                    "link": href,
                    "title": title,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        items.append(
            {
                "id": item_id,
                "source_name": str(source.get("name") or source.get("url") or "source"),
                "source_url": base_url,
                "source_type": _norm(source.get("source_type")) or "unknown",
                "source_applies_to": _as_list(source.get("applies_to")),
                "title": title,
                "link": href,
                "summary": title,
                "published_at": "",
            }
        )
    return items


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in list(node):
        if _local_name(child.tag) in wanted:
            return _plain_text("".join(child.itertext()))
    return ""


def _entry_link(node: ET.Element) -> str:
    direct = _child_text(node, "link")
    if direct:
        return direct
    for child in list(node):
        if _local_name(child.tag) == "link":
            href = str(child.attrib.get("href") or "").strip()
            if href:
                return href
    return ""


def _parse_feed_items(source: Dict[str, Any], feed_text: str) -> List[Dict[str, Any]]:
    if _norm(source.get("format")) == "html":
        return _parse_html_items(source, feed_text)
    try:
        root = ET.fromstring(feed_text)
    except ET.ParseError as exc:
        if "<html" in feed_text[:2000].lower():
            return _parse_html_items(source, feed_text)
        raise ValueError(f"invalid feed XML: {exc}") from exc

    nodes = [
        node
        for node in root.iter()
        if _local_name(node.tag) in {"item", "entry"}
    ]
    items: List[Dict[str, Any]] = []
    for node in nodes:
        title = _child_text(node, "title")
        link = _entry_link(node)
        summary = _child_text(node, "description", "summary", "content")
        published = _child_text(node, "pubDate", "published", "updated")
        if not title and not link:
            continue
        item_id = hashlib.sha256(
            json.dumps(
                {
                    "source": source.get("url"),
                    "link": link,
                    "title": title,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        items.append(
            {
                "id": item_id,
                "source_name": str(source.get("name") or source.get("url") or "source"),
                "source_url": str(source.get("url") or ""),
                "source_type": _norm(source.get("source_type")) or "unknown",
                "source_applies_to": _as_list(source.get("applies_to")),
                "title": title,
                "link": link,
                "summary": summary,
                "published_at": published,
            }
        )
    return items


def _radar_score(item: Dict[str, Any]) -> int:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    score = 0
    for keyword, weight in _RADAR_KEYWORDS.items():
        if keyword in text:
            score += weight
    if _norm(item.get("source_type")) == "official":
        score += 2
    return score


def _radar_applies_to(item: Dict[str, Any]) -> List[str]:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    applies = _as_list(item.get("source_applies_to"))
    mappings = [
        ("agent", "agent workflows"),
        ("tool", "tool use"),
        ("eval", "evaluation"),
        ("prompt", "prompting"),
        ("model", "model capabilities"),
        ("api", "API usage"),
        ("codex", "Codex"),
        ("chatgpt", "ChatGPT"),
        ("claude", "Claude"),
        ("gemini", "Gemini"),
        ("copilot", "Copilot"),
        ("mcp", "MCP"),
    ]
    for needle, label in mappings:
        if needle in text and label not in applies:
            applies.append(label)
    return applies[:8] or ["AI product usage"]


def _radar_reusable_workflow(item: Dict[str, Any]) -> bool:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return any(
        marker in text
        for marker in (
            "guide",
            "cookbook",
            "best practice",
            "prompt",
            "eval",
            "tool use",
            "workflow",
            "agent",
        )
    )


def _radar_candidate(item: Dict[str, Any]) -> Dict[str, Any]:
    score = _radar_score(item)
    summary = _clip(_plain_text(item.get("summary")), _MAX_RADAR_SUMMARY_CHARS)
    title = _plain_text(item.get("title"))
    finding = f"{item.get('source_name')}: {title}"
    if summary:
        finding = f"{finding}. {summary}"
    return {
        "finding": finding,
        "source_url": item.get("link") or item.get("source_url"),
        "source_type": item.get("source_type") or "unknown",
        "verification_level": "official" if _norm(item.get("source_type")) == "official" else "source_checked",
        "impact": "high" if score >= 12 else "medium",
        "risk": "low",
        "reusable_workflow": _radar_reusable_workflow(item),
        "applies_to": _radar_applies_to(item),
        "notes": f"learning_radar score={score}; published_at={item.get('published_at') or 'unknown'}",
    }


def run_learning_radar(
    *,
    sources: List[Dict[str, Any]] | None = None,
    fetcher: Callable[[str], str] | None = None,
    register: bool = True,
    max_items: int = 8,
) -> Dict[str, Any]:
    """Fetch AI-product sources, filter new useful items, and register candidates."""

    radar_sources = sources if sources is not None else _load_radar_sources()
    fetch = fetcher or _fetch_feed
    seen = _load_seen_items()
    now = _hermes_now().isoformat()
    max_new_items = _positive_int(max_items, default=8)
    errors: List[Dict[str, str]] = []
    candidates: List[Dict[str, Any]] = []

    for source in radar_sources:
        url = str(source.get("url") or "").strip()
        if not url:
            continue
        try:
            feed_text = fetch(url)
            items = _parse_feed_items(source, feed_text)
        except Exception as exc:
            errors.append({"source": url, "error": str(exc)})
            continue

        for item in items:
            if item["id"] in seen:
                continue
            score = _radar_score(item)
            if score < 6:
                continue
            candidate = _radar_candidate(item)
            candidates.append(
                {
                    "item": item,
                    "score": score,
                    "candidate": candidate,
                }
            )

    candidates.sort(key=lambda entry: entry["score"], reverse=True)
    candidates = candidates[:max_new_items]

    registered: List[Dict[str, Any]] = []
    for entry in candidates:
        if register:
            registered.append(_register_candidate(entry["candidate"]))
            item = entry["item"]
            seen[item["id"]] = {
                "id": item["id"],
                "seen_at": now,
                "title": item.get("title"),
                "link": item.get("link"),
                "source_name": item.get("source_name"),
            }

    if register and candidates:
        _save_seen_items(seen)

    result = {
        "success": True,
        "source_count": len(radar_sources),
        "new_item_count": len(candidates),
        "registered_count": len(registered),
        "candidates": [entry["candidate"] for entry in candidates],
        "registered": registered,
        "errors": errors,
        "ran_at": now,
    }
    _append_radar_run(
        {
            "ran_at": now,
            "source_count": result["source_count"],
            "new_item_count": result["new_item_count"],
            "registered_count": result["registered_count"],
            "error_count": len(errors),
        }
    )
    return result


def ensure_learning_radar_cron(schedule: str = "every 12h") -> Dict[str, Any]:
    """Create or update the single recurring cron job that runs the radar."""

    from cron.jobs import create_job, list_jobs, parse_schedule, update_job

    existing = next((job for job in list_jobs(include_disabled=True) if job.get("name") == _RADAR_JOB_NAME), None)
    if existing:
        parsed = parse_schedule(schedule)
        job = update_job(
            existing["id"],
            {
                "prompt": _RADAR_JOB_PROMPT,
                "schedule": parsed,
                "schedule_display": parsed.get("display", schedule),
                "deliver": "local",
                "lane": "cron_scout",
                "skills": [],
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
            },
        )
        return {"success": True, "created": False, "job": job}

    job = create_job(
        prompt=_RADAR_JOB_PROMPT,
        schedule=schedule,
        name=_RADAR_JOB_NAME,
        repeat=None,
        deliver="local",
    )
    job = update_job(job["id"], {"lane": "cron_scout", "skills": []}) or job
    return {"success": True, "created": True, "job": job}


def learning_absorption(
    action: str,
    candidate: Dict[str, Any] | None = None,
    query: str | None = None,
    response: str | None = None,
    capability_id: str | None = None,
    usage: Dict[str, Any] | None = None,
    limit: int | None = None,
    sources: List[Dict[str, Any]] | None = None,
    register: bool | None = None,
    schedule: str | None = None,
) -> str:
    """Tool dispatcher for learning absorption decisions and registry updates."""
    normalized = _norm(action)
    try:
        if normalized == "decide":
            result = decide_learning_absorption(candidate or {})
            return json.dumps({"success": True, **result}, ensure_ascii=False)
        if normalized == "register":
            capability = _register_candidate(candidate or {})
            return json.dumps({"success": True, "capability": capability}, ensure_ascii=False)
        if normalized == "select":
            capabilities = select_applicable_capabilities(query or "", limit=_positive_int(limit, default=3))
            return json.dumps({"success": True, "capabilities": capabilities}, ensure_ascii=False)
        if normalized == "record_use":
            capability = _record_capability_use(capability_id or "", usage)
            return json.dumps({"success": True, "capability": capability}, ensure_ascii=False)
        if normalized == "teacher_brief":
            result = build_teacher_brief(query or "", limit=_positive_int(limit, default=3))
            return json.dumps({"success": True, **result}, ensure_ascii=False)
        if normalized == "record_speech_feedback":
            result = _record_speech_feedback(usage)
            return json.dumps({"success": True, **result}, ensure_ascii=False)
        if normalized == "speech_feedback_summary":
            result = summarize_speech_feedback(limit=_positive_int(limit, default=_MAX_SPEECH_FEEDBACK_ENTRIES))
            return json.dumps({"success": True, "summary": result}, ensure_ascii=False)
        if normalized == "learning_report":
            result = build_learning_report(limit=_positive_int(limit, default=5))
            return json.dumps({"success": True, "report": result}, ensure_ascii=False)
        if normalized == "evaluate_speech_response":
            result = evaluate_speech_response(
                query or "",
                response or "",
                limit=_positive_int(limit, default=3),
            )
            return json.dumps({"success": True, **result}, ensure_ascii=False)
        if normalized == "radar_run":
            result = run_learning_radar(
                sources=sources,
                register=True if register is None else _as_bool(register),
                max_items=_positive_int(limit, default=8),
            )
            return json.dumps(result, ensure_ascii=False)
        if normalized == "radar_install":
            return tool_error(
                "radar_install is disabled from the agent tool surface; create recurring jobs explicitly through reviewed cron configuration.",
                success=False,
            )
        if normalized == "radar_sources":
            return json.dumps({"success": True, "sources": _load_radar_sources()}, ensure_ascii=False)
        if normalized == "list":
            payload = _load_registry()
            return json.dumps({"success": True, **payload}, ensure_ascii=False)
        return tool_error(
            "Unknown action. Use: decide, register, select, record_use, teacher_brief, record_speech_feedback, speech_feedback_summary, learning_report, evaluate_speech_response, radar_run, radar_sources, list",
            success=False,
        )
    except ValueError as exc:
        return tool_error(str(exc), success=False)


LEARNING_ABSORPTION_SCHEMA = {
    "name": "learning_absorption",
    "description": (
        "Evaluate AI-product, agent-workflow, prompt, MCP, evaluation, or routing discoveries "
        "before turning them into durable Hermes behavior. Use this after research/scout work "
        "to create a candidate card, decide whether the finding should become a source note, "
        "knowledge-base item, playbook, routing default, skill candidate, user brief, or "
        "verification backlog item, and optionally register the resulting capability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "decide",
                    "register",
                    "select",
                    "record_use",
                    "teacher_brief",
                    "record_speech_feedback",
                    "speech_feedback_summary",
                    "learning_report",
                    "evaluate_speech_response",
                    "radar_run",
                    "radar_sources",
                    "list",
                ],
                "description": "decide returns an absorption decision; register persists it; select returns relevant active capabilities; record_use records that a capability shaped a turn; teacher_brief returns a concise Chinese explanation of relevant learned capabilities; record_speech_feedback stores user feedback about whether Hermes spoke too much, too little, or in the wrong mode; speech_feedback_summary summarizes that feedback; learning_report summarizes recent learned capabilities and speech feedback for user-facing updates; evaluate_speech_response checks a final response against the learned speech policy and records obvious mismatches; radar_run fetches AI learning sources and registers new findings; radar_sources lists configured sources; list reads the registry.",
            },
            "candidate": {
                "type": "object",
                "description": "Learning candidate card fields such as finding, source_url, source_type, verification_level, impact, risk, reusable_workflow, affects_routing, affects_code, applies_to, front_channel_reason, and notes. front_channel_reason may be major_new_technique, current_project_relevance, execution_blocker, or direction_decision; omit it for local-only learning.",
                "additionalProperties": True,
            },
            "query": {
                "type": "string",
                "description": "User turn or task summary used by action=select, action=teacher_brief, or action=evaluate_speech_response to choose relevant active capabilities.",
            },
            "response": {
                "type": "string",
                "description": "Final assistant response text used by action=evaluate_speech_response.",
            },
            "capability_id": {
                "type": "string",
                "description": "Capability id used by action=record_use.",
            },
            "usage": {
                "type": "object",
                "description": "Usage evidence for action=record_use, such as task, outcome, and evidence. For action=record_speech_feedback, include query, actual_mode, expected_mode, outcome, note, and capability_ids when available.",
                "additionalProperties": True,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of selected capabilities for action=select, recent capabilities for action=learning_report, or maximum new items for action=radar_run.",
                "minimum": 1,
                "maximum": 10,
            },
            "sources": {
                "type": "array",
                "description": "Optional source override for action=radar_run; normally omitted so ~/.hermes/learning/radar_sources.json or built-in defaults are used.",
                "items": {"type": "object", "additionalProperties": True},
            },
            "register": {
                "type": "boolean",
                "description": "For action=radar_run, whether to persist absorbed capabilities. Defaults to true.",
            },
            "schedule": {
                "type": "string",
                "description": "For action=radar_install, the cron schedule such as 'every 12h'.",
            },
        },
        "required": ["action"],
    },
}


def check_learning_absorption_requirements() -> bool:
    return True


from tools.registry import registry, tool_error

registry.register(
    name="learning_absorption",
    toolset="memory",
    schema=LEARNING_ABSORPTION_SCHEMA,
    handler=lambda args, **kw: learning_absorption(
        action=args.get("action", ""),
        candidate=args.get("candidate"),
        query=args.get("query"),
        response=args.get("response"),
        capability_id=args.get("capability_id"),
        usage=args.get("usage"),
        limit=args.get("limit"),
        sources=args.get("sources"),
        register=args.get("register"),
        schedule=args.get("schedule"),
    ),
    check_fn=check_learning_absorption_requirements,
    emoji="🧩",
)
