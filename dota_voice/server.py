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
``POST /api/sessions/{id}/move``    manual team move
``POST /api/sessions/{id}/speaking``speaking indicator (client pushes this)
``POST /api/sessions/{id}/end``     end a session
``GET  /healthz``                   liveness
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .agent import Orchestrator, SessionManager
from .agent.tools import (
    MovePlayerAction,
    MovePlayerTool,
    SetDeafenAction,
    SetDeafenTool,
    SetMuteAction,
    SetMuteTool,
)
from .bus import EventBus
from .config import Settings
from .gsi import parse_gsi
from .models import OverlayEvent, Team
from .store import build_store
from .voice import build_voice_backend

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class Service:
    """Owns all long-lived objects for one running bridge."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.store = build_store(settings.store, settings.redis_url)
        self.voice = build_voice_backend(settings)
        self.manager = SessionManager(
            store=self.store, voice=self.voice, bus=self.bus, settings=settings
        )
        self.orchestrator = Orchestrator(self.manager)
        self._reaper: asyncio.Task | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.manager.bind_loop(loop)
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
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
        return {
            "status": "ok",
            "sessions": len(service.manager.active_runtimes()),
            "overlay_clients": service.bus.subscriber_count,
            "voice_backend": settings.voice,
            "store": settings.store,
            "llm_enabled": settings.llm_enabled,
            "metrics": service.manager.metrics,
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

    @app.post("/api/sessions/{match_id}/move")
    async def move(match_id: str, body: dict):
        runtime = _runtime_or_404(match_id)
        player_id = body.get("player_id")
        team_raw = body.get("team")
        if not player_id or team_raw is None:
            raise HTTPException(status_code=400, detail="нужны player_id и team")
        try:
            team = Team(str(team_raw).lower())
        except ValueError:
            raise HTTPException(status_code=400, detail="team должен быть radiant или dire")
        await service.manager.call_tool_async(
            runtime,
            MovePlayerTool.name,
            MovePlayerAction(player_id=player_id, team=team),
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

    @app.post("/api/sessions/{match_id}/end")
    async def end_session(match_id: str):
        runtime = _runtime_or_404(match_id)
        await runtime.end("manual")
        await service.manager.detach(match_id)
        return {"ok": True}

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
