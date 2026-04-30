"""Hermes self-evolution control-plane primitives.

The self-evolution office is the proactive system-improvement layer: it observes
Hermes as a whole, turns evidence into upgrade opportunities, decides the safe
next action, and writes auditable reports.  It deliberately does not perform
high-risk changes directly; execution remains routed through the harness/queue
and live-gateway safety gates.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home


class AuthorityLevel(str, Enum):
    """Maximum autonomy permitted for a self-evolution recommendation."""

    OBSERVE = "observe"
    RECOMMEND = "recommend"
    TASK_DRAFT = "task_draft"
    LOW_RISK_EXECUTION = "low_risk_execution"
    USER_APPROVAL = "user_approval"


@dataclass
class SystemImprovementOpportunity:
    id: str
    area: str
    symptom: str
    root_cause_hypothesis: str
    evidence: list[str]
    global_impact: str
    recommended_direction: str
    options: list[str]
    priority_score: int
    authority_level_required: AuthorityLevel
    can_execute_now: bool
    verification_plan: list[str]
    rollback_plan: list[str]
    worker_strategy: str = "hermes_orchestrated"
    risk_tags: list[str] = field(default_factory=list)


@dataclass
class EvolutionDecision:
    opportunity_id: str
    decision: str
    reason: str
    selected_option: str
    worker_strategy: str
    risk_tags: list[str]
    acceptance: list[str]


@dataclass
class OptimizationTaskDraft:
    id: str
    title: str
    prompt: str
    workstream: str
    plan_required: bool
    risk_tags: list[str]
    acceptance: list[str]
    source_opportunity_id: str
    worker_strategy: str


@dataclass
class SelfEvolutionRun:
    run_id: str
    created_at: str
    trigger: str
    scope: list[str]
    observations: dict[str, Any]
    opportunities: list[SystemImprovementOpportunity]
    decisions: list[EvolutionDecision]
    task_drafts: list[OptimizationTaskDraft]
    actions_taken: list[str]
    blocked_actions: list[str]
    verification: list[str]
    summary: dict[str, Any]
    next_review_at: str | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe or "self-evolution"


def _enum_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_enum_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _enum_safe(val) for key, val in value.items()}
    return value


def run_to_dict(run: SelfEvolutionRun) -> dict[str, Any]:
    return _enum_safe(asdict(run))


def classify_authority_level(*, risk_tags: Iterable[str] | None = None, action: str = "") -> AuthorityLevel:
    """Classify the maximum authority needed for a proposed improvement."""

    tags = {str(tag).strip().lower() for tag in (risk_tags or []) if str(tag).strip()}
    text = str(action or "").lower()
    approval_markers = {
        "live_gateway",
        "restart",
        "stop_service",
        "delete",
        "memory_delete",
        "model_routing",
        "account_pool",
        "provider",
        "paid_cost",
        "auth",
        "security",
        "irreversible",
        "broad_rewrite",
    }
    if tags.intersection(approval_markers) or any(marker in text for marker in approval_markers):
        return AuthorityLevel.USER_APPROVAL
    if tags.intersection({"tests", "read_only", "report", "diagnostic", "skill_draft"}):
        return AuthorityLevel.LOW_RISK_EXECUTION
    if tags.intersection({"task_draft", "plan_required", "queue_task"}):
        return AuthorityLevel.TASK_DRAFT
    return AuthorityLevel.TASK_DRAFT


class SelfEvolutionEngine:
    """Observe Hermes, find upgrade opportunities, and decide safe next actions."""

    DEFAULT_SCOPE = ["memory", "skills", "workers", "queue_cron", "gateway", "config"]

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        hermes_home: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.config = config or {}
        self.hermes_home = Path(hermes_home) if hermes_home else get_hermes_home()
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
        raw = self.config.get("self_evolution", {}) if isinstance(self.config, dict) else {}
        self.self_config = raw if isinstance(raw, dict) else {}

    def _cfg(self, key: str, default: Any) -> Any:
        return self.self_config.get(key, default)

    def collect_observations(self) -> dict[str, Any]:
        """Collect a conservative read-only snapshot for a self-evolution run."""

        observations: dict[str, Any] = {
            "memory": self._collect_memory_summary(),
            "skills": self._collect_skill_summary(),
            "workers": self._collect_worker_summary(),
            "queue_cron": self._collect_queue_cron_summary(),
            "gateway": self._collect_gateway_summary(),
            "config": self._collect_config_summary(),
        }
        return observations

    def _collect_memory_summary(self) -> dict[str, Any]:
        memory_cfg = self.config.get("memory", {}) if isinstance(self.config, dict) else {}
        memory_dir = self.hermes_home / "memories"
        bytes_used = 0
        file_count = 0
        if memory_dir.exists():
            for path in memory_dir.glob("**/*"):
                if path.is_file():
                    file_count += 1
                    try:
                        bytes_used += path.stat().st_size
                    except OSError:
                        pass
        return {
            "usage_ratio": 0.0,
            "file_count": file_count,
            "bytes_used": bytes_used,
            "memory_char_limit": memory_cfg.get("memory_char_limit"),
            "user_char_limit": memory_cfg.get("user_char_limit"),
            "procedural_entries": [],
            "long_entries": [],
        }

    def _collect_skill_summary(self) -> dict[str, Any]:
        skill_roots = [self.hermes_home / "skills"]
        count = 0
        for root in skill_roots:
            if not root.exists():
                continue
            for path in root.glob("**/SKILL.md"):
                if path.is_file():
                    count += 1
        return {"skill_count": count, "roots": [str(root) for root in skill_roots]}

    def _collect_worker_summary(self) -> dict[str, Any]:
        custom_paths = [
            self.project_root / "gateway" / "worker_runtime.py",
            self.project_root / "tools" / "delegate_tool.py",
            self.project_root / "agent" / "task_queue.py",
        ]
        external_workers = {
            "claude_code": "available" if shutil.which("claude") else "missing",
            "opencode": "available" if shutil.which("opencode") else "missing",
            "codex": "available" if shutil.which("codex") else "missing",
            "gemini": "available" if shutil.which("gemini") else "missing",
        }
        return {
            "custom_worker_count": sum(1 for path in custom_paths if path.exists()),
            "custom_worker_paths": [str(path) for path in custom_paths if path.exists()],
            "external_workers": external_workers,
            "recent_custom_worker_failures": 0,
        }

    def _collect_queue_cron_summary(self) -> dict[str, Any]:
        queue_summary: dict[str, Any] = {}
        try:
            from gateway.task_queue_bridge import task_queue_status

            queue_summary = task_queue_status(self.config)
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            queue_summary = {"error": repr(exc)}
        cron_dir = self.hermes_home / "cron"
        cron_job_count = 0
        cron_main_driver_count = 0
        for filename in ("jobs.json", "jobs.jsonl"):
            path = cron_dir / filename
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if filename.endswith("jsonl"):
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                loaded = json.loads(text or "[]")
                rows = loaded if isinstance(loaded, list) else list(loaded.values()) if isinstance(loaded, dict) else []
            cron_job_count += len(rows)
            cron_main_driver_count += sum(1 for row in rows if isinstance(row, dict) and not row.get("queue_first"))
        return {
            "queue_status": queue_summary,
            "cron_job_count": cron_job_count,
            "cron_main_driver_count": cron_main_driver_count,
            "queue_first": cron_main_driver_count == 0,
        }

    def _collect_gateway_summary(self) -> dict[str, Any]:
        state_path = self.hermes_home / "gateway_state.json"
        if not state_path.exists():
            return {"state_file_exists": False}
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"state_file_exists": True, "error": repr(exc)}
        return {
            "state_file_exists": True,
            "gateway_state": data.get("gateway_state"),
            "pid": data.get("pid"),
            "queued_tasks": data.get("queued_tasks"),
            "live_tasks": data.get("live_tasks"),
            "platforms": data.get("platforms"),
        }

    def _collect_config_summary(self) -> dict[str, Any]:
        if not isinstance(self.config, dict):
            return {"root_keys": []}
        return {
            "root_keys": sorted(str(key) for key in self.config.keys()),
            "self_evolution_enabled": bool(self.self_config.get("enabled", True)),
            "auto_create_tasks": bool(self.self_config.get("auto_create_tasks", False)),
        }

    def evaluate(self, observations: dict[str, Any]) -> list[SystemImprovementOpportunity]:
        opportunities: list[SystemImprovementOpportunity] = []
        opportunities.extend(self._evaluate_memory(observations.get("memory", {})))
        opportunities.extend(self._evaluate_workers(observations.get("workers", {})))
        opportunities.extend(self._evaluate_queue_cron(observations.get("queue_cron", {})))
        opportunities.extend(self._evaluate_config(observations.get("config", {})))
        opportunities.sort(key=lambda item: item.priority_score, reverse=True)
        max_findings = int(self._cfg("max_findings_per_run", 8) or 8)
        return opportunities[:max(1, max_findings)]

    def _evaluate_memory(self, memory: Any) -> list[SystemImprovementOpportunity]:
        if not isinstance(memory, dict):
            return []
        threshold = float(self._cfg("memory_pressure_threshold", 0.8) or 0.8)
        usage = float(memory.get("usage_ratio") or 0.0)
        procedural = list(memory.get("procedural_entries") or [])
        long_entries = list(memory.get("long_entries") or [])
        if usage < threshold and not procedural and not long_entries:
            return []
        evidence = [f"usage_ratio={usage:.2f}"]
        if procedural:
            evidence.append(f"procedural_entries={len(procedural)}")
        if long_entries:
            evidence.append(f"long_entries={len(long_entries)}")
        return [
            SystemImprovementOpportunity(
                id="memory-state-placement",
                area="memory",
                symptom="Global memory is carrying state that may be better placed elsewhere",
                root_cause_hypothesis="Durable preferences, procedures, and long knowledge are mixed in the prompt-injected memory layer",
                evidence=evidence,
                global_impact="Overloaded memory reduces the practical effect of the most important preferences and increases prompt cost",
                recommended_direction="Rebalance durable state across memory, skills, external knowledge, and project-local context",
                options=[
                    "Extract repeatable procedures into skills",
                    "Move long research/system notes to an external knowledge store or local review report",
                    "Keep only compact user preferences and stable environment facts in memory",
                ],
                priority_score=90 if usage >= 0.9 else 80,
                authority_level_required=AuthorityLevel.TASK_DRAFT,
                can_execute_now=False,
                verification_plan=["memory usage falls below threshold", "procedural knowledge has skill coverage", "no user preference is deleted without approval"],
                rollback_plan=["Keep original memory entries until the user approves removals"],
                worker_strategy="hermes_orchestrated",
                risk_tags=["memory", "task_draft", "plan_required"],
            )
        ]

    def _evaluate_workers(self, workers: Any) -> list[SystemImprovementOpportunity]:
        if not isinstance(workers, dict):
            return []
        custom_count = int(workers.get("custom_worker_count") or 0)
        failures = int(workers.get("recent_custom_worker_failures") or 0)
        external = workers.get("external_workers") or {}
        available = {name for name, state in external.items() if state == "available"}
        if custom_count <= 0 or not available:
            return []
        preferred = "Claude Code" if "claude_code" in available else sorted(available)[0].replace("_", " ")
        score = 88 + min(7, failures)
        return [
            SystemImprovementOpportunity(
                id="worker-buy-vs-build",
                area="workers",
                symptom="Hermes has custom worker machinery while stronger external worker tools are available",
                root_cause_hypothesis="The system may be duplicating code-editing and agent-execution capabilities instead of using mature external workers",
                evidence=[f"custom_worker_count={custom_count}", f"available_external_workers={sorted(available)}", f"recent_custom_worker_failures={failures}"],
                global_impact="Custom wheels can add bugs, maintenance load, token waste, and repeated rework when Hermes should focus on orchestration and verification",
                recommended_direction=f"Prefer {preferred} as a coding worker where available; keep Hermes responsible for routing, policy, queueing, and verification",
                options=[
                    f"Use {preferred} for implementation-heavy worker lanes",
                    "Keep custom worker only for Hermes-specific orchestration and safe wrappers",
                    "Maintain a worker capability matrix before expanding internal workers",
                ],
                priority_score=min(score, 95),
                authority_level_required=AuthorityLevel.TASK_DRAFT,
                can_execute_now=False,
                verification_plan=["worker capability matrix exists", "implementation tasks route to best available worker", "custom worker scope is reduced or justified"],
                rollback_plan=["Keep current custom worker path until external worker route passes smoke tests"],
                worker_strategy="prefer_external_worker",
                risk_tags=["workers", "task_draft", "plan_required"],
            )
        ]

    def _evaluate_queue_cron(self, queue_cron: Any) -> list[SystemImprovementOpportunity]:
        if not isinstance(queue_cron, dict):
            return []
        cron_main = int(queue_cron.get("cron_main_driver_count") or 0)
        queue_first = bool(queue_cron.get("queue_first", cron_main == 0))
        if cron_main <= 0 and queue_first:
            return []
        return [
            SystemImprovementOpportunity(
                id="queue-first-control-plane",
                area="queue_cron",
                symptom="Some cron work still appears to be a main execution driver",
                root_cause_hypothesis="Cron and queue responsibilities may still overlap",
                evidence=[f"cron_main_driver_count={cron_main}", f"queue_first={queue_first}"],
                global_impact="Duplicated control planes make progress, retries, and stale-running recovery harder to reason about",
                recommended_direction="Move recurring work to queue-first execution and keep cron as watchdog/wakeup only",
                options=["Migrate one remaining cron driver at a time", "Keep cron only for watchdog and stale-state recovery"],
                priority_score=82,
                authority_level_required=AuthorityLevel.TASK_DRAFT,
                can_execute_now=False,
                verification_plan=["due work enters queue", "cron does not directly execute main workload", "queue status has no stale running tasks"],
                rollback_plan=["Disable queue-first flag for the affected job if dispatch fails"],
                worker_strategy="hermes_orchestrated",
                risk_tags=["queue", "cron", "plan_required"],
            )
        ]

    def _evaluate_config(self, config_summary: Any) -> list[SystemImprovementOpportunity]:
        if not isinstance(config_summary, dict):
            return []
        if config_summary.get("self_evolution_enabled", True):
            return []
        return [
            SystemImprovementOpportunity(
                id="self-evolution-disabled",
                area="config",
                symptom="Self-evolution is disabled",
                root_cause_hypothesis="The system cannot proactively replace the user's optimization-steering role",
                evidence=["self_evolution_enabled=false"],
                global_impact="The user remains the default architect/operator for system improvements",
                recommended_direction="Enable report-only self-evolution first, then widen autonomy only after evidence",
                options=["Enable report-only mode", "Keep disabled and run manually"],
                priority_score=75,
                authority_level_required=AuthorityLevel.USER_APPROVAL,
                can_execute_now=False,
                verification_plan=["manual self-evolution run produces report", "no automatic high-risk changes occur"],
                rollback_plan=["Set self_evolution.enabled=false"],
                worker_strategy="hermes_orchestrated",
                risk_tags=["config", "approval"],
            )
        ]

    def decide(self, opportunity: SystemImprovementOpportunity) -> EvolutionDecision:
        level = opportunity.authority_level_required
        auto_tasks = bool(self._cfg("auto_create_tasks", False))
        auto_low_risk = bool(self._cfg("auto_execute_low_risk", False))
        if level == AuthorityLevel.USER_APPROVAL:
            decision = "ask_approval"
            reason = "Change touches a safety boundary or could be irreversible."
        elif level == AuthorityLevel.LOW_RISK_EXECUTION and auto_low_risk and opportunity.can_execute_now:
            decision = "execute_low_risk"
            reason = "Low-risk reversible improvement is within the approved mandate."
        elif level in {AuthorityLevel.TASK_DRAFT, AuthorityLevel.LOW_RISK_EXECUTION} and auto_tasks:
            decision = "create_task_draft"
            reason = "Opportunity is suitable for plan-required queue work."
        else:
            decision = "defer"
            reason = "Report-only mode is active; no task is created automatically."
        return EvolutionDecision(
            opportunity_id=opportunity.id,
            decision=decision,
            reason=reason,
            selected_option=opportunity.options[0] if opportunity.options else opportunity.recommended_direction,
            worker_strategy=opportunity.worker_strategy,
            risk_tags=list(opportunity.risk_tags),
            acceptance=list(opportunity.verification_plan),
        )

    def _draft_task(
        self,
        opportunity: SystemImprovementOpportunity,
        decision: EvolutionDecision,
    ) -> OptimizationTaskDraft:
        title = f"Self-evolution: {opportunity.area} - {opportunity.id}"
        prompt = (
            f"Improve Hermes based on self-evolution finding `{opportunity.id}`.\n"
            f"Symptom: {opportunity.symptom}\n"
            f"Hypothesis: {opportunity.root_cause_hypothesis}\n"
            f"Recommended direction: {opportunity.recommended_direction}\n"
            f"Selected option: {decision.selected_option}\n"
            "Create/update a plan artifact before implementation; verify with fresh evidence; "
            "do not perform high-risk actions without explicit approval."
        )
        return OptimizationTaskDraft(
            id=f"self-evolution-{_slug(opportunity.id)}",
            title=title,
            prompt=prompt,
            workstream="self_evolution",
            plan_required=True,
            risk_tags=list(opportunity.risk_tags),
            acceptance=list(opportunity.verification_plan),
            source_opportunity_id=opportunity.id,
            worker_strategy=opportunity.worker_strategy,
        )

    def build_run(
        self,
        *,
        trigger: str,
        observations: dict[str, Any],
        opportunities: list[SystemImprovementOpportunity],
    ) -> SelfEvolutionRun:
        decisions = [self.decide(item) for item in opportunities]
        by_id = {item.id: item for item in opportunities}
        task_drafts = [
            self._draft_task(by_id[decision.opportunity_id], decision)
            for decision in decisions
            if decision.decision == "create_task_draft" and decision.opportunity_id in by_id
        ]
        run_id = f"self-evolution-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        actions_taken = [d.opportunity_id for d in decisions if d.decision == "execute_low_risk"]
        blocked_actions = [d.opportunity_id for d in decisions if d.decision == "ask_approval"]
        summary = {
            "opportunity_count": len(opportunities),
            "task_draft_count": len(task_drafts),
            "decision_counts": _count_by(decision.decision for decision in decisions),
            "top_opportunities": [item.id for item in opportunities[:3]],
        }
        return SelfEvolutionRun(
            run_id=run_id,
            created_at=utc_now_iso(),
            trigger=trigger,
            scope=list(self._cfg("review_scopes", self.DEFAULT_SCOPE)),
            observations=observations,
            opportunities=opportunities,
            decisions=decisions,
            task_drafts=task_drafts,
            actions_taken=actions_taken,
            blocked_actions=blocked_actions,
            verification=["self-evolution run completed", "high-risk actions remain approval-gated"],
            summary=summary,
            next_review_at=None,
        )

    def run(self, *, observations: dict[str, Any] | None = None, trigger: str = "manual") -> SelfEvolutionRun:
        collected = observations if observations is not None else self.collect_observations()
        opportunities = self.evaluate(collected)
        return self.build_run(trigger=trigger, observations=collected, opportunities=opportunities)


def _count_by(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def write_report(run: SelfEvolutionRun, *, report_dir: str | Path | None = None) -> Path:
    base = Path(report_dir) if report_dir is not None else get_hermes_home() / "reviews" / "self_evolution"
    base.mkdir(parents=True, exist_ok=True)
    stamp = run.created_at.replace("Z", "").replace(":", "").replace("-", "")
    path = base / f"{stamp}-{_slug(run.run_id)}.json"
    path.write_text(json.dumps(run_to_dict(run), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_report(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def render_concise_summary(run: SelfEvolutionRun, report_path: str | Path | None = None) -> str:
    if not run.opportunities:
        result = "系统进化部已完成一轮只读审查，暂未发现需要立即升级的高价值事项。"
    else:
        top = run.opportunities[0]
        result = f"系统进化部已完成一轮只读审查，发现 {len(run.opportunities)} 个优化机会；最高优先级是：{top.recommended_direction}。"
    blockers = len([d for d in run.decisions if d.decision == "ask_approval"])
    blocker_text = "无。" if blockers == 0 else f"有 {blockers} 项需要你批准后才能动。"
    next_text = "下一步是按优先级创建 plan-required 优化任务。" if run.opportunities else "下一步是保持定期自查。"
    if report_path:
        next_text += f" 报告已保存：{report_path}"
    return f"Result:\n{result}\n\nBlocker:\n{blocker_text}\n\nNext step:\n{next_text}"


def run_self_evolution(
    *,
    config: dict[str, Any] | None = None,
    observations: dict[str, Any] | None = None,
    report_dir: str | Path | None = None,
    trigger: str = "manual",
) -> tuple[SelfEvolutionRun, Path]:
    engine = SelfEvolutionEngine(config=config)
    run = engine.run(observations=observations, trigger=trigger)
    report_path = write_report(run, report_dir=report_dir)
    return run, report_path
