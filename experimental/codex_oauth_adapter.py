from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import fcntl
except Exception:  # pragma: no cover - adapter runs on Linux, fallback for import-only platforms.
    fcntl = None

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from openai import OpenAI

from hermes_cli.auth import refresh_codex_oauth_pure, _codex_access_token_is_expiring
from agent.auxiliary_client import _CODEX_AUX_BASE_URL

AUTH_DIR = Path(os.getenv("CODEX_ADAPTER_AUTH_DIR", "/home/wutj/.cli-proxy-api"))
STATE_PATH = Path(os.getenv("CODEX_ADAPTER_STATE_PATH", "/home/wutj/.hermes/codex_oauth_adapter_state.json"))
AFFINITY_PATH = Path(os.getenv("CODEX_ADAPTER_AFFINITY_PATH", "/home/wutj/.hermes/codex_oauth_adapter_affinity.json"))
KEYS_PATH = Path(os.getenv("CODEX_ADAPTER_KEYS_PATH", "/home/wutj/.hermes/codex_oauth_adapter_keys.json"))
CONFIG_PATH = Path(os.getenv("CODEX_ADAPTER_CONFIG_PATH", "/home/wutj/.hermes/config.yaml"))
MODEL_PUBLIC = ["gpt-5.5", "gpt-5.5-mini"]
REFRESH_SKEW_SECONDS = 300
COOLDOWN_401_SECONDS = 10 * 60
COOLDOWN_429_SECONDS = 60 * 60
COOLDOWN_402_SECONDS = 6 * 60 * 60
COOLDOWN_5XX_SECONDS = 5 * 60
COOLDOWN_DEFAULT_SECONDS = 15 * 60
MAX_UPSTREAM_ATTEMPTS = int(os.getenv("CODEX_ADAPTER_MAX_ATTEMPTS", "3"))
UPSTREAM_TIMEOUT_SECONDS = float(os.getenv("CODEX_ADAPTER_UPSTREAM_TIMEOUT", "90"))
UPSTREAM_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=UPSTREAM_TIMEOUT_SECONDS, write=UPSTREAM_TIMEOUT_SECONDS, pool=30.0)
MAX_CONCURRENT_PER_ACCOUNT = max(1, int(os.getenv("CODEX_ADAPTER_MAX_CONCURRENT_PER_ACCOUNT", "2")))
ACCOUNT_LEASE_TTL_SECONDS = max(60, int(os.getenv("CODEX_ADAPTER_ACCOUNT_LEASE_TTL_SECONDS", "900")))
KEY_AUDIT_FIELDS = ("created_by", "purpose", "session_id", "task_id")
DEFAULT_RESPONSES_INSTRUCTIONS = "You are a helpful assistant."


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_responses_body(body: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(body)
    if not str(normalized.get("instructions") or "").strip():
        normalized["instructions"] = DEFAULT_RESPONSES_INSTRUCTIONS
    return normalized


@dataclass
class CodexAccount:
    path: Path
    email: str
    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    expired: str
    last_refresh: str
    disabled: bool
    type: str

    @classmethod
    def from_file(cls, path: Path) -> "CodexAccount":
        data = json.loads(path.read_text())
        return cls(
            path=path,
            email=str(data.get("email", "")),
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            id_token=str(data.get("id_token", "")),
            account_id=str(data.get("account_id", "")),
            expired=str(data.get("expired", "")),
            last_refresh=str(data.get("last_refresh", "")),
            disabled=bool(data.get("disabled", False)),
            type=str(data.get("type", "codex")),
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "account_id": self.account_id,
            "disabled": self.disabled,
            "email": self.email,
            "expired": self.expired,
            "id_token": self.id_token,
            "last_refresh": self.last_refresh,
            "refresh_token": self.refresh_token,
            "type": self.type,
        }

    def persist(self) -> None:
        self.path.write_text(json.dumps(self.to_json(), ensure_ascii=False))


class AccountPool:
    def __init__(self, auth_dir: Path, state_path: Path):
        self.auth_dir = auth_dir
        self.state_path = state_path
        self._lock = threading.Lock()
        self._index = 0
        self._refresh_locks: Dict[str, threading.Lock] = {}
        self._leases: Dict[str, int] = {}
        self._lease_ids: Dict[str, List[str]] = {}
        self._state: Dict[str, Dict[str, Any]] = self._load_state()

    def _load_state(self) -> Dict[str, Dict[str, Any]]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _persist_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2))

    @contextmanager
    def _state_file_guard(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_name(f"{self.state_path.name}.lock")
        with lock_path.open("a+") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _reload_state(self) -> None:
        self._state = self._load_state()

    def _entry_key(self, account: CodexAccount) -> str:
        return str(account.path)

    def _entry_state(self, account: CodexAccount) -> Dict[str, Any]:
        key = self._entry_key(account)
        if key not in self._state:
            self._state[key] = {
                "cooldown_until": 0.0,
                "last_error_code": None,
                "last_error_message": "",
                "failure_count": 0,
                "last_used_at": 0.0,
                "last_ok_at": 0.0,
                "last_refresh_attempt_at": 0.0,
            }
        return self._state[key]

    def _active_leases(self, account: CodexAccount, now: float) -> Dict[str, float]:
        state = self._entry_state(account)
        raw = state.get("active_leases")
        if not isinstance(raw, dict):
            raw = {}
            state["active_leases"] = raw

        ttl = float(ACCOUNT_LEASE_TTL_SECONDS)
        for lease_id, started_at in list(raw.items()):
            try:
                age = now - float(started_at)
            except (TypeError, ValueError):
                age = ttl + 1
            if age > ttl:
                raw.pop(lease_id, None)
        return raw

    def _active_lease_count(self, account: CodexAccount, now: float) -> int:
        return len(self._active_leases(account, now))

    def _record_lease(self, account: CodexAccount, now: float) -> None:
        key = self._entry_key(account)
        lease_id = f"{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(8)}"
        self._active_leases(account, now)[lease_id] = now
        self._lease_ids.setdefault(key, []).append(lease_id)
        self._leases[key] = self._leases.get(key, 0) + 1

    def _release_recorded_lease(self, account: CodexAccount, now: float) -> None:
        key = self._entry_key(account)
        local_ids = self._lease_ids.get(key) or []
        lease_id = local_ids.pop() if local_ids else None
        if local_ids:
            self._lease_ids[key] = local_ids
        else:
            self._lease_ids.pop(key, None)

        active = self._active_leases(account, now)
        if lease_id:
            active.pop(lease_id, None)

        current = self._leases.get(key, 0)
        if current <= 1:
            self._leases.pop(key, None)
        else:
            self._leases[key] = current - 1

    def _entry_refresh_lock(self, account: CodexAccount) -> threading.Lock:
        key = self._entry_key(account)
        if key not in self._refresh_locks:
            self._refresh_locks[key] = threading.Lock()
        return self._refresh_locks[key]

    @contextmanager
    def _refresh_guard(self, account: CodexAccount):
        lock = self._entry_refresh_lock(account)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def load_accounts(self) -> List[CodexAccount]:
        accounts: List[CodexAccount] = []
        for path in sorted(self.auth_dir.glob("codex-*.json")):
            try:
                account = CodexAccount.from_file(path)
                if not account.disabled and account.access_token and account.refresh_token:
                    accounts.append(account)
            except Exception:
                continue
        return accounts

    def _is_available(self, account: CodexAccount, *, now: Optional[float] = None) -> bool:
        state = self._entry_state(account)
        ts = now if now is not None else time.time()
        return ts >= float(state.get("cooldown_until") or 0.0)

    def health_summary(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            with self._state_file_guard():
                self._reload_state()
                accounts = self.load_accounts()
                available = sum(1 for account in accounts if self._is_available(account, now=now))
                in_use = sum(self._active_lease_count(account, now) for account in accounts)
                cooldown = len(accounts) - available
                self._persist_state()
        return {"total": len(accounts), "available": available, "cooldown": cooldown, "in_use": in_use}

    def next_account(self) -> CodexAccount:
        return self.acquire_account()

    def acquire_account(self, preferred_path: Optional[str] = None) -> CodexAccount:
        with self._lock:
            with self._state_file_guard():
                self._reload_state()
                accounts = self.load_accounts()
                if not accounts:
                    raise RuntimeError("No usable codex accounts found")

                now = time.time()
                available = [account for account in accounts if self._is_available(account, now=now)]
                active = available or accounts

                if preferred_path:
                    for account in active:
                        if (
                            str(account.path) == preferred_path
                            and self._active_lease_count(account, now) < MAX_CONCURRENT_PER_ACCOUNT
                        ):
                            self._record_lease(account, now)
                            self._entry_state(account)["last_used_at"] = now
                            self._persist_state()
                            return account

                under_limit = [
                    account
                    for account in active
                    if self._active_lease_count(account, now) < MAX_CONCURRENT_PER_ACCOUNT
                ]
                candidate_pool = under_limit or active
                candidate_pool = sorted(
                    candidate_pool,
                    key=lambda account: (
                        self._active_lease_count(account, now),
                        self._entry_state(account).get("last_used_at") or 0.0,
                    ),
                )

                account = candidate_pool[0]
                self._record_lease(account, now)
                self._entry_state(account)["last_used_at"] = now
                self._persist_state()
                return account

    def release_account(self, account: CodexAccount) -> None:
        with self._lock:
            with self._state_file_guard():
                self._reload_state()
                self._release_recorded_lease(account, time.time())
                self._persist_state()

    def ensure_fresh(self, account: CodexAccount) -> CodexAccount:
        if not _codex_access_token_is_expiring(account.access_token, REFRESH_SKEW_SECONDS):
            return account
        with self._refresh_guard(account):
            reloaded = CodexAccount.from_file(account.path)
            if not _codex_access_token_is_expiring(reloaded.access_token, REFRESH_SKEW_SECONDS):
                return reloaded
            with self._lock:
                with self._state_file_guard():
                    self._reload_state()
                    state = self._entry_state(reloaded)
                    state["last_refresh_attempt_at"] = time.time()
                    self._persist_state()
            refreshed = refresh_codex_oauth_pure(reloaded.access_token, reloaded.refresh_token)
            reloaded.access_token = refreshed["access_token"]
            reloaded.refresh_token = refreshed["refresh_token"]
            reloaded.last_refresh = str(refreshed.get("last_refresh", reloaded.last_refresh))
            reloaded.persist()
            return reloaded

    def mark_success(self, account: CodexAccount) -> None:
        with self._lock:
            with self._state_file_guard():
                self._reload_state()
                state = self._entry_state(account)
                state.update(
                    {
                        "cooldown_until": 0.0,
                        "last_error_code": None,
                        "last_error_message": "",
                        "failure_count": 0,
                        "last_ok_at": time.time(),
                    }
                )
                self._persist_state()

    def mark_failure(self, account: CodexAccount, status_code: Optional[int], message: str) -> None:
        with self._lock:
            with self._state_file_guard():
                self._reload_state()
                state = self._entry_state(account)
                state["last_error_code"] = status_code
                state["last_error_message"] = message[:500]
                state["failure_count"] = int(state.get("failure_count") or 0) + 1
                state["cooldown_until"] = time.time() + self._cooldown_seconds(status_code)
                self._persist_state()

    @staticmethod
    def _cooldown_seconds(status_code: Optional[int]) -> int:
        if status_code == 401:
            return COOLDOWN_401_SECONDS
        if status_code == 429:
            return COOLDOWN_429_SECONDS
        if status_code == 402:
            return COOLDOWN_402_SECONDS
        if status_code and status_code >= 500:
            return COOLDOWN_5XX_SECONDS
        return COOLDOWN_DEFAULT_SECONDS


pool = AccountPool(AUTH_DIR, STATE_PATH)
app = FastAPI(title="Codex OAuth Adapter", version="0.4.0")


class AffinityStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> Dict[str, Dict[str, str]]:
        if not self.path.exists():
            return {"prompt_cache_keys": {}, "response_ids": {}}
        try:
            data = json.loads(self.path.read_text())
        except Exception:
            return {"prompt_cache_keys": {}, "response_ids": {}}
        if not isinstance(data, dict):
            return {"prompt_cache_keys": {}, "response_ids": {}}
        return {
            "prompt_cache_keys": data.get("prompt_cache_keys") if isinstance(data.get("prompt_cache_keys"), dict) else {},
            "response_ids": data.get("response_ids") if isinstance(data.get("response_ids"), dict) else {},
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))

    def resolve_account_path(self, body: Dict[str, Any]) -> Optional[str]:
        previous_response_id = body.get("previous_response_id")
        if isinstance(previous_response_id, str) and previous_response_id.strip():
            account_path = self._data["response_ids"].get(previous_response_id.strip())
            if account_path:
                return account_path
        prompt_cache_key = body.get("prompt_cache_key")
        if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
            return self._data["prompt_cache_keys"].get(prompt_cache_key.strip())
        return None

    def remember(self, body: Dict[str, Any], account_path: str, response_id: Optional[str] = None) -> None:
        with self._lock:
            prompt_cache_key = body.get("prompt_cache_key")
            if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
                self._data["prompt_cache_keys"][prompt_cache_key.strip()] = account_path
            if isinstance(response_id, str) and response_id.strip():
                self._data["response_ids"][response_id.strip()] = account_path
            # Bound persistence so long-lived services don't grow forever.
            for key in ("prompt_cache_keys", "response_ids"):
                mapping = self._data[key]
                if len(mapping) > 2000:
                    items = list(mapping.items())[-1000:]
                    self._data[key] = dict(items)
            self._persist()


affinity_store = AffinityStore(AFFINITY_PATH)


class KeyStore:
    def __init__(self, path: Path, config_path: Path):
        self.path = path
        self.config_path = config_path
        self._lock = threading.Lock()
        self._data = self._load()

    def _seed_primary_key_from_config(self) -> str:
        try:
            lines = self.config_path.read_text().splitlines()
        except Exception:
            return ""
        in_provider = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- name:"):
                provider_name = stripped.split(":", 1)[1].strip().strip("'\"")
                in_provider = provider_name == "gpt-mainline-codex-local"
                continue
            if in_provider and stripped.startswith("api_key:"):
                return stripped.split(":", 1)[1].strip().strip("'\"")
        return ""

    def _bootstrap(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload.setdefault("admin_key", "")
        payload.setdefault("keys", {})
        if not payload["admin_key"]:
            payload["admin_key"] = f"sk-hermes-admin-{secrets.token_urlsafe(24)}"
        if "primary" not in payload["keys"]:
            seeded = self._seed_primary_key_from_config() or f"sk-hermes-codex-{secrets.token_urlsafe(24)}"
            payload["keys"]["primary"] = {
                "key": seeded,
                "label": "primary",
                "created_at": int(time.time()),
                "revoked": False,
            }
        return payload

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text())
            except Exception:
                payload = {}
        else:
            payload = {}
        payload = self._bootstrap(payload if isinstance(payload, dict) else {})
        self._persist(payload)
        return payload

    def _persist(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload or self._data, ensure_ascii=False, indent=2))

    def verify_inference_key(self, token: str) -> bool:
        now = int(time.time())
        with self._lock:
            for entry in self._data.get("keys", {}).values():
                if not isinstance(entry, dict) or entry.get("revoked"):
                    continue
                if entry.get("key") != token:
                    continue
                expires_at = _optional_int(entry.get("expires_at"))
                if expires_at is not None and expires_at <= now:
                    return False
                entry["last_used_at"] = now
                self._persist()
                return True
        return False

    def verify_admin_key(self, token: str) -> bool:
        return bool(token) and token == self._data.get("admin_key")

    def list_keys(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        now = int(time.time())
        for label, entry in self._data.get("keys", {}).items():
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "")
            expires_at = _optional_int(entry.get("expires_at"))
            item = {
                "label": label,
                "created_at": entry.get("created_at"),
                "last_used_at": entry.get("last_used_at"),
                "revoked": bool(entry.get("revoked")),
                "expires_at": expires_at,
                "expired": expires_at is not None and expires_at <= now,
                "key_preview": f"{key[:10]}...{key[-4:]}" if len(key) > 14 else key,
            }
            for field in KEY_AUDIT_FIELDS:
                item[field] = entry.get(field)
            results.append(item)
        return sorted(results, key=lambda item: item["label"])

    def create_key(self, label: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        normalized = (label or "").strip()
        if not normalized:
            raise ValueError("label is required")
        metadata = metadata or {}
        with self._lock:
            if normalized in self._data["keys"] and not self._data["keys"][normalized].get("revoked"):
                raise ValueError(f"key '{normalized}' already exists")
            key = f"sk-hermes-codex-{secrets.token_urlsafe(24)}"
            entry = {
                "key": key,
                "label": normalized,
                "created_at": int(time.time()),
                "revoked": False,
            }
            for field in KEY_AUDIT_FIELDS:
                if metadata.get(field) is not None:
                    entry[field] = str(metadata[field])
            expires_at = _optional_int(metadata.get("expires_at"))
            if expires_at is not None:
                entry["expires_at"] = expires_at
            self._data["keys"][normalized] = entry
            self._persist()
            return {"label": normalized, "api_key": key}

    def revoke_key(self, label: str) -> None:
        normalized = (label or "").strip()
        if not normalized or normalized not in self._data["keys"]:
            raise ValueError(f"key '{label}' not found")
        if normalized == "primary":
            raise ValueError("cannot revoke primary key")
        self._data["keys"][normalized]["revoked"] = True
        self._persist()

    def primary_key(self) -> str:
        return str((self._data.get("keys", {}).get("primary") or {}).get("key") or "")

    def admin_key(self) -> str:
        return str(self._data.get("admin_key") or "")


key_store = KeyStore(KEYS_PATH, CONFIG_PATH)


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    summary = pool.health_summary()
    return {"ok": True, **summary}


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_inference_auth(authorization)
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 1677610602, "owned_by": "codex-oauth-adapter"}
            for model in MODEL_PUBLIC
        ],
    }


def _normalize_output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    try:
        output = getattr(response, "output", None) or []
        parts: List[str] = []
        for item in output:
            content = getattr(item, "content", None) or []
            for c in content:
                chunk = getattr(c, "text", None)
                if chunk:
                    parts.append(chunk)
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _collect_stream_text(stream: Any) -> str:
    parts: List[str] = []
    final_response: Any = None
    try:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    parts.append(delta)
            elif etype == "response.completed":
                final_response = getattr(event, "response", None)
    finally:
        close_fn = getattr(stream, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass
    text = "".join(parts).strip()
    if text:
        return text
    if final_response is not None:
        return _normalize_output_text(final_response)
    return ""


def _usage_payload(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    prompt_tokens = getattr(usage, "input_tokens", None)
    if not isinstance(prompt_tokens, int):
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0

    completion_tokens = getattr(usage, "output_tokens", None)
    if not isinstance(completion_tokens, int):
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0

    total_tokens = getattr(usage, "total_tokens", None)
    if not isinstance(total_tokens, int):
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _messages_to_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for msg in messages:
        raw_role = str(msg.get("role", "user") or "user")
        role = raw_role if raw_role in {"system", "developer", "user", "assistant"} else "user"
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
            content = "\n".join(text_parts)
        text = str(content)
        if raw_role == "tool":
            tool_name = msg.get("tool_name") or "tool"
            tool_call_id = msg.get("tool_call_id") or ""
            prefix = f"[tool result name={tool_name}"
            if tool_call_id:
                prefix += f" call_id={tool_call_id}"
            prefix += "]\n"
            text = prefix + text
        elif raw_role == "function":
            text = "[function result]\n" + text
        converted.append({"role": role, "content": text})
    return converted


def _build_upstream_headers(account: CodexAccount) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {account.access_token}",
        "Content-Type": "application/json",
    }


def _extract_bearer_token(authorization: Optional[str]) -> str:
    value = str(authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _require_inference_auth(authorization: Optional[str]) -> None:
    token = _extract_bearer_token(authorization)
    if not key_store.verify_inference_key(token):
        raise HTTPException(status_code=401, detail="Invalid or missing inference API key")


def _require_admin_auth(authorization: Optional[str]) -> None:
    token = _extract_bearer_token(authorization)
    if not key_store.verify_admin_key(token):
        raise HTTPException(status_code=401, detail="Invalid or missing admin API key")


async def _collect_responses_payload(response: httpx.Response) -> Dict[str, Any]:
    completed_response: Optional[Dict[str, Any]] = None
    collected_output_items: List[Dict[str, Any]] = []
    collected_text_deltas: List[str] = []

    async for raw_line in response.aiter_lines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except Exception:
                continue
            event_type = payload.get("type")
            if event_type == "response.output_item.done" and isinstance(payload.get("item"), dict):
                collected_output_items.append(payload["item"])
            elif event_type == "response.output_text.delta":
                delta = payload.get("delta")
                if isinstance(delta, str) and delta:
                    collected_text_deltas.append(delta)
            if payload.get("type") == "response.completed" and isinstance(payload.get("response"), dict):
                completed_response = payload["response"]
            elif payload.get("type") in {"response.failed", "response.incomplete"} and isinstance(payload.get("response"), dict):
                completed_response = payload["response"]

    if isinstance(completed_response, dict):
        output = completed_response.get("output")
        if not isinstance(output, list) or not output:
            if collected_output_items:
                completed_response["output"] = collected_output_items
            elif collected_text_deltas:
                text = "".join(collected_text_deltas)
                completed_response["output"] = [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": text,
                                "annotations": [],
                                "logprobs": [],
                            }
                        ],
                    }
                ]
                completed_response["output_text"] = text
        elif collected_text_deltas and not completed_response.get("output_text"):
            completed_response["output_text"] = "".join(collected_text_deltas)
        return completed_response
    raise RuntimeError("upstream responses stream ended without response.completed")


def _get_affinitized_account(body: Dict[str, Any]) -> CodexAccount:
    account_path = affinity_store.resolve_account_path(body)
    account = pool.acquire_account(preferred_path=account_path)
    try:
        return pool.ensure_fresh(account)
    except Exception:
        pool.release_account(account)
        raise


def _upstream_status(exc: Exception) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _should_retry_upstream_error(status_code: Optional[int], message: str) -> bool:
    if status_code in {401, 402, 429}:
        return True
    if status_code is not None:
        return status_code >= 500

    lowered = (message or "").lower()
    retryable_markers = (
        "timed out",
        "timeout",
        "connection error",
        "connection reset",
        "all connection attempts failed",
        "empty text",
        "empty output",
    )
    return any(marker in lowered for marker in retryable_markers)


def _run_upstream_completion_sync(
    account: CodexAccount,
    messages: List[Dict[str, Any]],
    model: str,
    max_output_tokens: Optional[int],
) -> Dict[str, Any]:
    client = OpenAI(
        api_key=account.access_token,
        base_url=_CODEX_AUX_BASE_URL,
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    )
    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": "You are a helpful assistant.",
        "store": False,
        "stream": True,
        "input": _messages_to_input(messages),
    }

    response = client.responses.create(**kwargs)
    text = _collect_stream_text(response)
    if not text.strip():
        raise RuntimeError("upstream returned empty text")

    return {
        "id": "chatcmpl-codex-adapter",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _encode_sse_chunk(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def _stream_chat_completion_payload(payload: Dict[str, Any]):
    chunk_id = str(payload.get("id") or "chatcmpl-codex-adapter")
    created = int(payload.get("created") or time.time())
    model = str(payload.get("model") or "gpt-5.5")
    choices = payload.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    content = str(message.get("content") or "")
    usage = payload.get("usage")

    if content:
        yield _encode_sse_chunk(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content},
                        "finish_reason": None,
                    }
                ],
            }
        )

    yield _encode_sse_chunk(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )

    if isinstance(usage, dict):
        yield _encode_sse_chunk(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": usage,
            }
        )

    yield "data: [DONE]\n\n"


def _iter_upstream_stream_chunks(
    account: CodexAccount,
    messages: List[Dict[str, Any]],
    model: str,
    max_output_tokens: Optional[int],
):
    client = OpenAI(
        api_key=account.access_token,
        base_url=_CODEX_AUX_BASE_URL,
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    )
    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": "You are a helpful assistant.",
        "store": False,
        "stream": True,
        "input": _messages_to_input(messages),
    }

    stream = client.responses.create(**kwargs)
    chunk_id = "chatcmpl-codex-adapter"
    created = int(time.time())
    model_name = model
    final_response: Any = None
    first_text_delta = True
    saw_text_delta = False

    try:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", None)
                if isinstance(delta, str) and delta:
                    saw_text_delta = True
                    delta_payload = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                    if first_text_delta:
                        delta_payload["choices"][0]["delta"]["role"] = "assistant"
                        first_text_delta = False
                    yield _encode_sse_chunk(delta_payload)
            elif etype in {"response.completed", "response.incomplete", "response.failed"}:
                final_response = getattr(event, "response", None)
    finally:
        close_fn = getattr(stream, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    if not saw_text_delta:
        text = _normalize_output_text(final_response)
        if not text.strip():
            raise RuntimeError("upstream returned empty text")
        yield _encode_sse_chunk(
            {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": text},
                        "finish_reason": None,
                    }
                ],
            }
        )

    yield _encode_sse_chunk(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )

    yield _encode_sse_chunk(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [],
            "usage": _usage_payload(final_response),
        }
    )
    yield "data: [DONE]\n\n"


def _stream_chat_completion_with_retries(
    messages: List[Dict[str, Any]],
    model: str,
    max_output_tokens: Optional[int],
    attempts: int,
):
    last_error = "unknown upstream error"

    for _ in range(attempts):
        account = pool.acquire_account()
        emitted = False
        try:
            account = pool.ensure_fresh(account)
            for chunk in _iter_upstream_stream_chunks(account, messages, model, max_output_tokens):
                emitted = True
                yield chunk
            pool.mark_success(account)
            return
        except Exception as exc:
            last_status = _upstream_status(exc)
            last_error = str(exc)
            pool.mark_failure(account, last_status, last_error)
            if emitted or not _should_retry_upstream_error(last_status, last_error):
                raise
        finally:
            pool.release_account(account)

    raise RuntimeError(f"upstream error: {last_error}")


async def _responses_json_with_retries(body: Dict[str, Any]) -> Dict[str, Any]:
    attempts = max(1, min(pool.health_summary()["total"], MAX_UPSTREAM_ATTEMPTS))
    last_error = "unknown upstream error"

    for _ in range(attempts):
        account = _get_affinitized_account(body)
        try:
            async with httpx.AsyncClient(timeout=UPSTREAM_HTTP_TIMEOUT, follow_redirects=False) as client:
                upstream_body = dict(body)
                upstream_body["stream"] = True
                async with client.stream(
                    "POST",
                    f"{_CODEX_AUX_BASE_URL}/responses",
                    headers=_build_upstream_headers(account),
                    json=upstream_body,
                ) as response:
                    if response.status_code != 200:
                        error_text = (await response.aread()).decode("utf-8", errors="replace")[:500]
                        pool.mark_failure(account, response.status_code, error_text)
                        last_error = error_text
                        if not _should_retry_upstream_error(response.status_code, error_text):
                            break
                        continue
                    payload = await _collect_responses_payload(response)
                pool.mark_success(account)
                affinity_store.remember(body, str(account.path), payload.get("id"))
                return payload
        except Exception as exc:
            status = _upstream_status(exc)
            last_error = str(exc)
            pool.mark_failure(account, status, last_error)
            if not _should_retry_upstream_error(status, last_error):
                break
        finally:
            pool.release_account(account)

    raise RuntimeError(f"upstream error: {last_error}")


async def _responses_stream_with_retries(body: Dict[str, Any]):
    attempts = max(1, min(pool.health_summary()["total"], MAX_UPSTREAM_ATTEMPTS))
    last_error = "unknown upstream error"

    for _ in range(attempts):
        account = _get_affinitized_account(body)
        try:
            async with httpx.AsyncClient(timeout=UPSTREAM_HTTP_TIMEOUT, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    f"{_CODEX_AUX_BASE_URL}/responses",
                    headers=_build_upstream_headers(account),
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        error_text = (await response.aread()).decode("utf-8", errors="replace")[:500]
                        pool.mark_failure(account, response.status_code, error_text)
                        last_error = error_text
                        if not _should_retry_upstream_error(response.status_code, error_text):
                            raise RuntimeError(error_text)
                        continue

                    affinity_store.remember(body, str(account.path))
                    async for chunk in response.aiter_bytes():
                        yield chunk
                    pool.mark_success(account)
                    return
        except RuntimeError:
            raise
        except Exception as exc:
            status = _upstream_status(exc)
            last_error = str(exc)
            pool.mark_failure(account, status, last_error)
            if not _should_retry_upstream_error(status, last_error):
                raise RuntimeError(last_error) from exc
        finally:
            pool.release_account(account)

    raise RuntimeError(f"upstream error: {last_error}")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: Optional[str] = Header(default=None)):
    _require_inference_auth(authorization)
    body = await request.json()
    messages = body.get("messages")
    model = str(body.get("model", "gpt-5.5"))
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages is required")
    if model not in MODEL_PUBLIC:
        raise HTTPException(status_code=400, detail=f"unsupported model: {model}")

    stream = bool(body.get("stream"))
    max_output_tokens = body.get("max_tokens")
    if not isinstance(max_output_tokens, int):
        max_output_tokens = None

    attempts = max(1, min(pool.health_summary()["total"], MAX_UPSTREAM_ATTEMPTS))
    last_error = "unknown upstream error"

    if stream:
        try:
            return StreamingResponse(
                _stream_chat_completion_with_retries(messages, model, max_output_tokens, attempts),
                media_type="text/event-stream",
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    for _ in range(attempts):
        account = None
        try:
            account = pool.acquire_account()
            account = pool.ensure_fresh(account)
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"no usable account: {exc}")

        try:
            payload = await asyncio.to_thread(
                _run_upstream_completion_sync,
                account,
                messages,
                model,
                max_output_tokens,
            )
            pool.mark_success(account)
            return JSONResponse(payload)
        except Exception as exc:
            last_status = _upstream_status(exc)
            last_error = str(exc)
            pool.mark_failure(account, last_status, last_error)
            if not _should_retry_upstream_error(last_status, last_error):
                break
        finally:
            if account is not None:
                pool.release_account(account)

    raise HTTPException(status_code=502, detail=f"upstream error: {last_error}")


@app.post("/v1/responses")
async def responses_api(request: Request, authorization: Optional[str] = Header(default=None)):
    _require_inference_auth(authorization)
    body = _normalize_responses_body(await request.json())
    model = str(body.get("model", "gpt-5.5"))
    if model not in MODEL_PUBLIC:
        raise HTTPException(status_code=400, detail=f"unsupported model: {model}")

    if body.get("stream") is True:
        return StreamingResponse(
            _responses_stream_with_retries(body),
            media_type="text/event-stream",
        )

    try:
        payload = await _responses_json_with_retries(body)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(payload)


@app.get("/v1/internal/keys")
def list_internal_keys(authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_admin_auth(authorization)
    admin_key = key_store.admin_key()
    return {
        "admin_key_preview": f"{admin_key[:10]}...{admin_key[-4:]}",
        "keys": key_store.list_keys(),
    }


@app.post("/v1/internal/keys")
async def create_internal_key(request: Request, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_admin_auth(authorization)
    body = await request.json()
    metadata = {field: body.get(field) for field in KEY_AUDIT_FIELDS if body.get(field) is not None}
    if body.get("expires_at") is not None:
        metadata["expires_at"] = body.get("expires_at")
    try:
        return key_store.create_key(str(body.get("label") or ""), metadata=metadata)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/v1/internal/keys/{label}")
def revoke_internal_key(label: str, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_admin_auth(authorization)
    try:
        key_store.revoke_key(label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "label": label}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=4311)
