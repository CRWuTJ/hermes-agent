"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        — OpenAI Chat Completions format (stateless; opt-in session continuity via X-Hermes-Session-Id header)
- POST /v1/responses               — OpenAI Responses API format (stateful via previous_response_id)
- GET  /v1/responses/{response_id} — Retrieve a stored response
- DELETE /v1/responses/{response_id} — Delete a stored response
- GET  /v1/models                  — lists hermes-agent as an available model
- POST /v1/runs                    — start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}/events    — SSE stream of structured lifecycle events
- GET  /health                     — health check
- GET  /api/status                 — machine-readable gateway/runtime status
- GET  /openapi.json               — OpenAPI contract for Hermes control-plane endpoints

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1.

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)
from gateway.task_control import (
    build_gateway_tasks_payload,
    build_task_action_error_payload as shared_build_task_action_error_payload,
    build_task_action_payload as shared_build_task_action_payload,
    build_task_detail_payload,
    normalize_priority_bucket,
    queued_task_actions as shared_queued_task_actions,
    queued_task_iso as shared_queued_task_iso,
    queued_task_payload as shared_queued_task_payload,
    queued_task_recovery_plan,
    record_harness_task_action,
    queued_task_source_label as shared_queued_task_source_label,
)

logger = logging.getLogger(__name__)

# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 1_000_000  # 1 MB default limit for POST bodies


def check_api_server_requirements() -> bool:
    """Check if API server dependencies are available."""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts.  Falls back to in-memory SQLite
    if the on-disk path is unavailable.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                name TEXT PRIMARY KEY,
                response_id TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU)."""
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        import time
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        return json.loads(row[0])

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity."""
        import time
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # Evict oldest entries beyond max_size
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            self._conn.execute(
                "DELETE FROM responses WHERE response_id IN "
                "(SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?)",
                (count - self._max_size,),
            )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted."""
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str) -> Optional[str]:
        """Get the latest response_id for a conversation name."""
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str) -> None:
        """Map a conversation name to its latest response_id."""
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (name, response_id) VALUES (?, ?)",
            (name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight."""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI-style error envelope."""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length."""
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        return await handler(request)
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors)."""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics."""
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        import time as _t
        now = _t.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]
        resp = await compute_coro()
        import time as _t
        self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
        self._purge()
        return resp


_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through hermes-agent's AIAgent.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        self._port: int = int(extra.get("port", os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))))
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        # Active run streams: run_id -> asyncio.Queue of SSE event dicts
        self._run_streams: Dict[str, "asyncio.Queue[Optional[Dict]]"] = {}
        # Creation timestamps for orphaned-run TTL sweep
        self._run_streams_created: Dict[str, float] = {}
        self._session_db: Optional[Any] = None  # Lazy-init SessionDB for session continuity
        self.gateway_runner = None

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple."""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin."""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        If no API key is configured, all requests are allowed.
        """
        if not self._api_key:
            return None  # No key configured — allow all (local-only use)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, self._api_key):
                return None  # Auth OK

        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    # ------------------------------------------------------------------
    # Session DB helper
    # ------------------------------------------------------------------

    def _ensure_session_db(self):
        """Lazily initialise and return the shared SessionDB instance.

        Sessions are persisted to ``state.db`` so that ``hermes sessions list``
        shows API-server conversations alongside CLI and gateway ones.
        """
        if self._session_db is None:
            try:
                from hermes_state import SessionDB
                self._session_db = SessionDB()
            except Exception as e:
                logger.debug("SessionDB unavailable for API server: %s", e)
        return self._session_db

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the hermes-api-server default.
        """
        from run_agent import AIAgent
        from gateway.run import _resolve_runtime_agent_kwargs, _resolve_gateway_model, _load_gateway_config
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        model = _resolve_gateway_model()

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server", include_default_mcp_servers=False))

        max_iterations = int(os.getenv("HERMES_MAX_ITERATIONS", "90"))

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
        from gateway.run import GatewayRunner
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "hermes-agent"})

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models — return hermes-agent as an available model."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": "hermes-agent",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "hermes",
                    "permission": [],
                    "root": "hermes-agent",
                    "parent": None,
                }
            ],
        })

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions — OpenAI Chat Completions format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = body.get("stream", False)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system":
                # Accumulate system messages
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in ("user", "assistant"):
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not user_message:
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # Allow caller to continue an existing session by passing X-Hermes-Session-Id.
        # When provided, history is loaded from state.db instead of from the request body.
        provided_session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
        if provided_session_id:
            session_id = provided_session_id
            try:
                db = self._ensure_session_db()
                if db is not None:
                    history = db.get_messages_as_conversation(session_id)
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            session_id = str(uuid.uuid4())
            # history already set from request body above

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", "hermes-agent")
        created = int(time.time())

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # Filter out None — the agent fires stream_delta_callback(None)
                # to signal the CLI display to close its response box before
                # tool execution, but the SSE writer uses None as end-of-stream
                # sentinel.  Forwarding it would prematurely close the HTTP
                # response, causing Open WebUI (and similar frontends) to miss
                # the final answer after tool calls.  The SSE loop detects
                # completion via agent_task.done() instead.
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Inject tool progress into the SSE stream for Open WebUI."""
                if event_type != "tool.started":
                    return  # Only show tool start events in chat stream
                if name.startswith("_"):
                    return  # Skip internal events (_thinking)
                from agent.display import get_tool_emoji
                emoji = get_tool_emoji(name)
                label = preview or name
                _stream_q.put(f"\n`{emoji} {label}`\n")

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                agent_ref=agent_ref,
            ))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
            )

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(body, keys=["model", "messages", "tools", "tool_choice", "stream"])
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_completion)
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_completion()
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        return web.json_response(response_data, headers={"X-Hermes-Session-Id": session_id})

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted via ``agent.interrupt()`` so it stops making
        LLM API calls, and the asyncio task wrapper is cancelled.
        """
        import queue as _q

        sse_headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())

            # Stream content chunks as they arrive from the agent
            loop = asyncio.get_event_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                content_chunk = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created, "model": model,
                                    "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                                }
                                await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                            except _q.Empty:
                                break
                        break
                    continue

                if delta is None:  # End of stream sentinel
                    break

                content_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                }
                await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())

            # Get usage from completed agent
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception:
                pass

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected mid-stream.  Interrupt the agent so it
            # stops making LLM API calls at the next loop iteration, then
            # cancel the asyncio task wrapper.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)

        return response

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses — OpenAI Responses API format."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = body.get("store", True)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = self._response_store.get_conversation(conversation)
            # No error if conversation doesn't exist yet — it's a new conversation

        # Normalize input to message list
        input_messages: List[Dict[str, str]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for item in raw_input:
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    content = item.get("content", "")
                    # Handle content that may be a list of content parts
                    if isinstance(content, list):
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "input_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, dict) and part.get("type") == "output_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        content = "\n".join(text_parts)
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # Accept explicit conversation_history from the request body.
        # This lets stateless clients supply their own history instead of
        # relying on server-side response chaining via previous_response_id.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            conversation_history = list(stored.get("conversation_history", []))
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        # Append new input messages to history (all but the last become history)
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # Last input message is the user_message
        user_message = input_messages[-1].get("content", "") if input_messages else ""
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto" and len(conversation_history) > 100:
            conversation_history = conversation_history[-100:]

        # Run the agent (with Idempotency-Key support)
        session_id = str(uuid.uuid4())

        async def _compute_response():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(
                body,
                keys=["input", "instructions", "previous_response_id", "conversation", "model", "tools"],
            )
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_response)
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_response()
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = list(conversation_history)
        full_history.append({"role": "user", "content": user_message})
        # Add agent's internal messages if available
        agent_messages = result.get("messages", [])
        if agent_messages:
            full_history.extend(agent_messages)
        else:
            full_history.append({"role": "assistant", "content": final_response})

        # Build output items (includes tool calls + final message)
        output_items = self._extract_output_items(result)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", "hermes-agent"),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
            })
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        return web.json_response(response_data)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} — retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} — delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        deleted = self._response_store.delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    # ------------------------------------------------------------------
    # Cron jobs API
    # ------------------------------------------------------------------

    # Check cron module availability once (not per-request)
    _CRON_AVAILABLE = False
    try:
        from cron.jobs import (
            list_jobs as _cron_list,
            get_due_jobs as _cron_get_due,
            get_job as _cron_get,
            create_job as _cron_create,
            update_job as _cron_update,
            remove_job as _cron_remove,
            pause_job as _cron_pause,
            resume_job as _cron_resume,
            trigger_job as _cron_trigger,
            job_with_lane_metadata as _cron_present_job,
            summarize_job_lanes as _cron_summarize_lanes,
            _normalize_lane as _cron_normalize_lane,
            LANE_ORDER as _cron_lane_order,
            LANE_DISPLAY_NAMES as _cron_lane_display_names,
        )
        # Wrap as staticmethod to prevent descriptor binding — these are plain
        # module functions, not instance methods.  Without this, self._cron_*()
        # injects ``self`` as the first positional argument and every call
        # raises TypeError.
        _cron_list = staticmethod(_cron_list)
        _cron_get_due = staticmethod(_cron_get_due)
        _cron_get = staticmethod(_cron_get)
        _cron_create = staticmethod(_cron_create)
        _cron_update = staticmethod(_cron_update)
        _cron_remove = staticmethod(_cron_remove)
        _cron_pause = staticmethod(_cron_pause)
        _cron_resume = staticmethod(_cron_resume)
        _cron_trigger = staticmethod(_cron_trigger)
        _cron_present_job = staticmethod(_cron_present_job)
        _cron_summarize_lanes = staticmethod(_cron_summarize_lanes)
        _cron_normalize_lane = staticmethod(_cron_normalize_lane)
        _CRON_LANE_ORDER = tuple(_cron_lane_order)
        _CRON_LANE_DISPLAY_NAMES = dict(_cron_lane_display_names)
        _CRON_AVAILABLE = True
    except ImportError:
        pass

    _CRON_LANE_ORDER = ("interactive", "cron_scout", "housekeeping")
    _CRON_LANE_DISPLAY_NAMES = {
        "interactive": "interactive",
        "cron_scout": "cron/scout",
        "housekeeping": "housekeeping",
    }
    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update — prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled", "lane"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    def _check_jobs_available(self) -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not self._CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """Validate and extract job_id. Returns (job_id, error_response)."""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    def _job_payload(self, job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return an API-friendly job payload with shared lane metadata."""
        if not job:
            return job
        return self._cron_present_job(job)

    def _normalize_job_lane_or_error(self, lane: Any) -> tuple[Optional[str], Optional["web.Response"]]:
        """Normalize lane input once so create/update share the same API behavior."""
        try:
            return self._cron_normalize_lane(lane), None
        except ValueError as exc:
            return None, web.json_response({"error": str(exc)}, status=400)

    def _lane_capabilities_payload(self) -> Dict[str, Any]:
        """Expose lane enums/defaults so API clients don't have to hardcode them."""
        return {
            "lane_values": list(self._CRON_LANE_ORDER),
            "lane_labels": dict(self._CRON_LANE_DISPLAY_NAMES),
            "default_lane_by_deliver": {
                "local": "cron_scout",
                "non_local": "interactive",
            },
            "clear_lane_values": [None, ""],
        }

    def _openapi_contract(self) -> Dict[str, Any]:
        """Return an OpenAPI document for the Hermes control-plane endpoints."""
        lane_values = list(self._CRON_LANE_ORDER)
        lane_labels = dict(self._CRON_LANE_DISPLAY_NAMES)

        def _nullable(schema: Dict[str, Any]) -> Dict[str, Any]:
            return {"anyOf": [schema, {"type": "null"}]}

        def _nullable_ref(name: str) -> Dict[str, Any]:
            return {"anyOf": [{"$ref": f"#/components/schemas/{name}"}, {"type": "null"}]}

        def _nullable_string(*, fmt: Optional[str] = None) -> Dict[str, Any]:
            schema: Dict[str, Any] = {"type": "string"}
            if fmt:
                schema["format"] = fmt
            return _nullable(schema)

        schemas: Dict[str, Any] = {
            "CronLaneValue": {
                "type": "string",
                "enum": lane_values,
                "description": "Canonical scheduler lane used by Hermes cron routing.",
            },
            "CronLaneCounts": {
                "type": "object",
                "properties": {lane: {"type": "integer", "minimum": 0} for lane in lane_values},
                "required": lane_values,
                "additionalProperties": False,
            },
            "CronJobOrigin": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "chat_name": _nullable_string(),
                    "thread_id": _nullable_string(),
                },
                "additionalProperties": True,
            },
            "CronRepeatState": {
                "type": "object",
                "properties": {
                    "times": _nullable({"type": "integer", "minimum": 1}),
                    "completed": {"type": "integer", "minimum": 0},
                },
                "required": ["times", "completed"],
                "additionalProperties": False,
            },
            "CronSchedule": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "minutes": {"type": "integer", "minimum": 1},
                    "expr": {"type": "string"},
                    "run_at": {"type": "string", "format": "date-time"},
                    "display": {"type": "string"},
                },
                "required": ["kind"],
                "additionalProperties": True,
            },
            "CronJob": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "[a-f0-9]{12}"},
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "skill": _nullable_string(),
                    "model": _nullable_string(),
                    "provider": _nullable_string(),
                    "base_url": _nullable_string(),
                    "script": _nullable_string(),
                    "schedule": {"$ref": "#/components/schemas/CronSchedule"},
                    "schedule_display": {"type": "string"},
                    "repeat": {"$ref": "#/components/schemas/CronRepeatState"},
                    "enabled": {"type": "boolean"},
                    "state": {"type": "string"},
                    "paused_at": _nullable_string(fmt="date-time"),
                    "paused_reason": _nullable_string(),
                    "created_at": {"type": "string", "format": "date-time"},
                    "next_run_at": _nullable_string(fmt="date-time"),
                    "last_run_at": _nullable_string(fmt="date-time"),
                    "last_status": _nullable_string(),
                    "last_error": _nullable_string(),
                    "last_delivery_error": _nullable_string(),
                    "deliver": {"type": "string"},
                    "origin": _nullable_ref("CronJobOrigin"),
                    "lane": _nullable_ref("CronLaneValue"),
                    "effective_lane": {"$ref": "#/components/schemas/CronLaneValue"},
                    "lane_source": {
                        "type": "string",
                        "enum": ["explicit", "default"],
                    },
                },
                "required": ["id", "name", "deliver", "lane", "effective_lane", "lane_source"],
                "additionalProperties": True,
            },
            "CronJobEnvelope": {
                "type": "object",
                "properties": {
                    "job": {"$ref": "#/components/schemas/CronJob"},
                },
                "required": ["job"],
                "additionalProperties": False,
            },
            "CronJobSummary": {
                "type": "object",
                "properties": {
                    "active_jobs": {"type": "integer", "minimum": 0},
                    "total_jobs": {"type": "integer", "minimum": 0},
                    "lane_counts": {"$ref": "#/components/schemas/CronLaneCounts"},
                    "due_now": {"$ref": "#/components/schemas/CronLaneCounts"},
                },
                "required": ["active_jobs", "total_jobs", "lane_counts", "due_now"],
                "additionalProperties": False,
            },
            "CronJobLaneCapabilities": {
                "type": "object",
                "properties": {
                    "lane_values": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/CronLaneValue"},
                    },
                    "lane_labels": {
                        "type": "object",
                        "properties": {lane: {"type": "string", "enum": [label]} for lane, label in lane_labels.items()},
                        "required": lane_values,
                        "additionalProperties": False,
                    },
                    "default_lane_by_deliver": {
                        "type": "object",
                        "properties": {
                            "local": {"$ref": "#/components/schemas/CronLaneValue"},
                            "non_local": {"$ref": "#/components/schemas/CronLaneValue"},
                        },
                        "required": ["local", "non_local"],
                        "additionalProperties": False,
                    },
                    "clear_lane_values": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                    },
                },
                "required": ["lane_values", "lane_labels", "default_lane_by_deliver", "clear_lane_values"],
                "additionalProperties": False,
            },
            "CronJobListResponse": {
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/CronJob"},
                    },
                    "summary": {"$ref": "#/components/schemas/CronJobSummary"},
                    "capabilities": {"$ref": "#/components/schemas/CronJobLaneCapabilities"},
                },
                "required": ["jobs", "summary", "capabilities"],
                "additionalProperties": False,
            },
            "OpenAIErrorDetail": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "type": {"type": "string"},
                    "param": _nullable_string(),
                    "code": _nullable_string(),
                },
                "required": ["message", "type"],
                "additionalProperties": False,
            },
            "ControlPlaneAuthErrorResponse": {
                "type": "object",
                "properties": {
                    "error": {"$ref": "#/components/schemas/OpenAIErrorDetail"},
                },
                "required": ["error"],
                "additionalProperties": False,
            },
            "ControlPlaneErrorResponse": {
                "type": "object",
                "properties": {
                    "error": {"type": "string"},
                },
                "required": ["error"],
                "additionalProperties": False,
            },
            "TaskActionErrorResponse": {
                "type": "object",
                "properties": {
                    "error": {"type": "string"},
                    "reason_code": {
                        "type": "string",
                        "enum": [
                            "task_not_found",
                            "task_already_active",
                            "foreground_not_supported",
                            "cancel_not_supported",
                            "cancel_handle_missing",
                            "reprioritize_not_supported",
                            "reprioritize_requires_queued_task",
                            "recover_requires_queued_task",
                            "recover_not_recommended",
                            "invalid_priority_bucket",
                            "invalid_request_body",
                        ],
                    },
                },
                "required": ["error", "reason_code"],
                "additionalProperties": False,
            },
            "CreateCronJobRequest": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "schedule": {"type": "string"},
                    "prompt": {"type": "string"},
                    "deliver": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "repeat": {"type": "integer", "minimum": 1},
                    "lane": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/CronLaneValue"},
                            {"type": "string", "enum": [""]},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["name", "schedule"],
                "additionalProperties": False,
            },
            "UpdateCronJobRequest": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "schedule": {"type": "string"},
                    "prompt": {"type": "string"},
                    "deliver": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "skill": {"type": "string"},
                    "repeat": {"type": "integer", "minimum": 1},
                    "enabled": {"type": "boolean"},
                    "lane": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/CronLaneValue"},
                            {"type": "string", "enum": [""]},
                            {"type": "null"},
                        ]
                    },
                },
                "additionalProperties": False,
            },
            "DeleteCronJobResponse": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"},
                },
                "required": ["ok"],
                "additionalProperties": False,
            },
            "GatewayPlatformStatus": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "error_code": _nullable_string(),
                    "error_message": _nullable_string(),
                    "updated_at": _nullable_string(fmt="date-time"),
                },
                "additionalProperties": True,
            },
            "LiveTask": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "lane": {"$ref": "#/components/schemas/CronLaneValue"},
                    "label": {"type": "string"},
                    "kind": {"type": "string"},
                    "control_mode": {
                        "type": "string",
                        "enum": ["managed_runtime", "read_only"],
                    },
                    "actions": {"type": "array", "items": {"type": "string"}},
                    "source": {"type": "string"},
                    "started_at": _nullable_string(fmt="date-time"),
                    "running_seconds": {"type": "integer", "minimum": 0},
                    "running_age": {"type": "string"},
                },
                "required": ["task_id", "lane", "kind", "control_mode", "actions"],
                "additionalProperties": True,
            },
            "LiveTaskStatus": {
                "type": "object",
                "properties": {
                    "active_count": {"type": "integer", "minimum": 0},
                    "lane_counts": {"$ref": "#/components/schemas/CronLaneCounts"},
                    "oldest_running": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/LiveTask"},
                            {"type": "null"},
                        ]
                    },
                    "tasks": {"type": "array", "items": {"$ref": "#/components/schemas/LiveTask"}},
                },
                "required": ["active_count", "lane_counts", "tasks"],
                "additionalProperties": False,
            },
            "QueuedTask": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "state": {"type": "string", "enum": ["queued"]},
                    "session_key": {"type": "string"},
                    "lane": {"$ref": "#/components/schemas/CronLaneValue"},
                    "priority": {"type": "integer"},
                    "priority_bucket": {"type": "string", "enum": ["now", "next", "later"]},
                    "priority_bucket_options": {"type": "array", "items": {"type": "string", "enum": ["now", "next", "later"]}},
                    "reply_policy": {"type": "string"},
                    "cancellation_policy": {"type": "string"},
                    "queued_at": _nullable_string(fmt="date-time"),
                    "wait_seconds": {"type": "integer", "minimum": 0},
                    "wait_age": {"type": "string"},
                    "reason": {"type": "string"},
                    "preview": _nullable_string(),
                    "kind": {"type": "string"},
                    "control_mode": {"type": "string", "enum": ["queued"]},
                    "actions": {"type": "array", "items": {"type": "string", "enum": ["foreground", "reprioritize", "cancel"]}},
                    "source": {"type": "string"},
                    "starvation_alert": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/QueuedStarvationAlert"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": [
                    "task_id",
                    "state",
                    "session_key",
                    "lane",
                    "priority",
                    "reply_policy",
                    "cancellation_policy",
                    "queued_at",
                    "wait_seconds",
                    "wait_age",
                    "reason",
                    "kind",
                    "control_mode",
                    "actions",
                    "priority_bucket_options",
                    "source",
                ],
                "additionalProperties": False,
            },
            "QueuedBucketStarvation": {
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "enum": ["now", "next", "later"]},
                    "queued_count": {"type": "integer", "minimum": 0},
                    "oldest_wait_seconds": {
                        "anyOf": [
                            {"type": "integer", "minimum": 0},
                            {"type": "null"},
                        ]
                    },
                    "oldest_wait_age": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                    "oldest_task_id": {"type": "string"},
                },
                "required": ["bucket", "queued_count", "oldest_wait_seconds", "oldest_wait_age", "oldest_task_id"],
                "additionalProperties": False,
            },
            "QueuedStarvationAlert": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["warning"]},
                    "reason_code": {"type": "string", "enum": ["bucket_wait_threshold_exceeded"]},
                    "reason": {"type": "string"},
                    "bucket": {"type": "string", "enum": ["now", "next", "later"]},
                    "threshold_seconds": {"type": "integer", "minimum": 0},
                    "threshold_age": {"type": "string"},
                    "current_wait_seconds": {
                        "anyOf": [
                            {"type": "integer", "minimum": 0},
                            {"type": "null"},
                        ]
                    },
                    "current_wait_age": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                    "oldest_task_id": {"type": "string"},
                    "suggested_action": {"type": "string", "enum": ["foreground", "reprioritize"]},
                    "suggested_bucket": {
                        "anyOf": [
                            {"type": "string", "enum": ["now", "next", "later"]},
                            {"type": "null"},
                        ]
                    },
                    "suggested_command": {"type": "string"},
                },
                "required": [
                    "level",
                    "reason_code",
                    "reason",
                    "bucket",
                    "threshold_seconds",
                    "threshold_age",
                    "current_wait_seconds",
                    "current_wait_age",
                    "oldest_task_id",
                    "suggested_action",
                    "suggested_bucket",
                    "suggested_command",
                ],
                "additionalProperties": False,
            },
            "QueuedTaskStatus": {
                "type": "object",
                "properties": {
                    "queued_count": {"type": "integer", "minimum": 0},
                    "lane_counts": {"$ref": "#/components/schemas/CronLaneCounts"},
                    "bucket_counts": {
                        "type": "object",
                        "properties": {
                            "now": {"type": "integer", "minimum": 0},
                            "next": {"type": "integer", "minimum": 0},
                            "later": {"type": "integer", "minimum": 0}
                        },
                        "required": ["now", "next", "later"],
                        "additionalProperties": False
                    },
                    "next_task": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/QueuedTask"},
                            {"type": "null"},
                        ]
                    },
                    "oldest_waiting": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/QueuedTask"},
                            {"type": "null"},
                        ]
                    },
                    "starving_bucket": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/QueuedBucketStarvation"},
                            {"type": "null"},
                        ]
                    },
                    "starvation_alert": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/QueuedStarvationAlert"},
                            {"type": "null"},
                        ]
                    },
                    "tasks": {"type": "array", "items": {"$ref": "#/components/schemas/QueuedTask"}},
                },
                "required": ["queued_count", "lane_counts", "bucket_counts", "next_task", "oldest_waiting", "starving_bucket", "starvation_alert", "tasks"],
                "additionalProperties": False,
            },
            "GatewayTasksResponse": {
                "type": "object",
                "properties": {
                    "queued": {"$ref": "#/components/schemas/QueuedTaskStatus"},
                    "live": {"$ref": "#/components/schemas/LiveTaskStatus"},
                },
                "required": ["queued", "live"],
                "additionalProperties": False,
            },
            "TaskDetailResponse": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "state": {"type": "string", "enum": ["queued", "active"]},
                    "task": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/QueuedTask"},
                            {"$ref": "#/components/schemas/LiveTask"},
                        ]
                    },
                },
                "required": ["task_id", "state", "task"],
                "additionalProperties": False,
            },
            "TaskReprioritizeRequest": {
                "type": "object",
                "properties": {
                    "bucket": {"type": "string", "enum": ["now", "next", "later"]},
                },
                "required": ["bucket"],
                "additionalProperties": False,
            },
            "TaskActionResponse": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["foreground", "reprioritize", "recover", "cancel"]},
                    "status": {
                        "type": "string",
                        "enum": ["started", "queued_next", "reprioritized", "recovered", "cancelled", "cancellation_requested"],
                    },
                    "message": {"type": "string"},
                    "task": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/QueuedTask"},
                            {"$ref": "#/components/schemas/LiveTask"},
                        ]
                    },
                },
                "required": ["task_id", "action", "status", "message", "task"],
                "additionalProperties": False,
            },
            "CronStatusSummary": {
                "type": "object",
                "properties": {
                    "active_jobs": {"type": "integer", "minimum": 0},
                    "total_jobs": {"type": "integer", "minimum": 0},
                    "lane_counts": {"$ref": "#/components/schemas/CronLaneCounts"},
                    "due_now": {"$ref": "#/components/schemas/CronLaneCounts"},
                },
                "required": ["active_jobs", "total_jobs", "lane_counts", "due_now"],
                "additionalProperties": False,
            },
            "GatewayStatus": {
                "type": "object",
                "properties": {
                    "gateway_state": {"type": "string"},
                    "exit_reason": _nullable_string(),
                    "updated_at": _nullable_string(fmt="date-time"),
                    "platforms": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/components/schemas/GatewayPlatformStatus"},
                    },
                    "queued_tasks": {"$ref": "#/components/schemas/QueuedTaskStatus"},
                    "live_tasks": {"$ref": "#/components/schemas/LiveTaskStatus"},
                    "cron": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/CronStatusSummary"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["gateway_state", "exit_reason", "updated_at", "platforms", "queued_tasks", "live_tasks", "cron"],
                "additionalProperties": False,
            },
        }

        job_id_param = {
            "name": "job_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "pattern": "[a-f0-9]{12}"},
            "description": "Hermes cron job ID.",
        }
        task_id_param = {
            "name": "task_id",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "minLength": 1},
            "description": "Hermes queued or active task ID.",
        }

        json_response = lambda schema_name, description: {  # noqa: E731
            "description": description,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{schema_name}"},
                }
            },
        }
        auth_error_response = lambda description="Invalid or missing bearer token.": json_response(  # noqa: E731
            "ControlPlaneAuthErrorResponse", description
        )
        simple_error_response = lambda description: json_response("ControlPlaneErrorResponse", description)  # noqa: E731
        task_action_error_response = lambda description: json_response("TaskActionErrorResponse", description)  # noqa: E731

        return {
            "openapi": "3.1.0",
            "info": {
                "title": "Hermes Control Plane API",
                "version": "1.0.0",
                "description": (
                    "Machine-readable contract for Hermes control-plane endpoints: "
                    "gateway status, task control, and cron job management surfaces."
                ),
            },
            "servers": [{"url": f"http://{self._host}:{self._port}"}],
            "security": [{"bearerAuth": []}],
            "paths": {
                "/api/status": {
                    "get": {
                        "summary": "Get gateway runtime status",
                        "operationId": "getGatewayStatus",
                        "tags": ["control-plane"],
                        "responses": {
                            "200": json_response("GatewayStatus", "Machine-readable gateway/runtime status."),
                            "401": auth_error_response(),
                            "500": simple_error_response("Internal error while building gateway status."),
                        },
                    }
                },
                "/api/tasks": {
                    "get": {
                        "summary": "List queued and active gateway tasks",
                        "operationId": "getGatewayTasks",
                        "tags": ["control-plane"],
                        "responses": {
                            "200": json_response("GatewayTasksResponse", "Queued task backlog plus active runtime tasks."),
                            "401": auth_error_response(),
                            "500": simple_error_response("Internal error while building task control-plane status."),
                        },
                    }
                },
                "/api/tasks/{task_id}": {
                    "get": {
                        "summary": "Get one queued or active gateway task",
                        "operationId": "getGatewayTask",
                        "tags": ["control-plane"],
                        "parameters": [task_id_param],
                        "responses": {
                            "200": json_response("TaskDetailResponse", "Queued or active task detail."),
                            "401": auth_error_response(),
                            "404": simple_error_response("Task was not found."),
                            "500": simple_error_response("Internal error while building task detail."),
                        },
                    }
                },
                "/api/tasks/{task_id}/foreground": {
                    "post": {
                        "summary": "Foreground a queued task",
                        "operationId": "foregroundGatewayTask",
                        "tags": ["control-plane"],
                        "parameters": [task_id_param],
                        "responses": {
                            "200": json_response("TaskActionResponse", "Foregrounded task action result."),
                            "400": task_action_error_response("Task is active or cannot be foregrounded."),
                            "401": auth_error_response(),
                            "404": task_action_error_response("Task was not found."),
                            "500": simple_error_response("Internal error while foregrounding a task."),
                        },
                    }
                },
                "/api/tasks/{task_id}/reprioritize": {
                    "post": {
                        "summary": "Change a queued task's priority bucket",
                        "operationId": "reprioritizeGatewayTask",
                        "tags": ["control-plane"],
                        "parameters": [task_id_param],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/TaskReprioritizeRequest"}
                                }
                            },
                        },
                        "responses": {
                            "200": json_response("TaskActionResponse", "Queued task reprioritized."),
                            "400": task_action_error_response("Priority bucket is invalid or task cannot be reprioritized."),
                            "401": auth_error_response(),
                            "404": task_action_error_response("Task was not found."),
                            "500": simple_error_response("Internal error while reprioritizing a task."),
                        },
                    }
                },
                "/api/tasks/{task_id}/recover": {
                    "post": {
                        "summary": "Apply the current starvation recovery recommendation for a queued task",
                        "operationId": "recoverGatewayTask",
                        "tags": ["control-plane"],
                        "parameters": [task_id_param],
                        "responses": {
                            "200": json_response("TaskActionResponse", "Queued task recovered via the current recommendation."),
                            "400": task_action_error_response("Task has no active recovery recommendation or cannot be recovered."),
                            "401": auth_error_response(),
                            "404": task_action_error_response("Task was not found."),
                            "500": simple_error_response("Internal error while recovering a task."),
                        },
                    }
                },
                "/api/tasks/{task_id}/cancel": {
                    "post": {
                        "summary": "Cancel a queued or managed runtime task",
                        "operationId": "cancelGatewayTask",
                        "tags": ["control-plane"],
                        "parameters": [task_id_param],
                        "responses": {
                            "200": json_response("TaskActionResponse", "Task cancellation action result."),
                            "400": task_action_error_response("Task cannot be cancelled from the control plane."),
                            "401": auth_error_response(),
                            "404": task_action_error_response("Task was not found."),
                            "500": simple_error_response("Internal error while cancelling a task."),
                        },
                    }
                },
                "/api/jobs": {
                    "get": {
                        "summary": "List cron jobs",
                        "operationId": "listCronJobs",
                        "tags": ["cron-jobs"],
                        "responses": {
                            "200": json_response("CronJobListResponse", "Cron job list plus lane summary and capabilities."),
                            "401": auth_error_response(),
                            "500": simple_error_response("Internal error while listing cron jobs."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                    "post": {
                        "summary": "Create a cron job",
                        "operationId": "createCronJob",
                        "tags": ["cron-jobs"],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CreateCronJobRequest"},
                                }
                            },
                        },
                        "responses": {
                            "200": json_response("CronJobEnvelope", "Created cron job payload."),
                            "400": simple_error_response("Validation error while creating a cron job."),
                            "401": auth_error_response(),
                            "500": simple_error_response("Internal error while creating a cron job."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                },
                "/api/jobs/{job_id}": {
                    "parameters": [job_id_param],
                    "get": {
                        "summary": "Get one cron job",
                        "operationId": "getCronJob",
                        "tags": ["cron-jobs"],
                        "responses": {
                            "200": json_response("CronJobEnvelope", "Single cron job payload."),
                            "400": simple_error_response("Invalid cron job ID format."),
                            "401": auth_error_response(),
                            "404": simple_error_response("Cron job was not found."),
                            "500": simple_error_response("Internal error while fetching a cron job."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                    "patch": {
                        "summary": "Update a cron job",
                        "operationId": "updateCronJob",
                        "tags": ["cron-jobs"],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/UpdateCronJobRequest"},
                                }
                            },
                        },
                        "responses": {
                            "200": json_response("CronJobEnvelope", "Updated cron job payload."),
                            "400": simple_error_response("Validation error while updating a cron job."),
                            "401": auth_error_response(),
                            "404": simple_error_response("Cron job was not found."),
                            "500": simple_error_response("Internal error while updating a cron job."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                    "delete": {
                        "summary": "Delete a cron job",
                        "operationId": "deleteCronJob",
                        "tags": ["cron-jobs"],
                        "responses": {
                            "200": json_response("DeleteCronJobResponse", "Deletion acknowledgement."),
                            "400": simple_error_response("Invalid cron job ID format."),
                            "401": auth_error_response(),
                            "404": simple_error_response("Cron job was not found."),
                            "500": simple_error_response("Internal error while deleting a cron job."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                },
                "/api/jobs/{job_id}/pause": {
                    "parameters": [job_id_param],
                    "post": {
                        "summary": "Pause a cron job",
                        "operationId": "pauseCronJob",
                        "tags": ["cron-jobs"],
                        "responses": {
                            "200": json_response("CronJobEnvelope", "Paused cron job payload."),
                            "400": simple_error_response("Invalid cron job ID format."),
                            "401": auth_error_response(),
                            "404": simple_error_response("Cron job was not found."),
                            "500": simple_error_response("Internal error while pausing a cron job."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                },
                "/api/jobs/{job_id}/resume": {
                    "parameters": [job_id_param],
                    "post": {
                        "summary": "Resume a cron job",
                        "operationId": "resumeCronJob",
                        "tags": ["cron-jobs"],
                        "responses": {
                            "200": json_response("CronJobEnvelope", "Resumed cron job payload."),
                            "400": simple_error_response("Invalid cron job ID format."),
                            "401": auth_error_response(),
                            "404": simple_error_response("Cron job was not found."),
                            "500": simple_error_response("Internal error while resuming a cron job."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                },
                "/api/jobs/{job_id}/run": {
                    "parameters": [job_id_param],
                    "post": {
                        "summary": "Trigger a cron job immediately",
                        "operationId": "runCronJob",
                        "tags": ["cron-jobs"],
                        "responses": {
                            "200": json_response("CronJobEnvelope", "Triggered cron job payload."),
                            "400": simple_error_response("Invalid cron job ID format."),
                            "401": auth_error_response(),
                            "404": simple_error_response("Cron job was not found."),
                            "500": simple_error_response("Internal error while triggering a cron job."),
                            "501": simple_error_response("Cron module not available."),
                        },
                    },
                },
            },
            "components": {
                "securitySchemes": {
                    "bearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "API key",
                    }
                },
                "schemas": schemas,
            },
        }

    def _empty_live_tasks_payload(self) -> Dict[str, Any]:
        return {
            "active_count": 0,
            "lane_counts": {lane: 0 for lane in self._CRON_LANE_ORDER},
            "oldest_running": None,
            "tasks": [],
        }

    def _empty_queued_tasks_payload(self) -> Dict[str, Any]:
        return {
            "queued_count": 0,
            "lane_counts": {lane: 0 for lane in self._CRON_LANE_ORDER},
            "bucket_counts": {bucket: 0 for bucket in ("now", "next", "later")},
            "next_task": None,
            "oldest_waiting": None,
            "starving_bucket": None,
            "starvation_alert": None,
            "tasks": [],
        }

    @staticmethod
    def _queued_task_sort_key(task: Dict[str, Any]) -> tuple[Any, ...]:
        return (
            int(task.get("priority") or 50),
            str(task.get("queued_at") or ""),
            str(task.get("task_id") or ""),
        )

    @staticmethod
    def _queued_task_actions(adapter: Any) -> List[str]:
        return shared_queued_task_actions(adapter)

    @staticmethod
    def _queued_task_source_label(message_event: Any) -> str:
        return shared_queued_task_source_label(message_event)

    @staticmethod
    def _queued_task_iso(value: Any) -> Optional[str]:
        return shared_queued_task_iso(value)

    def _queued_task_payload(self, envelope: Any, *, actions: Optional[List[str]] = None) -> Dict[str, Any]:
        return shared_queued_task_payload(envelope, actions=actions)

    def _harness_task_snapshot(self, task_id: str) -> Optional[Dict[str, Any]]:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        try:
            from agent.harness import get_harness_manager

            manager = get_harness_manager()
            if not getattr(manager, "enabled", False):
                return None
            snapshot = manager.task_snapshot(task_id)
            return dict(snapshot) if isinstance(snapshot, dict) else None
        except Exception:
            return None

    def _attach_harness_task(self, task_payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(task_payload or {})
        snapshot = self._harness_task_snapshot(payload.get("task_id"))
        if snapshot is not None:
            payload["harness"] = snapshot
        return payload

    def _normalize_queued_task_payload(self, task_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from gateway.status import normalize_queued_tasks_payload

            normalized = normalize_queued_tasks_payload({"tasks": [task_payload]})
            tasks = normalized.get("tasks") if isinstance(normalized, dict) else None
            if isinstance(tasks, list) and tasks:
                return self._attach_harness_task(dict(tasks[0]))
        except Exception:
            pass

        try:
            from task_lanes import normalize_status_task_lane

            fallback = dict(task_payload)
            fallback["lane"] = normalize_status_task_lane(fallback.get("lane"))
            fallback["state"] = "queued"
            fallback["control_mode"] = "queued"
            fallback["actions"] = list(fallback.get("actions") or [])
            return self._attach_harness_task(fallback)
        except Exception:
            return self._attach_harness_task(dict(task_payload))

    def _control_adapters(self) -> List[Any]:
        runner = getattr(self, "gateway_runner", None)
        adapters = getattr(runner, "adapters", {}) if runner is not None else {}
        if not isinstance(adapters, dict):
            return []
        return [candidate for candidate in adapters.values() if candidate is not self]

    def _find_queued_task(self, task_id: str) -> tuple[Optional[Any], Optional[Any]]:
        for candidate in self._control_adapters():
            snapshot_pending = getattr(candidate, "all_pending_tasks_snapshot", None)
            if not callable(snapshot_pending):
                continue
            try:
                pending = list(snapshot_pending() or [])
            except Exception:
                continue
            for envelope in pending:
                if str(getattr(envelope, "task_id", "") or "") == task_id:
                    return candidate, envelope
        return None, None

    def _find_live_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        tasks = self._live_tasks_status_payload().get("tasks")
        if not isinstance(tasks, list):
            return None
        for task in tasks:
            if isinstance(task, dict) and str(task.get("task_id") or "") == task_id:
                return self._attach_harness_task(dict(task))
        return None

    @staticmethod
    def _task_detail_payload(*, task_id: str, state: str, task: Dict[str, Any]) -> Dict[str, Any]:
        return build_task_detail_payload(task_id=task_id, state=state, task=task)

    @staticmethod
    def _task_action_payload(
        *,
        task_id: str,
        action: str,
        status: str,
        task: Dict[str, Any],
        message: Optional[str] = None,
        target_bucket: Optional[str] = None,
    ) -> Dict[str, Any]:
        return shared_build_task_action_payload(
            task_id=task_id,
            action=action,
            status=status,
            message=message,
            task=task,
            target_bucket=target_bucket,
        )

    @staticmethod
    def _task_action_error_payload(*, task_id: str, action: str, reason_code: str, message: Optional[str] = None) -> Dict[str, Any]:
        return shared_build_task_action_error_payload(
            task_id=task_id,
            action=action,
            reason_code=reason_code,
            message=message,
        )

    def _queued_tasks_status_payload(self) -> Dict[str, Any]:
        raw_tasks: List[Dict[str, Any]] = []
        for candidate in self._control_adapters():
            snapshot_pending = getattr(candidate, "all_pending_tasks_snapshot", None)
            if not callable(snapshot_pending):
                continue
            try:
                pending = list(snapshot_pending() or [])
            except Exception:
                continue
            actions = self._queued_task_actions(candidate)
            for envelope in pending:
                raw_tasks.append(self._queued_task_payload(envelope, actions=actions))

        raw_tasks.sort(key=self._queued_task_sort_key)

        try:
            from gateway.status import normalize_queued_tasks_payload

            normalized = normalize_queued_tasks_payload({"tasks": raw_tasks})
            tasks = normalized.get("tasks") if isinstance(normalized.get("tasks"), list) else []
            normalized["tasks"] = [self._attach_harness_task(task) for task in tasks if isinstance(task, dict)]
            next_task = normalized.get("next_task")
            if isinstance(next_task, dict):
                normalized["next_task"] = self._attach_harness_task(next_task)
            oldest_waiting = normalized.get("oldest_waiting")
            if isinstance(oldest_waiting, dict):
                normalized["oldest_waiting"] = self._attach_harness_task(oldest_waiting)
            return normalized
        except Exception:
            payload = self._empty_queued_tasks_payload()
            payload["tasks"] = [self._attach_harness_task(task) for task in raw_tasks if isinstance(task, dict)]
            payload["queued_count"] = len(raw_tasks)
            if raw_tasks:
                payload["next_task"] = self._normalize_queued_task_payload(raw_tasks[0])
            return payload

    def _live_tasks_status_payload(self, runtime_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = runtime_status if isinstance(runtime_status, dict) else {}
        live_tasks = payload.get("live_tasks") if isinstance(payload.get("live_tasks"), dict) else None
        if not live_tasks:
            try:
                from gateway.run import _current_live_task_status_payload

                live_tasks = _current_live_task_status_payload()
            except Exception:
                try:
                    from gateway.run import task_lane_registry

                    live_tasks = task_lane_registry.status_snapshot()
                except Exception:
                    live_tasks = self._empty_live_tasks_payload()

        try:
            from gateway.status import normalize_live_tasks_payload

            normalized = normalize_live_tasks_payload(live_tasks)
        except Exception:
            normalized = self._empty_live_tasks_payload()

        tasks = normalized.get("tasks") if isinstance(normalized.get("tasks"), list) else []
        normalized["tasks"] = [self._attach_harness_task(task) for task in tasks if isinstance(task, dict)]
        oldest_running = normalized.get("oldest_running")
        if isinstance(oldest_running, dict):
            normalized["oldest_running"] = self._attach_harness_task(oldest_running)
        return normalized

    def _tasks_payload(self, runtime_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return build_gateway_tasks_payload(
            queued=self._queued_tasks_status_payload(),
            live=self._live_tasks_status_payload(runtime_status),
        )

    def _cron_status_payload(self) -> Optional[Dict[str, Any]]:
        if not self._CRON_AVAILABLE:
            return None
        jobs = self._cron_list(include_disabled=True)
        active_jobs = [job for job in jobs if job.get("enabled", True)]
        lane_summary = self._cron_summarize_lanes(active_jobs, due_jobs=self._cron_get_due())
        return {
            "active_jobs": len(active_jobs),
            "total_jobs": len(jobs),
            "lane_counts": lane_summary["active"],
            "due_now": lane_summary["due"],
        }

    async def _handle_status(self, request: "web.Request") -> "web.Response":
        """GET /api/status — machine-readable gateway/runtime status."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            try:
                from gateway.status import build_gateway_status_payload, read_runtime_status

                runtime_status = read_runtime_status() or {}
            except Exception:
                runtime_status = {}
                from gateway.status import build_gateway_status_payload

            return web.json_response(
                build_gateway_status_payload(
                    runtime_status=runtime_status,
                    queued_tasks=self._queued_tasks_status_payload(),
                    live_tasks=self._live_tasks_status_payload(runtime_status),
                    cron_payload=self._cron_status_payload(),
                )
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    def _task_id_or_error(self, request: "web.Request") -> tuple[str, Optional["web.Response"]]:
        task_id = str(request.match_info.get("task_id") or "").strip()
        if not task_id:
            return "", web.json_response({"error": "Task ID is required"}, status=400)
        return task_id, None

    async def _reprioritize_bucket_or_error(
        self,
        request: "web.Request",
        *,
        task_id: str,
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return None, web.json_response(
                self._task_action_error_payload(
                    task_id=task_id,
                    action="reprioritize",
                    reason_code="invalid_request_body",
                ),
                status=400,
            )
        if not isinstance(body, dict):
            body = {}
        bucket = normalize_priority_bucket(body.get("bucket"))
        if bucket:
            return bucket, None
        return None, web.json_response(
            self._task_action_error_payload(
                task_id=task_id,
                action="reprioritize",
                reason_code="invalid_priority_bucket",
            ),
            status=400,
        )

    def _queued_task_status_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        queued_status = self._queued_tasks_status_payload()
        for task in list((queued_status or {}).get("tasks") or []):
            if isinstance(task, dict) and str(task.get("task_id") or "") == task_id:
                return dict(task)
        return None

    def _task_detail_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        queued_task = self._queued_task_status_by_id(task_id)
        if queued_task is not None:
            return self._task_detail_payload(task_id=task_id, state="queued", task=queued_task)

        live_task = self._find_live_task(task_id)
        if live_task is not None:
            return self._task_detail_payload(task_id=task_id, state="active", task=live_task)

        return None

    async def _handle_tasks(self, request: "web.Request") -> "web.Response":
        """GET /api/tasks — queued backlog plus active runtime tasks."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        try:
            return web.json_response(self._tasks_payload())
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_task_detail(self, request: "web.Request") -> "web.Response":
        """GET /api/tasks/{task_id} — queued or active task detail."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        task_id, task_err = self._task_id_or_error(request)
        if task_err:
            return task_err
        try:
            payload = self._task_detail_by_id(task_id)
            if payload is None:
                return web.json_response({"error": "Task not found"}, status=404)
            return web.json_response(payload)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_foreground_task(self, request: "web.Request") -> "web.Response":
        """POST /api/tasks/{task_id}/foreground — promote a queued task."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        task_id, task_err = self._task_id_or_error(request)
        if task_err:
            return task_err
        try:
            candidate, queued_task = self._find_queued_task(task_id)
            if queued_task is not None:
                foreground_pending = getattr(candidate, "foreground_pending_task", None) if candidate is not None else None
                if not callable(foreground_pending):
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="foreground",
                            reason_code="foreground_not_supported",
                        ),
                        status=400,
                    )
                foregrounded = foreground_pending(str(getattr(queued_task, "session_key", "") or ""), task_id)
                if foregrounded is None:
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="foreground",
                            reason_code="task_not_found",
                        ),
                        status=404,
                    )
                disposition, envelope = foregrounded
                status = disposition if disposition in {"started", "queued_next"} else "started"
                task_payload = self._normalize_queued_task_payload(
                    self._queued_task_payload(envelope, actions=self._queued_task_actions(candidate))
                )
                record_harness_task_action(
                    task_id=task_id,
                    action="foreground",
                    status=status,
                    surface="api",
                    session_key=str(getattr(queued_task, "session_key", "") or ""),
                    task_context=envelope,
                )
                return web.json_response(
                    self._task_action_payload(
                        task_id=task_id,
                        action="foreground",
                        status=status,
                        task=task_payload,
                    )
                )

            if self._find_live_task(task_id) is not None:
                return web.json_response(
                    self._task_action_error_payload(
                        task_id=task_id,
                        action="foreground",
                        reason_code="task_already_active",
                    ),
                    status=400,
                )
            return web.json_response(
                self._task_action_error_payload(
                    task_id=task_id,
                    action="foreground",
                    reason_code="task_not_found",
                ),
                status=404,
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_reprioritize_task(self, request: "web.Request") -> "web.Response":
        """POST /api/tasks/{task_id}/reprioritize — change a queued task's priority bucket."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        task_id, task_err = self._task_id_or_error(request)
        if task_err:
            return task_err
        bucket, bucket_err = await self._reprioritize_bucket_or_error(request, task_id=task_id)
        if bucket_err:
            return bucket_err
        try:
            candidate, queued_task = self._find_queued_task(task_id)
            if queued_task is not None:
                reprioritize_pending = getattr(candidate, "reprioritize_pending_task", None) if candidate is not None else None
                if not callable(reprioritize_pending):
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="reprioritize",
                            reason_code="reprioritize_not_supported",
                        ),
                        status=400,
                    )
                updated = reprioritize_pending(str(getattr(queued_task, "session_key", "") or ""), task_id, bucket)
                if updated is None:
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="reprioritize",
                            reason_code="task_not_found",
                        ),
                        status=404,
                    )
                task_payload = self._normalize_queued_task_payload(
                    self._queued_task_payload(updated, actions=self._queued_task_actions(candidate))
                )
                record_harness_task_action(
                    task_id=task_id,
                    action="reprioritize",
                    status="reprioritized",
                    surface="api",
                    session_key=str(getattr(queued_task, "session_key", "") or ""),
                    target_bucket=bucket,
                    task_context=updated,
                )
                return web.json_response(
                    self._task_action_payload(
                        task_id=task_id,
                        action="reprioritize",
                        status="reprioritized",
                        target_bucket=bucket,
                        task=task_payload,
                    )
                )

            if self._find_live_task(task_id) is not None:
                return web.json_response(
                    self._task_action_error_payload(
                        task_id=task_id,
                        action="reprioritize",
                        reason_code="reprioritize_requires_queued_task",
                    ),
                    status=400,
                )
            return web.json_response(
                self._task_action_error_payload(
                    task_id=task_id,
                    action="reprioritize",
                    reason_code="task_not_found",
                ),
                status=404,
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_recover_task(self, request: "web.Request") -> "web.Response":
        """POST /api/tasks/{task_id}/recover — apply the queued task's current recovery recommendation."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        task_id, task_err = self._task_id_or_error(request)
        if task_err:
            return task_err
        try:
            candidate, queued_task = self._find_queued_task(task_id)
            if queued_task is not None:
                queued_task_payload = self._queued_task_status_by_id(task_id)
                recovery_plan = queued_task_recovery_plan(queued_task_payload)
                if recovery_plan is None:
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="recover",
                            reason_code="recover_not_recommended",
                        ),
                        status=400,
                    )
                recovery_action = str(recovery_plan.get("action") or "").strip().lower()
                target_bucket = normalize_priority_bucket(recovery_plan.get("target_bucket"))
                if recovery_action == "reprioritize":
                    reprioritize_pending = getattr(candidate, "reprioritize_pending_task", None) if candidate is not None else None
                    if not callable(reprioritize_pending):
                        return web.json_response(
                            self._task_action_error_payload(
                                task_id=task_id,
                                action="reprioritize",
                                reason_code="reprioritize_not_supported",
                            ),
                            status=400,
                        )
                    updated = reprioritize_pending(str(getattr(queued_task, "session_key", "") or ""), task_id, str(target_bucket or ""))
                    if updated is None:
                        return web.json_response(
                            self._task_action_error_payload(
                                task_id=task_id,
                                action="recover",
                                reason_code="task_not_found",
                            ),
                            status=404,
                        )
                    task_payload = self._normalize_queued_task_payload(
                        self._queued_task_payload(updated, actions=self._queued_task_actions(candidate))
                    )
                    record_harness_task_action(
                        task_id=task_id,
                        action="recover",
                        status="recovered",
                        surface="api",
                        session_key=str(getattr(queued_task, "session_key", "") or ""),
                        target_bucket=target_bucket,
                        task_context=updated,
                        metadata={"recovery_action": "reprioritize"},
                    )
                    return web.json_response(
                        self._task_action_payload(
                            task_id=task_id,
                            action="recover",
                            status="recovered",
                            target_bucket=target_bucket,
                            task=task_payload,
                        )
                    )
                foreground_pending = getattr(candidate, "foreground_pending_task", None) if candidate is not None else None
                if not callable(foreground_pending):
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="foreground",
                            reason_code="foreground_not_supported",
                        ),
                        status=400,
                    )
                foregrounded = foreground_pending(str(getattr(queued_task, "session_key", "") or ""), task_id)
                if foregrounded is None:
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="recover",
                            reason_code="task_not_found",
                        ),
                        status=404,
                    )
                disposition, envelope = foregrounded
                task_payload = self._normalize_queued_task_payload(
                    self._queued_task_payload(envelope, actions=self._queued_task_actions(candidate))
                )
                action_status = disposition if disposition in {"started", "queued_next"} else "started"
                record_harness_task_action(
                    task_id=task_id,
                    action="recover",
                    status=action_status,
                    surface="api",
                    session_key=str(getattr(queued_task, "session_key", "") or ""),
                    task_context=envelope,
                    metadata={"recovery_action": "foreground"},
                )
                return web.json_response(
                    self._task_action_payload(
                        task_id=task_id,
                        action="recover",
                        status=action_status,
                        task=task_payload,
                    )
                )

            if self._find_live_task(task_id) is not None:
                return web.json_response(
                    self._task_action_error_payload(
                        task_id=task_id,
                        action="recover",
                        reason_code="recover_requires_queued_task",
                    ),
                    status=400,
                )

            return web.json_response(
                self._task_action_error_payload(
                    task_id=task_id,
                    action="recover",
                    reason_code="task_not_found",
                ),
                status=404,
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_cancel_task(self, request: "web.Request") -> "web.Response":
        """POST /api/tasks/{task_id}/cancel — cancel a queued or managed runtime task."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        task_id, task_err = self._task_id_or_error(request)
        if task_err:
            return task_err
        try:
            candidate, queued_task = self._find_queued_task(task_id)
            if queued_task is not None:
                cancel_pending = getattr(candidate, "cancel_pending_task", None) if candidate is not None else None
                if not callable(cancel_pending):
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="cancel",
                            reason_code="cancel_not_supported",
                        ),
                        status=400,
                    )
                removed = cancel_pending(str(getattr(queued_task, "session_key", "") or ""), task_id)
                if removed is None:
                    return web.json_response(
                        self._task_action_error_payload(
                            task_id=task_id,
                            action="cancel",
                            reason_code="task_not_found",
                        ),
                        status=404,
                    )
                task_payload = self._normalize_queued_task_payload(
                    self._queued_task_payload(removed, actions=self._queued_task_actions(candidate))
                )
                record_harness_task_action(
                    task_id=task_id,
                    action="cancel",
                    status="cancelled",
                    surface="api",
                    session_key=str(getattr(queued_task, "session_key", "") or ""),
                    task_context=removed,
                )
                return web.json_response(
                    self._task_action_payload(
                        task_id=task_id,
                        action="cancel",
                        status="cancelled",
                        task=task_payload,
                    )
                )

            if self._find_live_task(task_id) is not None:
                runner = getattr(self, "gateway_runner", None)
                cancel_runtime = getattr(runner, "_cancel_managed_runtime_task", None) if runner is not None else None
                if callable(cancel_runtime) and cancel_runtime(task_id):
                    live_task = self._find_live_task(task_id) or {}
                    return web.json_response(
                        self._task_action_payload(
                            task_id=task_id,
                            action="cancel",
                            status="cancellation_requested",
                            task=live_task,
                        )
                    )
                return web.json_response(
                    self._task_action_error_payload(
                        task_id=task_id,
                        action="cancel",
                        reason_code="cancel_handle_missing",
                    ),
                    status=400,
                )

            return web.json_response(
                self._task_action_error_payload(
                    task_id=task_id,
                    action="cancel",
                    reason_code="task_not_found",
                ),
                status=404,
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_openapi(self, request: "web.Request") -> "web.Response":
        """GET /openapi.json — OpenAPI contract for Hermes control-plane endpoints."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        return web.json_response(self._openapi_contract())

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs — list all cron jobs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in ("true", "1")
            jobs = self._cron_list(include_disabled=include_disabled)
            jobs_payload = [self._job_payload(job) for job in jobs]
            active_jobs = [job for job in jobs if job.get("enabled", True)]
            lane_summary = self._cron_summarize_lanes(active_jobs, due_jobs=self._cron_get_due())
            return web.json_response(
                {
                    "jobs": jobs_payload,
                    "summary": {
                        "active_jobs": len(active_jobs),
                        "total_jobs": len(jobs),
                        "lane_counts": lane_summary["active"],
                        "due_now": lane_summary["due"],
                    },
                    "capabilities": self._lane_capabilities_payload(),
                }
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs — create a new cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            normalized_lane = None
            if "lane" in body:
                normalized_lane, lane_err = self._normalize_job_lane_or_error(body.get("lane"))
                if lane_err:
                    return lane_err

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
            }
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat
            if "lane" in body:
                kwargs["lane"] = normalized_lane

            job = self._cron_create(**kwargs)
            return web.json_response({"job": self._job_payload(job)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} — get a single cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": self._job_payload(job)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} — update a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # Whitelist allowed fields to prevent arbitrary key injection
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            if "lane" in sanitized:
                sanitized["lane"], lane_err = self._normalize_job_lane_or_error(sanitized.get("lane"))
                if lane_err:
                    return lane_err
            # Validate lengths if present
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            job = self._cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": self._job_payload(job)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} — delete a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = self._cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause — pause a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": self._job_payload(job)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume — resume a paused cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": self._job_payload(job)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run — trigger immediate execution."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = self._cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": self._job_payload(job)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_output_items(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Build the full output item array from the agent's messages.

        Walks *result["messages"]* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # Final assistant message
        final = result.get("final_response", "")
        if not final:
            final = result.get("error", "(No response generated)")

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        agent_ref: Optional[list] = None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.

        If *agent_ref* is a one-element list, the AIAgent instance is stored
        at ``agent_ref[0]`` before ``run_conversation`` begins.  This allows
        callers (e.g. the SSE writer) to call ``agent.interrupt()`` from
        another thread to stop in-progress LLM calls.
        """
        loop = asyncio.get_event_loop()

        def _run():
            agent = self._create_agent(
                ephemeral_system_prompt=ephemeral_system_prompt,
                session_id=session_id,
                stream_delta_callback=stream_delta_callback,
                tool_progress_callback=tool_progress_callback,
            )
            if agent_ref is not None:
                agent_ref[0] = agent
            result = agent.run_conversation(
                user_message=user_message,
                conversation_history=conversation_history,
            )
            usage = {
                "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
            }
            return result, usage

        return await loop.run_in_executor(None, _run)

    # ------------------------------------------------------------------
    # /v1/runs — structured event streaming
    # ------------------------------------------------------------------

    _MAX_CONCURRENT_RUNS = 10  # Prevent unbounded resource allocation
    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        def _push(event: Dict[str, Any]) -> None:
            q = self._run_streams.get(run_id)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded

        return _callback

    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs — start an agent run, return run_id immediately."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # Enforce concurrency limit
        if len(self._run_streams) >= self._MAX_CONCURRENT_RUNS:
            return web.json_response(
                _openai_error(f"Too many concurrent runs (max {self._MAX_CONCURRENT_RUNS})", code="rate_limit_exceeded"),
                status=429,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        run_id = f"run_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = time.time()

        event_cb = self._make_run_event_callback(run_id, loop)

        # Also wire stream_delta_callback so message.delta events flow through
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                if instructions is None:
                    instructions = stored.get("instructions")

        # When input is a multi-message array, extract all but the last
        # message as conversation history (the last becomes user_message).
        # Only fires when no explicit history was provided.
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # Flatten multi-part content blocks to text
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        session_id = body.get("session_id") or run_id
        ephemeral_system_prompt = instructions

        async def _run_and_close():
            try:
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                )
                def _run_sync():
                    r = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=conversation_history,
                    )
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                q.put_nowait({
                    "event": "run.completed",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "output": final_response,
                    "usage": usage,
                })
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                # Sentinel: signal SSE stream to close
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

        task = asyncio.create_task(_run_and_close())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"run_id": run_id, "status": "started"}, status=202)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events — SSE stream of structured agent lifecycle events."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        # Allow subscribing slightly before the run is registered (race condition window)
        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # Run finished — send final SSE comment and close
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)

        return response

    async def _sweep_orphaned_runs(self) -> None:
        """Periodically clean up run streams that were never consumed."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            stale = [
                run_id
                for run_id, created_at in list(self._run_streams_created.items())
                if now - created_at > self._RUN_STREAM_TTL
            ]
            for run_id in stale:
                logger.debug("[api_server] sweeping orphaned run %s", run_id)
                self._run_streams.pop(run_id, None)
                self._run_streams_created.pop(run_id, None)

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            mws = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
            self._app = web.Application(middlewares=mws)
            self._app["api_server_adapter"] = self
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/api/status", self._handle_status)
            self._app.router.add_get("/api/tasks", self._handle_tasks)
            self._app.router.add_get("/api/tasks/{task_id}", self._handle_task_detail)
            self._app.router.add_post("/api/tasks/{task_id}/foreground", self._handle_foreground_task)
            self._app.router.add_post("/api/tasks/{task_id}/reprioritize", self._handle_reprioritize_task)
            self._app.router.add_post("/api/tasks/{task_id}/recover", self._handle_recover_task)
            self._app.router.add_post("/api/tasks/{task_id}/cancel", self._handle_cancel_task)
            self._app.router.add_get("/openapi.json", self._handle_openapi)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
            self._app.router.add_post("/v1/responses", self._handle_responses)
            self._app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
            self._app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
            # Cron jobs management API
            self._app.router.add_get("/api/jobs", self._handle_list_jobs)
            self._app.router.add_post("/api/jobs", self._handle_create_job)
            self._app.router.add_get("/api/jobs/{job_id}", self._handle_get_job)
            self._app.router.add_patch("/api/jobs/{job_id}", self._handle_update_job)
            self._app.router.add_delete("/api/jobs/{job_id}", self._handle_delete_job)
            self._app.router.add_post("/api/jobs/{job_id}/pause", self._handle_pause_job)
            self._app.router.add_post("/api/jobs/{job_id}/resume", self._handle_resume_job)
            self._app.router.add_post("/api/jobs/{job_id}/run", self._handle_run_job)
            # Structured event streaming
            self._app.router.add_post("/v1/runs", self._handle_runs)
            self._app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)
            # Start background sweep to clean up orphaned (unconsumed) run streams
            sweep_task = asyncio.create_task(self._sweep_orphaned_runs())
            try:
                self._background_tasks.add(sweep_task)
            except TypeError:
                pass
            if hasattr(sweep_task, "add_done_callback"):
                sweep_task.add_done_callback(self._background_tasks.discard)

            # Port conflict detection — fail fast if port is already in use
            import socket as _socket
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                    _s.settimeout(1)
                    _s.connect(('127.0.0.1', self._port))
                logger.error('[%s] Port %d already in use. Set a different port in config.yaml: platforms.api_server.port', self.name, self._port)
                return False
            except (ConnectionRefusedError, OSError):
                pass  # port is free

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._mark_connected()
            logger.info(
                "[%s] API server listening on http://%s:%d",
                self.name, self._host, self._port,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server."""
        self._mark_disconnected()
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used — HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
