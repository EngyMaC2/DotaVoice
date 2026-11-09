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

    def leave_post_game(self, match_id: str, player_id: str) -> dict:
        try:
            return self._client.post(
                f"/api/sessions/{match_id}/post-game/leave",
                json={"player_id": player_id},
            ).json()
        except Exception as exc:
            logger.warning("Не удалось выйти из окна после матча: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # team chat
    # ------------------------------------------------------------------

    def chat(self, match_id: str) -> dict:
        """The chat channels and history the local side may see."""
        try:
            return self._client.get(f"/api/sessions/{match_id}/chat").json()
        except Exception as exc:
            logger.debug("Чат недоступен: %s", exc)
            return {}

    def send_chat(self, match_id: str, text: str, channel: str | None = None) -> dict:
        body: dict = {"text": text}
        if channel:
            body["channel"] = channel
        try:
            return self._client.post(
                f"/api/sessions/{match_id}/chat", json=body
            ).json()
        except Exception as exc:
            logger.warning("Не удалось отправить сообщение: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # microphone modes
    # ------------------------------------------------------------------

    def mic(self) -> dict:
        try:
            return self._client.get("/api/voice/mic").json()
        except Exception as exc:
            logger.debug("Состояние микрофона недоступно: %s", exc)
            return {}

    def set_mic_mode(self, mode: str) -> dict:
        try:
            return self._client.post("/api/voice/mic", json={"mode": mode}).json()
        except Exception as exc:
            logger.warning("Не удалось сменить режим микрофона: %s", exc)
            return {}

    def set_mic_keys(self, **keys) -> dict:
        try:
            return self._client.post("/api/voice/mic", json=keys).json()
        except Exception as exc:
            logger.warning("Не удалось сохранить клавиши микрофона: %s", exc)
            return {}

    def cycle_mic(self) -> dict:
        """Cycle the mode; used by the overlay's indicator."""
        try:
            return self._client.post("/api/voice/mic/cycle").json()
        except Exception as exc:
            logger.warning("Не удалось переключить режим микрофона: %s", exc)
            return {}

    def toggle_self_mute(self) -> dict:
        """Silence/un-silence the local microphone in every mode."""
        try:
            return self._client.post("/api/voice/mic/mute", json={"toggle": True}).json()
        except Exception as exc:
            logger.warning("Не удалось переключить мут микрофона: %s", exc)
            return {}

    def mic_key(self, action: str = "press") -> dict:
        """Feed an in-app hotkey press to the engine."""
        try:
            return self._client.post("/api/voice/mic/key", json={"action": action}).json()
        except Exception as exc:
            logger.warning("Не удалось передать нажатие клавиши: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # P2WLAN
    # ------------------------------------------------------------------

    def p2wlan(self) -> dict:
        try:
            return self._client.get("/api/p2wlan").json()
        except Exception as exc:
            logger.debug("Состояние P2WLAN недоступно: %s", exc)
            return {}

    def p2wlan_invite(self, match_id: str) -> dict:
        try:
            return self._client.post(
                "/api/p2wlan/invite", json={"match_id": match_id}
            ).json()
        except Exception as exc:
            logger.warning("Не удалось получить приглашение: %s", exc)
            return {}

    def p2wlan_join(self, invite_key: str, match_id: str | None = None) -> dict:
        body = {"invite_key": invite_key}
        if match_id:
            body["match_id"] = match_id
        try:
            return self._client.post("/api/p2wlan/join", json=body).json()
        except Exception as exc:
            logger.warning("Не удалось войти в комнату: %s", exc)
            return {}

    def overlay_url(self) -> str:
        return f"{self.base_url}/overlay"

    # ------------------------------------------------------------------

    def _post(self, path: str, body: dict) -> None:
        try:
            self._client.post(path, json=body)
        except Exception as exc:
            logger.warning("Запрос %s не удался: %s", path, exc)
