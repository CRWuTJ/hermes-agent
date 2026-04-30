from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _json_default(value: Any):
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _make_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, set):
        return sorted(_make_json_safe(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_make_json_safe(item) for item in value]
    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            try:
                safe[key] = _make_json_safe(item)
            except TypeError:
                continue
        return safe
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _emit_message(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
    sys.stdout.flush()


def _gwconv_runtime_root() -> Path:
    return get_hermes_home() / "runtime" / "gwconv"


def _ensure_worker_runtime_dir(run_id: Optional[str] = None) -> tuple[str, Path]:
    worker_run_id = (run_id or uuid.uuid4().hex).strip() or uuid.uuid4().hex
    runtime_dir = _gwconv_runtime_root() / worker_run_id
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return worker_run_id, runtime_dir


def _write_json_artifact(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _read_json_artifact(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed reading detached worker artifact %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _strip_launcher_noise(lines: list[str]) -> list[str]:
    stripped: list[str] = []
    for line in lines:
        text = str(line or "").strip()
        if not text or text.startswith("Running as unit: "):
            continue
        stripped.append(text)
    return stripped


class GatewayConversationWorkerHandle:
    def __init__(
        self,
        *,
        proc: subprocess.Popen[str],
        unit_name: str,
        runtime_dir: Optional[Path] = None,
        worker_run_id: Optional[str] = None,
        approval_request_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.proc = proc
        self.unit_name = unit_name
        self.runtime_dir = runtime_dir
        self.worker_run_id = worker_run_id
        self._approval_request_callback = approval_request_callback
        self._result_event = threading.Event()
        self._latest_status: Dict[str, Any] = {}
        self._pending_approval: Optional[Dict[str, Any]] = None
        self._pending_approval_lock = threading.Lock()
        self._result: Optional[Dict[str, Any]] = None
        self._result_lock = threading.Lock()
        self._stderr_tail: list[str] = []
        self.model: Optional[str] = None
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _set_result_once(self, result: Dict[str, Any]) -> bool:
        """Store a terminal result exactly once and release waiters."""
        with self._result_lock:
            if self._result_event.is_set():
                return False
            self._result = result
            self._result_event.set()
            return True

    def _recover_result_from_artifacts(self) -> bool:
        """Load durable worker artifacts if the stdout protocol path failed."""
        recovered = self._result_from_artifacts()
        if not recovered:
            return False
        return self._set_result_once(recovered)

    def _result_from_artifacts(self) -> Optional[Dict[str, Any]]:
        if self.runtime_dir is None:
            return None

        result_payload = _read_json_artifact(self.runtime_dir / "result.json")
        if result_payload:
            result = dict(result_payload)
            result.setdefault("failed", False)
            result.setdefault("terminal_state", "completed")
            result.setdefault("failure_kind", "")
            return result

        error_payload = _read_json_artifact(self.runtime_dir / "error.json")
        if error_payload:
            final_response = str(
                error_payload.get("final_response")
                or f"⚠️ {error_payload.get('error') or 'Detached worker failed.'}"
            )
            return {
                "final_response": final_response,
                "messages": list(error_payload.get("messages") or []),
                "api_calls": int(error_payload.get("api_calls") or 0),
                "tools": list(error_payload.get("tools") or []),
                "failed": True,
                "terminal_state": str(error_payload.get("terminal_state") or "failed"),
                "failure_kind": str(error_payload.get("failure_kind") or "worker_error"),
                "traceback": error_payload.get("traceback"),
                "error": error_payload.get("error"),
                "worker_unit_name": error_payload.get("worker_unit_name") or self.unit_name,
                "worker_run_dir": error_payload.get("worker_run_dir") or str(self.runtime_dir),
            }
        return None

    def _protocol_failure_result(self) -> Dict[str, Any]:
        stderr_lines = _strip_launcher_noise(self._stderr_tail[-20:])
        diagnostics = [
            "⚠️ Detached worker protocol failure: worker exited without sending a terminal result, and no durable result artifact was found.",
        ]
        if self.unit_name:
            diagnostics.append(f"Conversation worker unit: {self.unit_name}")
        if self.runtime_dir is not None:
            diagnostics.append(f"Worker run dir: {self.runtime_dir}")
        if stderr_lines:
            diagnostics.append("Worker stderr tail:")
            diagnostics.extend(stderr_lines)
        return {
            "final_response": "\n".join(diagnostics),
            "messages": [],
            "api_calls": 0,
            "tools": [],
            "failed": True,
            "terminal_state": "protocol_violation",
            "failure_kind": "protocol_violation",
            "worker_unit_name": self.unit_name,
            "worker_run_dir": str(self.runtime_dir) if self.runtime_dir is not None else None,
        }

    def _finalize_terminal_result(self) -> None:
        try:
            if self.proc.poll() is not None and self._stderr_thread.is_alive():
                self._stderr_thread.join(timeout=0.1)
        except Exception:
            logger.debug("Detached worker stderr drain wait failed", exc_info=True)
        try:
            if self.proc.stderr is not None:
                remainder = self.proc.stderr.read()
                if remainder:
                    for raw_line in remainder.splitlines():
                        line = raw_line.rstrip("\n")
                        if line:
                            self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 200:
                        self._stderr_tail = self._stderr_tail[-200:]
        except Exception:
            logger.debug("Detached worker stderr remainder read failed", exc_info=True)
        recovered = self._result_from_artifacts()
        self._set_result_once(recovered or self._protocol_failure_result())

    def _stdout_loop(self) -> None:
        if self.proc.stdout is None:
            self._set_result_once({
                "final_response": "⚠️ Detached worker did not expose stdout.",
                "messages": [],
                "api_calls": 0,
                "tools": [],
                "failed": True,
            })
            return
        try:
            for raw_line in self.proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    logger.debug("Ignoring non-JSON worker stdout line: %s", line[:200])
                    continue
                msg_type = data.get("type")
                if msg_type == "status":
                    activity = data.get("activity") or {}
                    if isinstance(activity, dict):
                        self._latest_status = activity
                    continue
                if msg_type == "approval_request":
                    approval = data.get("approval") or {}
                    if isinstance(approval, dict):
                        with self._pending_approval_lock:
                            self._pending_approval = approval
                        if callable(self._approval_request_callback):
                            try:
                                self._approval_request_callback(dict(approval))
                            except Exception:
                                logger.debug("Detached approval callback failed", exc_info=True)
                    continue
                if msg_type == "result":
                    result = data.get("result") or {}
                    if isinstance(result, dict):
                        terminal_result = result
                        terminal_result.setdefault("failed", False)
                        terminal_result.setdefault("terminal_state", "completed")
                        terminal_result.setdefault("failure_kind", "")
                        self.model = result.get("model") or self.model
                        self.session_prompt_tokens = int(result.get("input_tokens") or 0)
                        self.session_completion_tokens = int(result.get("output_tokens") or 0)
                        self.context_compressor = SimpleNamespace(last_prompt_tokens=int(result.get("last_prompt_tokens") or 0))
                    else:
                        terminal_result = {
                            "final_response": "⚠️ Detached worker returned malformed result payload.",
                            "messages": [],
                            "api_calls": 0,
                            "tools": [],
                            "failed": True,
                            "terminal_state": "protocol_violation",
                            "failure_kind": "malformed_result_payload",
                        }
                    self._set_result_once(terminal_result)
                    return
                if msg_type == "error":
                    error_message = str(data.get("error") or "Detached worker failed.")
                    self._set_result_once({
                        "final_response": f"⚠️ {error_message}",
                        "messages": [],
                        "api_calls": 0,
                        "tools": [],
                        "failed": True,
                        "terminal_state": "failed",
                        "failure_kind": "worker_error",
                        "traceback": data.get("traceback"),
                    })
                    return
        except Exception:
            # A worker can leak non-UTF-8 or otherwise malformed bytes through
            # systemd-run --pipe.  The durable result.json/error.json artifact is
            # the source of truth; do not leave waiters blocked just because the
            # stdout protocol reader died.
            logger.warning(
                "Detached worker stdout reader failed; waiting for durable artifact recovery",
                exc_info=True,
            )

        if not self._result_event.is_set() and self.proc.poll() is not None:
            self._finalize_terminal_result()

    def _stderr_loop(self) -> None:
        if self.proc.stderr is None:
            return
        for raw_line in self.proc.stderr:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > 200:
                self._stderr_tail = self._stderr_tail[-200:]

    def _send_control(self, payload: Dict[str, Any]) -> bool:
        if self.proc.stdin is None or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
            self.proc.stdin.flush()
            return True
        except Exception:
            logger.debug("Failed sending detached worker control payload", exc_info=True)
            return False

    def interrupt(self, message: Optional[str] = None) -> bool:
        return self._send_control({"type": "interrupt", "message": message or ""})

    def terminate(self, reason: str = "cancelled") -> bool:
        """Stop the detached worker unit and unblock local waiters.

        Returns True only when the local pipe process exited or systemd reports
        the transient unit is no longer active. Callers can then distinguish a
        confirmed stop from a best-effort signal.
        """
        def _unit_stopped() -> bool:
            if not self.unit_name:
                return False
            try:
                from tools.detached_runtime import systemctl_show_properties
                props = systemctl_show_properties(self.unit_name, "ActiveState", "MainPID")
            except Exception:
                logger.debug("Failed checking detached worker unit state %s", self.unit_name, exc_info=True)
                return False
            active_state = str(props.get("ActiveState") or "").strip().lower()
            main_pid = str(props.get("MainPID") or "").strip()
            return active_state in {"inactive", "failed"} or main_pid == "0"

        def _wait_until_stopped(timeout: float) -> bool:
            deadline = time.time() + max(timeout, 0.0)
            while time.time() <= deadline:
                try:
                    if self.proc.poll() is not None:
                        return True
                except Exception:
                    pass
                if _unit_stopped():
                    return True
                time.sleep(0.1)
            return False

        if self.unit_name:
            try:
                from tools.detached_runtime import signal_unit
                signal_unit(self.unit_name, "TERM")
            except Exception:
                logger.debug("Failed sending TERM to detached worker unit %s", self.unit_name, exc_info=True)
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            logger.debug("Failed sending TERM to detached worker process", exc_info=True)

        stopped = _wait_until_stopped(2.0)
        if not stopped:
            if self.unit_name:
                try:
                    from tools.detached_runtime import signal_unit
                    signal_unit(self.unit_name, "KILL")
                except Exception:
                    logger.debug("Failed sending KILL to detached worker unit %s", self.unit_name, exc_info=True)
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                logger.debug("Failed killing detached worker process", exc_info=True)
            stopped = _wait_until_stopped(2.0)

        with self._pending_approval_lock:
            self._pending_approval = None
        if not self._result_event.is_set():
            self._set_result_once({
                "final_response": "⚠️ Detached worker was terminated before producing a result.",
                "messages": [],
                "api_calls": 0,
                "tools": [],
                "failed": True,
                "terminal_state": "cancelled",
                "failure_kind": reason or "cancelled",
                "worker_unit_name": self.unit_name,
                "worker_run_dir": str(self.runtime_dir) if self.runtime_dir is not None else None,
                "termination_confirmed": stopped,
            })
        return stopped

    def resolve_approval(self, choice: str) -> bool:
        with self._pending_approval_lock:
            approval = dict(self._pending_approval) if self._pending_approval else None
        if not approval:
            return False

        approval_id = approval.get("approval_id")
        sent = self._send_control(
            {
                "type": "approval_response",
                "approval_id": approval_id,
                "choice": choice,
            }
        )
        if not sent:
            return False

        with self._pending_approval_lock:
            current = self._pending_approval
            if not current:
                return True
            current_id = current.get("approval_id") if isinstance(current, dict) else None
            if (approval_id and current_id == approval_id) or (not approval_id and current == approval):
                self._pending_approval = None
        return True

    def has_pending_approval(self) -> bool:
        with self._pending_approval_lock:
            return bool(self._pending_approval)

    def get_pending_approval(self) -> Optional[Dict[str, Any]]:
        with self._pending_approval_lock:
            return dict(self._pending_approval) if self._pending_approval else None

    def get_activity_summary(self) -> Dict[str, Any]:
        return dict(self._latest_status or {})

    def wait_for_result_blocking(self) -> Dict[str, Any]:
        while not self._result_event.is_set():
            if self._recover_result_from_artifacts():
                break
            if self.proc.poll() is not None and not self._result_event.is_set():
                self._finalize_terminal_result()
                break
            time.sleep(0.1)
        return dict(self._result or {
            "final_response": "⚠️ Detached worker finished without a result.",
            "messages": [],
            "api_calls": 0,
            "tools": [],
            "failed": True,
        })

    async def wait_for_result(self) -> Dict[str, Any]:
        while not self._result_event.is_set():
            if self._recover_result_from_artifacts():
                break
            if self.proc.poll() is not None and not self._result_event.is_set():
                self._finalize_terminal_result()
                break
            await asyncio.sleep(0.1)
        return self.wait_for_result_blocking()


def start_gateway_conversation_worker(
    *,
    request: Dict[str, Any],
    approval_request_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> GatewayConversationWorkerHandle:
    from tools.detached_runtime import popen_transient_unit

    worker_run_id, runtime_dir = _ensure_worker_runtime_dir()
    proc, unit_name = popen_transient_unit(
        unit_prefix="hermes-gwconv",
        cwd=os.getcwd(),
        argv=[sys.executable, "-m", "gateway.worker_runtime", "--child"],
        extra_env={
            "HERMES_INTERACTIVE": "1",
            "HERMES_EXEC_ASK": "",
            "HERMES_GATEWAY_SESSION": "",
            "HERMES_SESSION_PLATFORM": os.getenv("HERMES_SESSION_PLATFORM", ""),
            "HERMES_SESSION_CHAT_ID": os.getenv("HERMES_SESSION_CHAT_ID", ""),
            "HERMES_SESSION_THREAD_ID": os.getenv("HERMES_SESSION_THREAD_ID", ""),
            "HERMES_CRON_AUTO_DELIVER_PLATFORM": os.getenv("HERMES_CRON_AUTO_DELIVER_PLATFORM", ""),
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID": os.getenv("HERMES_CRON_AUTO_DELIVER_CHAT_ID", ""),
            "HERMES_CRON_AUTO_DELIVER_THREAD_ID": os.getenv("HERMES_CRON_AUTO_DELIVER_THREAD_ID", ""),
            "HERMES_GWCONV_RUN_ID": worker_run_id,
            "HERMES_GWCONV_RUN_DIR": str(runtime_dir),
            "PYTHONPATH": os.getenv("PYTHONPATH", ""),
        },
    )
    request_payload = dict(request or {})
    request_payload["worker_run_id"] = worker_run_id
    request_payload["worker_run_dir"] = str(runtime_dir)
    request_payload["worker_unit_name"] = unit_name
    _write_json_artifact(
        runtime_dir / "meta.json",
        {
            "worker_run_id": worker_run_id,
            "worker_unit_name": unit_name,
            "created_at": time.time(),
        },
    )
    _write_json_artifact(runtime_dir / "request.json", _make_json_safe(request_payload))
    handle = GatewayConversationWorkerHandle(
        proc=proc,
        unit_name=unit_name,
        runtime_dir=runtime_dir,
        worker_run_id=worker_run_id,
        approval_request_callback=approval_request_callback,
    )
    if proc.stdin is None:
        raise RuntimeError("Detached gateway worker stdin is unavailable")
    safe_request = _make_json_safe(request_payload)
    proc.stdin.write(json.dumps(safe_request, ensure_ascii=False, default=_json_default) + "\n")
    proc.stdin.flush()
    return handle


def _approval_callback_factory(control_state: Dict[str, Any]) -> Callable[..., str]:
    def _callback(command: str, description: str, *, allow_permanent: bool = True) -> str:
        approval_id = uuid.uuid4().hex
        reply_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        control_state["approval_queues"][approval_id] = reply_queue
        approval_payload = {
            "approval_id": approval_id,
            "command": command,
            "description": description,
            "allow_permanent": bool(allow_permanent),
        }
        _emit_message({"type": "approval_request", "approval": approval_payload})
        try:
            return reply_queue.get(timeout=300)
        except queue.Empty:
            return "deny"
        finally:
            control_state["approval_queues"].pop(approval_id, None)
    return _callback


def _stdin_control_loop(control_state: Dict[str, Any]) -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        msg_type = payload.get("type")
        if msg_type == "interrupt":
            message = str(payload.get("message") or "Interrupted")
            control_state["last_interrupt"] = message
            agent = control_state.get("agent")
            if agent is not None:
                try:
                    agent.interrupt(message)
                except Exception:
                    logger.debug("Detached worker interrupt failed", exc_info=True)
            continue
        if msg_type == "approval_response":
            approval_id = str(payload.get("approval_id") or "")
            choice = str(payload.get("choice") or "deny")
            reply_queue = control_state["approval_queues"].get(approval_id)
            if reply_queue is not None:
                try:
                    reply_queue.put_nowait(choice)
                except queue.Full:
                    pass


def _status_loop(control_state: Dict[str, Any]) -> None:
    while not control_state["done"].is_set():
        agent = control_state.get("agent")
        if agent is not None and hasattr(agent, "get_activity_summary"):
            try:
                summary = agent.get_activity_summary() or {}
                _emit_message({"type": "status", "activity": summary})
            except Exception:
                logger.debug("Detached worker status emit failed", exc_info=True)
        time.sleep(1.0)


def _prepare_worker_credential_pool(request: Dict[str, Any], init_kwargs: Dict[str, Any]) -> tuple[Any, Optional[str], Any]:
    provider_key = str(request.get("credential_pool_provider") or "").strip()
    preferred_credential_id = str(request.get("preferred_credential_id") or "").strip() or None
    if not provider_key:
        return None, None, None
    try:
        from agent.credential_pool import load_pool
    except Exception:
        logger.debug("Detached worker could not import credential_pool loader", exc_info=True)
        return None, None, None
    try:
        pool = load_pool(provider_key)
    except Exception:
        logger.debug("Detached worker failed loading credential pool '%s'", provider_key, exc_info=True)
        return None, None, None
    if pool is None or not getattr(pool, "has_credentials", lambda: False)():
        return None, None, None
    lease_id = None
    entry = None
    try:
        if preferred_credential_id:
            lease_id = pool.acquire_lease(preferred_credential_id)
        else:
            lease_id = pool.acquire_lease()
        entry = pool.current() if hasattr(pool, "current") else None
        if entry is None and hasattr(pool, "peek"):
            entry = pool.peek()
    except Exception:
        logger.debug("Detached worker failed preparing credential pool lease", exc_info=True)
        lease_id = None
        entry = None
    init_kwargs["credential_pool"] = pool
    return pool, lease_id, entry


def _run_child() -> int:
    request_line = sys.stdin.readline()
    if not request_line:
        _emit_message({"type": "error", "error": "No worker request payload received."})
        return 1

    try:
        request = json.loads(request_line)
    except Exception as exc:
        _emit_message({"type": "error", "error": f"Invalid worker request payload: {exc}"})
        return 1

    worker_run_id = str(
        request.get("worker_run_id")
        or os.getenv("HERMES_GWCONV_RUN_ID", "")
        or uuid.uuid4().hex
    ).strip() or uuid.uuid4().hex
    raw_runtime_dir = str(request.get("worker_run_dir") or os.getenv("HERMES_GWCONV_RUN_DIR", "")).strip()
    runtime_dir = Path(raw_runtime_dir) if raw_runtime_dir else (_gwconv_runtime_root() / worker_run_id)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    worker_unit_name = str(request.get("worker_unit_name") or "").strip()
    _write_json_artifact(
        runtime_dir / "meta.json",
        {
            "worker_run_id": worker_run_id,
            "worker_unit_name": worker_unit_name,
            "pid": os.getpid(),
            "started_at": time.time(),
        },
    )

    control_state: Dict[str, Any] = {
        "agent": None,
        "approval_queues": {},
        "last_interrupt": "",
        "done": threading.Event(),
    }
    stdin_thread = threading.Thread(target=_stdin_control_loop, args=(control_state,), daemon=True)
    stdin_thread.start()
    status_thread = threading.Thread(target=_status_loop, args=(control_state,), daemon=True)
    status_thread.start()

    try:
        from hermes_state import SessionDB
        from run_agent import AIAgent
        from tools.terminal_tool import set_approval_callback

        runtime_kwargs = dict(request.get("runtime_kwargs") or {})
        if request.get("with_session_db"):
            runtime_kwargs["session_db"] = SessionDB()
        else:
            runtime_kwargs["session_db"] = None

        init_kwargs = {
            "model": request.get("model"),
            **runtime_kwargs,
            "max_iterations": int(request.get("max_iterations") or 90),
            "quiet_mode": True,
            "verbose_logging": False,
            "enabled_toolsets": list(request.get("enabled_toolsets") or []),
            "ephemeral_system_prompt": request.get("ephemeral_system_prompt") or None,
            "prefill_messages": request.get("prefill_messages") or None,
            "reasoning_config": request.get("reasoning_config"),
            "providers_allowed": request.get("providers_allowed"),
            "providers_ignored": request.get("providers_ignored"),
            "providers_order": request.get("providers_order"),
            "provider_sort": request.get("provider_sort"),
            "provider_require_parameters": bool(request.get("provider_require_parameters", False)),
            "provider_data_collection": request.get("provider_data_collection"),
            "session_id": request.get("session_id"),
            "platform": request.get("platform"),
            "user_id": request.get("user_id"),
            "fallback_model": request.get("fallback_model"),
        }

        worker_pool, worker_lease_id, worker_pool_entry = _prepare_worker_credential_pool(request, init_kwargs)

        approval_callback = _approval_callback_factory(control_state)
        set_approval_callback(approval_callback)
        try:
            agent = AIAgent(**init_kwargs)
            control_state["agent"] = agent
            if worker_pool_entry is not None and hasattr(agent, "_swap_credential"):
                try:
                    agent._swap_credential(worker_pool_entry)
                except Exception:
                    logger.debug("Detached worker failed binding preferred pool credential", exc_info=True)
            pending_interrupt = control_state.get("last_interrupt")
            if pending_interrupt:
                try:
                    agent.interrupt(str(pending_interrupt))
                except Exception:
                    logger.debug("Failed applying pending interrupt to detached agent", exc_info=True)
            result = agent.run_conversation(
                request.get("message", ""),
                conversation_history=request.get("conversation_history") or [],
                task_id=request.get("session_id"),
            )
        finally:
            set_approval_callback(None)
            if worker_pool is not None and worker_lease_id:
                try:
                    worker_pool.release_lease(worker_lease_id)
                except Exception:
                    logger.debug("Detached worker failed releasing credential pool lease", exc_info=True)

        result_payload = dict(result or {})
        result_payload.setdefault("tools", list(getattr(agent, "tools", []) or []))
        result_payload.setdefault("failed", False)
        result_payload.setdefault("terminal_state", "completed")
        result_payload["last_prompt_tokens"] = int(getattr(getattr(agent, "context_compressor", None), "last_prompt_tokens", 0) or 0)
        result_payload["input_tokens"] = int(getattr(agent, "session_prompt_tokens", 0) or 0)
        result_payload["output_tokens"] = int(getattr(agent, "session_completion_tokens", 0) or 0)
        result_payload["model"] = getattr(agent, "model", None)
        result_payload["worker_unit_name"] = worker_unit_name or None
        result_payload["worker_run_dir"] = str(runtime_dir)
        _write_json_artifact(runtime_dir / "result.json", result_payload)
        _emit_message({"type": "result", "result": result_payload})
        return 0
    except Exception as exc:
        error_payload = {
            "final_response": f"⚠️ Detached gateway worker failed: {exc}",
            "error": f"Detached gateway worker failed: {exc}",
            "traceback": traceback.format_exc(),
            "failed": True,
            "terminal_state": "failed",
            "failure_kind": "worker_error",
            "worker_unit_name": worker_unit_name or None,
            "worker_run_dir": str(runtime_dir),
        }
        _write_json_artifact(runtime_dir / "error.json", error_payload)
        _emit_message({
            "type": "error",
            "error": error_payload["error"],
            "traceback": error_payload["traceback"],
        })
        return 1
    finally:
        control_state["done"].set()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--child", action="store_true")
    args, _ = parser.parse_known_args(argv)
    if args.child:
        return _run_child()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
