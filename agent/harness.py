from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home


DEFAULT_HARNESS_CONFIG = {
    "enabled": False,
    "db_path": "",
    "default_autonomy_target": "bounded",
    "plan": {
        "always_require_for_surfaces": ["cron", "delegate", "background"],
        "max_direct_chars": 280,
        "keywords": [
            "plan",
            "design",
            "retrofit",
            "redesign",
            "refactor",
            "migration",
            "architecture",
            "workflow",
            "harness",
            "规划",
            "方案",
            "改造",
            "重构",
            "迁移",
            "架构",
            "治理",
        ],
        "inject_contract_context": True,
    },
    "acceptance": {
        "require_evidence_for_plan_required": True,
        "verification_tools": [
            "terminal",
            "read_file",
            "search_files",
            "browser_console",
            "browser_snapshot",
            "execute_code",
            "process",
        ],
    },
    "drift": {
        "enabled": True,
        "blocked_tool_threshold": 2,
        "read_only_streak_threshold": 6,
    },
    "audit": {
        "capture_tool_results": True,
        "result_preview_chars": 280,
    },
    "rate_limits": {
        "subscription_retry_seconds": 1800,
        "pause_high_value_models": True,
        "allow_model_downgrade": False,
        "high_value_model_patterns": ["gpt-5", "claude-opus", "claude-sonnet"],
    },
}


_HARNESS_MANAGER: Optional["HarnessManager"] = None
_HARNESS_LOCK = threading.RLock()

_READ_ONLY_TOOLS = {
    "read_file",
    "search_files",
    "session_search",
    "skill_view",
    "skills_list",
    "browser_snapshot",
    "browser_console",
    "browser_get_images",
    "browser_vision",
    "vision_analyze",
    "process",
    "todo",
    "memory",
    "clarify",
}
_FILE_MUTATION_TOOLS = {"write_file", "patch", "browser_type"}
_PLAN_ARTIFACT_TOOL_NAMES = {"write_file", "patch"}
_ALWAYS_ALLOWED_META_TOOLS = {"todo", "memory", "clarify", "session_search", "skill_view", "skills_list"}
_TERMINAL_MUTATION_PATTERNS = [
    r"(^|\\s)(rm|mv|cp|chmod|chown|mkdir|touch|tee|sed|perl|python|python3|node|npm|pnpm|yarn|pip|poetry|git\\s+(apply|checkout|switch|restore|commit|merge|rebase)|make|cargo|go|docker|kubectl|systemctl)($|\\s)",
    r">",
    r"\\|\\s*(tee|python|python3|node|sed|perl)",
]

_WORKSTREAM_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("task_governance", ("harness", "账本", "ledger", "admission", "acceptance", "governance", "任务账本")),
    ("model_chain", ("model", "gpt", "5.5", "5.4", "模型", "provider", "relay", "链路", "codex")),
    ("telegram_stability", ("telegram", "断链", "私聊", "对话", "live", "gateway service", "重启", "restart")),
    ("gateway_worker", ("gateway", "worker", "detached", "cgroup", "systemd", "控制面", "旁路")),
    ("tool_limits", ("tool limit", "工具上限", "上限", "额度", "限额", "quota", "rate limit", "usage limit")),
    ("context_management", ("session", "context", "上下文", "压缩", "主题", "topic", "history", "历史")),
    ("visibility", ("可见", "汇报", "reporter", "report", "silence", "沉默", "origin", "deliver")),
    ("parallel_delegation", ("delegate", "并行", "分工", "subagent", "opencode", "claude", "gemini")),
    ("workspace_hygiene", ("脏工作区", "dirty", "git status", "提交", "commit", "diff", "patch", "混提交")),
    ("cpa_proxy", ("cpa", "cliproxy", "proxy", "代理", "8317", "8328", "47claude")),
    ("knowledge_base", ("obsidian", "知识库", "记忆", "memory", "playbook", "vault")),
)

_DEFAULT_WORKSTREAM = "general"

_WORKSPACE_PATH_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "task_governance",
        (
            "agent/harness.py",
            "test_harness",
            "harness_config",
            "model_tools_harness",
        ),
    ),
    (
        "context_management",
        (
            "topic_router",
            "context",
            "session_hygiene",
            "trajectory_compressor",
        ),
    ),
    (
        "visibility",
        (
            "status_command",
            "gateway/status.py",
            "gateway/task_control.py",
            "notify_on_complete",
        ),
    ),
    (
        "workspace_hygiene",
        (
            "website/",
            "docs/",
            "package-lock.json",
            "uv.lock",
            ".tmp/",
            ".hermes/",
        ),
    ),
    (
        "gateway_worker",
        (
            "gateway/",
            "worker_runtime",
            "detached",
            "background",
            "api_server",
            "api-server",
            "telegram_network",
        ),
    ),
    (
        "model_chain",
        (
            "model",
            "codex",
            "credential_pool",
            "auxiliary_client",
            "copilot",
            "litellm",
            "run_agent.py",
        ),
    ),
    (
        "tool_limits",
        (
            "terminal_tool",
            "process_registry",
            "delegate_tool",
            "mcp_tool",
            "browser_tool",
            "quota",
            "rate_limit",
        ),
    ),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_harness_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _normalize_goal(text: str, limit: int = 240) -> str:
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _workstream_matches(text: str) -> dict[str, int]:
    normalized = (text or "").lower()
    scores: dict[str, int] = {}
    for label, keywords in _WORKSTREAM_RULES:
        score = 0
        for keyword in keywords:
            if keyword.lower() in normalized:
                score += 1
        if score:
            scores[label] = score
    return scores


def classify_workstream(text: str) -> str:
    scores = _workstream_matches(text)
    if not scores:
        return _DEFAULT_WORKSTREAM
    # Keep the declaration order as the tie-breaker so model-chain requests
    # stay focused when they mention a second workstream as a boundary.
    ranked_labels = [label for label, _keywords in _WORKSTREAM_RULES if label in scores]
    return max(ranked_labels, key=lambda label: scores[label])


def infer_out_of_scope_workstreams(text: str, primary: str) -> list[str]:
    scores = _workstream_matches(text)
    return [
        label
        for label, _keywords in _WORKSTREAM_RULES
        if label != primary and label in scores
    ]


def _format_workstream_label(label: str) -> str:
    return str(label or _DEFAULT_WORKSTREAM).strip() or _DEFAULT_WORKSTREAM


def _parse_git_status_line(line: str) -> Optional[dict[str, str]]:
    raw = str(line or "").rstrip()
    if not raw:
        return None
    if len(raw) >= 4 and raw[2] == " ":
        status = raw[:2].strip() or "M"
        path = raw[3:].strip()
    else:
        parts = raw.split(maxsplit=1)
        if len(parts) != 2:
            return None
        status, path = parts[0].strip(), parts[1].strip()
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1].strip()
    if not path:
        return None
    return {"status": status, "path": path.replace("\\", "/")}


def classify_workspace_path(path: str, *, overrides: Optional[dict[str, str]] = None) -> tuple[str, str]:
    normalized = str(path or "").replace("\\", "/").strip()
    lowered = normalized.lower()
    for raw_pattern, raw_label in (overrides or {}).items():
        pattern = str(raw_pattern or "").replace("\\", "/").strip().lower()
        if pattern and (lowered == pattern or lowered.endswith(f"/{pattern}") or lowered.startswith(pattern)):
            return _format_workstream_label(raw_label), f"override:{raw_pattern}"
    for label, patterns in _WORKSPACE_PATH_RULES:
        for pattern in patterns:
            if pattern.lower() in lowered:
                return label, pattern
    return "workspace_hygiene", "fallback"


def build_workspace_change_ledger(
    status_lines: list[str],
    *,
    overrides: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    by_workstream: dict[str, list[dict[str, str]]] = {}
    entries: list[dict[str, str]] = []
    for line in status_lines or []:
        parsed = _parse_git_status_line(line)
        if not parsed:
            continue
        workstream, reason = classify_workspace_path(parsed["path"], overrides=overrides)
        entry = {
            "status": parsed["status"],
            "path": parsed["path"],
            "workstream": workstream,
            "reason": reason,
        }
        entries.append(entry)
        by_workstream.setdefault(workstream, []).append(entry)
    ordered: dict[str, list[dict[str, str]]] = {}
    known_labels = [label for label, _keywords in _WORKSTREAM_RULES]
    for label in [*known_labels, *sorted(set(by_workstream) - set(known_labels))]:
        if label in by_workstream:
            ordered[label] = by_workstream[label]
    return {
        "total": len(entries),
        "entries": entries,
        "by_workstream": ordered,
    }


def render_workspace_change_ledger(ledger: dict[str, Any], *, limit_per_workstream: int = 12) -> str:
    payload = ledger if isinstance(ledger, dict) else {}
    by_workstream = payload.get("by_workstream") if isinstance(payload.get("by_workstream"), dict) else {}
    lines = [
        "Hermes workspace change ledger",
        f"Total dirty entries: {int(payload.get('total') or 0)}",
    ]
    for workstream, entries in by_workstream.items():
        safe_entries = entries if isinstance(entries, list) else []
        lines.append(f"- {workstream}: {len(safe_entries)}")
        for entry in safe_entries[: max(1, int(limit_per_workstream or 1))]:
            if not isinstance(entry, dict):
                continue
            lines.append(f"  - {entry.get('status') or '?'} {entry.get('path') or ''}")
        if len(safe_entries) > limit_per_workstream:
            lines.append(f"  - ... {len(safe_entries) - limit_per_workstream} more")
    return "\n".join(lines)


def _default_db_path() -> Path:
    return get_hermes_home() / "runtime" / "harness.sqlite3"


def _safe_path(path: str | Path, *, base: str | Path | None = None) -> Path:
    candidate = Path(str(path or "")).expanduser()
    if not candidate.is_absolute() and base:
        candidate = Path(base) / candidate
    return candidate.resolve(strict=False)


def _is_subpath(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except Exception:
        return False


def _terminal_is_mutating(command: str) -> bool:
    text = str(command or "").strip()
    if not text:
        return False
    for pattern in _TERMINAL_MUTATION_PATTERNS:
        if re.search(pattern, text):
            return True
    return False


def _task_context_details(task_context: Any) -> dict[str, Any]:
    details = {
        "preview": "",
        "lane": "",
        "kind": "",
        "priority": None,
        "source": "",
        "platform": "",
        "session_key": "",
        "queued_at": "",
        "started_at": "",
        "control_mode": "",
    }

    def _label(value: Any) -> str:
        raw = getattr(value, "value", value)
        text = str(raw or "").strip()
        return text

    def _iso(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        text = str(value).strip()
        return text

    if isinstance(task_context, dict):
        source = task_context.get("source")
        source_platform = task_context.get("platform") or getattr(source, "platform", None)
        details.update({
            "preview": str(task_context.get("preview") or task_context.get("text") or task_context.get("label") or task_context.get("normalized_goal") or "").strip(),
            "lane": str(task_context.get("lane") or "").strip(),
            "kind": str(task_context.get("kind") or "").strip(),
            "priority": task_context.get("priority"),
            "source": _label(source) or _label(source_platform),
            "platform": _label(source_platform) or _label(source),
            "session_key": str(task_context.get("session_key") or "").strip(),
            "queued_at": _iso(task_context.get("queued_at")),
            "started_at": _iso(task_context.get("started_at")),
            "control_mode": str(task_context.get("control_mode") or "").strip(),
        })
        return details

    message_event = getattr(task_context, "message_event", None)
    source = getattr(message_event, "source", None)
    source_platform = getattr(source, "platform", None)
    details.update({
        "preview": str(getattr(message_event, "text", None) or getattr(task_context, "preview", None) or getattr(task_context, "label", None) or "").strip(),
        "lane": str(getattr(task_context, "lane", "") or "").strip(),
        "kind": str(getattr(task_context, "kind", "") or "").strip(),
        "priority": getattr(task_context, "priority", None),
        "source": _label(source_platform) or _label(source),
        "platform": _label(source_platform) or _label(source),
        "session_key": str(getattr(task_context, "session_key", "") or "").strip(),
        "queued_at": _iso(getattr(task_context, "queued_at", None)),
        "started_at": _iso(getattr(task_context, "started_at", None)),
        "control_mode": str(getattr(task_context, "control_mode", "") or "").strip(),
    })
    return details


def _load_runtime_config(config: Optional[dict] = None) -> dict:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    harness_cfg = (config or {}).get("harness", {})
    return _deep_merge(DEFAULT_HARNESS_CONFIG, harness_cfg)


def reset_harness_manager() -> None:
    global _HARNESS_MANAGER
    with _HARNESS_LOCK:
        _HARNESS_MANAGER = None


def get_harness_manager(config: Optional[dict] = None) -> "HarnessManager":
    global _HARNESS_MANAGER
    with _HARNESS_LOCK:
        if _HARNESS_MANAGER is None or config is not None:
            _HARNESS_MANAGER = HarnessManager(config)
        return _HARNESS_MANAGER


@dataclass
class TaskContract:
    task_id: str
    session_id: str
    surface: str
    platform: str
    raw_request: str
    normalized_goal: str
    plan_mode: str
    workspace_root: str
    plan_artifact_path: str = ""
    parent_task_id: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    side_effect_budget: str = "bounded_write"
    autonomy_target: str = "bounded"
    runtime_budgets: dict[str, Any] = field(default_factory=dict)
    evidence_requirements: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class HarnessStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _decode_task_row(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        data = dict(row)
        for key in (
            "acceptance_criteria_json",
            "write_scope_json",
            "runtime_budgets_json",
            "evidence_requirements_json",
            "metadata_json",
        ):
            raw = data.pop(key, None)
            target_key = key.replace("_json", "")
            data[target_key] = json.loads(raw) if raw else ([] if "criteria" in key or "scope" in key or "requirements" in key else {})
        return data

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    parent_task_id TEXT,
                    session_id TEXT,
                    surface TEXT,
                    platform TEXT,
                    state TEXT,
                    normalized_goal TEXT,
                    raw_request TEXT,
                    plan_mode TEXT,
                    plan_artifact_path TEXT,
                    acceptance_criteria_json TEXT,
                    write_scope_json TEXT,
                    side_effect_budget TEXT,
                    autonomy_target TEXT,
                    runtime_budgets_json TEXT,
                    evidence_requirements_json TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    admitted_at TEXT,
                    completed_at TEXT,
                    interrupted INTEGER DEFAULT 0,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    artifact_kind TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    label TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def upsert_task(self, contract: TaskContract, state: str, *, interrupted: bool = False, last_error: str = "") -> None:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM tasks WHERE task_id = ?",
                (contract.task_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            admitted_at = now if not existing else None
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, parent_task_id, session_id, surface, platform, state,
                    normalized_goal, raw_request, plan_mode, plan_artifact_path,
                    acceptance_criteria_json, write_scope_json, side_effect_budget,
                    autonomy_target, runtime_budgets_json, evidence_requirements_json,
                    metadata_json, created_at, updated_at, admitted_at, interrupted, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    parent_task_id=excluded.parent_task_id,
                    session_id=excluded.session_id,
                    surface=excluded.surface,
                    platform=excluded.platform,
                    state=excluded.state,
                    normalized_goal=excluded.normalized_goal,
                    raw_request=excluded.raw_request,
                    plan_mode=excluded.plan_mode,
                    plan_artifact_path=excluded.plan_artifact_path,
                    acceptance_criteria_json=excluded.acceptance_criteria_json,
                    write_scope_json=excluded.write_scope_json,
                    side_effect_budget=excluded.side_effect_budget,
                    autonomy_target=excluded.autonomy_target,
                    runtime_budgets_json=excluded.runtime_budgets_json,
                    evidence_requirements_json=excluded.evidence_requirements_json,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at,
                    interrupted=excluded.interrupted,
                    last_error=excluded.last_error
                """,
                (
                    contract.task_id,
                    contract.parent_task_id,
                    contract.session_id,
                    contract.surface,
                    contract.platform,
                    state,
                    contract.normalized_goal,
                    contract.raw_request,
                    contract.plan_mode,
                    contract.plan_artifact_path,
                    json.dumps(contract.acceptance_criteria, ensure_ascii=False),
                    json.dumps(contract.write_scope, ensure_ascii=False),
                    contract.side_effect_budget,
                    contract.autonomy_target,
                    json.dumps(contract.runtime_budgets, ensure_ascii=False),
                    json.dumps(contract.evidence_requirements, ensure_ascii=False),
                    json.dumps(contract.metadata, ensure_ascii=False),
                    created_at,
                    now,
                    admitted_at,
                    1 if interrupted else 0,
                    last_error,
                ),
            )

    def transition_state(self, task_id: str, new_state: str, *, interrupted: bool = False, last_error: str = "", completed_at: str = "") -> None:
        now = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET state = ?,
                    updated_at = ?,
                    interrupted = ?,
                    last_error = ?,
                    completed_at = COALESCE(NULLIF(?, ''), completed_at)
                WHERE task_id = ?
                """,
                (new_state, now, 1 if interrupted else 0, last_error, completed_at, task_id),
            )

    def append_event(self, task_id: str, event_type: str, payload: Optional[dict[str, Any]] = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    event_type,
                    json.dumps(payload or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def add_artifact(self, task_id: str, artifact_kind: str, artifact_path: str, *, label: str = "", metadata: Optional[dict[str, Any]] = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO task_artifacts (task_id, artifact_kind, artifact_path, label, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    artifact_kind,
                    artifact_path,
                    label,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    _utc_now(),
                ),
            )

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._decode_task_row(row)

    def list_tasks(
        self,
        *,
        limit: int = 20,
        surfaces: Optional[list[str]] = None,
        states: Optional[list[str]] = None,
        session_id: str = "",
        platform: str = "",
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        clauses: list[str] = []
        params: list[Any] = []

        normalized_surfaces = [str(item).strip() for item in (surfaces or []) if str(item).strip()]
        if normalized_surfaces:
            clauses.append(f"surface IN ({', '.join('?' for _ in normalized_surfaces)})")
            params.extend(normalized_surfaces)

        normalized_states = [str(item).strip() for item in (states or []) if str(item).strip()]
        if normalized_states:
            clauses.append(f"state IN ({', '.join('?' for _ in normalized_states)})")
            params.extend(normalized_states)

        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)

        if platform:
            clauses.append("platform = ?")
            params.append(platform)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(max(1, int(limit or 20)))

        with self._lock, self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [decoded for row in rows if (decoded := self._decode_task_row(row)) is not None]

    def get_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT event_type, payload_json, created_at FROM task_events WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        events = []
        for row in rows:
            events.append(
                {
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"] or "{}"),
                    "created_at": row["created_at"],
                }
            )
        return events

    def get_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT artifact_kind, artifact_path, label, metadata_json, created_at FROM task_artifacts WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        artifacts = []
        for row in rows:
            artifacts.append(
                {
                    "artifact_kind": row["artifact_kind"],
                    "artifact_path": row["artifact_path"],
                    "label": row["label"],
                    "metadata": json.loads(row["metadata_json"] or "{}"),
                    "created_at": row["created_at"],
                }
            )
        return artifacts


class HarnessManager:
    def __init__(self, config: Optional[dict] = None):
        self.full_config = copy.deepcopy(config or {})
        self.config = _load_runtime_config(config)
        db_path = self.config.get("db_path") or str(_default_db_path())
        self.store = HarnessStore(db_path)
        self.enabled = bool(self.config.get("enabled", False))

    def _plan_mode_for(self, surface: str, user_request: str) -> str:
        plan_cfg = self.config.get("plan", {})
        surfaces = set(plan_cfg.get("always_require_for_surfaces", []))
        if surface in surfaces:
            return "plan_required"

        normalized = (user_request or "").lower()
        keywords = [str(item).lower() for item in plan_cfg.get("keywords", [])]
        if any(keyword in normalized for keyword in keywords):
            return "plan_required"

        if len((user_request or "").strip()) > int(plan_cfg.get("max_direct_chars", 280)):
            return "plan_required"
        return "direct"

    def _plan_artifact_path_for(self, user_request: str) -> str:
        try:
            from agent.skill_commands import build_plan_path

            return str(build_plan_path(user_request))
        except Exception:
            safe_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", _normalize_goal(user_request, limit=48)).strip("-") or "task"
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            return f".hermes/plans/{stamp}-{safe_slug}.md"

    def admit_turn(
        self,
        *,
        task_id: str,
        session_id: str,
        surface: str,
        platform: str,
        user_request: str,
        workspace_root: str,
        max_iterations: int,
        parent_task_id: str = "",
    ) -> TaskContract:
        plan_mode = self._plan_mode_for(surface, user_request)
        plan_artifact_path = self._plan_artifact_path_for(user_request) if plan_mode == "plan_required" else ""
        acceptance_criteria = [
            "Do not claim completion without fresh verification evidence.",
            "Stay within the stated goal and write scope.",
        ]
        evidence_requirements = ["final_response"]
        if plan_mode == "plan_required":
            acceptance_criteria.insert(0, "Create or update the plan artifact before broad implementation.")
            evidence_requirements.extend(["plan_artifact", "verification_evidence"])

        workstream = classify_workstream(user_request)
        out_of_scope_workstreams = infer_out_of_scope_workstreams(user_request, workstream)

        contract = TaskContract(
            task_id=task_id,
            parent_task_id=parent_task_id,
            session_id=session_id,
            surface=surface,
            platform=platform,
            raw_request=user_request,
            normalized_goal=_normalize_goal(user_request),
            plan_mode=plan_mode,
            workspace_root=workspace_root,
            plan_artifact_path=plan_artifact_path,
            acceptance_criteria=acceptance_criteria,
            write_scope=[workspace_root] if workspace_root else [],
            side_effect_budget="bounded_write",
            autonomy_target=self.config.get("default_autonomy_target", "bounded"),
            runtime_budgets={"max_iterations": max_iterations},
            evidence_requirements=evidence_requirements,
            metadata={
                "contract_version": 3,
                "workspace_root": workspace_root,
                "workstream": workstream,
                "out_of_scope_workstreams": out_of_scope_workstreams,
            },
        )
        existing = self.store.get_task(task_id)
        initial_state = "planning" if plan_mode == "plan_required" else "admitted"
        self.store.upsert_task(contract, initial_state)
        self.store.append_event(
            task_id,
            "task.created" if existing is None else "task.resumed",
            {
                "surface": surface,
                "platform": platform,
                "plan_mode": plan_mode,
                "workspace_root": workspace_root,
            },
        )
        gate_event = "gate.plan.required" if plan_mode == "plan_required" else "gate.plan.waived"
        self.store.append_event(
            task_id,
            gate_event,
            {"plan_artifact_path": plan_artifact_path},
        )
        return contract

    def build_turn_context(self, contract: TaskContract) -> str:
        if not self.enabled:
            return ""
        if not self.config.get("plan", {}).get("inject_contract_context", True):
            return ""

        lines = [
            "[Harness contract — obey before proceeding.]",
            f"Goal: {contract.normalized_goal}",
            f"Plan mode: {contract.plan_mode}",
            f"Autonomy target: {contract.autonomy_target}",
        ]
        workstream = _format_workstream_label(contract.metadata.get("workstream", ""))
        if workstream and workstream != _DEFAULT_WORKSTREAM:
            lines.append(f"Workstream: {workstream}")
        out_of_scope = [
            _format_workstream_label(item)
            for item in contract.metadata.get("out_of_scope_workstreams", [])
            if str(item or "").strip()
        ]
        if out_of_scope:
            lines.append(f"Out of scope: {', '.join(out_of_scope)}")
        lines.append("Progress style: report result, blocker, and next step only; avoid internal process chatter.")
        if contract.plan_artifact_path:
            lines.append(f"Plan artifact target: {contract.plan_artifact_path}")
        for criterion in contract.acceptance_criteria[:3]:
            lines.append(f"Acceptance: {criterion}")
        if contract.plan_mode == "plan_required":
            lines.append("Do not jump into broad implementation before producing or updating the plan artifact.")
        return "\n".join(lines)

    def task_snapshot(self, task_id: str) -> Optional[dict[str, Any]]:
        if not self.enabled or not task_id:
            return None
        task = self.store.get_task(task_id)
        if not task:
            return None
        events = self.store.get_events(task_id)
        artifacts = self.store.get_artifacts(task_id)
        event_counts: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("event_type") or "").strip()
            if not event_type:
                continue
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        artifact_counts: dict[str, int] = {}
        for artifact in artifacts:
            artifact_kind = str(artifact.get("artifact_kind") or "").strip()
            if not artifact_kind:
                continue
            artifact_counts[artifact_kind] = artifact_counts.get(artifact_kind, 0) + 1
        latest_event = events[-1] if events else None
        latest_action = None
        latest_recovery = None
        for event in reversed(events):
            if event.get("event_type") != "task.action":
                continue
            payload = event.get("payload") or {}
            summary = {
                "action": str(payload.get("action") or "").strip(),
                "status": str(payload.get("status") or "").strip(),
                "surface": str(payload.get("surface") or "").strip(),
                "target_bucket": str(payload.get("target_bucket") or "").strip(),
                "session_key": str(payload.get("session_key") or "").strip(),
                "created_at": event.get("created_at"),
            }
            if latest_action is None:
                latest_action = summary
            if latest_recovery is None and summary["action"] == "recover":
                latest_recovery = summary
            if latest_action is not None and latest_recovery is not None:
                break
        return {
            "task_id": task.get("task_id"),
            "parent_task_id": task.get("parent_task_id") or "",
            "session_id": task.get("session_id") or "",
            "surface": task.get("surface") or "",
            "platform": task.get("platform") or "",
            "state": task.get("state") or "",
            "plan_mode": task.get("plan_mode") or "direct",
            "normalized_goal": task.get("normalized_goal") or "",
            "plan_artifact_path": task.get("plan_artifact_path") or "",
            "updated_at": task.get("updated_at"),
            "completed_at": task.get("completed_at"),
            "last_error": task.get("last_error") or "",
            "artifacts": {
                "count": len(artifacts),
                "by_kind": artifact_counts,
            },
            "events": {
                "count": len(events),
                "by_type": event_counts,
                "latest": latest_event,
            },
            "signals": {
                "verification_count": event_counts.get("evidence.verification", 0),
                "blocked_count": event_counts.get("gate.tool.blocked", 0),
                "drift_count": event_counts.get("drift.detected", 0),
                "action_count": event_counts.get("task.action", 0),
            },
            "control": {
                "latest_action": latest_action,
                "latest_recovery": latest_recovery,
            },
        }

    def summarize_tasks(
        self,
        *,
        limit: int = 5,
        surfaces: Optional[list[str]] = None,
        states: Optional[list[str]] = None,
        session_id: str = "",
        platform: str = "",
    ) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "total": 0,
                "states": {},
                "surfaces": {},
                "recent": [],
            }
        tasks = self.store.list_tasks(
            limit=max(1, int(limit or 5)),
            surfaces=surfaces,
            states=states,
            session_id=session_id,
            platform=platform,
        )
        state_counts: dict[str, int] = {}
        surface_counts: dict[str, int] = {}
        recent: list[dict[str, Any]] = []
        for task in tasks:
            state = str(task.get("state") or "unknown").strip() or "unknown"
            surface = str(task.get("surface") or "unknown").strip() or "unknown"
            task_id = str(task.get("task_id") or "").strip()
            state_counts[state] = state_counts.get(state, 0) + 1
            surface_counts[surface] = surface_counts.get(surface, 0) + 1
            snapshot = self.task_snapshot(task_id) if task_id else None
            control = snapshot.get("control") if isinstance(snapshot, dict) and isinstance(snapshot.get("control"), dict) else {}
            recent.append({
                "task_id": task_id,
                "state": state,
                "surface": surface,
                "goal": task.get("normalized_goal") or "",
                "updated_at": task.get("updated_at"),
                "control": control,
            })
        return {
            "enabled": True,
            "total": len(tasks),
            "states": state_counts,
            "surfaces": surface_counts,
            "recent": recent,
        }

    def _task_base_root(self, task: dict[str, Any]) -> Path:
        metadata = task.get("metadata") or {}
        workspace_root = metadata.get("workspace_root") or ""
        if workspace_root:
            return _safe_path(workspace_root)
        write_scope = task.get("write_scope") or []
        if write_scope:
            return _safe_path(write_scope[0])
        return _safe_path(os.getcwd())

    def _task_write_roots(self, task: dict[str, Any]) -> list[Path]:
        roots = []
        for raw_root in task.get("write_scope") or []:
            try:
                roots.append(_safe_path(raw_root))
            except Exception:
                continue
        return roots

    def _resolve_candidate_paths(self, task: dict[str, Any], tool_name: str, args: dict[str, Any]) -> list[Path]:
        if not isinstance(args, dict):
            return []
        base = self._task_base_root(task)
        paths: list[Path] = []
        raw_path = args.get("path")
        if raw_path:
            try:
                paths.append(_safe_path(raw_path, base=base))
            except Exception:
                pass
        workdir = args.get("workdir")
        if tool_name == "terminal" and workdir:
            try:
                paths.append(_safe_path(workdir, base=base))
            except Exception:
                pass
        return paths

    def _is_plan_artifact_call(self, task: dict[str, Any], tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name not in _PLAN_ARTIFACT_TOOL_NAMES or not isinstance(args, dict):
            return False
        plan_artifact_path = str(task.get("plan_artifact_path") or "").strip()
        if not plan_artifact_path:
            return False
        candidate_paths = self._resolve_candidate_paths(task, tool_name, args)
        if not candidate_paths:
            return False
        plan_path = _safe_path(plan_artifact_path, base=self._task_base_root(task))
        return any(candidate == plan_path for candidate in candidate_paths)

    def _is_verification_tool(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name == "terminal":
            return not _terminal_is_mutating((args or {}).get("command", ""))
        verification_tools = set(self.config.get("acceptance", {}).get("verification_tools", []))
        return tool_name in verification_tools

    def _is_read_only_tool(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name == "terminal":
            return not _terminal_is_mutating((args or {}).get("command", ""))
        return tool_name in _READ_ONLY_TOOLS

    def _is_mutating_tool(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name in _FILE_MUTATION_TOOLS:
            return True
        if tool_name == "terminal":
            return _terminal_is_mutating((args or {}).get("command", ""))
        if tool_name == "delegate_task":
            return True
        return tool_name not in _READ_ONLY_TOOLS and tool_name not in _ALWAYS_ALLOWED_META_TOOLS

    def _count_events(self, task_id: str, event_type: str) -> int:
        return sum(1 for event in self.store.get_events(task_id) if event.get("event_type") == event_type)

    def _mark_drift(self, task_id: str, reason: str, *, payload: Optional[dict[str, Any]] = None) -> None:
        data = {"reason": reason}
        if payload:
            data.update(payload)
        self.store.append_event(task_id, "drift.detected", data)
        self.store.transition_state(task_id, "needs_replan")

    def preflight_tool_call(
        self,
        *,
        task_id: str,
        tool_name: str,
        args: dict[str, Any],
        session_id: str = "",
        tool_call_id: str = "",
    ) -> dict[str, Any]:
        if not self.enabled or not task_id:
            return {"allowed": True}

        task = self.store.get_task(task_id)
        if not task:
            return {"allowed": True}

        state = str(task.get("state") or "")
        if state in {"completed", "cancelled"} and self._is_mutating_tool(tool_name, args):
            reason = f"Task {task_id} is already {state}; start a new task instead of mutating it further."
            self.store.append_event(task_id, "gate.tool.blocked", {"tool_name": tool_name, "reason": reason, "state": state, "tool_call_id": tool_call_id, "session_id": session_id})
            return {"allowed": False, "reason": reason, "state": state}

        if state == "needs_acceptance" and self._is_mutating_tool(tool_name, args):
            reason = "Task is awaiting acceptance; collect review/verification evidence or start a new task before more edits."
            self.store.append_event(task_id, "gate.tool.blocked", {"tool_name": tool_name, "reason": reason, "state": state, "tool_call_id": tool_call_id, "session_id": session_id})
            return {"allowed": False, "reason": reason, "state": state}

        if task.get("plan_mode") == "plan_required" and state == "planning":
            planning_allowed = (
                self._is_read_only_tool(tool_name, args)
                or tool_name in _ALWAYS_ALLOWED_META_TOOLS
                or self._is_plan_artifact_call(task, tool_name, args)
            )
            if not planning_allowed:
                reason = "Plan-required task is still in planning; write or update the plan artifact before broader implementation."
                self.store.append_event(task_id, "gate.tool.blocked", {"tool_name": tool_name, "reason": reason, "state": state, "tool_call_id": tool_call_id, "session_id": session_id})
                blocked_count = self._count_events(task_id, "gate.tool.blocked")
                threshold = int(self.config.get("drift", {}).get("blocked_tool_threshold", 2))
                if blocked_count >= threshold:
                    self._mark_drift(task_id, "repeated_plan_gate_violations", payload={"blocked_count": blocked_count, "tool_name": tool_name})
                refreshed = self.store.get_task(task_id) or {}
                return {"allowed": False, "reason": reason, "state": refreshed.get("state", state)}

        if self._is_mutating_tool(tool_name, args):
            write_roots = self._task_write_roots(task)
            candidate_paths = self._resolve_candidate_paths(task, tool_name, args)
            if candidate_paths and write_roots and not any(
                any(_is_subpath(candidate, root) for root in write_roots)
                for candidate in candidate_paths
            ):
                reason = f"Tool write scope violation: {tool_name} targets outside the admitted workspace."
                self.store.append_event(task_id, "gate.tool.blocked", {"tool_name": tool_name, "reason": reason, "paths": [str(path) for path in candidate_paths], "state": state, "tool_call_id": tool_call_id, "session_id": session_id})
                blocked_count = self._count_events(task_id, "gate.tool.blocked")
                threshold = int(self.config.get("drift", {}).get("blocked_tool_threshold", 2))
                if blocked_count >= threshold:
                    self._mark_drift(task_id, "write_scope_violation", payload={"blocked_count": blocked_count, "tool_name": tool_name})
                refreshed = self.store.get_task(task_id) or {}
                return {"allowed": False, "reason": reason, "state": refreshed.get("state", state)}

        return {"allowed": True, "state": state}

    def record_tool_start(
        self,
        *,
        task_id: str,
        tool_name: str,
        args: dict[str, Any],
        session_id: str = "",
        tool_call_id: str = "",
    ) -> None:
        if not self.enabled or not task_id:
            return
        self.store.append_event(
            task_id,
            "tool.started",
            {
                "tool_name": tool_name,
                "args": args,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
            },
        )

    def record_tool_complete(
        self,
        *,
        task_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        session_id: str = "",
        tool_call_id: str = "",
    ) -> None:
        if not self.enabled or not task_id:
            return
        preview_chars = int(self.config.get("audit", {}).get("result_preview_chars", 280))
        preview = str(result)
        if len(preview) > preview_chars:
            preview = preview[: preview_chars - 1] + "…"
        self.store.append_event(
            task_id,
            "tool.completed",
            {
                "tool_name": tool_name,
                "args": args,
                "result_preview": preview,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
            },
        )

        task = self.store.get_task(task_id) or {}
        candidate_paths = self._resolve_candidate_paths(task, tool_name, args)
        artifact_path = None
        artifact_kind = None
        if candidate_paths:
            artifact_path = str(candidate_paths[0])
            artifact_kind = "file"
        elif isinstance(args, dict) and args.get("patch"):
            artifact_path = "inline-patch"
            artifact_kind = "patch"
        if artifact_path and artifact_kind:
            self.store.add_artifact(task_id, artifact_kind, artifact_path, label=tool_name)
            self.store.append_event(
                task_id,
                "artifact.created",
                {
                    "artifact_kind": artifact_kind,
                    "artifact_path": artifact_path,
                    "tool_name": tool_name,
                },
            )

        if task and self._is_plan_artifact_call(task, tool_name, args):
            plan_path = str(_safe_path(task.get("plan_artifact_path") or "", base=self._task_base_root(task)))
            self.store.add_artifact(task_id, "plan_artifact", plan_path, label=tool_name)
            self.store.append_event(
                task_id,
                "gate.plan.satisfied",
                {"plan_artifact_path": plan_path, "tool_name": tool_name},
            )
            if task.get("state") == "planning":
                self.store.transition_state(task_id, "admitted")
        elif task and task.get("state") in {"admitted", "planning"} and self._is_mutating_tool(tool_name, args):
            self.store.transition_state(task_id, "active")

        if task and self._is_verification_tool(tool_name, args):
            self.store.append_event(
                task_id,
                "evidence.verification",
                {"tool_name": tool_name, "tool_call_id": tool_call_id},
            )

        if self.config.get("drift", {}).get("enabled", True):
            events = self.store.get_events(task_id)
            streak = 0
            for event in reversed(events):
                if event.get("event_type") != "tool.completed":
                    continue
                payload = event.get("payload") or {}
                if self._is_read_only_tool(payload.get("tool_name", ""), payload.get("args") or {}):
                    streak += 1
                    continue
                break
            threshold = int(self.config.get("drift", {}).get("read_only_streak_threshold", 6))
            if streak >= threshold and not any(ev.get("event_type") == "drift.detected" for ev in events):
                self._mark_drift(task_id, "read_only_streak", payload={"streak": streak})

    def record_tool_error(
        self,
        *,
        task_id: str,
        tool_name: str,
        error: str,
        session_id: str = "",
        tool_call_id: str = "",
    ) -> None:
        if not self.enabled or not task_id:
            return
        self.store.append_event(
            task_id,
            "tool.failed",
            {
                "tool_name": tool_name,
                "error": error,
                "session_id": session_id,
                "tool_call_id": tool_call_id,
            },
        )

    def record_process_spawned(
        self,
        *,
        task_id: str,
        process_session_id: str,
        command: str,
        pid: Optional[int] = None,
        session_key: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled or not task_id or not process_session_id:
            return
        task = self.store.get_task(task_id) or {}
        payload = {
            "process_session_id": process_session_id,
            "command": command,
            "pid": pid,
            "session_key": session_key,
        }
        if metadata:
            payload.update(metadata)
        self.store.append_event(task_id, "process.spawned", payload)
        if task.get("state") in {"planning", "admitted"}:
            self.store.transition_state(task_id, "active")

    def record_process_completed(
        self,
        *,
        task_id: str,
        process_session_id: str,
        exit_code: Optional[int],
        output_preview: str = "",
        session_key: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled or not task_id or not process_session_id:
            return
        event_type = "process.completed" if exit_code in (0, None) else "process.failed"
        payload = {
            "process_session_id": process_session_id,
            "exit_code": exit_code,
            "output_preview": _normalize_goal(output_preview, limit=280) if output_preview else "",
            "session_key": session_key,
        }
        if metadata:
            payload.update(metadata)
        self.store.append_event(task_id, event_type, payload)

    def record_task_action(
        self,
        *,
        task_id: str,
        action: str,
        status: str,
        surface: str,
        session_key: str = "",
        platform: str = "",
        target_bucket: Any = None,
        task_context: Any = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled or not task_id:
            return

        action_text = str(action or "").strip().lower()
        status_text = str(status or "").strip().lower()
        surface_text = str(surface or "gateway_control").strip() or "gateway_control"
        context = _task_context_details(task_context)
        session_key_text = str(session_key or context.get("session_key") or "").strip()
        platform_text = str(platform or context.get("platform") or context.get("source") or "gateway").strip() or "gateway"
        target_bucket_text = str(target_bucket or "").strip().lower() or ""

        task = self.store.get_task(task_id)
        if not task:
            preview = str(context.get("preview") or f"Gateway queued task {task_id}").strip()
            control_metadata = {
                "created_from": "task_control",
                "session_key": session_key_text,
                "lane": context.get("lane") or "",
                "kind": context.get("kind") or "queued_message",
                "source": context.get("source") or platform_text,
                "control_mode": context.get("control_mode") or "queued",
                "queued_at": context.get("queued_at") or "",
                "started_at": context.get("started_at") or "",
            }
            if context.get("priority") is not None:
                control_metadata["priority"] = context.get("priority")
            contract = TaskContract(
                task_id=task_id,
                session_id=session_key_text or task_id,
                surface=surface_text,
                platform=platform_text,
                raw_request=preview,
                normalized_goal=_normalize_goal(preview),
                plan_mode="direct",
                workspace_root=os.getcwd(),
                autonomy_target=str(self.config.get("default_autonomy_target") or "bounded"),
                metadata=control_metadata,
            )
            initial_state = "queued"
            if action_text in {"foreground", "recover"} and status_text == "started":
                initial_state = "active"
            elif action_text == "cancel" and status_text == "cancelled":
                initial_state = "cancelled"
            self.store.upsert_task(contract, initial_state)
            task = self.store.get_task(task_id) or {}

        payload = {
            "action": action_text,
            "status": status_text,
            "surface": surface_text,
            "session_key": session_key_text,
        }
        for key in ("preview", "lane", "kind", "priority", "source", "platform", "queued_at", "started_at", "control_mode"):
            value = context.get(key)
            if value not in (None, ""):
                payload[key] = value
        if target_bucket_text:
            payload["target_bucket"] = target_bucket_text
        if metadata:
            payload["metadata"] = dict(metadata)
        self.store.append_event(task_id, "task.action", payload)

        if action_text == "cancel" and status_text == "cancelled":
            self.store.transition_state(task_id, "cancelled", completed_at=_utc_now())
        elif action_text in {"foreground", "recover"} and status_text == "started":
            self.store.transition_state(task_id, "active")
        elif status_text == "queued_next" or action_text == "reprioritize" or (action_text == "recover" and target_bucket_text):
            self.store.transition_state(task_id, "queued")


    def record_budget_pause(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        reason: str,
        retry_after_seconds: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.enabled or not task_id:
            return {}

        rate_cfg = self.config.get("rate_limits", {}) if isinstance(self.config.get("rate_limits"), dict) else {}
        if retry_after_seconds is None:
            retry_after_seconds = int(rate_cfg.get("subscription_retry_seconds", 1800) or 1800)
        retry_after_seconds = max(1, int(retry_after_seconds))
        retry_after_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        ).isoformat()
        if metadata and metadata.get("retry_after_at"):
            retry_after_at = str(metadata.get("retry_after_at") or "").strip()

        payload = {
            "provider": str(provider or "").strip(),
            "model": str(model or "").strip(),
            "reason": _normalize_goal(reason, limit=280),
            "retry_after_seconds": retry_after_seconds,
            "retry_after_at": retry_after_at,
        }
        if metadata:
            payload["metadata"] = dict(metadata)

        self.store.append_event(task_id, "budget.pause", payload)
        self.store.transition_state(
            task_id,
            "waiting_external",
            last_error=payload["reason"],
            completed_at=_utc_now(),
        )
        return payload

    def _latest_budget_pause(self, task_id: str) -> Optional[dict[str, Any]]:
        latest: Optional[dict[str, Any]] = None
        for event in self.store.get_events(task_id):
            if event.get("event_type") != "budget.pause":
                continue
            payload = dict(event.get("payload") or {})
            payload.setdefault("created_at", event.get("created_at"))
            latest = payload
        return latest

    def list_budget_paused_tasks(
        self,
        *,
        now: Any = None,
        due_only: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        now_dt = _parse_harness_datetime(now) or datetime.now(timezone.utc)
        tasks = self.store.list_tasks(states=["waiting_external"], limit=max(1, int(limit or 20)))
        paused: list[dict[str, Any]] = []
        for task in tasks:
            task_id = str(task.get("task_id") or "").strip()
            pause = self._latest_budget_pause(task_id)
            if not pause:
                continue
            retry_after_at = str(pause.get("retry_after_at") or "").strip()
            retry_dt = _parse_harness_datetime(retry_after_at)
            due = retry_dt is None or retry_dt <= now_dt
            if due_only and not due:
                continue
            metadata = task.get("metadata") or {}
            paused.append(
                {
                    "task_id": task_id,
                    "session_id": task.get("session_id") or "",
                    "platform": task.get("platform") or "",
                    "workstream": _format_workstream_label(metadata.get("workstream", "")),
                    "goal": _normalize_goal(task.get("normalized_goal") or task.get("raw_request") or task_id, limit=96),
                    "provider": pause.get("provider") or "",
                    "model": pause.get("model") or "",
                    "reason": pause.get("reason") or "",
                    "retry_after_seconds": int(pause.get("retry_after_seconds") or 0),
                    "retry_after_at": retry_after_at,
                    "due": due,
                }
            )
        return paused

    def record_budget_resume(
        self,
        *,
        task_id: str,
        status: str = "queued",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.enabled or not task_id:
            return {}
        status_text = str(status or "queued").strip() or "queued"
        payload = {
            "status": status_text,
            "resumed_at": _utc_now(),
        }
        pause = self._latest_budget_pause(task_id)
        if pause:
            payload["pause"] = {
                "provider": pause.get("provider") or "",
                "model": pause.get("model") or "",
                "reason": pause.get("reason") or "",
                "retry_after_at": pause.get("retry_after_at") or "",
            }
        if metadata:
            payload["metadata"] = dict(metadata)
        self.store.append_event(task_id, "budget.resume", payload)
        self.store.transition_state(task_id, status_text)
        return payload

    def budget_resume_digest(self, *, now: Any = None, limit: int = 5) -> str:
        due = self.list_budget_paused_tasks(now=now, due_only=True, limit=max(1, int(limit or 5)))
        if not due:
            return ""
        lines = []
        for item in due[: max(1, int(limit or 5))]:
            parts = [
                str(item.get("task_id") or ""),
                str(item.get("model") or ""),
                str(item.get("reason") or ""),
            ]
            lines.append(" · ".join(part for part in parts if part))
        return "Ready to resume: " + "; ".join(lines)

    def should_pause_for_rate_limit(
        self,
        *,
        provider: str,
        model: str,
        error_text: str,
        status_code: Any = None,
    ) -> bool:
        if not self.enabled:
            return False
        rate_cfg = self.config.get("rate_limits", {}) if isinstance(self.config.get("rate_limits"), dict) else {}
        if not bool(rate_cfg.get("pause_high_value_models", True)):
            return False

        text = f"{provider or ''} {model or ''} {error_text or ''}".lower()
        is_rate_limit = (
            status_code == 429
            or "rate limit" in text
            or "rate_limit" in text
            or "too many requests" in text
            or "usage limit" in text
            or "quota" in text
        )
        if not is_rate_limit:
            return False

        patterns = rate_cfg.get("high_value_model_patterns") or ["gpt-5", "claude-opus", "claude-sonnet"]
        model_text = f"{provider or ''} {model or ''}".lower()
        return any(str(pattern or "").lower() in model_text for pattern in patterns)

    def allow_model_downgrade_for_rate_limit(self, *, provider: str = "", model: str = "") -> bool:
        return False

    def visibility_digest(self, *, limit: int = 5) -> str:
        if not self.enabled:
            return "Hermes task visibility is disabled."

        tasks = self.store.list_tasks(limit=max(1, int(limit or 5)))
        completed: list[str] = []
        blocked: list[str] = []
        next_items: list[str] = []

        for task in tasks:
            task_id = str(task.get("task_id") or "").strip()
            state = str(task.get("state") or "unknown").strip()
            goal = _normalize_goal(task.get("normalized_goal") or task.get("raw_request") or task_id, limit=96)
            metadata = task.get("metadata") or {}
            workstream = _format_workstream_label(metadata.get("workstream", ""))
            prefix = f"{workstream}: {goal}" if workstream != _DEFAULT_WORKSTREAM else goal

            if state == "completed":
                completed.append(prefix)
                continue

            if state in {"waiting_external", "failed", "needs_replan"}:
                reason = task.get("last_error") or ""
                if not reason:
                    for event in reversed(self.store.get_events(task_id)):
                        if event.get("event_type") == "budget.pause":
                            reason = (event.get("payload") or {}).get("reason") or ""
                            break
                blocked.append(f"{prefix} ({_normalize_goal(reason, limit=80)})" if reason else prefix)
                continue

            if state in {"queued", "admitted", "planning", "active", "needs_acceptance"}:
                next_items.append(prefix)

        def _line(title: str, items: list[str]) -> str:
            if not items:
                return f"{title}: none"
            return f"{title}: " + " | ".join(items[:limit])

        return "\n".join([
            _line("Completed", completed),
            _line("Blocked", blocked),
            _line("Next", next_items),
        ])

    def finalize_turn(
        self,
        *,
        task_id: str,
        final_response: str,
        completed: bool,
        interrupted: bool,
        api_calls: int,
        message_count: int,
    ) -> str:
        if not self.enabled or not task_id:
            return "disabled"

        task = self.store.get_task(task_id) or {}
        plan_mode = task.get("plan_mode", "direct")
        require_acceptance = bool(self.config.get("acceptance", {}).get("require_evidence_for_plan_required", True))
        artifacts = self.store.get_artifacts(task_id)
        events = self.store.get_events(task_id)
        has_plan_artifact = any(artifact.get("artifact_kind") == "plan_artifact" for artifact in artifacts)
        has_verification_evidence = any(event.get("event_type") == "evidence.verification" for event in events)
        drift_detected = any(event.get("event_type") == "drift.detected" for event in events)

        if interrupted:
            new_state = "waiting_external"
        elif not completed:
            new_state = "failed"
        elif plan_mode == "plan_required" and not has_plan_artifact:
            new_state = "needs_replan"
        elif drift_detected:
            new_state = "needs_replan"
        elif plan_mode == "plan_required" and require_acceptance and not has_verification_evidence:
            new_state = "needs_replan"
        elif plan_mode == "plan_required" and require_acceptance:
            new_state = "needs_acceptance"
        else:
            new_state = "completed"

        completed_at = _utc_now() if new_state in {"completed", "failed", "cancelled", "needs_acceptance", "waiting_external", "needs_replan"} else ""
        self.store.transition_state(task_id, new_state, interrupted=interrupted, completed_at=completed_at)
        self.store.append_event(
            task_id,
            "task.transition",
            {
                "state": new_state,
                "completed": completed,
                "interrupted": interrupted,
                "api_calls": api_calls,
                "message_count": message_count,
            },
        )
        if new_state == "needs_acceptance":
            self.store.append_event(
                task_id,
                "gate.acceptance.required",
                {
                    "reason": "plan_required_task_needs_evidence_review",
                    "response_preview": _normalize_goal(final_response, limit=180),
                },
            )
        elif new_state == "needs_replan":
            reason = "missing_plan_artifact"
            if drift_detected:
                reason = "drift_detected"
            elif plan_mode == "plan_required" and require_acceptance and not has_verification_evidence:
                reason = "missing_verification_evidence"
            self.store.append_event(
                task_id,
                "gate.replan.required",
                {
                    "reason": reason,
                    "response_preview": _normalize_goal(final_response, limit=180),
                },
            )
        elif new_state == "completed":
            self.store.append_event(
                task_id,
                "task.completed",
                {"response_preview": _normalize_goal(final_response, limit=180)},
            )
        return new_state
