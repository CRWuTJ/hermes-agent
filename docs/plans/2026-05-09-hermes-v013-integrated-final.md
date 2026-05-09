# Hermes v0.13 Integrated Final Plan

## Objective

Merge the two concurrent retrofit threads onto the live v0.13 baseline without
letting overlapping intent create duplicated routing, task, or learning logic.

The integrated baseline is:

- Base: `v2026.5.7` / `498bfc7bc`
- Integration branch: `codex/hermes-integrated-final-v013`
- Worktree: `/home/wutj/.hermes/hermes-agent/.worktrees/hermes-integrated-final-v013`
- Live source reference: `/home/wutj/.hermes/hermes-agent-v013-20260509`

## Thread Consolidation

### Kept from the live v0.13 thread

The live v0.13 work already provides the main self-improvement loop and should
remain the semantic authority for learning-related behavior:

- `tools/learning_absorption_tool.py`
- capability/context injection in `run_agent.py`
- scheduler and MCP/tool-surface cleanup
- tests covering learning absorption, missing MCP dependencies, and tools config

These changes were migrated into the integration branch as the base behavior.

### Ported from the older retrofit thread

Only the low-conflict, user-visible capabilities were ported:

- Natural dispatch: short follow-up messages can route to background execution
  when recent context shows an actionable technical task.
- Delegate auth repair: placeholder Codex keys are rejected unless a compatible
  parent runtime key can be safely reused.
- OpenAI-compatible image lane: image generation can use an OpenAI-style image
  endpoint before falling back to FAL.
- Cron ticker heartbeat: the gateway writes a fresh cron heartbeat so the
  external health guard does not misclassify a healthy gateway as stale.
- Long cron tick heartbeat: legitimate long-running cron ticks refresh
  `tick_running` while they are still making progress, so the external health
  guard does not repeatedly restart a live gateway during normal cron work.

### Deferred from the older retrofit thread

The old durable task queue and worker-routing patch was not merged directly.
It was structurally useful, but it targeted an older runtime shape and conflicts
with v0.13 primitives such as background jobs, kanban state, and the live
teacher/learning loop.

Future work should adapt that intent as a small bridge into the v0.13
background/kanban surfaces, not as a parallel task queue.

Final audit result:

- Do not port `agent/task_queue.py`, `agent/worker_routing.py`, or
  `gateway/task_queue_bridge.py` into the v0.13 branch as a second scheduler.
- v0.13 Kanban is the durable task surface: it already provides SQLite-backed
  task state, claim locking, stale recovery, dispatcher control, and notifier
  delivery.
- `/background` remains an immediate separate-session execution path. Routing it
  directly through Kanban would create duplicate-execution risk because the two
  paths have different task ids, lifecycle state, retry behavior, and reply
  delivery.
- If background visibility is later needed, add metadata-only observability
  around `/background`; do not transfer execution ownership to Kanban without a
  single shared idempotency and state-transition model.

## Safety Rules

- Media generation stays in the foreground path unless explicitly commanded
  otherwise.
- Bare continuations such as "continue" route to background work only when
  recent context contains technical execution intent.
- Discussion, review, and analysis prompts stay conversational unless they
  include clear work verbs.
- Delegate children must not launch with placeholder provider keys.
- OpenAI image support is additive and falls back to the existing FAL lane.

## Verification

Targeted integration suite:

```text
388 passed in 12.07s
```

Covered areas:

- natural dispatch config and gateway routing
- OpenAI image generation lane and FAL fallback compatibility
- delegate credential resolution and provider integration
- learning absorption tool
- learning capability context injection
- missing MCP dependency handling
- tools config exposure
- gateway runtime status and cron heartbeat freshness
- systemd status comparison without transient PATH drift
- full gateway service management helper coverage
- long cron tick heartbeat regression

Additional background/Kanban audit suite:

```text
328 passed in 147.58s
```

Covered areas:

- `/background` command creation, cleanup, and response delivery
- natural foreground/background admission control
- Kanban database claim and dispatch semantics
- Kanban CLI behavior and gateway dispatcher configuration
- Kanban worker tool visibility

Cron heartbeat regression suite:

```text
44 passed in 4.32s
372 passed in 184.12s
```

Covered areas:

- runtime status heartbeat persistence
- long cron tick `tick_running` refresh behavior
- combined background, natural dispatch, status, and Kanban regression surface
