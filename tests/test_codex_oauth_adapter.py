import importlib.util
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient


def load_adapter_module(monkeypatch=None):
    repo_root = Path(__file__).parent.parent
    module_path = repo_root / "experimental" / "codex_oauth_adapter.py"
    temp_root = Path(tempfile.mkdtemp())
    auth_dir = temp_root / "auth"
    auth_dir.mkdir()
    config_path = temp_root / "config.yaml"
    config_path.write_text(
        "custom_providers:\n"
        "- name: gpt-mainline-codex-local\n"
        "  api_key: adapter-test-key\n"
    )
    env = {
        "CODEX_ADAPTER_AUTH_DIR": str(auth_dir),
        "CODEX_ADAPTER_STATE_PATH": str(temp_root / "state.json"),
        "CODEX_ADAPTER_AFFINITY_PATH": str(temp_root / "affinity.json"),
        "CODEX_ADAPTER_KEYS_PATH": str(temp_root / "keys.json"),
        "CODEX_ADAPTER_CONFIG_PATH": str(config_path),
    }
    if monkeypatch is not None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    else:
        for key, value in env.items():
            os.environ[key] = value
    spec = importlib.util.spec_from_file_location("codex_oauth_adapter", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_retry_policy_retries_timeout_without_http_status():
    adapter = load_adapter_module()

    assert adapter._should_retry_upstream_error(None, "Request timed out") is True


def test_retry_policy_does_not_retry_non_retryable_4xx():
    adapter = load_adapter_module()

    assert adapter._should_retry_upstream_error(400, "bad request") is False


def test_retry_policy_retries_5xx_and_rate_limit_statuses():
    adapter = load_adapter_module()

    assert adapter._should_retry_upstream_error(500, "server error") is True
    assert adapter._should_retry_upstream_error(429, "rate limit") is True
    assert adapter._should_retry_upstream_error(401, "expired token") is True


def test_empty_output_is_treated_as_retryable_failure():
    adapter = load_adapter_module()

    assert adapter._should_retry_upstream_error(None, "upstream returned empty text") is True


def test_upstream_request_does_not_forward_unsupported_max_output_tokens(monkeypatch):
    adapter = load_adapter_module()
    captured = {}

    class FakeStream:
        def __iter__(self):
            yield type("Event", (), {"type": "response.output_text.delta", "delta": "OK"})()

        def close(self):
            pass

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

        responses = FakeResponses()

    monkeypatch.setattr(adapter, "OpenAI", FakeOpenAI)
    account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )

    result = adapter._run_upstream_completion_sync(
        account,
        [{"role": "user", "content": "Say OK"}],
        "gpt-5.5",
        64,
    )

    assert result["choices"][0]["message"]["content"] == "OK"
    assert "max_output_tokens" not in captured
    assert captured["model"] == "gpt-5.5"
    assert result["model"] == "gpt-5.5"


def test_streaming_chat_completion_returns_sse_visible_content(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}

    class FakeStream:
        def __iter__(self):
            yield type("Event", (), {"type": "response.output_text.delta", "delta": "OK"})()

        def close(self):
            pass

    class FakeResponses:
        def create(self, **kwargs):
            return FakeStream()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

        responses = FakeResponses()

    fake_account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )

    monkeypatch.setattr(adapter, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(adapter.pool, "health_summary", lambda: {"total": 1})
    monkeypatch.setattr(adapter.pool, "acquire_account", lambda preferred_path=None: fake_account)
    monkeypatch.setattr(adapter.pool, "release_account", lambda account: None)
    monkeypatch.setattr(adapter.pool, "ensure_fresh", lambda account: account)
    monkeypatch.setattr(adapter.pool, "mark_success", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_failure", lambda account, status_code, message: None)

    client = TestClient(adapter.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Say OK"}],
            "stream": True,
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"content":"OK"' in response.text
    assert "data: [DONE]" in response.text


def test_streaming_chat_completion_preserves_upstream_deltas(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}

    completed_response = type(
        "CompletedResponse",
        (),
        {
            "usage": type(
                "Usage",
                (),
                {"input_tokens": 11, "output_tokens": 2, "total_tokens": 13},
            )(),
            "output_text": "Hello",
        },
    )()

    class FakeStream:
        def __iter__(self):
            yield type("Event", (), {"type": "response.output_text.delta", "delta": "Hel"})()
            yield type("Event", (), {"type": "response.output_text.delta", "delta": "lo"})()
            yield type("Event", (), {"type": "response.completed", "response": completed_response})()

        def close(self):
            pass

    class FakeResponses:
        def create(self, **kwargs):
            return FakeStream()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            pass

        responses = FakeResponses()

    fake_account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )

    monkeypatch.setattr(adapter, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(adapter.pool, "health_summary", lambda: {"total": 1})
    monkeypatch.setattr(adapter.pool, "acquire_account", lambda preferred_path=None: fake_account)
    monkeypatch.setattr(adapter.pool, "release_account", lambda account: None)
    monkeypatch.setattr(adapter.pool, "ensure_fresh", lambda account: account)
    monkeypatch.setattr(adapter.pool, "mark_success", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_failure", lambda account, status_code, message: None)

    client = TestClient(adapter.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Say Hello"}],
            "stream": True,
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert '"content":"Hel"' in response.text
    assert '"content":"lo"' in response.text
    assert '"total_tokens":13' in response.text


def test_chat_completion_uses_acquire_and_release_for_non_stream_response(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}
    fake_account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )
    released = []

    monkeypatch.setattr(adapter.pool, "health_summary", lambda: {"total": 1})
    monkeypatch.setattr(adapter.pool, "acquire_account", lambda preferred_path=None: fake_account)
    monkeypatch.setattr(adapter.pool, "release_account", lambda account: released.append(account))
    monkeypatch.setattr(adapter.pool, "next_account", lambda: (_ for _ in ()).throw(AssertionError("next_account should not be used")))
    monkeypatch.setattr(adapter.pool, "ensure_fresh", lambda account: account)
    monkeypatch.setattr(adapter.pool, "mark_success", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_failure", lambda account, status_code, message: None)
    monkeypatch.setattr(adapter, "_run_upstream_completion_sync", lambda *args: {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-5.5",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    })

    client = TestClient(adapter.app)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Say OK"}],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert released == [fake_account]


def test_responses_api_proxies_json_payload(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            yield 'data: {"type":"response.created","response":{"id":"resp_123","status":"in_progress"}}'
            yield 'data: {"type":"response.completed","response":{"id":"resp_123","object":"response","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"OK"}]}]}}'
            yield "data: [DONE]"

        async def aread(self):
            return b'{"id":"resp_123"}'

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json

            class _Ctx:
                async def __aenter__(self_inner):
                    return FakeResponse()

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return _Ctx()

    fake_account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )

    monkeypatch.setattr(adapter.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(adapter.pool, "health_summary", lambda: {"total": 1})
    monkeypatch.setattr(adapter.pool, "acquire_account", lambda preferred_path=None: fake_account)
    monkeypatch.setattr(adapter.pool, "release_account", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_success", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_failure", lambda account, status_code, message: None)

    client = TestClient(adapter.app)
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.5",
            "instructions": "You are a helpful assistant.",
            "input": [{"role": "user", "content": "Say OK"}],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["id"] == "resp_123"
    assert captured["url"].endswith("/responses")
    assert captured["json"]["model"] == "gpt-5.5"


def test_responses_api_supplies_default_instructions_when_missing(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            yield 'data: {"type":"response.completed","response":{"id":"resp_123","object":"response","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"OK"}]}]}}'
            yield "data: [DONE]"

        async def aread(self):
            return b"{}"

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured["json"] = json

            class _Ctx:
                async def __aenter__(self_inner):
                    return FakeResponse()

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return _Ctx()

    fake_account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )

    monkeypatch.setattr(adapter.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(adapter.pool, "health_summary", lambda: {"total": 1})
    monkeypatch.setattr(adapter.pool, "acquire_account", lambda preferred_path=None: fake_account)
    monkeypatch.setattr(adapter.pool, "release_account", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_success", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_failure", lambda account, status_code, message: None)

    client = TestClient(adapter.app)
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.5",
            "input": [{"role": "user", "content": "Say OK"}],
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert captured["json"]["instructions"] == "You are a helpful assistant."


def test_responses_api_maps_upstream_runtime_error_to_502(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}

    async def fail_upstream(body):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(adapter, "_responses_json_with_retries", fail_upstream)

    client = TestClient(adapter.app, raise_server_exceptions=False)
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.5",
            "input": [{"role": "user", "content": "Say OK"}],
        },
        headers=headers,
    )

    assert response.status_code == 502
    assert "upstream exploded" in response.json()["detail"]


def test_responses_api_proxies_streaming_bytes(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}

    class FakeStreamResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self):
            yield b'data: {"type":"response.output_text.delta","delta":"Hel"}\n\n'
            yield b'data: {"type":"response.output_text.delta","delta":"lo"}\n\n'
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            return None

    class FakeStreamContext:
        def __init__(self):
            self.response = FakeStreamResponse()

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, json=None):
            return FakeStreamContext()

    fake_account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )

    monkeypatch.setattr(adapter.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(adapter.pool, "health_summary", lambda: {"total": 1})
    monkeypatch.setattr(adapter.pool, "acquire_account", lambda preferred_path=None: fake_account)
    monkeypatch.setattr(adapter.pool, "release_account", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_success", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_failure", lambda account, status_code, message: None)

    client = TestClient(adapter.app)
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.5",
            "instructions": "You are a helpful assistant.",
            "input": [{"role": "user", "content": "Say Hello"}],
            "stream": True,
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'delta":"Hel"' in response.text
    assert 'delta":"lo"' in response.text
    assert "data: [DONE]" in response.text


def test_responses_api_json_backfills_empty_completed_output(monkeypatch):
    adapter = load_adapter_module()
    headers = {"Authorization": f"Bearer {adapter.key_store.primary_key()}"}

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_lines(self):
            yield 'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"OK"}]}}'
            yield 'data: {"type":"response.completed","response":{"id":"resp_456","object":"response","status":"completed","output":[]}}'
            yield "data: [DONE]"

        async def aread(self):
            return b""

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, headers=None, json=None):
            class _Ctx:
                async def __aenter__(self_inner):
                    return FakeResponse()

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return _Ctx()

    fake_account = adapter.CodexAccount(
        path=adapter.Path("/tmp/codex-test.json"),
        email="test@example.com",
        access_token="token",
        refresh_token="refresh",
        id_token="id",
        account_id="acct",
        expired="",
        last_refresh="",
        disabled=False,
        type="codex",
    )

    monkeypatch.setattr(adapter.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(adapter.pool, "health_summary", lambda: {"total": 1})
    monkeypatch.setattr(adapter.pool, "acquire_account", lambda preferred_path=None: fake_account)
    monkeypatch.setattr(adapter.pool, "release_account", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_success", lambda account: None)
    monkeypatch.setattr(adapter.pool, "mark_failure", lambda account, status_code, message: None)

    client = TestClient(adapter.app)
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.5",
            "instructions": "You are a helpful assistant.",
            "input": [{"role": "user", "content": "Say OK"}],
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == "resp_456"
    assert payload["output"][0]["content"][0]["text"] == "OK"


def test_internal_key_admin_can_create_and_list_keys():
    adapter = load_adapter_module()
    client = TestClient(adapter.app)
    admin_headers = {"Authorization": f"Bearer {adapter.key_store.admin_key()}"}
    label = f"child-agent-{secrets.token_hex(4)}"

    create_response = client.post(
        "/v1/internal/keys",
        json={"label": label},
        headers=admin_headers,
    )
    assert create_response.status_code == 200
    key_payload = create_response.json()
    assert key_payload["label"] == label
    assert key_payload["api_key"].startswith("sk-hermes-codex-")

    list_response = client.get("/v1/internal/keys", headers=admin_headers)
    assert list_response.status_code == 200
    labels = {item["label"] for item in list_response.json()["keys"]}
    assert "primary" in labels
    assert label in labels


def test_internal_key_create_round_trips_audit_metadata():
    adapter = load_adapter_module()
    client = TestClient(adapter.app)
    admin_headers = {"Authorization": f"Bearer {adapter.key_store.admin_key()}"}
    label = f"child-agent-{secrets.token_hex(4)}"
    expires_at = 4_102_444_800

    create_response = client.post(
        "/v1/internal/keys",
        json={
            "label": label,
            "created_by": "delegate_task",
            "purpose": "child-agent",
            "session_id": "session-123",
            "task_id": "task-0",
            "expires_at": expires_at,
        },
        headers=admin_headers,
    )
    assert create_response.status_code == 200

    list_response = client.get("/v1/internal/keys", headers=admin_headers)
    key_entry = next(item for item in list_response.json()["keys"] if item["label"] == label)
    assert key_entry["created_by"] == "delegate_task"
    assert key_entry["purpose"] == "child-agent"
    assert key_entry["session_id"] == "session-123"
    assert key_entry["task_id"] == "task-0"
    assert key_entry["expires_at"] == expires_at
    assert key_entry["expired"] is False


def test_expired_internal_key_is_rejected_for_inference():
    adapter = load_adapter_module()
    client = TestClient(adapter.app)
    admin_headers = {"Authorization": f"Bearer {adapter.key_store.admin_key()}"}
    label = f"expired-child-{secrets.token_hex(4)}"

    create_response = client.post(
        "/v1/internal/keys",
        json={"label": label, "expires_at": 1},
        headers=admin_headers,
    )
    assert create_response.status_code == 200
    key_payload = create_response.json()

    response = client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {key_payload['api_key']}"},
    )

    assert response.status_code == 401


def test_inference_key_auth_updates_last_used_at():
    adapter = load_adapter_module()
    client = TestClient(adapter.app)
    admin_headers = {"Authorization": f"Bearer {adapter.key_store.admin_key()}"}
    label = f"active-child-{secrets.token_hex(4)}"

    create_response = client.post(
        "/v1/internal/keys",
        json={"label": label},
        headers=admin_headers,
    )
    key_payload = create_response.json()

    response = client.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {key_payload['api_key']}"},
    )

    assert response.status_code == 200
    key_entry = next(item for item in adapter.key_store.list_keys() if item["label"] == label)
    assert isinstance(key_entry["last_used_at"], int)
    assert key_entry["last_used_at"] > 0


def test_account_pool_lease_spreads_parallel_selections():
    adapter = load_adapter_module()
    with tempfile.TemporaryDirectory() as tmp:
        auth_dir = Path(tmp) / "auth"
        auth_dir.mkdir()
        state_path = Path(tmp) / "state.json"

        base = {
            "access_token": "token",
            "refresh_token": "refresh",
            "id_token": "id",
            "account_id": "acct",
            "expired": "",
            "last_refresh": "",
            "disabled": False,
            "type": "codex",
        }

        (auth_dir / "codex-a.json").write_text(
            json.dumps({**base, "email": "a@example.com", "access_token": "token-a"})
        )
        (auth_dir / "codex-b.json").write_text(
            json.dumps({**base, "email": "b@example.com", "access_token": "token-b"})
        )

        pool = adapter.AccountPool(auth_dir, state_path)
        first = pool.acquire_account()
        second = pool.acquire_account()

        assert first.path != second.path

        pool.release_account(first)
        pool.release_account(second)


def test_account_pool_next_account_uses_persisted_lease():
    adapter = load_adapter_module()
    with tempfile.TemporaryDirectory() as tmp:
        auth_dir = Path(tmp) / "auth"
        auth_dir.mkdir()
        state_path = Path(tmp) / "state.json"
        account_path = auth_dir / "codex-a.json"
        account_path.write_text(json.dumps({
            "access_token": "token-a",
            "refresh_token": "refresh",
            "id_token": "id",
            "account_id": "acct-a",
            "email": "a@example.com",
            "expired": "",
            "last_refresh": "",
            "disabled": False,
            "type": "codex",
        }))

        pool = adapter.AccountPool(auth_dir, state_path)
        account = pool.next_account()

        state = json.loads(state_path.read_text())
        assert len(state[str(account_path)]["active_leases"]) == 1

        pool.release_account(account)


def test_account_pool_persists_leases_across_pool_instances(monkeypatch):
    adapter = load_adapter_module()
    monkeypatch.setattr(adapter, "MAX_CONCURRENT_PER_ACCOUNT", 1)
    with tempfile.TemporaryDirectory() as tmp:
        auth_dir = Path(tmp) / "auth"
        auth_dir.mkdir()
        state_path = Path(tmp) / "state.json"

        base = {
            "access_token": "token",
            "refresh_token": "refresh",
            "id_token": "id",
            "account_id": "acct",
            "expired": "",
            "last_refresh": "",
            "disabled": False,
            "type": "codex",
        }
        (auth_dir / "codex-a.json").write_text(
            json.dumps({**base, "email": "a@example.com", "access_token": "token-a"})
        )
        (auth_dir / "codex-b.json").write_text(
            json.dumps({**base, "email": "b@example.com", "access_token": "token-b"})
        )

        first_pool = adapter.AccountPool(auth_dir, state_path)
        second_pool = adapter.AccountPool(auth_dir, state_path)

        first = first_pool.acquire_account()
        second = second_pool.acquire_account(preferred_path=str(first.path))

        assert second.path != first.path

        first_pool.release_account(first)
        second_pool.release_account(second)


def test_account_pool_ignores_stale_persisted_leases(monkeypatch):
    adapter = load_adapter_module()
    monkeypatch.setattr(adapter, "MAX_CONCURRENT_PER_ACCOUNT", 1)
    monkeypatch.setattr(adapter, "ACCOUNT_LEASE_TTL_SECONDS", 60)
    with tempfile.TemporaryDirectory() as tmp:
        auth_dir = Path(tmp) / "auth"
        auth_dir.mkdir()
        state_path = Path(tmp) / "state.json"

        account_path = auth_dir / "codex-a.json"
        account_path.write_text(json.dumps({
            "access_token": "token-a",
            "refresh_token": "refresh",
            "id_token": "id",
            "account_id": "acct-a",
            "email": "a@example.com",
            "expired": "",
            "last_refresh": "",
            "disabled": False,
            "type": "codex",
        }))
        state_path.write_text(json.dumps({
            str(account_path): {
                "cooldown_until": 0.0,
                "last_used_at": 1.0,
                "active_leases": {
                    "dead-process": 100.0,
                },
            }
        }))
        monkeypatch.setattr(adapter.time, "time", lambda: 1_000.0)

        pool = adapter.AccountPool(auth_dir, state_path)
        account = pool.acquire_account(preferred_path=str(account_path))

        assert account.path == account_path
        state = json.loads(state_path.read_text())
        leases = state[str(account_path)]["active_leases"]
        assert "dead-process" not in leases
        assert len(leases) == 1

        pool.release_account(account)


def test_account_pool_mark_success_preserves_other_process_leases(monkeypatch):
    adapter = load_adapter_module()
    monkeypatch.setattr(adapter, "MAX_CONCURRENT_PER_ACCOUNT", 2)
    with tempfile.TemporaryDirectory() as tmp:
        auth_dir = Path(tmp) / "auth"
        auth_dir.mkdir()
        state_path = Path(tmp) / "state.json"
        account_path = auth_dir / "codex-a.json"
        account_path.write_text(json.dumps({
            "access_token": "token-a",
            "refresh_token": "refresh",
            "id_token": "id",
            "account_id": "acct-a",
            "email": "a@example.com",
            "expired": "",
            "last_refresh": "",
            "disabled": False,
            "type": "codex",
        }))

        first_pool = adapter.AccountPool(auth_dir, state_path)
        second_pool = adapter.AccountPool(auth_dir, state_path)
        first = first_pool.acquire_account()
        second = second_pool.acquire_account()

        assert len(json.loads(state_path.read_text())[str(account_path)]["active_leases"]) == 2

        first_pool.mark_success(first)

        state = json.loads(state_path.read_text())
        assert len(state[str(account_path)]["active_leases"]) == 2

        first_pool.release_account(first)
        second_pool.release_account(second)
