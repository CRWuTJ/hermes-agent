"""
Tests for the OpenAI-compatible API server gateway adapter.

Tests cover:
- Chat Completions endpoint (request parsing, response format)
- Responses API endpoint (request parsing, response format)
- previous_response_id chaining (store/retrieve)
- Auth (valid key, invalid key, no key configured)
- /v1/models endpoint
- /health endpoint
- System prompt extraction
- Error handling (invalid JSON, missing fields)
"""

import json
import re
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    ResponseStore,
    _CORS_HEADERS,
    check_api_server_requirements,
    cors_middleware,
    security_headers_middleware,
)
from gateway.platforms.base import MessageEvent, MessageTaskEnvelope, MessageType
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# check_api_server_requirements
# ---------------------------------------------------------------------------


class TestCheckRequirements:
    def test_returns_true_when_aiohttp_available(self):
        assert check_api_server_requirements() is True

    @patch("gateway.platforms.api_server.AIOHTTP_AVAILABLE", False)
    def test_returns_false_without_aiohttp(self):
        assert check_api_server_requirements() is False


# ---------------------------------------------------------------------------
# ResponseStore
# ---------------------------------------------------------------------------


class TestResponseStore:
    def test_put_and_get(self):
        store = ResponseStore(max_size=10)
        store.put("resp_1", {"output": "hello"})
        assert store.get("resp_1") == {"output": "hello"}

    def test_get_missing_returns_none(self):
        store = ResponseStore(max_size=10)
        assert store.get("resp_missing") is None

    def test_lru_eviction(self):
        store = ResponseStore(max_size=3)
        store.put("resp_1", {"output": "one"})
        store.put("resp_2", {"output": "two"})
        store.put("resp_3", {"output": "three"})
        # Adding a 4th should evict resp_1
        store.put("resp_4", {"output": "four"})
        assert store.get("resp_1") is None
        assert store.get("resp_2") is not None
        assert len(store) == 3

    def test_access_refreshes_lru(self):
        store = ResponseStore(max_size=3)
        store.put("resp_1", {"output": "one"})
        store.put("resp_2", {"output": "two"})
        store.put("resp_3", {"output": "three"})
        # Access resp_1 to move it to end
        store.get("resp_1")
        # Now resp_2 is the oldest — adding a 4th should evict resp_2
        store.put("resp_4", {"output": "four"})
        assert store.get("resp_2") is None
        assert store.get("resp_1") is not None

    def test_update_existing_key(self):
        store = ResponseStore(max_size=10)
        store.put("resp_1", {"output": "v1"})
        store.put("resp_1", {"output": "v2"})
        assert store.get("resp_1") == {"output": "v2"}
        assert len(store) == 1

    def test_delete_existing(self):
        store = ResponseStore(max_size=10)
        store.put("resp_1", {"output": "hello"})
        assert store.delete("resp_1") is True
        assert store.get("resp_1") is None
        assert len(store) == 0

    def test_delete_missing(self):
        store = ResponseStore(max_size=10)
        assert store.delete("resp_missing") is False


# ---------------------------------------------------------------------------
# Adapter initialization
# ---------------------------------------------------------------------------


class TestAdapterInit:
    def test_default_config(self):
        config = PlatformConfig(enabled=True)
        adapter = APIServerAdapter(config)
        assert adapter._host == "127.0.0.1"
        assert adapter._port == 8642
        assert adapter._api_key == ""
        assert adapter.platform == Platform.API_SERVER

    def test_custom_config_from_extra(self):
        config = PlatformConfig(
            enabled=True,
            extra={
                "host": "0.0.0.0",
                "port": 9999,
                "key": "sk-test",
                "cors_origins": ["http://localhost:3000"],
            },
        )
        adapter = APIServerAdapter(config)
        assert adapter._host == "0.0.0.0"
        assert adapter._port == 9999
        assert adapter._api_key == "sk-test"
        assert adapter._cors_origins == ("http://localhost:3000",)

    def test_config_from_env(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_HOST", "10.0.0.1")
        monkeypatch.setenv("API_SERVER_PORT", "7777")
        monkeypatch.setenv("API_SERVER_KEY", "sk-env")
        monkeypatch.setenv("API_SERVER_CORS_ORIGINS", "http://localhost:3000, http://127.0.0.1:3000")
        config = PlatformConfig(enabled=True)
        adapter = APIServerAdapter(config)
        assert adapter._host == "10.0.0.1"
        assert adapter._port == 7777
        assert adapter._api_key == "sk-env"
        assert adapter._cors_origins == (
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        )


# ---------------------------------------------------------------------------
# Auth checking
# ---------------------------------------------------------------------------


class TestAuth:
    def test_no_key_configured_allows_all(self):
        config = PlatformConfig(enabled=True)
        adapter = APIServerAdapter(config)
        mock_request = MagicMock()
        mock_request.headers = {}
        assert adapter._check_auth(mock_request) is None

    def test_valid_key_passes(self):
        config = PlatformConfig(enabled=True, extra={"key": "sk-test123"})
        adapter = APIServerAdapter(config)
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Bearer sk-test123"}
        assert adapter._check_auth(mock_request) is None

    def test_invalid_key_returns_401(self):
        config = PlatformConfig(enabled=True, extra={"key": "sk-test123"})
        adapter = APIServerAdapter(config)
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Bearer wrong-key"}
        result = adapter._check_auth(mock_request)
        assert result is not None
        assert result.status == 401

    def test_missing_auth_header_returns_401(self):
        config = PlatformConfig(enabled=True, extra={"key": "sk-test123"})
        adapter = APIServerAdapter(config)
        mock_request = MagicMock()
        mock_request.headers = {}
        result = adapter._check_auth(mock_request)
        assert result is not None
        assert result.status == 401

    def test_malformed_auth_header_returns_401(self):
        config = PlatformConfig(enabled=True, extra={"key": "sk-test123"})
        adapter = APIServerAdapter(config)
        mock_request = MagicMock()
        mock_request.headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        result = adapter._check_auth(mock_request)
        assert result is not None
        assert result.status == 401


# ---------------------------------------------------------------------------
# Helpers for HTTP tests
# ---------------------------------------------------------------------------


def _make_adapter(api_key: str = "", cors_origins=None) -> APIServerAdapter:
    """Create an adapter with optional API key."""
    extra = {}
    if api_key:
        extra["key"] = api_key
    if cors_origins is not None:
        extra["cors_origins"] = cors_origins
    config = PlatformConfig(enabled=True, extra=extra)
    return APIServerAdapter(config)


def _create_app(adapter: APIServerAdapter) -> web.Application:
    """Create the aiohttp app from the adapter (without starting the full server)."""
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_get("/v1/health", adapter._handle_health)
    app.router.add_get("/api/status", adapter._handle_status)
    app.router.add_get("/api/tasks", adapter._handle_tasks)
    app.router.add_get("/api/tasks/{task_id}", adapter._handle_task_detail)
    app.router.add_post("/api/tasks/{task_id}/foreground", adapter._handle_foreground_task)
    app.router.add_post("/api/tasks/{task_id}/reprioritize", adapter._handle_reprioritize_task)
    app.router.add_post("/api/tasks/{task_id}/recover", adapter._handle_recover_task)
    app.router.add_post("/api/tasks/{task_id}/cancel", adapter._handle_cancel_task)
    app.router.add_get("/openapi.json", adapter._handle_openapi)
    app.router.add_get("/v1/models", adapter._handle_models)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/responses/{response_id}", adapter._handle_get_response)
    app.router.add_delete("/v1/responses/{response_id}", adapter._handle_delete_response)
    return app


_DOCS_PATH = Path(__file__).resolve().parents[2] / "website" / "docs" / "user-guide" / "features" / "api-server.md"


def _read_api_server_docs() -> str:
    return _DOCS_PATH.read_text(encoding="utf-8")


def _markdown_section(markdown: str, heading: str) -> str:
    match = re.search(rf"^{re.escape(heading)}\s*$", markdown, flags=re.MULTILINE)
    assert match, f"Missing heading: {heading}"
    level = len(heading) - len(heading.lstrip("#"))
    remainder = markdown[match.end() :]
    next_heading = re.search(rf"^(?:#{{1,{level}}})\s", remainder, flags=re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(markdown)
    return markdown[match.end() : end]


def _markdown_json_blocks(markdown: str, heading: str) -> list[dict]:
    section = _markdown_section(markdown, heading)
    blocks = re.findall(r"```json\n(.*?)\n```", section, flags=re.DOTALL)
    assert blocks, f"Missing JSON block under heading: {heading}"
    return [json.loads(block) for block in blocks]


def _assert_matches_openapi_schema(openapi: dict, schema_name: str, instance: dict) -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        from jsonschema import Draft202012Validator, RefResolver

        resolver = RefResolver.from_schema(openapi)
        schema = openapi["components"]["schemas"][schema_name]
        Draft202012Validator(schema, resolver=resolver).validate(instance)


def _make_task_source(chat_id: str = "123") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id="user-123",
        user_name="tester",
    )


def _make_task_event(text: str, *, source: SessionSource | None = None, message_id: str = "msg-1") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source or _make_task_source(),
        message_id=message_id,
    )


def _make_queued_task(
    *,
    task_id: str = "task-hi",
    session_key: str = "telegram:user:123",
    priority: int = 10,
    lane: str = "cron_scout",
    preview: str = "high priority queued follow-up",
    reply_policy: str = "status_only",
    cancellation_policy: str = "preserve",
    reason: str = "busy_followup",
) -> MessageTaskEnvelope:
    return MessageTaskEnvelope(
        task_id=task_id,
        session_key=session_key,
        message_event=_make_task_event(preview),
        priority=priority,
        lane=lane,
        reply_policy=reply_policy,
        cancellation_policy=cancellation_policy,
        queued_at=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc),
        reason=reason,
    )


def _make_live_tasks_payload(*, tasks: list[dict]) -> dict:
    lane_counts = {"interactive": 0, "cron_scout": 0, "housekeeping": 0}
    for task in tasks:
        lane = task.get("lane")
        if lane in lane_counts:
            lane_counts[lane] += 1
    return {
        "active_count": len(tasks),
        "lane_counts": lane_counts,
        "tasks": tasks,
    }


def _attach_control_runner(adapter: APIServerAdapter, control_adapter: Any | None = None, *, cancel_runtime_result: bool = False) -> MagicMock:
    runner = MagicMock()
    runner.adapters = {}
    if control_adapter is not None:
        runner.adapters["telegram"] = control_adapter
    runner.adapters["api"] = adapter
    runner._cancel_managed_runtime_task = MagicMock(return_value=cancel_runtime_result)
    adapter.gateway_runner = runner
    return runner


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


# ---------------------------------------------------------------------------
# /health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_security_headers_present(self, adapter):
        """Responses should include basic security headers."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health")
            assert resp.status == 200
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("Referrer-Policy") == "no-referrer"

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["platform"] == "hermes-agent"

    @pytest.mark.asyncio
    async def test_v1_health_alias_returns_ok(self, adapter):
        """GET /v1/health should return the same response as /health."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert data["platform"] == "hermes-agent"


# ---------------------------------------------------------------------------
# /v1/models endpoint
# ---------------------------------------------------------------------------


class TestModelsEndpoint:
    @pytest.mark.asyncio
    async def test_models_returns_hermes_agent(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/models")
            assert resp.status == 200
            data = await resp.json()
            assert data["object"] == "list"
            assert len(data["data"]) == 1
            assert data["data"][0]["id"] == "hermes-agent"
            assert data["data"][0]["owned_by"] == "hermes"

    @pytest.mark.asyncio
    async def test_models_requires_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/models")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_models_with_valid_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get(
                "/v1/models",
                headers={"Authorization": "Bearer sk-secret"},
            )
            assert resp.status == 200


# ---------------------------------------------------------------------------
# /v1/chat/completions endpoint
# ---------------------------------------------------------------------------


class TestChatCompletionsEndpoint:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "Invalid JSON" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_missing_messages_returns_400(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/chat/completions", json={"model": "test"})
            assert resp.status == 400
            data = await resp.json()
            assert "messages" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_empty_messages_returns_400(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/chat/completions", json={"model": "test", "messages": []})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_stream_true_returns_sse(self, adapter):
        """stream=true returns SSE format with the full response."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            async def _mock_run_agent(**kwargs):
                # Simulate streaming: invoke stream_delta_callback with tokens
                cb = kwargs.get("stream_delta_callback")
                if cb:
                    cb("Hello!")
                    cb(None)  # End signal
                return (
                    {"final_response": "Hello!", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent) as mock_run:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                )
                assert resp.status == 200
                assert "text/event-stream" in resp.headers.get("Content-Type", "")
                body = await resp.text()
                assert "data: " in body
                assert "[DONE]" in body
                assert "Hello!" in body

    @pytest.mark.asyncio
    async def test_stream_survives_tool_call_none_sentinel(self, adapter):
        """stream_delta_callback(None) mid-stream (tool calls) must NOT kill the SSE stream.

        The agent fires stream_delta_callback(None) to tell the CLI display to
        close its response box before executing tool calls.  The API server's
        _on_delta must filter this out so the SSE response stays open and the
        final answer (streamed after tool execution) reaches the client.
        """
        import asyncio

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            async def _mock_run_agent(**kwargs):
                cb = kwargs.get("stream_delta_callback")
                if cb:
                    # Simulate: agent streams partial text, then fires None
                    # (tool call box-close signal), then streams the final answer
                    cb("Thinking")
                    cb(None)          # mid-stream None from tool calls
                    await asyncio.sleep(0.05)  # simulate tool execution delay
                    cb(" about it...")
                    cb(None)          # another None (possible second tool round)
                    await asyncio.sleep(0.05)
                    cb(" The answer is 42.")
                return (
                    {"final_response": "Thinking about it... The answer is 42.", "messages": [], "api_calls": 3},
                    {"input_tokens": 20, "output_tokens": 15, "total_tokens": 35},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test",
                        "messages": [{"role": "user", "content": "What is the answer?"}],
                        "stream": True,
                    },
                )
                assert resp.status == 200
                body = await resp.text()
                assert "[DONE]" in body
                # The final answer text must appear in the SSE stream
                assert "The answer is 42." in body
                # All partial text must be present too
                assert "Thinking" in body
                assert " about it..." in body

    @pytest.mark.asyncio
    async def test_stream_includes_tool_progress(self, adapter):
        """tool_progress_callback fires → progress appears in the SSE stream."""
        import asyncio

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            async def _mock_run_agent(**kwargs):
                cb = kwargs.get("stream_delta_callback")
                tp_cb = kwargs.get("tool_progress_callback")
                # Simulate tool progress before streaming content
                if tp_cb:
                    tp_cb("tool.started", "terminal", "ls -la", {"command": "ls -la"})
                if cb:
                    await asyncio.sleep(0.05)
                    cb("Here are the files.")
                return (
                    {"final_response": "Here are the files.", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test",
                        "messages": [{"role": "user", "content": "list files"}],
                        "stream": True,
                    },
                )
                assert resp.status == 200
                body = await resp.text()
                assert "[DONE]" in body
                # Tool progress message must appear in the stream
                assert "ls -la" in body
                # Final content must also be present
                assert "Here are the files." in body

    @pytest.mark.asyncio
    async def test_stream_tool_progress_skips_internal_events(self, adapter):
        """Internal events (name starting with _) are not streamed."""
        import asyncio

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            async def _mock_run_agent(**kwargs):
                cb = kwargs.get("stream_delta_callback")
                tp_cb = kwargs.get("tool_progress_callback")
                if tp_cb:
                    tp_cb("tool.started", "_thinking", "some internal state", {})
                    tp_cb("tool.started", "web_search", "Python docs", {"query": "Python docs"})
                if cb:
                    await asyncio.sleep(0.05)
                    cb("Found it.")
                return (
                    {"final_response": "Found it.", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )

            with patch.object(adapter, "_run_agent", side_effect=_mock_run_agent):
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test",
                        "messages": [{"role": "user", "content": "search"}],
                        "stream": True,
                    },
                )
                assert resp.status == 200
                body = await resp.text()
                # Internal _thinking event should NOT appear
                assert "some internal state" not in body
                # Real tool progress should appear
                assert "Python docs" in body

    @pytest.mark.asyncio
    async def test_no_user_message_returns_400(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={
                    "model": "test",
                    "messages": [{"role": "system", "content": "You are helpful."}],
                },
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_successful_completion(self, adapter):
        """Test a successful chat completion with mocked agent."""
        mock_result = {
            "final_response": "Hello! How can I help you today?",
            "messages": [],
            "api_calls": 1,
        }

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-agent",
                        "messages": [{"role": "user", "content": "Hello"}],
                    },
                )

            assert resp.status == 200
            data = await resp.json()
            assert data["object"] == "chat.completion"
            assert data["id"].startswith("chatcmpl-")
            assert data["model"] == "hermes-agent"
            assert len(data["choices"]) == 1
            assert data["choices"][0]["message"]["role"] == "assistant"
            assert data["choices"][0]["message"]["content"] == "Hello! How can I help you today?"
            assert data["choices"][0]["finish_reason"] == "stop"
            assert "usage" in data

    @pytest.mark.asyncio
    async def test_system_prompt_extracted(self, adapter):
        """System messages from the client are passed as ephemeral_system_prompt."""
        mock_result = {
            "final_response": "I am a pirate! Arrr!",
            "messages": [],
            "api_calls": 1,
        }

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-agent",
                        "messages": [
                            {"role": "system", "content": "You are a pirate."},
                            {"role": "user", "content": "Hello"},
                        ],
                    },
                )

            assert resp.status == 200
            # Check that _run_agent was called with the system prompt
            call_kwargs = mock_run.call_args
            assert call_kwargs.kwargs.get("ephemeral_system_prompt") == "You are a pirate."
            assert call_kwargs.kwargs.get("user_message") == "Hello"

    @pytest.mark.asyncio
    async def test_conversation_history_passed(self, adapter):
        """Previous user/assistant messages become conversation_history."""
        mock_result = {"final_response": "3", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-agent",
                        "messages": [
                            {"role": "user", "content": "1+1=?"},
                            {"role": "assistant", "content": "2"},
                            {"role": "user", "content": "Now add 1 more"},
                        ],
                    },
                )

            assert resp.status == 200
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["user_message"] == "Now add 1 more"
            assert len(call_kwargs["conversation_history"]) == 2
            assert call_kwargs["conversation_history"][0] == {"role": "user", "content": "1+1=?"}
            assert call_kwargs["conversation_history"][1] == {"role": "assistant", "content": "2"}

    @pytest.mark.asyncio
    async def test_agent_error_returns_500(self, adapter):
        """Agent exception returns 500."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.side_effect = RuntimeError("Provider failed")
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-agent",
                        "messages": [{"role": "user", "content": "Hello"}],
                    },
                )

            assert resp.status == 500
            data = await resp.json()
            assert "Provider failed" in data["error"]["message"]


# ---------------------------------------------------------------------------
# /v1/responses endpoint
# ---------------------------------------------------------------------------


class TestResponsesEndpoint:
    @pytest.mark.asyncio
    async def test_missing_input_returns_400(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/responses", json={"model": "test"})
            assert resp.status == 400
            data = await resp.json()
            assert "input" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/responses",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_successful_response_with_string_input(self, adapter):
        """String input is wrapped in a user message."""
        mock_result = {
            "final_response": "Paris is the capital of France.",
            "messages": [],
            "api_calls": 1,
        }

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "What is the capital of France?",
                    },
                )

            assert resp.status == 200
            data = await resp.json()
            assert data["object"] == "response"
            assert data["id"].startswith("resp_")
            assert data["status"] == "completed"
            assert len(data["output"]) == 1
            assert data["output"][0]["type"] == "message"
            assert data["output"][0]["content"][0]["type"] == "output_text"
            assert data["output"][0]["content"][0]["text"] == "Paris is the capital of France."

    @pytest.mark.asyncio
    async def test_successful_response_with_array_input(self, adapter):
        """Array input with role/content objects."""
        mock_result = {"final_response": "Done", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": [
                            {"role": "user", "content": "Hello"},
                            {"role": "user", "content": "What is 2+2?"},
                        ],
                    },
                )

            assert resp.status == 200
            call_kwargs = mock_run.call_args.kwargs
            # Last message is user_message, rest are history
            assert call_kwargs["user_message"] == "What is 2+2?"
            assert len(call_kwargs["conversation_history"]) == 1

    @pytest.mark.asyncio
    async def test_instructions_as_ephemeral_prompt(self, adapter):
        """The instructions field maps to ephemeral_system_prompt."""
        mock_result = {"final_response": "Ahoy!", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "Hello",
                        "instructions": "Talk like a pirate.",
                    },
                )

            assert resp.status == 200
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["ephemeral_system_prompt"] == "Talk like a pirate."

    @pytest.mark.asyncio
    async def test_previous_response_id_chaining(self, adapter):
        """Test that responses can be chained via previous_response_id."""
        mock_result_1 = {
            "final_response": "2",
            "messages": [{"role": "assistant", "content": "2"}],
            "api_calls": 1,
        }

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # First request
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result_1, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp1 = await cli.post(
                    "/v1/responses",
                    json={"model": "hermes-agent", "input": "What is 1+1?"},
                )

            assert resp1.status == 200
            data1 = await resp1.json()
            response_id = data1["id"]

            # Second request chaining from the first
            mock_result_2 = {
                "final_response": "3",
                "messages": [{"role": "assistant", "content": "3"}],
                "api_calls": 1,
            }

            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result_2, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp2 = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "Now add 1 more",
                        "previous_response_id": response_id,
                    },
                )

            assert resp2.status == 200
            # The conversation_history should contain the full history from the first response
            call_kwargs = mock_run.call_args.kwargs
            assert len(call_kwargs["conversation_history"]) > 0
            assert call_kwargs["user_message"] == "Now add 1 more"

    @pytest.mark.asyncio
    async def test_invalid_previous_response_id_returns_404(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/responses",
                json={
                    "model": "hermes-agent",
                    "input": "follow up",
                    "previous_response_id": "resp_nonexistent",
                },
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_store_false_does_not_store(self, adapter):
        """When store=false, the response is NOT stored."""
        mock_result = {"final_response": "OK", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "Hello",
                        "store": False,
                    },
                )

            assert resp.status == 200
            data = await resp.json()
            # The response has an ID but it shouldn't be retrievable
            assert adapter._response_store.get(data["id"]) is None

    @pytest.mark.asyncio
    async def test_instructions_inherited_from_previous(self, adapter):
        """If no instructions provided, carry forward from previous response."""
        mock_result = {"final_response": "Ahoy!", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # First request with instructions
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp1 = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "Hello",
                        "instructions": "Be a pirate",
                    },
                )

            data1 = await resp1.json()
            resp_id = data1["id"]

            # Second request without instructions
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp2 = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "Tell me more",
                        "previous_response_id": resp_id,
                    },
                )

            assert resp2.status == 200
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["ephemeral_system_prompt"] == "Be a pirate"

    @pytest.mark.asyncio
    async def test_agent_error_returns_500(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.side_effect = RuntimeError("Boom")
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "hermes-agent", "input": "Hello"},
                )

            assert resp.status == 500

    @pytest.mark.asyncio
    async def test_invalid_input_type_returns_400(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/responses",
                json={"model": "hermes-agent", "input": 42},
            )
            assert resp.status == 400


# ---------------------------------------------------------------------------
# Auth on endpoints
# ---------------------------------------------------------------------------


class TestEndpointAuth:
    @pytest.mark.asyncio
    async def test_chat_completions_requires_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_responses_requires_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/responses",
                json={"model": "test", "input": "hi"},
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_models_requires_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/models")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_health_does_not_require_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health")
            assert resp.status == 200


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    def test_platform_enum_has_api_server(self):
        assert Platform.API_SERVER.value == "api_server"

    def test_env_override_enables_api_server(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_ENABLED", "true")
        from gateway.config import load_gateway_config
        config = load_gateway_config()
        assert Platform.API_SERVER in config.platforms
        assert config.platforms[Platform.API_SERVER].enabled is True

    def test_env_override_with_key(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_KEY", "sk-mykey")
        from gateway.config import load_gateway_config
        config = load_gateway_config()
        assert Platform.API_SERVER in config.platforms
        assert config.platforms[Platform.API_SERVER].extra.get("key") == "sk-mykey"

    def test_env_override_port_and_host(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_ENABLED", "true")
        monkeypatch.setenv("API_SERVER_PORT", "9999")
        monkeypatch.setenv("API_SERVER_HOST", "0.0.0.0")
        from gateway.config import load_gateway_config
        config = load_gateway_config()
        assert config.platforms[Platform.API_SERVER].extra.get("port") == 9999
        assert config.platforms[Platform.API_SERVER].extra.get("host") == "0.0.0.0"

    def test_env_override_cors_origins(self, monkeypatch):
        monkeypatch.setenv("API_SERVER_ENABLED", "true")
        monkeypatch.setenv(
            "API_SERVER_CORS_ORIGINS",
            "http://localhost:3000, http://127.0.0.1:3000",
        )
        from gateway.config import load_gateway_config
        config = load_gateway_config()
        assert config.platforms[Platform.API_SERVER].extra.get("cors_origins") == [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]

    def test_api_server_in_connected_platforms(self):
        config = GatewayConfig()
        config.platforms[Platform.API_SERVER] = PlatformConfig(enabled=True)
        connected = config.get_connected_platforms()
        assert Platform.API_SERVER in connected

    def test_api_server_not_in_connected_when_disabled(self):
        config = GatewayConfig()
        config.platforms[Platform.API_SERVER] = PlatformConfig(enabled=False)
        connected = config.get_connected_platforms()
        assert Platform.API_SERVER not in connected


# ---------------------------------------------------------------------------
# Multiple system messages
# ---------------------------------------------------------------------------


class TestMultipleSystemMessages:
    @pytest.mark.asyncio
    async def test_multiple_system_messages_concatenated(self, adapter):
        mock_result = {"final_response": "OK", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-agent",
                        "messages": [
                            {"role": "system", "content": "You are helpful."},
                            {"role": "system", "content": "Be concise."},
                            {"role": "user", "content": "Hello"},
                        ],
                    },
                )

            assert resp.status == 200
            call_kwargs = mock_run.call_args.kwargs
            prompt = call_kwargs["ephemeral_system_prompt"]
            assert "You are helpful." in prompt
            assert "Be concise." in prompt


# ---------------------------------------------------------------------------
# send() method (not used but required by base)
# ---------------------------------------------------------------------------


class TestSendMethod:
    @pytest.mark.asyncio
    async def test_send_returns_not_supported(self):
        config = PlatformConfig(enabled=True)
        adapter = APIServerAdapter(config)
        result = await adapter.send("chat1", "hello")
        assert result.success is False
        assert "HTTP request/response" in result.error


# ---------------------------------------------------------------------------
# GET /v1/responses/{response_id}
# ---------------------------------------------------------------------------


class TestGetResponse:
    @pytest.mark.asyncio
    async def test_get_stored_response(self, adapter):
        """GET returns a previously stored response."""
        mock_result = {"final_response": "Hello!", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            # Create a response first
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "hermes-agent", "input": "Hi"},
                )

            assert resp.status == 200
            data = await resp.json()
            response_id = data["id"]

            # Now GET it
            resp2 = await cli.get(f"/v1/responses/{response_id}")
            assert resp2.status == 200
            data2 = await resp2.json()
            assert data2["id"] == response_id
            assert data2["object"] == "response"
            assert data2["status"] == "completed"

    @pytest.mark.asyncio
    async def test_get_not_found(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/responses/resp_nonexistent")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_requires_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/responses/resp_any")
            assert resp.status == 401


# ---------------------------------------------------------------------------
# DELETE /v1/responses/{response_id}
# ---------------------------------------------------------------------------


class TestDeleteResponse:
    @pytest.mark.asyncio
    async def test_delete_stored_response(self, adapter):
        """DELETE removes a stored response and returns confirmation."""
        mock_result = {"final_response": "Hello!", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "hermes-agent", "input": "Hi"},
                )

            data = await resp.json()
            response_id = data["id"]

            # Delete it
            resp2 = await cli.delete(f"/v1/responses/{response_id}")
            assert resp2.status == 200
            data2 = await resp2.json()
            assert data2["id"] == response_id
            assert data2["object"] == "response"
            assert data2["deleted"] is True

            # Verify it's gone
            resp3 = await cli.get(f"/v1/responses/{response_id}")
            assert resp3.status == 404

    @pytest.mark.asyncio
    async def test_delete_not_found(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.delete("/v1/responses/resp_nonexistent")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, auth_adapter):
        app = _create_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.delete("/v1/responses/resp_any")
            assert resp.status == 401


# ---------------------------------------------------------------------------
# Tool calls in output
# ---------------------------------------------------------------------------


class TestToolCallsInOutput:
    @pytest.mark.asyncio
    async def test_tool_calls_in_output(self, adapter):
        """When agent returns tool calls, they appear as function_call items."""
        mock_result = {
            "final_response": "The result is 42.",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression": "6*7"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_abc123",
                    "content": "42",
                },
                {
                    "role": "assistant",
                    "content": "The result is 42.",
                },
            ],
            "api_calls": 2,
        }

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "hermes-agent", "input": "What is 6*7?"},
                )

            assert resp.status == 200
            data = await resp.json()
            output = data["output"]

            # Should have: function_call, function_call_output, message
            assert len(output) == 3
            assert output[0]["type"] == "function_call"
            assert output[0]["name"] == "calculator"
            assert output[0]["arguments"] == '{"expression": "6*7"}'
            assert output[0]["call_id"] == "call_abc123"
            assert output[1]["type"] == "function_call_output"
            assert output[1]["call_id"] == "call_abc123"
            assert output[1]["output"] == "42"
            assert output[2]["type"] == "message"
            assert output[2]["content"][0]["text"] == "The result is 42."

    @pytest.mark.asyncio
    async def test_no_tool_calls_still_works(self, adapter):
        """Without tool calls, output is just a message."""
        mock_result = {"final_response": "Hello!", "messages": [], "api_calls": 1}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "hermes-agent", "input": "Hello"},
                )

            assert resp.status == 200
            data = await resp.json()
            assert len(data["output"]) == 1
            assert data["output"][0]["type"] == "message"


# ---------------------------------------------------------------------------
# Usage / token counting
# ---------------------------------------------------------------------------


class TestUsageCounting:
    @pytest.mark.asyncio
    async def test_responses_usage(self, adapter):
        """Responses API returns real token counts."""
        mock_result = {"final_response": "Done", "messages": [], "api_calls": 1}
        usage = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, usage)
                resp = await cli.post(
                    "/v1/responses",
                    json={"model": "hermes-agent", "input": "Hi"},
                )

            assert resp.status == 200
            data = await resp.json()
            assert data["usage"]["input_tokens"] == 100
            assert data["usage"]["output_tokens"] == 50
            assert data["usage"]["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_chat_completions_usage(self, adapter):
        """Chat completions returns real token counts."""
        mock_result = {"final_response": "Done", "messages": [], "api_calls": 1}
        usage = {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, usage)
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={
                        "model": "hermes-agent",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )

            assert resp.status == 200
            data = await resp.json()
            assert data["usage"]["prompt_tokens"] == 200
            assert data["usage"]["completion_tokens"] == 80
            assert data["usage"]["total_tokens"] == 280


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    @pytest.mark.asyncio
    async def test_truncation_auto_limits_history(self, adapter):
        """With truncation=auto, history over 100 messages is trimmed."""
        mock_result = {"final_response": "OK", "messages": [], "api_calls": 1}

        # Pre-seed a stored response with a long history
        long_history = [{"role": "user", "content": f"msg {i}"} for i in range(150)]
        adapter._response_store.put("resp_prev", {
            "response": {"id": "resp_prev", "object": "response"},
            "conversation_history": long_history,
            "instructions": None,
        })

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "follow up",
                        "previous_response_id": "resp_prev",
                        "truncation": "auto",
                    },
                )

        assert resp.status == 200
        call_kwargs = mock_run.call_args.kwargs
        # History should be truncated to 100
        assert len(call_kwargs["conversation_history"]) <= 100

    @pytest.mark.asyncio
    async def test_no_truncation_keeps_full_history(self, adapter):
        """Without truncation=auto, long history is passed as-is."""
        mock_result = {"final_response": "OK", "messages": [], "api_calls": 1}

        long_history = [{"role": "user", "content": f"msg {i}"} for i in range(150)]
        adapter._response_store.put("resp_prev2", {
            "response": {"id": "resp_prev2", "object": "response"},
            "conversation_history": long_history,
            "instructions": None,
        })

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/responses",
                    json={
                        "model": "hermes-agent",
                        "input": "follow up",
                        "previous_response_id": "resp_prev2",
                    },
                )

        assert resp.status == 200
        call_kwargs = mock_run.call_args.kwargs
        assert len(call_kwargs["conversation_history"]) == 150


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCORS:
    def test_origin_allowed_for_non_browser_client(self, adapter):
        assert adapter._origin_allowed("") is True

    def test_origin_rejected_by_default(self, adapter):
        assert adapter._origin_allowed("http://evil.example") is False

    def test_origin_allowed_for_allowlist_match(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        assert adapter._origin_allowed("http://localhost:3000") is True

    def test_cors_headers_for_origin_disabled_by_default(self, adapter):
        assert adapter._cors_headers_for_origin("http://localhost:3000") is None

    def test_cors_headers_for_origin_matches_allowlist(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        headers = adapter._cors_headers_for_origin("http://localhost:3000")
        assert headers is not None
        assert headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert "POST" in headers["Access-Control-Allow-Methods"]

    def test_cors_headers_for_origin_rejects_unknown_origin(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        assert adapter._cors_headers_for_origin("http://evil.example") is None

    @pytest.mark.asyncio
    async def test_cors_headers_not_present_by_default(self, adapter):
        """CORS is disabled unless explicitly configured."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health")
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") is None

    @pytest.mark.asyncio
    async def test_browser_origin_rejected_by_default(self, adapter):
        """Browser-originated requests are rejected unless explicitly allowed."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health", headers={"Origin": "http://evil.example"})
            assert resp.status == 403
            assert resp.headers.get("Access-Control-Allow-Origin") is None

    @pytest.mark.asyncio
    async def test_cors_options_preflight_rejected_by_default(self, adapter):
        """Browser preflight is rejected unless CORS is explicitly configured."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.options(
                "/v1/chat/completions",
                headers={
                    "Origin": "http://evil.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert resp.status == 403
            assert resp.headers.get("Access-Control-Allow-Origin") is None

    @pytest.mark.asyncio
    async def test_cors_headers_present_for_allowed_origin(self):
        """Allowed origins receive explicit CORS headers."""
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health", headers={"Origin": "http://localhost:3000"})
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"
            assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")
            assert "DELETE" in resp.headers.get("Access-Control-Allow-Methods", "")

    @pytest.mark.asyncio
    async def test_cors_allows_idempotency_key_header(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.options(
                "/v1/chat/completions",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Idempotency-Key",
                },
            )
            assert resp.status == 200
            assert "Idempotency-Key" in resp.headers.get("Access-Control-Allow-Headers", "")

    @pytest.mark.asyncio
    async def test_cors_sets_vary_origin_header(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/health", headers={"Origin": "http://localhost:3000"})
            assert resp.status == 200
            assert resp.headers.get("Vary") == "Origin"

    @pytest.mark.asyncio
    async def test_cors_options_preflight_allowed_for_configured_origin(self):
        """Configured origins can complete browser preflight."""
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.options(
                "/v1/chat/completions",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:3000"
            assert "Authorization" in resp.headers.get("Access-Control-Allow-Headers", "")


    @pytest.mark.asyncio
    async def test_cors_preflight_sets_max_age(self):
        adapter = _make_adapter(cors_origins=["http://localhost:3000"])
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.options(
                "/v1/chat/completions",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
            assert resp.status == 200
            assert resp.headers.get("Access-Control-Max-Age") == "600"
# ---------------------------------------------------------------------------
# Conversation parameter
# ---------------------------------------------------------------------------


class TestConversationParameter:
    @pytest.mark.asyncio
    async def test_conversation_creates_new(self, adapter):
        """First request with a conversation name works (new conversation)."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (
                    {"final_response": "Hello!", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
                resp = await cli.post("/v1/responses", json={
                    "input": "hi",
                    "conversation": "my-chat",
                })
                assert resp.status == 200
                data = await resp.json()
                assert data["status"] == "completed"
                # Conversation mapping should be set
                assert adapter._response_store.get_conversation("my-chat") is not None

    @pytest.mark.asyncio
    async def test_conversation_chains_automatically(self, adapter):
        """Second request with same conversation name chains to first."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (
                    {"final_response": "First response", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
                # First request
                resp1 = await cli.post("/v1/responses", json={
                    "input": "hello",
                    "conversation": "test-conv",
                })
                assert resp1.status == 200
                data1 = await resp1.json()
                resp1_id = data1["id"]

                # Second request — should chain
                mock_run.return_value = (
                    {"final_response": "Second response", "messages": [], "api_calls": 1},
                    {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                )
                resp2 = await cli.post("/v1/responses", json={
                    "input": "follow up",
                    "conversation": "test-conv",
                })
                assert resp2.status == 200

                # The second call should have received conversation history from the first
                assert mock_run.call_count == 2
                second_call_kwargs = mock_run.call_args_list[1]
                history = second_call_kwargs.kwargs.get("conversation_history",
                          second_call_kwargs[1].get("conversation_history", []) if len(second_call_kwargs) > 1 else [])
                # History should be non-empty (contains messages from first response)
                assert len(history) > 0

    @pytest.mark.asyncio
    async def test_conversation_and_previous_response_id_conflict(self, adapter):
        """Cannot use both conversation and previous_response_id."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/v1/responses", json={
                "input": "hi",
                "conversation": "my-chat",
                "previous_response_id": "resp_abc123",
            })
            assert resp.status == 400
            data = await resp.json()
            assert "Cannot use both" in data["error"]["message"]

    @pytest.mark.asyncio
    async def test_separate_conversations_are_isolated(self, adapter):
        """Different conversation names have independent histories."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (
                    {"final_response": "Response A", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
                # Conversation A
                await cli.post("/v1/responses", json={"input": "conv-a msg", "conversation": "conv-a"})
                # Conversation B
                mock_run.return_value = (
                    {"final_response": "Response B", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
                await cli.post("/v1/responses", json={"input": "conv-b msg", "conversation": "conv-b"})

                # They should have different response IDs in the mapping
                assert adapter._response_store.get_conversation("conv-a") != adapter._response_store.get_conversation("conv-b")

    @pytest.mark.asyncio
    async def test_conversation_store_false_no_mapping(self, adapter):
        """If store=false, conversation mapping is not updated."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (
                    {"final_response": "Ephemeral", "messages": [], "api_calls": 1},
                    {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                )
                resp = await cli.post("/v1/responses", json={
                    "input": "hi",
                    "conversation": "ephemeral-chat",
                    "store": False,
                })
                assert resp.status == 200
                # Conversation mapping should NOT be set since store=false
                assert adapter._response_store.get_conversation("ephemeral-chat") is None


# ---------------------------------------------------------------------------
# X-Hermes-Session-Id header (session continuity)
# ---------------------------------------------------------------------------


class TestSessionIdHeader:
    @pytest.mark.asyncio
    async def test_new_session_response_includes_session_id_header(self, adapter):
        """Without X-Hermes-Session-Id, a new session is created and returned in the header."""
        mock_result = {"final_response": "Hello!", "messages": [], "api_calls": 1}
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "Hi"}]},
                )
            assert resp.status == 200
            assert resp.headers.get("X-Hermes-Session-Id") is not None

    @pytest.mark.asyncio
    async def test_provided_session_id_is_used_and_echoed(self, adapter):
        """When X-Hermes-Session-Id is provided, it's passed to the agent and echoed in the response."""
        mock_result = {"final_response": "Continuing!", "messages": [], "api_calls": 1}
        mock_db = MagicMock()
        mock_db.get_messages_as_conversation.return_value = [
            {"role": "user", "content": "previous message"},
            {"role": "assistant", "content": "previous reply"},
        ]
        adapter._session_db = mock_db
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

                resp = await cli.post(
                    "/v1/chat/completions",
                    headers={"X-Hermes-Session-Id": "my-session-123"},
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "Continue"}]},
                )

            assert resp.status == 200
            assert resp.headers.get("X-Hermes-Session-Id") == "my-session-123"
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["session_id"] == "my-session-123"

    @pytest.mark.asyncio
    async def test_provided_session_id_loads_history_from_db(self, adapter):
        """When X-Hermes-Session-Id is provided, history comes from SessionDB not request body."""
        mock_result = {"final_response": "OK", "messages": [], "api_calls": 1}
        db_history = [
            {"role": "user", "content": "stored message 1"},
            {"role": "assistant", "content": "stored reply 1"},
        ]
        mock_db = MagicMock()
        mock_db.get_messages_as_conversation.return_value = db_history
        adapter._session_db = mock_db
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

                resp = await cli.post(
                    "/v1/chat/completions",
                    headers={"X-Hermes-Session-Id": "existing-session"},
                    # Request body has different history — should be ignored
                    json={
                        "model": "hermes-agent",
                        "messages": [
                            {"role": "user", "content": "old msg from client"},
                            {"role": "assistant", "content": "old reply from client"},
                            {"role": "user", "content": "new question"},
                        ],
                    },
                )

            assert resp.status == 200
            call_kwargs = mock_run.call_args.kwargs
            # History must come from DB, not from the request body
            assert call_kwargs["conversation_history"] == db_history
            assert call_kwargs["user_message"] == "new question"

    @pytest.mark.asyncio
    async def test_db_failure_falls_back_to_empty_history(self, adapter):
        """If SessionDB raises, history falls back to empty and request still succeeds."""
        mock_result = {"final_response": "OK", "messages": [], "api_calls": 1}
        # Simulate DB failure: _session_db is None and SessionDB() constructor raises
        adapter._session_db = None
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run, \
                 patch("hermes_state.SessionDB", side_effect=Exception("DB unavailable")):
                mock_run.return_value = (mock_result, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})

                resp = await cli.post(
                    "/v1/chat/completions",
                    headers={"X-Hermes-Session-Id": "some-session"},
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "Hi"}]},
                )

            assert resp.status == 200
            call_kwargs = mock_run.call_args.kwargs
            assert call_kwargs["conversation_history"] == []
            assert call_kwargs["session_id"] == "some-session"


class TestControlPlaneStatusEndpoint:
    @pytest.mark.asyncio
    async def test_api_status_returns_live_tasks_and_cron_summary(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        queued_task = _make_queued_task()
        stale_task = _make_queued_task(task_id="task-stale", priority=80, lane="housekeeping")
        stale_task.queued_at = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task, stale_task]
        control_adapter.foreground_pending_task = MagicMock()
        control_adapter.reprioritize_pending_task = MagicMock()
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)
        runtime_status = {
            "gateway_state": "running",
            "exit_reason": None,
            "updated_at": "2026-04-19T12:00:00+00:00",
            "platforms": {
                "telegram": {
                    "state": "connected",
                    "error_code": None,
                    "error_message": None,
                    "updated_at": "2026-04-19T12:00:00+00:00",
                }
            },
            "live_tasks": _make_live_tasks_payload(
                tasks=[
                    {
                        "task_id": "bg-1",
                        "lane": "cron_scout",
                        "label": "background task",
                        "kind": "background",
                        "control_mode": "managed_runtime",
                        "actions": ["cancel"],
                        "source": "gateway",
                        "started_at": "2026-04-19T12:00:01+00:00",
                    }
                ]
            ),
        }
        monkeypatch.setattr("gateway.status.read_runtime_status", lambda: runtime_status)
        monkeypatch.setattr(APIServerAdapter, "_CRON_AVAILABLE", True)
        monkeypatch.setattr(
            APIServerAdapter,
            "_cron_list",
            staticmethod(
                lambda include_disabled=True: [
                    {"id": "job-1", "deliver": "origin", "enabled": True},
                    {"id": "job-2", "deliver": "local", "lane": "housekeeping", "enabled": True},
                    {"id": "job-3", "deliver": "origin", "enabled": False},
                ]
            ),
        )
        monkeypatch.setattr(
            APIServerAdapter,
            "_cron_get_due",
            staticmethod(lambda: [{"id": "job-2", "deliver": "local", "lane": "housekeeping", "enabled": True}]),
        )
        monkeypatch.setattr(
            APIServerAdapter,
            "_cron_summarize_lanes",
            staticmethod(
                lambda jobs, due_jobs=None: {
                    "active": {"interactive": 1, "cron_scout": 0, "housekeeping": 1},
                    "due": {"interactive": 0, "cron_scout": 0, "housekeeping": 1},
                }
            ),
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/status")
            data = await resp.json()

        assert resp.status == 200
        assert data["gateway_state"] == "running"
        assert data["platforms"]["telegram"]["state"] == "connected"
        assert data["queued_tasks"]["queued_count"] == 2
        assert data["queued_tasks"]["bucket_counts"] == {"now": 1, "next": 0, "later": 1}
        assert data["queued_tasks"]["next_task"]["task_id"] == "task-hi"
        assert data["queued_tasks"]["next_task"]["priority_bucket"] == "now"
        assert data["queued_tasks"]["next_task"]["wait_seconds"] == 300
        assert data["queued_tasks"]["next_task"]["wait_age"] == "5m"
        assert data["queued_tasks"]["oldest_waiting"]["task_id"] == "task-stale"
        assert data["queued_tasks"]["oldest_waiting"]["priority_bucket"] == "later"
        assert data["queued_tasks"]["oldest_waiting"]["wait_seconds"] == 7500
        assert data["queued_tasks"]["oldest_waiting"]["wait_age"] == "2h 5m"
        assert data["queued_tasks"]["starving_bucket"] == {
            "bucket": "later",
            "queued_count": 1,
            "oldest_wait_seconds": 7500,
            "oldest_wait_age": "2h 5m",
            "oldest_task_id": "task-stale",
        }
        assert data["queued_tasks"]["starvation_alert"] == {
            "level": "warning",
            "reason_code": "bucket_wait_threshold_exceeded",
            "reason": "later bucket exceeded starvation threshold",
            "bucket": "later",
            "threshold_seconds": 7200,
            "threshold_age": "2h",
            "current_wait_seconds": 7500,
            "current_wait_age": "2h 5m",
            "oldest_task_id": "task-stale",
            "suggested_action": "reprioritize",
            "suggested_bucket": "next",
            "suggested_command": "/task task-stale recover",
        }
        assert data["queued_tasks"]["tasks"][0]["priority_bucket"] == "now"
        assert data["queued_tasks"]["tasks"][0]["wait_seconds"] == 300
        assert data["queued_tasks"]["tasks"][0]["wait_age"] == "5m"
        assert data["live_tasks"]["tasks"][0]["kind"] == "background"
        assert data["live_tasks"]["tasks"][0]["control_mode"] == "managed_runtime"
        assert data["live_tasks"]["tasks"][0]["running_seconds"] == 299
        assert data["live_tasks"]["tasks"][0]["running_age"] == "4m"
        assert data["cron"]["active_jobs"] == 2
        assert data["cron"]["lane_counts"] == {
            "interactive": 1,
            "cron_scout": 0,
            "housekeeping": 1,
        }
        assert data["cron"]["due_now"] == {
            "interactive": 0,
            "cron_scout": 0,
            "housekeeping": 1,
        }


class TestTasksEndpoint:
    @pytest.mark.asyncio
    async def test_api_tasks_returns_queued_and_live_payload(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        queued_task = _make_queued_task()
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task]
        control_adapter.foreground_pending_task = MagicMock()
        control_adapter.reprioritize_pending_task = MagicMock()
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)

        monkeypatch.setattr(
            "gateway.run._current_live_task_status_payload",
            lambda: _make_live_tasks_payload(
                tasks=[
                    {
                        "task_id": "turn-1",
                        "lane": "interactive",
                        "label": "message turn",
                        "kind": "live_turn",
                        "control_mode": "read_only",
                        "actions": [],
                        "source": "gateway",
                        "started_at": "2026-04-19T12:00:01+00:00",
                    }
                ]
            ),
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/tasks")
            data = await resp.json()

        assert resp.status == 200
        assert data["queued"]["queued_count"] == 1
        queued = data["queued"]["tasks"][0]
        assert queued["task_id"] == "task-hi"
        assert queued["priority_bucket"] == "now"
        assert queued["priority_bucket_options"] == ["now", "next", "later"]
        assert queued["wait_seconds"] == 300
        assert queued["wait_age"] == "5m"
        assert queued["actions"] == ["foreground", "reprioritize", "cancel"]
        assert queued["source"] == "telegram"
        live = data["live"]["tasks"][0]
        assert live["task_id"] == "turn-1"
        assert live["control_mode"] == "read_only"
        assert live["actions"] == []
        assert live["running_seconds"] == 299
        assert live["running_age"] == "4m"

    @pytest.mark.asyncio
    async def test_api_task_detail_returns_queued_task(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        queued_task = _make_queued_task()
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task]
        control_adapter.foreground_pending_task = MagicMock()
        control_adapter.reprioritize_pending_task = MagicMock()
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/tasks/task-hi")
            data = await resp.json()

        assert resp.status == 200
        assert data["state"] == "queued"
        assert data["task"]["priority_bucket"] == "now"
        assert data["task"]["priority_bucket_options"] == ["now", "next", "later"]
        assert data["task"]["wait_seconds"] == 300
        assert data["task"]["wait_age"] == "5m"
        assert data["task"]["actions"] == ["foreground", "reprioritize", "cancel"]

    @pytest.mark.asyncio
    async def test_api_task_detail_returns_active_task(self, adapter, monkeypatch):
        _attach_control_runner(adapter)
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        monkeypatch.setattr(
            "gateway.run._current_live_task_status_payload",
            lambda: _make_live_tasks_payload(
                tasks=[
                    {
                        "task_id": "bg-1",
                        "lane": "cron_scout",
                        "label": "background task",
                        "kind": "background",
                        "control_mode": "managed_runtime",
                        "actions": ["cancel"],
                        "source": "gateway",
                        "started_at": "2026-04-19T12:00:01+00:00",
                    }
                ]
            ),
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/tasks/bg-1")
            data = await resp.json()

        assert resp.status == 200
        assert data["state"] == "active"
        assert data["task"]["kind"] == "background"
        assert data["task"]["control_mode"] == "managed_runtime"
        assert data["task"]["actions"] == ["cancel"]
        assert data["task"]["running_seconds"] == 299
        assert data["task"]["running_age"] == "4m"

    @pytest.mark.asyncio
    async def test_api_task_detail_returns_404_for_missing_task(self, adapter):
        _attach_control_runner(adapter)
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/tasks/missing-task")
            data = await resp.json()

        assert resp.status == 404
        assert data == {"error": "Task not found"}

    @pytest.mark.asyncio
    async def test_api_tasks_foreground_returns_started(self, adapter):
        queued_task = _make_queued_task()
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task]
        control_adapter.foreground_pending_task = MagicMock(return_value=("started", queued_task))
        control_adapter.reprioritize_pending_task = MagicMock()
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/task-hi/foreground")
            data = await resp.json()

        assert resp.status == 200
        assert data["action"] == "foreground"
        assert data["status"] == "started"
        assert data["message"] == "Foregrounded queued task task-hi — starting now."
        assert data["task"]["actions"] == ["foreground", "reprioritize", "cancel"]

    @pytest.mark.asyncio
    async def test_api_tasks_foreground_rejects_active_task(self, adapter, monkeypatch):
        _attach_control_runner(adapter)
        monkeypatch.setattr(
            "gateway.run._current_live_task_status_payload",
            lambda: _make_live_tasks_payload(
                tasks=[
                    {
                        "task_id": "turn-1",
                        "lane": "interactive",
                        "label": "message turn",
                        "kind": "live_turn",
                        "control_mode": "read_only",
                        "actions": [],
                        "source": "gateway",
                        "started_at": "2026-04-19T12:00:01+00:00",
                    }
                ]
            ),
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/turn-1/foreground")
            data = await resp.json()

        assert resp.status == 400
        assert data == {
            "error": "Task is already active — foreground is only supported for queued tasks.",
            "reason_code": "task_already_active",
        }

    @pytest.mark.asyncio
    async def test_api_tasks_reprioritize_updates_queued_task(self, adapter):
        queued_task = _make_queued_task()
        reprioritized_task = _make_queued_task(priority=80)
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task]
        control_adapter.foreground_pending_task = MagicMock()
        control_adapter.reprioritize_pending_task = MagicMock(return_value=reprioritized_task)
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/task-hi/reprioritize", json={"bucket": "later"})
            data = await resp.json()

        assert resp.status == 200
        assert data["action"] == "reprioritize"
        assert data["status"] == "reprioritized"
        assert data["message"] == "Moved queued task task-hi to later priority."
        assert data["task"]["priority"] == 80
        assert data["task"]["priority_bucket"] == "later"
        assert data["task"]["priority_bucket_options"] == ["now", "next", "later"]
        assert data["task"]["actions"] == ["foreground", "reprioritize", "cancel"]

    @pytest.mark.asyncio
    async def test_api_tasks_reprioritize_rejects_invalid_bucket(self, adapter):
        queued_task = _make_queued_task()
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task]
        control_adapter.foreground_pending_task = MagicMock()
        control_adapter.reprioritize_pending_task = MagicMock()
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/task-hi/reprioritize", json={"bucket": "urgent"})
            data = await resp.json()

        assert resp.status == 400
        assert data == {
            "error": "Priority bucket must be one of: now, next, later.",
            "reason_code": "invalid_priority_bucket",
        }

    @pytest.mark.asyncio
    async def test_api_tasks_reprioritize_rejects_active_task(self, adapter, monkeypatch):
        _attach_control_runner(adapter)
        monkeypatch.setattr(
            "gateway.run._current_live_task_status_payload",
            lambda: _make_live_tasks_payload(
                tasks=[
                    {
                        "task_id": "turn-1",
                        "lane": "interactive",
                        "label": "message turn",
                        "kind": "live_turn",
                        "control_mode": "read_only",
                        "actions": [],
                        "source": "gateway",
                        "started_at": "2026-04-19T12:00:01+00:00",
                    }
                ]
            ),
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/turn-1/reprioritize", json={"bucket": "later"})
            data = await resp.json()

        assert resp.status == 400
        assert data == {
            "error": "Task is already active — reprioritize is only supported for queued tasks.",
            "reason_code": "reprioritize_requires_queued_task",
        }

    @pytest.mark.asyncio
    async def test_api_tasks_recover_uses_starvation_recommendation(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        queued_task = _make_queued_task(task_id="task-stale", priority=80, lane="housekeeping")
        queued_task.queued_at = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
        recovered_task = _make_queued_task(task_id="task-stale", priority=50, lane="housekeeping")
        recovered_task.queued_at = queued_task.queued_at
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task]
        control_adapter.foreground_pending_task = MagicMock()
        control_adapter.reprioritize_pending_task = MagicMock(return_value=recovered_task)
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/task-stale/recover")
            data = await resp.json()

        assert resp.status == 200
        assert data["action"] == "recover"
        assert data["status"] == "recovered"
        assert data["message"] == "Recovered queued task task-stale — moved it to next priority."
        control_adapter.reprioritize_pending_task.assert_called_once_with("telegram:user:123", "task-stale", "next")
        assert data["task"]["priority"] == 50
        assert data["task"]["priority_bucket"] == "next"

    @pytest.mark.asyncio
    async def test_api_tasks_recover_rejects_non_starving_queued_task(self, adapter, monkeypatch):
        monkeypatch.setattr(
            "gateway.task_control._queued_task_now",
            lambda: datetime(2026, 4, 19, 12, 5, 0, tzinfo=timezone.utc),
        )
        queued_task = _make_queued_task()
        control_adapter = MagicMock()
        control_adapter.all_pending_tasks_snapshot.return_value = [queued_task]
        control_adapter.foreground_pending_task = MagicMock()
        control_adapter.reprioritize_pending_task = MagicMock()
        control_adapter.cancel_pending_task = MagicMock()
        _attach_control_runner(adapter, control_adapter)

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/task-hi/recover")
            data = await resp.json()

        assert resp.status == 400
        assert data == {
            "error": "Task doesn't currently have a recovery recommendation.",
            "reason_code": "recover_not_recommended",
        }

    @pytest.mark.asyncio
    async def test_api_tasks_cancel_returns_cancellation_requested_for_managed_runtime_task(self, adapter, monkeypatch):
        runner = _attach_control_runner(adapter, cancel_runtime_result=True)
        monkeypatch.setattr(
            "gateway.run._current_live_task_status_payload",
            lambda: _make_live_tasks_payload(
                tasks=[
                    {
                        "task_id": "bg-1",
                        "lane": "cron_scout",
                        "label": "background task",
                        "kind": "background",
                        "control_mode": "managed_runtime",
                        "actions": ["cancel"],
                        "source": "gateway",
                        "started_at": "2026-04-19T12:00:01+00:00",
                    }
                ]
            ),
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/bg-1/cancel")
            data = await resp.json()

        assert resp.status == 200
        assert data["action"] == "cancel"
        assert data["status"] == "cancellation_requested"
        assert data["message"] == "Cancellation requested for active task bg-1."
        runner._cancel_managed_runtime_task.assert_called_once_with("bg-1")

    @pytest.mark.asyncio
    async def test_api_tasks_cancel_rejects_live_task_without_handle(self, adapter, monkeypatch):
        _attach_control_runner(adapter, cancel_runtime_result=False)
        monkeypatch.setattr(
            "gateway.run._current_live_task_status_payload",
            lambda: _make_live_tasks_payload(
                tasks=[
                    {
                        "task_id": "turn-1",
                        "lane": "interactive",
                        "label": "message turn",
                        "kind": "live_turn",
                        "control_mode": "read_only",
                        "actions": [],
                        "source": "gateway",
                        "started_at": "2026-04-19T12:00:01+00:00",
                    }
                ]
            ),
        )

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/turn-1/cancel")
            data = await resp.json()

        assert resp.status == 400
        assert data == {
            "error": "Task is active but doesn't expose a cancel handle yet.",
            "reason_code": "cancel_handle_missing",
        }

    @pytest.mark.asyncio
    async def test_api_tasks_cancel_missing_task_returns_404(self, adapter):
        _attach_control_runner(adapter)
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/tasks/missing-task/cancel")
            data = await resp.json()

        assert resp.status == 404
        assert data == {
            "error": "Task not found",
            "reason_code": "task_not_found",
        }


class TestOpenAPIEndpoint:
    @pytest.mark.asyncio
    async def test_openapi_json_exposes_task_control_contracts(self, adapter):
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/openapi.json")
            data = await resp.json()

        assert resp.status == 200
        assert "/api/status" in data["paths"]
        assert "/api/tasks" in data["paths"]
        assert "/api/tasks/{task_id}" in data["paths"]
        assert "/api/tasks/{task_id}/foreground" in data["paths"]
        assert "/api/tasks/{task_id}/reprioritize" in data["paths"]
        assert "/api/tasks/{task_id}/recover" in data["paths"]
        assert "/api/tasks/{task_id}/cancel" in data["paths"]
        assert data["components"]["schemas"]["GatewayStatus"]["properties"]["queued_tasks"]["$ref"] == "#/components/schemas/QueuedTaskStatus"
        queued_status_schema = data["components"]["schemas"]["QueuedTaskStatus"]
        assert queued_status_schema["properties"]["next_task"]["anyOf"][0]["$ref"] == "#/components/schemas/QueuedTask"
        assert queued_status_schema["properties"]["oldest_waiting"]["anyOf"][0]["$ref"] == "#/components/schemas/QueuedTask"
        assert queued_status_schema["properties"]["starving_bucket"]["anyOf"][0]["$ref"] == "#/components/schemas/QueuedBucketStarvation"
        assert queued_status_schema["properties"]["starvation_alert"]["anyOf"][0]["$ref"] == "#/components/schemas/QueuedStarvationAlert"
        starvation_alert_schema = data["components"]["schemas"]["QueuedStarvationAlert"]
        assert set(starvation_alert_schema["properties"]["suggested_action"]["enum"]) == {"foreground", "reprioritize"}
        assert starvation_alert_schema["properties"]["suggested_bucket"]["anyOf"][0]["enum"] == ["now", "next", "later"]
        assert starvation_alert_schema["properties"]["suggested_command"]["type"] == "string"
        assert set(data["paths"]["/api/tasks/{task_id}/reprioritize"]["post"]["responses"].keys()) == {"200", "400", "401", "404", "500"}
        assert set(data["paths"]["/api/tasks/{task_id}/recover"]["post"]["responses"].keys()) == {"200", "400", "401", "404", "500"}
        queued_task_schema = data["components"]["schemas"]["QueuedTask"]
        assert queued_task_schema["properties"]["priority_bucket"]["enum"] == ["now", "next", "later"]
        assert queued_task_schema["properties"]["priority_bucket_options"]["items"]["enum"] == ["now", "next", "later"]
        assert queued_task_schema["properties"]["wait_seconds"]["type"] == "integer"
        assert queued_task_schema["properties"]["wait_age"]["type"] == "string"
        assert queued_task_schema["properties"]["actions"]["items"]["enum"] == ["foreground", "reprioritize", "cancel"]
        assert queued_task_schema["properties"]["starvation_alert"]["anyOf"][0]["$ref"] == "#/components/schemas/QueuedStarvationAlert"
        live_task_schema = data["components"]["schemas"]["LiveTask"]
        assert live_task_schema["properties"]["running_seconds"]["type"] == "integer"
        assert live_task_schema["properties"]["running_age"]["type"] == "string"
        task_action_schema = data["components"]["schemas"]["TaskActionResponse"]
        assert task_action_schema["properties"]["action"]["enum"] == ["foreground", "reprioritize", "recover", "cancel"]
        assert task_action_schema["properties"]["status"]["enum"] == [
            "started",
            "queued_next",
            "reprioritized",
            "recovered",
            "cancelled",
            "cancellation_requested",
        ]
        assert "TaskActionErrorResponse" in data["components"]["schemas"]


class TestOpenAPIDocsParity:
    @pytest.mark.asyncio
    async def test_task_control_docs_examples_match_openapi(self, adapter):
        openapi = adapter._openapi_contract()
        docs = _read_api_server_docs()

        get_status_example = _markdown_json_blocks(docs, "### GET /api/status")[0]
        get_tasks_example = _markdown_json_blocks(docs, "### GET /api/tasks")[0]
        get_task_example = _markdown_json_blocks(docs, "#### GET /api/tasks/{task_id}")[0]
        reprioritize_request, reprioritize_response = _markdown_json_blocks(
            docs,
            "#### POST /api/tasks/{task_id}/reprioritize",
        )
        recover_response = _markdown_json_blocks(docs, "#### POST /api/tasks/{task_id}/recover")[0]
        openapi_snippet = _markdown_json_blocks(docs, "### GET /openapi.json")[0]

        _assert_matches_openapi_schema(openapi, "GatewayStatus", get_status_example)
        _assert_matches_openapi_schema(openapi, "GatewayTasksResponse", get_tasks_example)
        _assert_matches_openapi_schema(openapi, "TaskDetailResponse", get_task_example)
        _assert_matches_openapi_schema(openapi, "TaskReprioritizeRequest", reprioritize_request)
        _assert_matches_openapi_schema(openapi, "TaskActionResponse", reprioritize_response)
        _assert_matches_openapi_schema(openapi, "TaskActionResponse", recover_response)

        assert "/api/tasks/{task_id}/reprioritize" in openapi_snippet["paths"]
        assert "/api/tasks/{task_id}/recover" in openapi_snippet["paths"]
        assert "TaskActionErrorResponse" in openapi_snippet["components"]["schemas"]
        queued_task_props = openapi_snippet["components"]["schemas"]["QueuedTask"]["properties"]
        assert "priority_bucket" in queued_task_props
        assert "priority_bucket_options" in queued_task_props
        assert "starvation_alert" in queued_task_props
