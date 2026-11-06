"""Small synchronous client for the embedded engine's HTTP API.

The desktop UI runs on the Qt thread and polls these endpoints on a timer; the
calls are fast and local, so a plain ``httpx`` client keeps the code simple.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class EngineAPI:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------

    def health(self) -> dict:
        try:
            return self._client.get("/healthz").json()
        except Exception as exc:
            logger.debug("health недоступен: %s", exc)
            return {}

    def voice(self) -> dict:
        try:
            return self._client.get("/api/voice").json()
        except Exception as exc:
            logger.debug("voice недоступен: %s", exc)
            return {}

    def sessions(self) -> list[dict]:
        try:
            return self._client.get("/api/sessions").json().get("sessions", [])
        except Exception as exc:
            logger.debug("sessions недоступны: %s", exc)
            return []

    def set_mute(self, match_id: str, player_id: str, muted: bool) -> None:
        self._post(f"/api/sessions/{match_id}/mute", {"player_id": player_id, "muted": muted})

    def set_deafen(self, match_id: str, player_id: str, deafened: bool) -> None:
        self._post(
            f"/api/sessions/{match_id}/deafen",
            {"player_id": player_id, "deafened": deafened},
        )

    def move(self, match_id: str, player_id: str, team: str) -> None:
        self._post(f"/api/sessions/{match_id}/move", {"player_id": player_id, "team": team})

    def speaking(self, match_id: str, player_id: str, speaking: bool) -> None:
        self._post(
            f"/api/sessions/{match_id}/speaking",
            {"player_id": player_id, "speaking": speaking},
        )

    def transmit(self, active: bool) -> dict:
        """Start/stop sending the local microphone (push-to-talk)."""
        try:
            return self._client.post("/api/voice/transmit", json={"active": active}).json()
        except Exception as exc:
            logger.warning("Не удалось переключить передачу: %s", exc)
            return {}

    def deafen_self(self, deafened: bool) -> dict:
        try:
            return self._client.post(
                "/api/voice/deafen", json={"deafened": deafened}
            ).json()
        except Exception as exc:
            logger.warning("Не удалось переключить оглушение: %s", exc)
            return {}

    def end_session(self, match_id: str) -> None:
        self._post(f"/api/sessions/{match_id}/end", {})

    def overlay_url(self) -> str:
        return f"{self.base_url}/overlay"

    # ------------------------------------------------------------------

    def _post(self, path: str, body: dict) -> None:
        try:
            self._client.post(path, json=body)
        except Exception as exc:
            logger.warning("Запрос %s не удался: %s", path, exc)
