---
sidebar_position: 14
title: "API Server"
description: "Expose hermes-agent as an OpenAI-compatible API for any frontend"
---

# API Server

The API server exposes hermes-agent as an OpenAI-compatible HTTP endpoint. Any frontend that speaks the OpenAI format — Open WebUI, LobeChat, LibreChat, NextChat, ChatBox, and hundreds more — can connect to hermes-agent and use it as a backend.

Your agent handles requests with its full toolset (terminal, file operations, web search, memory, skills) and returns the final response. When streaming, tool progress indicators appear inline so frontends can show what the agent is doing.

## Quick Start

### 1. Enable the API server

Add to `~/.hermes/.env`:

```bash
API_SERVER_ENABLED=true
API_SERVER_KEY=change-me-local-dev
# Optional: only if a browser must call Hermes directly
# API_SERVER_CORS_ORIGINS=http://localhost:3000
```

### 2. Start the gateway

```bash
hermes gateway
```

You'll see:

```
[API Server] API server listening on http://127.0.0.1:8642
```

### 3. Connect a frontend

Point any OpenAI-compatible client at `http://localhost:8642/v1`:

```bash
# Test with curl
curl http://localhost:8642/v1/chat/completions \
  -H "Authorization: Bearer change...dev" \
  -H "Content-Type: application/json" \
  -d '{"model": "hermes-agent", "messages": [{"role": "user", "content": "Hello!"}]}'
```

Or connect Open WebUI, LobeChat, or any other frontend — see the [Open WebUI integration guide](/docs/user-guide/messaging/open-webui) for step-by-step instructions.

## Endpoints

### POST /v1/chat/completions

Standard OpenAI Chat Completions format. Stateless — the full conversation is included in each request via the `messages` array.

**Request:**
```json
{
  "model": "hermes-agent",
  "messages": [
    {"role": "system", "content": "You are a Python expert."},
    {"role": "user", "content": "Write a fibonacci function"}
  ],
  "stream": false
}
```

**Response:**
```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "hermes-agent",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "Here's a fibonacci function..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 50, "completion_tokens": 200, "total_tokens": 250}
}
```

**Streaming** (`"stream": true`): Returns Server-Sent Events (SSE) with token-by-token response chunks. When streaming is enabled in config, tokens are emitted live as the LLM generates them. When disabled, the full response is sent as a single SSE chunk.

**Tool progress in streams**: When the agent calls tools during a streaming request, brief progress indicators are injected into the content stream as the tools start executing (e.g. `` `💻 pwd` ``, `` `🔍 Python docs` ``). These appear as inline markdown before the agent's response text, giving frontends like Open WebUI real-time visibility into tool execution.

### POST /v1/responses

OpenAI Responses API format. Supports server-side conversation state via `previous_response_id` — the server stores full conversation history (including tool calls and results) so multi-turn context is preserved without the client managing it.

**Request:**
```json
{
  "model": "hermes-agent",
  "input": "What files are in my project?",
  "instructions": "You are a helpful coding assistant.",
  "store": true
}
```

**Response:**
```json
{
  "id": "resp_abc123",
  "object": "response",
  "status": "completed",
  "model": "hermes-agent",
  "output": [
    {"type": "function_call", "name": "terminal", "arguments": "{\"command\": \"ls\"}", "call_id": "call_1"},
    {"type": "function_call_output", "call_id": "call_1", "output": "README.md src/ tests/"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Your project has..."}]}
  ],
  "usage": {"input_tokens": 50, "output_tokens": 200, "total_tokens": 250}
}
```

#### Multi-turn with previous_response_id

Chain responses to maintain full context (including tool calls) across turns:

```json
{
  "input": "Now show me the README",
  "previous_response_id": "resp_abc123"
}
```

The server reconstructs the full conversation from the stored response chain — all previous tool calls and results are preserved.

#### Named conversations

Use the `conversation` parameter instead of tracking response IDs:

```json
{"input": "Hello", "conversation": "my-project"}
{"input": "What's in src/?", "conversation": "my-project"}
{"input": "Run the tests", "conversation": "my-project"}
```

The server automatically chains to the latest response in that conversation. Like the `/title` command for gateway sessions.

### GET /v1/responses/\{id\}

Retrieve a previously stored response by ID.

### DELETE /v1/responses/\{id\}

Delete a stored response.

### GET /v1/models

Lists `hermes-agent` as an available model. Required by most frontends for model discovery.

### GET /health

Health check. Returns `{"status": "ok"}`. Also available at **GET /v1/health** for OpenAI-compatible clients that expect the `/v1/` prefix.

### GET /api/status

Machine-readable gateway status for dashboards and automation. This is separate from `/health`: `/health` stays minimal for liveness checks, while `/api/status` exposes the current runtime state, live task lanes, and cron backlog summary.

**Response:**
```json
{
  "gateway_state": "running",
  "exit_reason": null,
  "updated_at": "2026-04-19T12:00:00+00:00",
  "platforms": {
    "telegram": {
      "state": "connected",
      "error_code": null,
      "error_message": null,
      "updated_at": "2026-04-19T12:00:00+00:00"
    }
  },
  "queued_tasks": {
    "queued_count": 1,
    "lane_counts": {
      "interactive": 0,
      "cron_scout": 1,
      "housekeeping": 0
    },
    "bucket_counts": {
      "now": 1,
      "next": 0,
      "later": 0
    },
    "next_task": {
      "task_id": "task-hi",
      "state": "queued",
      "session_key": "telegram:user:123",
      "lane": "cron_scout",
      "priority": 10,
      "priority_bucket": "now",
      "priority_bucket_options": ["now", "next", "later"],
      "reply_policy": "status_only",
      "cancellation_policy": "preserve",
      "queued_at": "2026-04-19T12:00:00+00:00",
      "wait_seconds": 300,
      "wait_age": "5m",
      "reason": "busy_followup",
      "preview": "high priority queued follow-up",
      "kind": "queued_message",
      "control_mode": "queued",
      "actions": ["foreground", "reprioritize", "cancel"],
      "source": "telegram"
    },
    "oldest_waiting": {
      "task_id": "task-hi",
      "state": "queued",
      "session_key": "telegram:user:123",
      "lane": "cron_scout",
      "priority": 10,
      "priority_bucket": "now",
      "priority_bucket_options": ["now", "next", "later"],
      "reply_policy": "status_only",
      "cancellation_policy": "preserve",
      "queued_at": "2026-04-19T12:00:00+00:00",
      "wait_seconds": 300,
      "wait_age": "5m",
      "reason": "busy_followup",
      "preview": "high priority queued follow-up",
      "kind": "queued_message",
      "control_mode": "queued",
      "actions": ["foreground", "reprioritize", "cancel"],
      "source": "telegram"
    },
    "starving_bucket": {
      "bucket": "now",
      "queued_count": 1,
      "oldest_wait_seconds": 300,
      "oldest_wait_age": "5m",
      "oldest_task_id": "task-hi"
    },
    "starvation_alert": null,
    "tasks": [
      {
        "task_id": "task-hi",
        "state": "queued",
        "session_key": "telegram:user:123",
        "lane": "cron_scout",
        "priority": 10,
        "priority_bucket": "now",
        "priority_bucket_options": ["now", "next", "later"],
        "reply_policy": "status_only",
        "cancellation_policy": "preserve",
        "queued_at": "2026-04-19T12:00:00+00:00",
        "wait_seconds": 300,
        "wait_age": "5m",
        "reason": "busy_followup",
        "preview": "high priority queued follow-up",
        "kind": "queued_message",
        "control_mode": "queued",
        "actions": ["foreground", "reprioritize", "cancel"],
        "source": "telegram"
      }
    ]
  },
  "live_tasks": {
    "active_count": 1,
    "lane_counts": {
      "interactive": 0,
      "cron_scout": 1,
      "housekeeping": 0
    },
    "tasks": [
      {
        "task_id": "bg_120000_ab12cd",
        "lane": "cron_scout",
        "label": "background task",
        "kind": "background",
        "control_mode": "managed_runtime",
        "actions": ["cancel"],
        "source": "gateway",
        "started_at": "2026-04-19T12:00:00+00:00"
      }
    ]
  },
  "cron": {
    "active_jobs": 2,
    "total_jobs": 2,
    "lane_counts": {
      "interactive": 1,
      "cron_scout": 0,
      "housekeeping": 1
    },
    "due_now": {
      "interactive": 0,
      "cron_scout": 0,
      "housekeeping": 1
    }
  }
}
```

### GET /api/tasks

Machine-readable task control plane. This is the structured version of the chat `/tasks` surface: queued backlog on one side, active runtime tasks on the other, with explicit control metadata instead of human-only command hints.

**Response:**
```json
{
  "queued": {
    "queued_count": 1,
    "lane_counts": {
      "interactive": 0,
      "cron_scout": 1,
      "housekeeping": 0
    },
    "bucket_counts": {
      "now": 1,
      "next": 0,
      "later": 0
    },
    "next_task": {
      "task_id": "task-hi",
      "state": "queued",
      "session_key": "telegram:user:123",
      "lane": "cron_scout",
      "priority": 10,
      "priority_bucket": "now",
      "priority_bucket_options": ["now", "next", "later"],
      "reply_policy": "status_only",
      "cancellation_policy": "preserve",
      "queued_at": "2026-04-19T12:00:00+00:00",
      "wait_seconds": 300,
      "wait_age": "5m",
      "reason": "busy_followup",
      "preview": "high priority queued follow-up",
      "kind": "queued_message",
      "control_mode": "queued",
      "actions": ["foreground", "reprioritize", "cancel"],
      "source": "telegram"
    },
    "oldest_waiting": {
      "task_id": "task-hi",
      "state": "queued",
      "session_key": "telegram:user:123",
      "lane": "cron_scout",
      "priority": 10,
      "priority_bucket": "now",
      "priority_bucket_options": ["now", "next", "later"],
      "reply_policy": "status_only",
      "cancellation_policy": "preserve",
      "queued_at": "2026-04-19T12:00:00+00:00",
      "wait_seconds": 300,
      "wait_age": "5m",
      "reason": "busy_followup",
      "preview": "high priority queued follow-up",
      "kind": "queued_message",
      "control_mode": "queued",
      "actions": ["foreground", "reprioritize", "cancel"],
      "source": "telegram"
    },
    "starving_bucket": {
      "bucket": "now",
      "queued_count": 1,
      "oldest_wait_seconds": 300,
      "oldest_wait_age": "5m",
      "oldest_task_id": "task-hi"
    },
    "starvation_alert": null,
    "tasks": [
      {
        "task_id": "task-hi",
        "state": "queued",
        "session_key": "telegram:user:123",
        "lane": "cron_scout",
        "priority": 10,
        "priority_bucket": "now",
        "priority_bucket_options": ["now", "next", "later"],
        "reply_policy": "status_only",
        "cancellation_policy": "preserve",
        "queued_at": "2026-04-19T12:00:00+00:00",
        "wait_seconds": 300,
        "wait_age": "5m",
        "reason": "busy_followup",
        "preview": "high priority queued follow-up",
        "kind": "queued_message",
        "control_mode": "queued",
        "actions": ["foreground", "reprioritize", "cancel"],
        "source": "telegram"
      }
    ]
  },
  "live": {
    "active_count": 1,
    "lane_counts": {
      "interactive": 1,
      "cron_scout": 0,
      "housekeeping": 0
    },
    "tasks": [
      {
        "task_id": "turn-1",
        "lane": "interactive",
        "label": "message turn",
        "kind": "live_turn",
        "control_mode": "read_only",
        "actions": [],
        "source": "gateway",
        "started_at": "2026-04-19T12:00:01+00:00"
      }
    ]
  }
}
```

#### GET /api/tasks/{task_id}

Fetches one task by ID from either side of the control plane. Queued tasks come back with `state=queued`; active runtime tasks come back with `state=active`.

**Response:**
```json
{
  "task_id": "task-hi",
  "state": "queued",
  "task": {
    "task_id": "task-hi",
    "state": "queued",
    "session_key": "telegram:user:123",
    "lane": "cron_scout",
    "priority": 10,
    "priority_bucket": "now",
    "priority_bucket_options": ["now", "next", "later"],
    "reply_policy": "status_only",
    "cancellation_policy": "preserve",
    "queued_at": "2026-04-19T12:00:00+00:00",
    "wait_seconds": 300,
    "wait_age": "5m",
    "reason": "busy_followup",
    "preview": "high priority queued follow-up",
    "kind": "queued_message",
    "control_mode": "queued",
    "actions": ["foreground", "reprioritize", "cancel"],
    "source": "telegram"
  }
}
```

#### POST /api/tasks/{task_id}/foreground

Promotes a queued task. If the chat is idle, the task starts immediately; if the chat is already busy, the task is moved to the front so it runs next.

**Response:**
```json
{
  "task_id": "task-hi",
  "action": "foreground",
  "status": "started",
  "message": "Foregrounded queued task task-hi — starting now.",
  "task": {
    "task_id": "task-hi",
    "state": "queued",
    "session_key": "telegram:user:123",
    "lane": "cron_scout",
    "priority": 10,
    "priority_bucket": "now",
    "priority_bucket_options": ["now", "next", "later"],
    "reply_policy": "status_only",
    "cancellation_policy": "preserve",
    "queued_at": "2026-04-19T12:00:00+00:00",
    "wait_seconds": 300,
    "wait_age": "5m",
    "reason": "busy_followup",
    "preview": "high priority queued follow-up",
    "kind": "queued_message",
    "control_mode": "queued",
    "actions": ["foreground", "reprioritize", "cancel"],
    "source": "telegram"
  }
}
```

#### POST /api/tasks/{task_id}/reprioritize

Changes a queued task's bucket without forcing it to start immediately. The request body accepts one of `now`, `next`, or `later`.

**Request:**
```json
{
  "bucket": "later"
}
```

**Response:**
```json
{
  "task_id": "task-hi",
  "action": "reprioritize",
  "status": "reprioritized",
  "message": "Moved queued task task-hi to later priority.",
  "task": {
    "task_id": "task-hi",
    "state": "queued",
    "session_key": "telegram:user:123",
    "lane": "cron_scout",
    "priority": 80,
    "priority_bucket": "later",
    "priority_bucket_options": ["now", "next", "later"],
    "reply_policy": "status_only",
    "cancellation_policy": "preserve",
    "queued_at": "2026-04-19T12:00:00+00:00",
    "wait_seconds": 300,
    "wait_age": "5m",
    "reason": "busy_followup",
    "preview": "high priority queued follow-up",
    "kind": "queued_message",
    "control_mode": "queued",
    "actions": ["foreground", "reprioritize", "cancel"],
    "source": "telegram"
  }
}
```

#### POST /api/tasks/{task_id}/recover

Applies the task's current starvation recovery recommendation. The control plane keeps the recommendation machine-readable via `suggested_action` / `suggested_bucket`, and this endpoint turns that into a one-hop shortcut.

**Response:**
```json
{
  "task_id": "task-stale",
  "action": "recover",
  "status": "recovered",
  "message": "Recovered queued task task-stale — moved it to next priority.",
  "task": {
    "task_id": "task-stale",
    "state": "queued",
    "session_key": "telegram:user:123",
    "lane": "housekeeping",
    "priority": 50,
    "priority_bucket": "next",
    "priority_bucket_options": ["now", "next", "later"],
    "reply_policy": "status_only",
    "cancellation_policy": "preserve",
    "queued_at": "2026-04-19T10:00:00+00:00",
    "wait_seconds": 7500,
    "wait_age": "2h 5m",
    "reason": "busy_followup",
    "preview": "stale queued follow-up",
    "kind": "queued_message",
    "control_mode": "queued",
    "actions": ["foreground", "reprioritize", "cancel"],
    "source": "telegram"
  }
}
```

#### POST /api/tasks/{task_id}/cancel

Cancels a queued task immediately, or requests cancellation for a managed runtime task like `/background` or `/btw`.

**Response:**
```json
{
  "task_id": "bg-1",
  "action": "cancel",
  "status": "cancellation_requested",
  "message": "Cancellation requested for active task bg-1.",
  "task": {
    "task_id": "bg-1",
    "lane": "cron_scout",
    "label": "background task",
    "kind": "background",
    "control_mode": "managed_runtime",
    "actions": ["cancel"],
    "source": "gateway",
    "started_at": "2026-04-19T12:00:01+00:00"
  }
}
```

All `/api/status`, `/api/tasks*`, and `/api/jobs*` endpoints use the same bearer-token auth as the rest of the API server.

### GET /openapi.json

OpenAPI 3.1 contract for the Hermes control-plane surface. Right now it covers the machine-readable endpoints that matter for dashboards, frontends, and SDK generation:
- `/api/status`
- `/api/tasks`
- `/api/tasks/{task_id}`
- `/api/tasks/{task_id}/foreground`
- `/api/tasks/{task_id}/reprioritize`
- `/api/tasks/{task_id}/recover`
- `/api/tasks/{task_id}/cancel`
- `/api/jobs`
- `/api/jobs/{job_id}`
- `/api/jobs/{job_id}/pause`
- `/api/jobs/{job_id}/resume`
- `/api/jobs/{job_id}/run`

The schema includes the canonical lane enum, the enriched job payload (`lane`, `effective_lane`, `lane_source`), the `/api/status` live-task/cron summary shapes, the queued-task payload behind `/api/tasks`, the per-task detail envelope behind `/api/tasks/{task_id}`, the action result envelope used by task control endpoints, and the reason-coded task-action error envelope those endpoints return on `400`/`404`. That means clients can generate forms and typed models from one source instead of scraping docs examples.

**Response shape (abridged):**
```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "Hermes Control Plane API"
  },
  "paths": {
    "/api/status": {"get": {}},
    "/api/tasks": {"get": {}},
    "/api/tasks/{task_id}": {"get": {}},
    "/api/tasks/{task_id}/foreground": {"post": {}},
    "/api/tasks/{task_id}/reprioritize": {"post": {}},
    "/api/tasks/{task_id}/recover": {"post": {}},
    "/api/tasks/{task_id}/cancel": {"post": {}},
    "/api/jobs": {"get": {}, "post": {}},
    "/api/jobs/{job_id}": {"get": {}, "patch": {}, "delete": {}},
    "/api/jobs/{job_id}/pause": {"post": {}},
    "/api/jobs/{job_id}/resume": {"post": {}},
    "/api/jobs/{job_id}/run": {"post": {}}
  },
  "components": {
    "schemas": {
      "CronLaneValue": {
        "enum": ["interactive", "cron_scout", "housekeeping"]
      },
      "CronJob": {
        "properties": {
          "lane": {},
          "effective_lane": {},
          "lane_source": {}
        }
      },
      "LiveTask": {
        "properties": {
          "kind": {},
          "control_mode": {},
          "actions": {}
        }
      },
      "QueuedTask": {
        "properties": {
          "priority": {},
          "priority_bucket": {},
          "priority_bucket_options": {},
          "queued_at": {},
          "wait_seconds": {},
          "wait_age": {},
          "control_mode": {},
          "actions": {},
          "starvation_alert": {}
        }
      },
      "QueuedBucketStarvation": {
        "properties": {
          "bucket": {},
          "queued_count": {},
          "oldest_wait_seconds": {},
          "oldest_wait_age": {},
          "oldest_task_id": {}
        }
      },
      "QueuedStarvationAlert": {
        "properties": {
          "level": {},
          "reason_code": {},
          "reason": {},
          "bucket": {},
          "threshold_seconds": {},
          "threshold_age": {},
          "current_wait_seconds": {},
          "current_wait_age": {},
          "oldest_task_id": {},
          "suggested_action": {},
          "suggested_bucket": {},
          "suggested_command": {}
        }
      },
      "QueuedTaskStatus": {
        "properties": {
          "queued_count": {},
          "lane_counts": {},
          "bucket_counts": {},
          "next_task": {},
          "oldest_waiting": {},
          "starving_bucket": {},
          "starvation_alert": {},
          "tasks": {}
        }
      },
      "GatewayStatus": {
        "properties": {
          "queued_tasks": {},
          "live_tasks": {},
          "cron": {}
        }
      },
      "TaskDetailResponse": {
        "properties": {
          "state": {},
          "task": {}
        }
      },
      "TaskActionResponse": {
        "properties": {
          "action": {},
          "status": {},
          "task": {}
        }
      },
      "TaskActionErrorResponse": {
        "properties": {
          "error": {},
          "reason_code": {}
        }
      },
      "errors": {
        "auth": "ControlPlaneAuthErrorResponse",
        "validation": "ControlPlaneErrorResponse",
        "not_found": "ControlPlaneErrorResponse",
        "server_error": "ControlPlaneErrorResponse",
        "cron_unavailable": "ControlPlaneErrorResponse"
      }
    }
  }
}
```

### Control-plane error responses

The control-plane endpoints use three error shapes today:
- `401` auth failures return the same OpenAI-style error envelope the rest of the API server uses
- task action endpoints (`/api/tasks/{task_id}/foreground`, `/reprioritize`, `/recover`, and `/cancel`) return `{error, reason_code}` on `400`/`404`
- the rest of the `400`, `404`, `500`, and `501` control-plane failures return a simple Hermes control-plane error string

**401 Unauthorized**
```json
{
  "error": {
    "message": "Invalid API key",
    "type": "invalid_request_error",
    "code": "invalid_api_key"
  }
}
```

**400 Validation / bad request**
```json
{
  "error": "No valid fields to update"
}
```

**404 Not found**
```json
{
  "error": "Job not found"
}
```

**500 Internal error**
```json
{
  "error": "status exploded"
}
```

**501 Cron unavailable**
```json
{
  "error": "Cron module not available"
}
```

### Jobs API (`/api/jobs`)

The same API server also exposes a small REST API for cron job management. This is not part of the OpenAI-compatible surface — it's a Hermes-specific control plane for schedulers, dashboards, and automation UIs.

## System Prompt Handling

When a frontend sends a `system` message (Chat Completions) or `instructions` field (Responses API), hermes-agent **layers it on top** of its core system prompt. Your agent keeps all its tools, memory, and skills — the frontend's system prompt adds extra instructions.

This means you can customize behavior per-frontend without losing capabilities:
- Open WebUI system prompt: "You are a Python expert. Always include type hints."
- The agent still has terminal, file tools, web search, memory, etc.

## Authentication

Bearer token auth via the `Authorization` header:

```
Authorization: Bearer ***
```

Configure the key via `API_SERVER_KEY` env var. If you need a browser to call Hermes directly, also set `API_SERVER_CORS_ORIGINS` to an explicit allowlist.

:::warning Security
The API server gives full access to hermes-agent's toolset, **including terminal commands**. If you change the bind address to `0.0.0.0` (network-accessible), **always set `API_SERVER_KEY`** and keep `API_SERVER_CORS_ORIGINS` narrow — without that, remote callers may be able to execute arbitrary commands on your machine.

The default bind address (`127.0.0.1`) is for local-only use. Browser access is disabled by default; enable it only for explicit trusted origins.
:::

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `API_SERVER_ENABLED` | `false` | Enable the API server |
| `API_SERVER_PORT` | `8642` | HTTP server port |
| `API_SERVER_HOST` | `127.0.0.1` | Bind address (localhost only by default) |
| `API_SERVER_KEY` | _(none)_ | Bearer token for auth |
| `API_SERVER_CORS_ORIGINS` | _(none)_ | Comma-separated allowed browser origins |

### config.yaml

```yaml
# Not yet supported — use environment variables.
# config.yaml support coming in a future release.
```

## Security Headers

All responses include security headers:
- `X-Content-Type-Options: nosniff` — prevents MIME type sniffing
- `Referrer-Policy: no-referrer` — prevents referrer leakage

## CORS

The API server does **not** enable browser CORS by default.

For direct browser access, set an explicit allowlist:

```bash
API_SERVER_CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

When CORS is enabled:
- **Preflight responses** include `Access-Control-Max-Age: 600` (10 minute cache)
- **SSE streaming responses** include CORS headers so browser EventSource clients work correctly
- **`Idempotency-Key`** is an allowed request header — clients can send it for deduplication (responses are cached by key for 5 minutes)

Most documented frontends such as Open WebUI connect server-to-server and do not need CORS at all.

## Compatible Frontends

Any frontend that supports the OpenAI API format works. Tested/documented integrations:

| Frontend | Stars | Connection |
|----------|-------|------------|
| [Open WebUI](/docs/user-guide/messaging/open-webui) | 126k | Full guide available |
| LobeChat | 73k | Custom provider endpoint |
| LibreChat | 34k | Custom endpoint in librechat.yaml |
| AnythingLLM | 56k | Generic OpenAI provider |
| NextChat | 87k | BASE_URL env var |
| ChatBox | 39k | API Host setting |
| Jan | 26k | Remote model config |
| HF Chat-UI | 8k | OPENAI_BASE_URL |
| big-AGI | 7k | Custom endpoint |
| OpenAI Python SDK | — | `OpenAI(base_url="http://localhost:8642/v1")` |
| curl | — | Direct HTTP requests |

## Limitations

- **Response storage** — stored responses (for `previous_response_id`) are persisted in SQLite and survive gateway restarts. Max 100 stored responses (LRU eviction).
- **No file upload** — vision/document analysis via uploaded files is not yet supported through the API.
- **Model field is cosmetic** — the `model` field in requests is accepted but the actual LLM model used is configured server-side in config.yaml.
