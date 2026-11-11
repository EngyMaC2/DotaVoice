"""Audio capture / playback for the built-in voice relay.

Audio hardware is optional: the relay, the session logic and the overlay all
work without a microphone, so the app degrades gracefully on machines (and CI
containers) that have no sound device. When ``sounddevice`` *and* ``opuslib``
are available the client streams real Opus-encoded 20 ms frames; otherwise the
client stays in "state only" mode and still joins rooms, speaks, and mutes.

This module isolates all of that so the transport layer never imports audio
libraries directly.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

FRAME_RATE = 48000
FRAME_SAMPLES = 960  # 20 ms at 48 kHz
CHANNELS = 1


def audio_available() -> bool:
    return _sounddevice() is not None


def opus_available() -> bool:
    return _opus() is not None


def _sounddevice():  # pragma: no cover - hardware dependent
    try:
        import sounddevice  # type: ignore

        return sounddevice
    except Exception:
        return None


def _opus():  # pragma: no cover - native library dependent
    try:
        import opuslib  # type: ignore

        return opuslib
    except Exception:
        return None


class OpusCodec:
    """Thin wrapper around an Opus encoder/decoder pair."""

    def __init__(self) -> None:
        opuslib = _opus()
        if opuslib is None:
            raise RuntimeError("opuslib недоступен")
        self._encoder = opuslib.Encoder(FRAME_RATE, CHANNELS, "voip")
        self._decoder = opuslib.Decoder(FRAME_RATE, CHANNELS)

    def encode(self, pcm: bytes) -> bytes:
        return self._encoder.encode(pcm, FRAME_SAMPLES)

    def decode(self, data: bytes) -> bytes:
        return self._decoder.decode(data, FRAME_SAMPLES)


def list_devices() -> list[dict]:
    sd = _sounddevice()
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
    except Exception:  # pragma: no cover - hardware dependent
        return []
    return [
        {
            "index": index,
            "name": info["name"],
            "inputs": info["max_input_channels"],
            "outputs": info["max_output_channels"],
        }
        for index, info in enumerate(devices)
    ]


class AudioEngine:
    """Full-duplex audio: capture -> ``on_frame``, ``play`` -> speakers.

    Used by :class:`~dota_voice.voice.relay_client.RelayClient`. Capture only
    runs while the local player is transmitting (push-to-talk or open mic).
    """

    def __init__(
        self,
        on_frame: Callable[[bytes], None],
        *,
        input_device: str | int | None = None,
        output_device: str | int | None = None,
    ) -> None:
        self._on_frame = on_frame
        self._input = _resolve_device(input_device)
        self._output = _resolve_device(output_device)
        self._codec: OpusCodec | None = None
        self._stream = None
        self._lock = threading.Lock()
        self._transmitting = False

    @property
    def ready(self) -> bool:
        return audio_available() and opus_available()

    def start(self) -> bool:
        """Open the output stream; return whether real audio is running."""
        if not self.ready:
            logger.info("Звук недоступен — клиент работает в режиме «только состояние»")
            return False
        self._codec = OpusCodec()
        sd = _sounddevice()
        try:  # pragma: no cover - hardware dependent
            self._stream = sd.OutputStream(
                samplerate=FRAME_RATE,
                channels=CHANNELS,
                dtype="int16",
                device=self._output,
            )
            self._stream.start()
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning("Не удалось открыть устройство вывода: %s", exc)
            self._stream = None
            return False
        return True

    def set_transmitting(self, active: bool) -> None:  # pragma: no cover - hardware
        with self._lock:
            if active == self._transmitting:
                return
            self._transmitting = active
            if not self.ready:
                return
            sd = _sounddevice()
            if active:
                self._input_stream = sd.InputStream(
                    samplerate=FRAME_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=FRAME_SAMPLES,
                    device=self._input,
                    callback=self._capture,
                )
                self._input_stream.start()
            elif getattr(self, "_input_stream", None) is not None:
                self._input_stream.stop()
                self._input_stream.close()
                self._input_stream = None

    def _capture(self, indata, frames, time_info, status) -> None:  # pragma: no cover
        if self._codec is None:
            return
        try:
            encoded = self._codec.encode(bytes(indata))
        except Exception:
            return
        self._on_frame(encoded)

    def play(self, payload: bytes) -> None:  # pragma: no cover - hardware dependent
        if self._stream is None or self._codec is None:
            return
        try:
            pcm = self._codec.decode(payload)
        except Exception:
            return
        import numpy as np  # type: ignore

        samples = np.frombuffer(pcm, dtype="int16").reshape(-1, CHANNELS)
        self._stream.write(samples)

    def stop(self) -> None:  # pragma: no cover - hardware dependent
        with self._lock:
            self._transmitting = False
            stream = getattr(self, "_input_stream", None)
            if stream is not None:
                stream.stop()
                stream.close()
                self._input_stream = None
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None


def _resolve_device(device: str | int | None) -> int | None:
    """Turn a configured device name into an index ``sounddevice`` understands."""
    if device in (None, "", -1):
        return None
    if isinstance(device, int):
        return device
    try:
        return int(device)
    except (TypeError, ValueError):
        pass
    for info in list_devices():
        if info["name"] == device:
            return info["index"]
    return None
