# Command Voice Bridge

Team voice channels driven by live game state, orchestrated by an
[OpenHands](https://docs.openhands.dev/) agent.

The service watches a Dota 2 match through Game State Integration (GSI), binds
the match to a long-lived session keyed by `match_id`, splits players into two
isolated voice channels - **Силы Света** (Radiant) and **Силы Тьмы** (Dire) -
applies server-side mutes, and drives a transparent overlay that shows who is
talking, who is muted, and which side each player is on.

```
Игра (GSI) ──HTTP──▶ /gsi ──▶ Orchestrator ──▶ OpenHands agent ──▶ Voice backend (channels, mute)
                                   │                                        │
                                   └──▶ EventBus ──WS──▶ /ws/overlay ──▶ transparent overlay
```

The OpenHands agent is the controller. Each match owns exactly one
conversation, and the agent's tools (`create_voice_channels`, `sync_roster`,
`move_player`, `set_mute`, `set_deafen`, `get_session_state`, `end_session`)
are what actually mutate the voice channels and the overlay. The bridge runs
**deterministically by default** - one game event maps to the smallest set of
tool calls - so there is no LLM cost or latency on the hot path. Set an
`LLM_API_KEY` to additionally let the agent reason about ambiguous events.

## Why "one катка without breaks"

The session is bound to the game's stable `match_id`, not to process start
time. Consequences:

- A repeated `match_started` for the same `match_id` re-attaches to the
  existing session and conversation; it never creates a second pair of
  channels.
- If the game client restarts, GSI re-sends the same `match_id`, so the agent
  picks the session back up (and increments a reconnect counter).
- Channels are created only when a session has none, and the Discord backend
  additionally reuses channels by name, so even a full service restart cannot
  duplicate them.
- A session that goes quiet is only torn down after `DOTAVOICE_SESSION_GRACE_SECONDS`,
  which is the window in which a reconnecting client is still recognised.

## Quick start

```bash
pip install -r requirements.txt      # or: pip install -e ".[dev]"
cp .env.example .env                 # optional; sensible defaults exist
python -m dota_voice                 # serves on http://0.0.0.0:12000
```

Then open:

- `http://localhost:12000/` - control panel (active matches, teams, mutes)
- `http://localhost:12000/overlay` - transparent overlay (OBS Browser Source)
- `http://localhost:12000/healthz` - health + metrics

### Try it without the game

The simulator feeds realistic GSI payloads into a running bridge:

```bash
python -m scripts.simulate --scenario basic      # start, team switch, end
python -m scripts.simulate --scenario reconnect  # same match id, three times
python -m scripts.simulate --scenario mute       # server-side mute + speaking
```

There is also a fully in-process demo (no network, no game) that wires the real
agent and prints the resulting voice state at each step:

```bash
python -m scripts.demo
```

## Wiring up Dota 2

1. Copy the GSI config into your Dota 2 folder:

   ```
   gsi/gamestate_integration_dotavoice.cfg
     -> <Steam>/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/
   ```

2. Set the same token in two places:

   - the config file's `"auth": { "token": "..." }`
   - the service's `DOTAVOICE_GSI_TOKEN`

   If `DOTAVOICE_GSI_TOKEN` is empty, the token check is skipped (dev only).

3. Start the bridge and launch a match (or spectate one). The match appears in
   the control panel and on the overlay within a second.

The config requests only `provider`, `map`, `player`, and `hero` - enough to
identify the match and the roster, and nothing that changes per frame.

## Configuration

All settings come from the environment (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DOTAVOICE_PORT` | `12000` | HTTP port |
| `DOTAVOICE_GSI_TOKEN` | _(empty)_ | Shared secret checked against GSI `auth.token` |
| `DOTAVOICE_STORE` | `memory` | `memory` or `redis` (survives service restarts) |
| `DOTAVOICE_VOICE` | `memory` | `memory` or `discord` |
| `DOTAVOICE_SESSION_GRACE_SECONDS` | `900` | Idle window before a session is torn down |
| `DISCORD_BOT_TOKEN` / `DISCORD_GUILD_ID` | _(empty)_ | Discord voice backend credentials |
| `DISCORD_USER_MAP` | `{}` | JSON map of game player -> Discord user id |
| `LLM_API_KEY` | _(empty)_ | Set to enable the optional reasoning layer |

## Voice backends

**`memory`** (default) models channels and per-player voice state in-process.
It is what the demo and the test suite run against.

**`discord`** uses the Discord REST API to create the two team channels, move
members, and apply mute/deafen flags. Two things to know:

- A REST-only bot cannot drag a disconnected user into a channel. Players must
  already be in a voice channel in the guild (a shared "lobby" channel works
  well); the bridge then moves them to the correct team channel.
- A REST-only bot does not receive gateway speaking events. Speaking
  indicators are therefore driven from the game/client side via
  `POST /api/sessions/{match_id}/speaking`.

Adding another backend (TeamSpeak, a WebRTC SFU) means implementing the
`VoiceBackend` protocol in `dota_voice/voice/base.py` and returning it from
`build_voice_backend`.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/gsi` | Dota 2 GSI receiver (token-checked) |
| `GET` | `/ws/overlay` | WebSocket stream of overlay events |
| `GET` | `/overlay` | Transparent overlay page |
| `GET` | `/` | Control panel |
| `GET` | `/api/sessions` | Active session snapshots |
| `GET` | `/api/sessions/{match_id}` | One session snapshot |
| `POST` | `/api/sessions/{match_id}/mute` | `{player_id, muted}` |
| `POST` | `/api/sessions/{match_id}/deafen` | `{player_id, deafened}` |
| `POST` | `/api/sessions/{match_id}/move` | `{player_id, team}` |
| `POST` | `/api/sessions/{match_id}/speaking` | `{player_id, speaking}` |
| `POST` | `/api/sessions/{match_id}/end` | End a session |
| `GET` | `/healthz` | Health + metrics |

## The agent tools

| Tool | Effect |
|---|---|
| `create_voice_channels` | Create/reuse the two team channels (idempotent) |
| `sync_roster` | Place every player in the channel for their team |
| `move_player` | Move one player to a team's channel |
| `set_mute` / `set_deafen` | Server-side mute / deafen a player |
| `get_session_state` | Read phase, rosters, mutes, reconnect count |
| `end_session` | Mark ended, notify overlay, tear channels down |

Tool executors are synchronous (an SDK requirement) but the voice/overlay
stack is async, so each runtime bridges back onto the service event loop via
`MatchSessionRuntime.run_sync`. Agent/tool work is always dispatched on a
worker thread, so the event loop never blocks.

## Layout

```
dota_voice/
  models.py        domain models (Team, Player, Roster, MatchSession, ...)
  gsi.py           Dota 2 GSI payload parser
  config.py        environment-driven settings
  bus.py           asyncio pub/sub for overlay events
  store.py         session store (memory + optional Redis)
  server.py        FastAPI app: /gsi, /ws/overlay, control + overlay pages
  voice/           voice backend protocol, memory + Discord implementations
  agent/
    tools.py       OpenHands custom tools (Action/Observation/Executor)
    runtime.py     per-match runtime (session + channels + overlay + conversation)
    manager.py     one conversation per match id; reaping; tool dispatch
    orchestrator.py GSI event -> tool calls
    factory.py     agent construction
  static/          overlay.html, control.html
scripts/
  simulate.py      GSI simulator
  demo.py          in-process end-to-end demo
gsi/               Dota 2 GSI config template
tests/             pytest suite (real agent + real tool execution)
```

## Tests

```bash
python -m pytest -q
```

The suite drives the real OpenHands SDK - real conversations, real tool
execution, real persistence - with a stub key so no network calls happen. It
covers the GSI parser, session idempotency, team switching, mute/deafen,
reconnect-and-reuse, stale-session reaping, the HTTP API, the overlay
WebSocket, and both voice backends (Discord is exercised through a stubbed
HTTP transport, the only place a stub is used).
