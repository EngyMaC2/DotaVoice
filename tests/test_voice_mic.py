"""Tests for the microphone modes and the global-hotkey controller.

The controller is driven with a fake poller instead of the real OS, so the whole
mode machine - open mic, push-to-talk, toggle - is exercised deterministically
and without touching a real keyboard.
"""

from __future__ import annotations

import json

from dota_voice.voice.mic import (
    MicMode,
    MicSettings,
    MicrophoneController,
    NullPoller,
    conflicts_with_dota,
    default_poller,
    key_code,
)


class FakePoller:
    """A keyboard we control from the test."""

    def __init__(self) -> None:
        self.down: set[str] = set()

    def is_down(self, key: str) -> bool:
        return key.lower() in self.down

    def press(self, key: str) -> None:
        self.down.add(key.lower())

    def release(self, key: str) -> None:
        self.down.discard(key.lower())


def _controller(tmp_path, mode=MicMode.CONTINUOUS, poller=None, **kwargs):
    changes: list[bool] = []
    controller = MicrophoneController(
        settings=MicSettings(
            mode=mode.value,
            push_to_talk_key=kwargs.pop("push_to_talk_key", "v"),
            toggle_key=kwargs.pop("toggle_key", "v"),
        ),
        path=tmp_path / "microphone.json",
        poller=poller,
        on_change=changes.append,
        **kwargs,
    )
    return controller, changes


# ----------------------------------------------------------------------
# modes
# ----------------------------------------------------------------------


def test_continuous_mode_is_live_from_the_start(tmp_path):
    controller, _ = _controller(tmp_path, MicMode.CONTINUOUS)
    assert controller.mode is MicMode.CONTINUOUS
    assert controller.active is True


def test_push_to_talk_starts_silent_and_opens_while_the_key_is_held(tmp_path):
    poller = FakePoller()
    controller, changes = _controller(tmp_path, MicMode.PUSH_TO_TALK, poller)
    assert controller.active is False

    poller.press("v")
    controller.start()
    controller._poll_once()
    assert controller.active is True

    poller.release("v")
    controller._poll_once()
    assert controller.active is False
    assert changes == [True, False]
    controller.stop()


def test_toggle_flips_on_each_press(tmp_path):
    poller = FakePoller()
    controller, _ = _controller(tmp_path, MicMode.TOGGLE, poller)
    controller.start()

    poller.press("v")
    controller._poll_once()
    assert controller.active is True

    # A second press (key released then pressed again) turns it back off.
    poller.release("v")
    controller._poll_once()
    poller.press("v")
    controller._poll_once()
    assert controller.active is False
    controller.stop()


def test_toggle_ignores_a_held_key(tmp_path):
    poller = FakePoller()
    controller, _ = _controller(tmp_path, MicMode.TOGGLE, poller)
    controller.start()

    poller.press("v")
    controller._poll_once()
    controller._poll_once()  # still held: must not flip again
    assert controller.active is True
    controller.stop()


def test_key_down_and_key_up_drive_push_to_talk(tmp_path):
    controller, _ = _controller(tmp_path, MicMode.PUSH_TO_TALK)
    assert controller.active is False
    controller.key_down()
    assert controller.active is True
    controller.key_up()
    assert controller.active is False


def test_press_drives_the_toggle_mode(tmp_path):
    controller, _ = _controller(tmp_path, MicMode.TOGGLE)
    controller.press()
    assert controller.active is True
    controller.press()
    assert controller.active is False


def test_continuous_mode_ignores_the_hotkey(tmp_path):
    controller, changes = _controller(tmp_path, MicMode.CONTINUOUS)
    controller.press()
    assert controller.active is True
    assert changes == []


def test_cycling_walks_through_all_three_modes(tmp_path):
    controller, _ = _controller(tmp_path, MicMode.CONTINUOUS)
    assert controller.cycle_mode() is MicMode.PUSH_TO_TALK
    assert controller.cycle_mode() is MicMode.TOGGLE
    assert controller.cycle_mode() is MicMode.CONTINUOUS


def test_switching_to_push_to_talk_closes_the_microphone(tmp_path):
    controller, changes = _controller(tmp_path, MicMode.CONTINUOUS)
    controller.set_mode(MicMode.PUSH_TO_TALK)
    assert controller.active is False
    assert changes == [False]


def test_switching_to_continuous_opens_the_microphone(tmp_path):
    controller, changes = _controller(tmp_path, MicMode.PUSH_TO_TALK)
    controller.set_mode(MicMode.CONTINUOUS)
    assert controller.active is True
    assert changes == [True]


def test_mode_parsing_is_forgiving():
    assert MicMode.parse("push_to_talk") is MicMode.PUSH_TO_TALK
    assert MicMode.parse("Push-to-Talk") is MicMode.PUSH_TO_TALK
    assert MicMode.parse("toggle") is MicMode.TOGGLE
    assert MicMode.parse("нечто") is MicMode.CONTINUOUS
    assert MicMode.parse(None) is MicMode.CONTINUOUS


# ----------------------------------------------------------------------
# persistence
# ----------------------------------------------------------------------


def test_the_mode_and_keys_survive_a_restart(tmp_path):
    path = tmp_path / "microphone.json"
    first = MicrophoneController(
        settings=MicSettings(mode="toggle", push_to_talk_key="b", toggle_key="n"),
        path=path,
    )
    first.save()

    second = MicrophoneController(path=path)
    assert second.mode is MicMode.TOGGLE
    assert second.settings.push_to_talk_key == "b"
    assert second.settings.toggle_key == "n"


def test_settings_file_is_readable_json(tmp_path):
    path = tmp_path / "microphone.json"
    controller = MicrophoneController(settings=MicSettings(mode="toggle"), path=path)
    controller.save()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["mode"] == "toggle"


def test_a_corrupt_settings_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "microphone.json"
    path.write_text("{ это не json", encoding="utf-8")
    controller = MicrophoneController(path=path)
    assert controller.mode is MicMode.CONTINUOUS


# ----------------------------------------------------------------------
# hotkeys and the snapshot
# ----------------------------------------------------------------------


def test_the_hotkey_follows_the_mode(tmp_path):
    controller, _ = _controller(tmp_path, MicMode.PUSH_TO_TALK, push_to_talk_key="b")
    assert controller.hotkey == "b"
    controller.set_mode(MicMode.TOGGLE)
    controller.set_toggle_key("n")
    assert controller.hotkey == "n"


def test_snapshot_reports_everything_the_ui_needs(tmp_path):
    controller, _ = _controller(tmp_path, MicMode.PUSH_TO_TALK)
    snapshot = controller.snapshot()
    assert snapshot["mode"] == "push_to_talk"
    assert snapshot["mode_label"]
    assert snapshot["mode_color"].startswith("#")
    assert snapshot["active"] is False
    assert snapshot["global_hotkeys"] is False  # no poller in this test


def test_dota_key_conflicts_are_flagged():
    assert conflicts_with_dota("q")
    assert conflicts_with_dota("Q")
    assert conflicts_with_dota("space")
    assert not conflicts_with_dota("n")
    assert not conflicts_with_dota("")


def test_key_codes_cover_letters_and_specials():
    assert key_code("a") == 0x41
    assert key_code("space") is not None
    assert key_code("shift") is not None
    assert key_code("") is None


def test_the_default_poller_degrades_gracefully():
    # Headless CI has no X11; the controller must still work, just without
    # global hotkeys.
    poller = default_poller()
    assert isinstance(poller, NullPoller) or hasattr(poller, "is_down")


def test_starting_without_a_poller_is_safe(tmp_path):
    controller, _ = _controller(tmp_path, MicMode.PUSH_TO_TALK)
    assert controller.start() is False
    assert controller.active is False
    controller.stop()
