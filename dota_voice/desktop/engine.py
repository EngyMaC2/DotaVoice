"""Run the HTTP/WebSocket engine inside a background thread.

The desktop UI needs the engine (GSI receiver, relay, overlay websocket) but
must not block the Qt event loop. :class:`EngineThread` owns a dedicated asyncio
loop and a uvicorn server bound to a local port, so the UI can talk to it over
``http://127.0.0.1:<port>`` exactly like any other client.

Binding to an ephemeral port (``port=0``) is supported so two instances - or a
test - never fight over a fixed port.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import uvicorn

from ..config import Settings
from ..server import create_app

logger = logging.getLogger(__name__)


class EngineThread:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self.port = settings.port

    # ------------------------------------------------------------------

    def start(self, timeout: float = 20.0) -> int:
        """Start the engine and return the actual bound port."""
        self._thread = threading.Thread(
            target=self._run, name="dotavoice-engine", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("Движок не запустился вовремя")
        return self.port

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        config = uvicorn.Config(
            create_app(self.settings),
            host=self.settings.host,
            port=self.settings.port,
            log_level=self.settings.log_level.lower(),
            loop="asyncio",
        )
        self._server = uvicorn.Server(config)
        self._loop.create_task(self._watch_ready())
        try:
            self._loop.run_until_complete(self._server.serve())
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            self._stopped.set()

    async def _watch_ready(self) -> None:
        assert self._server is not None
        while not self._server.started:
            await asyncio.sleep(0.05)
        # ``serve()`` binds the socket; read back the real port (may be ephemeral).
        for server in getattr(self._server, "servers", []):
            for sock in server.sockets or []:
                self.port = sock.getsockname()[1]
                break
        self._ready.set()

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None and self._loop is not None:
            def _request_shutdown() -> None:
                assert self._server is not None
                self._server.should_exit = True

            self._loop.call_soon_threadsafe(_request_shutdown)
        self._stopped.wait(timeout)
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
