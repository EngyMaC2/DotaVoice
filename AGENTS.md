# Repository guide (DotaVoice)

Command Voice Bridge: binds team voice channels to a live Dota 2 match,
orchestrated by an OpenHands agent.

## Commands

```bash
pip install -r requirements.txt        # runtime + agent SDK + dev deps
python -m dota_voice                   # run the service (default port 12000)
python -m pytest -q                    # full test suite (~40 tests, fast)
python -m scripts.demo                 # in-process end-to-end demo, no network
python -m scripts.simulate --scenario basic   # feed GSI payloads to a running bridge
```

## Architecture in one paragraph

`POST /gsi` receives Dota 2 GSI JSON. `gsi.parse_gsi` normalizes it into a
`GameStateUpdate`. `agent.orchestrator.Orchestrator` binds it to a session keyed
by `match_id` via `agent.manager.SessionManager`, then drives OpenHands tools
(`agent.tools`) that mutate a `VoiceBackend` (`voice/`) and publish overlay
events on an `EventBus` (`bus.py`), which `server.py` fans out over
`/ws/overlay`.

## Conventions and gotchas

- **Sessions are keyed by `match_id`.** Never create a second session for an id
  that already has one - that is the whole point of the service. Reuse is
  enforced in `SessionManager.attach`, guarded by a per-match lock.
- **Conversation ids are deterministic** (`conversation_id_for_match`) so a
  restart re-attaches to the same persisted OpenHands conversation.
- **Tool executors are synchronous.** The SDK calls `ToolExecutor.__call__`
  directly. Async work goes through `MatchSessionRuntime.run_sync`, which hops
  back to the service loop via `run_coroutine_threadsafe`. Agent/tool calls are
  dispatched with `loop.run_in_executor` so the loop stays free. Do not call
  `run_sync` from the loop thread - it raises rather than deadlocking.
- **`Conversation` wants a `uuid.UUID`** for `conversation_id`, not a string.
  `MatchSession.conversation_id` is stored as a string and converted on attach.
- **`delete_on_close=False`** on conversations: session state must survive
  restarts.
- **Tool registration is idempotent** (`agent.tools.register_all`), because
  every new conversation rebuilds the agent.
- **Channel names** are `"{team label} · {match_id}"`; the Discord backend
  reuses channels by that name so a service restart cannot duplicate them.
- Tests use a stub LLM key, so no LLM calls happen. Discord is tested through a
  stubbed HTTP transport - that is the only stub in the suite.

## Environment

See `.env.example`. The only variable you must set for a real game is
`DOTAVOICE_GSI_TOKEN` (matching the GSI config's `auth.token`).
