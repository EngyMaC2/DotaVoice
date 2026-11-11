"""HTTP + WebSocket surface of the bridge.

Endpoints
---------
``POST /gsi``                       Dota 2 GSI receiver (token-checked)
``GET  /ws/overlay``                WebSocket stream for the transparent overlay
``GET  /overlay``                   transparent overlay page (OBS Browser Source)
``GET  /``                          control panel (active matches, teams, mutes)
``GET  /api/sessions``              JSON snapshot of active sessions
``POST /api/sessions/{id}/mute``    manual mute/unmute (push-to-talk server side)
``POST /api/sessions/{id}/deafen``  manual deafen/undeafen
``POST /api/sessions/{id}/speaking``speaking indicator (client pushes this)
``POST /api/sessions/{id}/end``     end a session
``GET  /api/p2wlan``                P2WLAN rooms and virtual addresses
``POST /api/p2wlan/members``        record a teammate on a room
``GET  /api/voice/mic``             microphone mode + hotkey state
``POST /api/voice/mic``             set the microphone mode / keys
``POST /api/voice/mic/cycle``       cycle the mode (overlay click)
``GET  /healthz``                   liveness
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .agent import Orchestrator, SessionManager
from .agent.tools import (
    SetDeafenAction,
    SetDeafenTool,
    SetMuteAction,
    SetMuteTool,
)
from .bus import EventBus
from .chat import ChatError
from .config import Settings
from .gsi import parse_gsi
from .models import ChatChannel, OverlayEvent, Team
from . import paths
from .presence import VoicePresence
from .security import autostart, default_policy, policy
from .security.updater import UpdateError, UpdatePolicy, load_manifest
from .store import build_store
from .voice import build_voice_backend
from .voice.mic import MicSettings, MicrophoneController, conflicts_with_dota
from .voice.relay import VoiceRelay

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _local_player_name(runtime) -> str:
    """The local player's id in a session, for the speaking indicator."""
    for player in runtime.session.roster.all_players():
        if player.is_local:
            return player.player_id or player.name or "хост"
    return "хост"


class Service:
    """Owns all long-lived objects for one running bridge."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.store = build_store(settings.store, settings.redis_url)
        self.relay: VoiceRelay | None = None
        if settings.voice == "relay":
            self.relay = VoiceRelay(host=settings.host, port=settings.port)
        self.voice = build_voice_backend(
            settings,
            relay=self.relay,
            on_peer_speaking=self._on_peer_speaking,
            on_peer_team=self._on_peer_team,
            on_peer_left=self._on_peer_left,
            on_chat=self._on_peer_chat,
        )
        # The peer-to-peer backend manages its own sessions; the relay path uses
        # the separate presence component instead.
        self.p2p = self.voice if settings.voice == "p2p" else None
        self.manager = SessionManager(
            store=self.store, voice=self.voice, bus=self.bus, settings=settings
        )
        self.orchestrator = Orchestrator(self.manager)
        self.presence = (
            VoicePresence(self.bus, self.manager, settings, relay=self.relay)
            if self.relay is not None and self.p2p is None
            else None
        )
        self._reaper: asyncio.Task | None = None
        self.mic = MicrophoneController(
            settings=MicSettings(
                mode=settings.mic_mode,
                push_to_talk_key=settings.mic_push_to_talk_key,
                toggle_key=settings.mic_toggle_key,
                click_feedback=settings.mic_click_feedback,
            ),
            path=paths.mic_file(),
            on_change=self._on_mic_change,
        )

    # ------------------------------------------------------------------

    @property
    def p2wlan(self):
        """The P2WLAN controller, when the P2P backend owns one."""
        return getattr(self.p2p, "controller", None)

    def _on_mic_change(self, active: bool) -> None:
        """Mirror the microphone state into the voice backend and the overlay."""
        rooms = self.set_transmitting(active)
        loop = self.manager._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self._announce_mic(active, rooms), loop)

    async def _announce_mic(self, active: bool, rooms: list[str]) -> None:
        for match_id in rooms:
            runtime = self.manager.get_runtime(match_id)
            if runtime is None:
                continue
            await runtime.set_speaking(_local_player_name(runtime), active)
        await self.bus.publish(
            OverlayEvent(
                type="mic_state",
                payload={"active": active, **self.mic.snapshot()},
            )
        )

    async def broadcast_mic_state(self) -> None:
        """Push the current microphone state to the overlay.

        Needed for changes that do not alter the effective live state - a new
        mode, a new hotkey, a self-mute toggle - where ``_on_mic_change`` never
        fires but the overlay label still has to follow along.
        """
        await self.bus.publish(
            OverlayEvent(
                type="mic_state",
                payload={**self.mic.snapshot()},
            )
        )

    def _on_peer_speaking(self, match_id: str, peer_name: str, speaking: bool) -> None:
        """Route a peer's speaking state into its match session."""
        runtime = self.manager.get_runtime(match_id)
        if runtime is None or runtime._loop is None:
            return
        coro = runtime.set_speaking(peer_name, speaking)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(coro, runtime._loop)

    def _on_peer_team(self, match_id: str, peer_name: str, team: str) -> None:
        """Fold a peer's self-reported team into the session, then re-sync.

        This is what makes team assignment automatic: a normal player's GSI
        reports only their own slot, so teammates' sides arrive from the peers'
        own clients. When one does, the roster changes and the players are
        re-placed into the matching channel.
        """
        runtime = self.manager.get_runtime(match_id)
        if runtime is None or runtime._loop is None:
            return
        from .models import Team

        parsed = Team.from_gsi(team)
        if parsed is Team.UNKNOWN:
            return

        async def _apply() -> None:
            if not await runtime.merge_peer_team(peer_name, parsed):
                return
            if runtime.session.radiant_channel_id is None:
                await runtime.ensure_channels()
            await runtime.sync_roster()
            await runtime.emit("state", **runtime.snapshot())

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_apply(), runtime._loop)

    def _on_peer_left(self, match_id: str, peer_name: str) -> None:
        """A teammate's app left the network: grey them out in the roster."""
        runtime = self.manager.get_runtime(match_id)
        if runtime is None or runtime._loop is None:
            return

        async def _apply() -> None:
            if await runtime.mark_peer_disconnected(peer_name):
                await runtime.emit("state", **runtime.snapshot())

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_apply(), runtime._loop)

    def _on_peer_chat(self, match_id: str, peer_name: str, message: dict) -> None:
        """Record a peer's chat message, then relay a shared one onward.

        The author's side comes from the mesh, which learned it from the peer's
        own announcements - never from the message text. Team text is already
        filtered to teammates by the mesh; a shared post-match message is
        forwarded once to the peers this client is linked to, so both sides see
        it (the duplicate id stops it looping back).
        """
        if not self.settings.chat_enabled:
            return
        runtime = self.manager.get_runtime(match_id)
        if runtime is None or runtime._loop is None:
            return
        try:
            channel = ChatChannel(str(message.get("channel") or ""))
        except ValueError:
            return
        team = Team.from_gsi(str(message.get("team") or ""))
        text = str(message.get("text") or "")
        message_id = str(message.get("id") or "")
        ts = message.get("ts")

        async def _apply() -> None:
            recorded, is_new = await runtime.ingest_chat(
                author_id=peer_name,
                text=text,
                channel=channel,
                team=team,
                message_id=message_id,
                ts=ts,
            )
            if recorded is None or not is_new:
                return
            # The post-match discussion is shared, so relay it once to the peers
            # we are linked to; team text needs no relay (the mesh already sent
            # it to every teammate directly).
            if channel.shared:
                self.send_chat(
                    match_id,
                    channel,
                    text,
                    message_id=recorded.id,
                    ts=recorded.ts,
                )

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_apply(), runtime._loop)

    def send_chat(
        self,
        match_id: str,
        channel: ChatChannel,
        text: str,
        *,
        message_id: str = "",
        ts: float | None = None,
    ) -> int:
        """Push a chat message onto the match's mesh.

        Used both for the local player's own messages and for relaying a shared
        message that arrived from a peer. No-op when chat is off or the backend
        has no chat path (memory/relay/discord). Returns peers reached.
        """
        if not self.settings.chat_enabled:
            return 0
        sender = getattr(self.voice, "send_chat", None)
        if sender is None:
            return 0
        with contextlib.suppress(Exception):
            return sender(
                match_id, channel.value, text, message_id=message_id, ts=ts
            )
        return 0

    def set_transmitting(self, active: bool) -> list[str]:
        if self.p2p is not None:
            return self.p2p.set_transmitting(active)
        if self.presence is not None:
            return self.presence.set_transmitting(active)
        return []

    def set_deafened(self, deafened: bool) -> list[str]:
        if self.p2p is not None:
            return self.p2p.set_deafened(deafened)
        if self.presence is not None:
            return self.presence.set_deafened(deafened)
        return []

    @property
    def joined_rooms(self) -> list[str]:
        if self.p2p is not None:
            return self.p2p.joined_rooms
        if self.presence is not None:
            return self.presence.joined_rooms
        return []

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.manager.bind_loop(loop)
        if self.relay is not None:
            await self.relay.start()
        if self.presence is not None:
            await self.presence.start()
        self.mic.start()
        # Open mic means we are transmitting from the start; tell the room.
        if self.mic.active:
            self.set_transmitting(True)
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        self.mic.stop()
        if self._reaper:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
        if self.presence is not None:
            await self.presence.stop()
        await self.manager.close()
        await self.voice.close()
        await self.store.close()

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                reaped = await self.manager.reap_stale()
                for match_id in reaped:
                    logger.info("Сессия %s разобрана", match_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Ошибка сборщика устаревших сессий: %s", exc)


def _check_token(settings: Settings, payload: dict) -> None:
    """Verify the GSI auth token when one is configured."""
    if not settings.gsi_token:
        return
    provided = (payload.get("auth") or {}).get("token")
    if provided != settings.gsi_token:
        raise HTTPException(status_code=403, detail="неверный токен GSI")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))

    service = Service(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        app.state.service = service
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="DotaVoice · Command Voice Bridge", lifespan=lifespan)

    # ------------------------------------------------------------------
    # GSI receiver
    # ------------------------------------------------------------------

    @app.post("/gsi")
    async def gsi(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="некорректный JSON")
        _check_token(settings, payload)
        update = parse_gsi(payload)
        result = await service.orchestrator.handle(update)
        return JSONResponse(result)

    # ------------------------------------------------------------------
    # health & introspection
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        relay_stats = None
        if service.relay is not None:
            relay_stats = {
                "rooms": service.relay.room_count,
                "bytes_forwarded": service.relay.bytes_forwarded,
                "packets_dropped": service.relay.packets_dropped,
            }
        return {
            "status": "ok",
            "sessions": len(service.manager.active_runtimes()),
            "overlay_clients": service.bus.subscriber_count,
            "voice_backend": settings.voice,
            "store": settings.store,
            "llm_enabled": settings.llm_enabled,
            "relay": relay_stats,
            "joined_rooms": service.joined_rooms,
            "p2wlan_enabled": service.p2wlan is not None,
            "steam_sdr": settings.steam_sdr_active,
            "mic": service.mic.snapshot(),
            # The overlay chat hotkey lives in the app config, not the mic, so
            # the control panel's hints read it from here.
            "chat_hotkey": settings.chat_hotkey,
            "metrics": service.manager.metrics,
        }

    @app.get("/api/voice")
    async def voice_status():
        """Voice subsystem status for the desktop app."""
        from .voice.audio import audio_available, list_devices, opus_available

        relay = None
        if service.relay is not None:
            relay = {
                "host": service.relay.host,
                "port": service.relay.port,
                "rooms": {
                    match_id: service.relay.room_snapshot(match_id)
                    for match_id in service.relay._rooms
                },
            }
        return {
            "backend": settings.voice,
            "audio_available": audio_available(),
            "opus_available": opus_available(),
            "joined_rooms": service.joined_rooms,
            "relay": relay,
            "devices": list_devices(),
        }

    @app.get("/api/sessions")
    async def list_sessions():
        return {"sessions": service.manager.snapshots()}

    @app.get("/api/sessions/{match_id}")
    async def get_session(match_id: str):
        runtime = service.manager.get_runtime(match_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="сессия не найдена")
        return runtime.snapshot()

    # ------------------------------------------------------------------
    # manual controls
    # ------------------------------------------------------------------

    def _runtime_or_404(match_id: str):
        runtime = service.manager.get_runtime(match_id)
        if runtime is None:
            raise HTTPException(status_code=404, detail="сессия не найдена")
        return runtime

    @app.post("/api/sessions/{match_id}/mute")
    async def mute(match_id: str, body: dict):
        runtime = _runtime_or_404(match_id)
        player_id = body.get("player_id")
        if not player_id:
            raise HTTPException(status_code=400, detail="нужно указать player_id")
        muted = bool(body.get("muted", True))
        await service.manager.call_tool_async(
            runtime, SetMuteTool.name, SetMuteAction(player_id=player_id, muted=muted)
        )
        return runtime.snapshot()

    @app.post("/api/sessions/{match_id}/deafen")
    async def deafen(match_id: str, body: dict):
        runtime = _runtime_or_404(match_id)
        player_id = body.get("player_id")
        if not player_id:
            raise HTTPException(status_code=400, detail="нужно указать player_id")
        deafened = bool(body.get("deafened", True))
        await service.manager.call_tool_async(
            runtime,
            SetDeafenTool.name,
            SetDeafenAction(player_id=player_id, deafened=deafened),
        )
        return runtime.snapshot()

    @app.post("/api/sessions/{match_id}/speaking")
    async def speaking(match_id: str, body: dict):
        runtime = _runtime_or_404(match_id)
        player_id = body.get("player_id")
        if not player_id:
            raise HTTPException(status_code=400, detail="нужно указать player_id")
        await runtime.set_speaking(player_id, bool(body.get("speaking", True)))
        return {"ok": True}

    # ------------------------------------------------------------------
    # push-to-talk / local microphone
    # ------------------------------------------------------------------

    @app.post("/api/voice/transmit")
    async def transmit(body: dict):
        """Start or stop sending the local microphone to the match rooms."""
        active = bool(body.get("active", True))
        rooms = service.set_transmitting(active)
        return {"ok": True, "transmitting": active, "rooms": rooms}

    @app.post("/api/voice/deafen")
    async def deafen_self(body: dict):
        """Stop or resume playback of incoming audio locally."""
        deafened = bool(body.get("deafened", True))
        rooms = service.set_deafened(deafened)
        return {"ok": True, "deafened": deafened, "rooms": rooms}

    @app.post("/api/sessions/{match_id}/end")
    async def end_session(match_id: str):
        """Force the whole match closed (operator's "завершить" button)."""
        runtime = _runtime_or_404(match_id)
        await runtime.close_post_game("manual")
        await service.manager.detach(match_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # chat (team rooms + post-match discussion)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{match_id}/chat")
    async def chat_state(match_id: str):
        # The side comes from the roster, never from the request: a caller
        # cannot ask for the other team's room and read it.
        runtime = _runtime_or_404(match_id)
        return runtime.chat_payload(runtime.local_team())

    @app.post("/api/sessions/{match_id}/chat")
    async def chat_send(match_id: str, body: dict):
        runtime = _runtime_or_404(match_id)
        if not service.settings.chat_enabled:
            raise HTTPException(status_code=403, detail="чат отключён")
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="пустое сообщение")
        # The author is the local player, never a caller-supplied id: a client
        # cannot post as someone else (the roster decides the author's side).
        local = runtime.local_player()
        player_id = (local.player_id if local else "") or "хост"
        channel_value = body.get("channel")
        channel = None
        if channel_value:
            try:
                channel = ChatChannel(str(channel_value))
            except ValueError:
                raise HTTPException(status_code=400, detail="неизвестный канал")
        try:
            message = await runtime.post_message(
                player_id=player_id, text=text, channel=channel
            )
        except ChatError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        # Send the local message out over the mesh (team room, or shared
        # post-match channel once it is open).
        service.send_chat(
            match_id, message.channel, message.text, message_id=message.id, ts=message.ts
        )
        return message.model_dump(mode="json")

    @app.post("/api/sessions/{match_id}/post-game/leave")
    async def post_game_leave(match_id: str, body: dict):
        runtime = _runtime_or_404(match_id)
        player_id = str(body.get("player_id") or "").strip()
        if not player_id:
            local = runtime.local_player()
            player_id = (local.player_id if local else "") or "хост"
        closed = await runtime.leave_post_game(player_id)
        if closed:
            await service.manager.detach(match_id)
        return {"ok": True, "closed": closed, **runtime.snapshot()}

    @app.post("/api/sessions/{match_id}/post-game/close")
    async def post_game_close(match_id: str):
        """Close the post-match window now (the UI's "закрыть" button)."""
        runtime = _runtime_or_404(match_id)
        await runtime.close_post_game("manual")
        await service.manager.detach(match_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # P2WLAN: the local virtual network that carries team voice
    # ------------------------------------------------------------------

    @app.get("/api/p2wlan")
    async def p2wlan_status():
        controller = service.p2wlan
        if controller is None:
            return {
                "enabled": False,
                "reason": "P2WLAN работает только с голосовым бэкендом «напрямую между игроками»",
            }
        return {"enabled": True, **controller.snapshot()}

    @app.post("/api/p2wlan/members")
    async def p2wlan_member(body: dict):
        """Record a teammate on the room (they get a virtual IP) and dial them."""
        match_id = body.get("match_id")
        member = body.get("member")
        if not match_id or not member:
            raise HTTPException(status_code=400, detail="нужны match_id и member")
        if service.p2p is None:
            raise HTTPException(status_code=409, detail="P2WLAN не включён")
        entry = service.p2p.add_room_member(
            match_id, str(member), body.get("team", "unknown")
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="комната не найдена")
        return {"ok": True, "member": entry.member, "virtual_ip": entry.virtual_ip}

    # ------------------------------------------------------------------
    # microphone modes
    # ------------------------------------------------------------------

    @app.get("/api/voice/mic")
    async def mic_status():
        return service.mic.snapshot()

    @app.post("/api/voice/mic")
    async def mic_set(body: dict):
        """Set the microphone mode and/or the hotkeys (persisted)."""
        warnings: list[str] = []
        if "mode" in body:
            service.mic.set_mode(str(body["mode"]))
        if "self_muted" in body:
            service.mic.set_self_mute(bool(body["self_muted"]))
        if "push_to_talk_key" in body:
            key = str(body["push_to_talk_key"])
            if conflicts_with_dota(key):
                warnings.append(
                    f"Клавиша «{key}» используется в управлении Dota 2 — "
                    "выберите другую, чтобы не мешать игре."
                )
            service.mic.set_push_to_talk_key(key)
        if "toggle_key" in body:
            key = str(body["toggle_key"])
            if conflicts_with_dota(key):
                warnings.append(
                    f"Клавиша «{key}» используется в управлении Dota 2 — "
                    "выберите другую, чтобы не мешать игре."
                )
            service.mic.set_toggle_key(key)
        if "click_feedback" in body:
            service.mic.set_click_feedback(bool(body["click_feedback"]))
        # A new mode or key can leave the effective live state unchanged, in
        # which case no change event fires; push the state anyway so the
        # overlay label never goes stale.
        await service.broadcast_mic_state()
        return {**service.mic.snapshot(), "warnings": warnings}

    @app.post("/api/voice/mic/cycle")
    async def mic_cycle():
        """Cycle the mode: open mic → push-to-talk → toggle → open mic."""
        mode = service.mic.cycle_mode()
        await service.broadcast_mic_state()
        return {"ok": True, "mode": mode.value, **service.mic.snapshot()}

    @app.post("/api/voice/mic/mute")
    async def mic_self_mute(body: dict):
        """Silence the local microphone in every mode (the "mute me" button).

        Unlike push-to-talk this does not depend on the current mode, so the
        button means the same thing wherever the player is.
        """
        muted = service.mic.toggle_self_mute() if body.get("toggle") else service.mic.set_self_mute(
            bool(body.get("muted", True))
        )
        await service.broadcast_mic_state()
        return {"ok": True, "self_muted": muted, **service.mic.snapshot()}

    @app.post("/api/voice/mic/key")
    async def mic_key(body: dict):
        """Feed a key press from the in-app shortcut (fallback for hotkeys)."""
        action = str(body.get("action", "press"))
        if action == "down":
            service.mic.key_down()
        elif action == "up":
            service.mic.key_up()
        else:
            service.mic.press()
        return {"ok": True, **service.mic.snapshot()}

    # ------------------------------------------------------------------
    # security / account safety
    # ------------------------------------------------------------------

    @app.get("/api/security")
    async def security_status():
        """The safety invariants, the policy flags, and autostart state."""
        current = default_policy()
        return {
            "policy": current.as_dict(),
            "invariants": current.invariants(),
            "autostart": autostart.status(),
            "voice_backend": settings.voice,
            "p2p_steam_sdr": settings.p2p_steam_sdr,
        }

    @app.post("/api/security/audit")
    async def security_audit(body: dict):
        """Audit a config payload against the safety policy."""
        report = policy.audit_config(body)
        return {**report.as_dict(), "text": policy.report_text(report)}

    @app.get("/api/security/autostart")
    async def autostart_get():
        return autostart.status()

    @app.post("/api/security/autostart")
    async def autostart_set(body: dict):
        if bool(body.get("enabled", True)):
            entry = autostart.enable()
            return {"ok": True, "enabled": True, "path": str(entry.path)}
        removed = autostart.disable()
        return {"ok": True, "enabled": False, "removed": removed}

    @app.post("/api/security/update/check")
    async def update_check(body: dict):
        """Verify a signed manifest and decide whether to offer the update.

        An unsigned or tampered manifest is rejected before anything else is
        considered; the client never applies an update it cannot verify.
        """
        manifest_text = body.get("manifest")
        public_key = body.get("public_key") or settings.update_public_key
        if not manifest_text:
            raise HTTPException(status_code=400, detail="нужен манифест")
        if not public_key:
            raise HTTPException(status_code=400, detail="не задан публичный ключ обновлений")
        if isinstance(manifest_text, dict):
            manifest_text = json.dumps(manifest_text)
        update_policy = UpdatePolicy(
            public_key=public_key,
            current_version=body.get("current_version") or settings.app_version,
            install_id=body.get("install_id", ""),
        )
        try:
            status = load_manifest(manifest_text, public_key, policy=update_policy)
        except UpdateError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return status.as_dict()

    # ------------------------------------------------------------------
    # overlay websocket
    # ------------------------------------------------------------------

    @app.websocket("/ws/overlay")
    async def overlay_ws(websocket: WebSocket):
        await websocket.accept()
        queue = await service.bus.subscribe()
        try:
            # Prime the client with current state.
            for runtime in service.manager.active_runtimes():
                await websocket.send_json(
                    OverlayEvent(type="state", match_id=runtime.session.match_id,
                                 payload=runtime.snapshot()).model_dump(mode="json")
                )
            while True:
                event = await queue.get()
                await websocket.send_json(event.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("WebSocket оверлея закрыт: %s", exc)
        finally:
            await service.bus.unsubscribe(queue)

    # ------------------------------------------------------------------
    # static pages
    # ------------------------------------------------------------------

    @app.get("/overlay", response_class=HTMLResponse)
    async def overlay_page():
        return (STATIC_DIR / "overlay.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def control_page():
        return (STATIC_DIR / "control.html").read_text(encoding="utf-8")

    return app
