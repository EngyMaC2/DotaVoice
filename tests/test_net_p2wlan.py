"""Tests for the P2WLAN controller.

The properties that matter are the ones the requirements call out:

* a room is created once per match and never duplicated - not on a repeated
  event, and not across an app restart;
* teammates who are handed the same invite land on the same subnet;
* the room map is persisted, so a restart re-attaches instead of forking.
"""

from __future__ import annotations

import ipaddress
import json

import pytest

from dota_voice.net.p2wlan import (
    LocalDriver,
    P2WlanController,
    P2WlanError,
    invite_key,
    virtual_address,
    virtual_subnet,
)


def _controller(tmp_path, **kwargs) -> P2WlanController:
    return P2WlanController(
        driver=LocalDriver(),
        state_path=tmp_path / "p2wlan.json",
        display_name="alice",
        **kwargs,
    )


def test_virtual_subnet_is_stable_and_match_specific():
    assert virtual_subnet("7123456789") == virtual_subnet("7123456789")
    assert virtual_subnet("7123456789") != virtual_subnet("9876543210")
    assert virtual_subnet("7123456789").prefixlen == 24


def test_virtual_addresses_are_distinct_and_inside_the_subnet():
    subnet = virtual_subnet("7123456789")
    alice = virtual_address("7123456789", "alice")
    bob = virtual_address("7123456789", "bob")
    assert alice != bob
    assert subnet.network_address + 1 < ipaddress.ip_address(alice) < (
        subnet.broadcast_address
    )


def test_invite_key_is_short_and_team_tagged():
    key = invite_key("7123456789", "radiant")
    assert key.startswith("p2w-")
    assert key.endswith("-radiant")
    assert len(key) <= 32


async def test_creating_a_room_twice_returns_the_same_room(tmp_path):
    controller = _controller(tmp_path)
    first = await controller.create_room("7123456789", "radiant")
    second = await controller.create_room("7123456789", "radiant")

    assert first is second
    assert first.invite_key == second.invite_key
    assert controller.metrics["rooms_created"] == 1
    assert controller.metrics["rooms_reused"] == 1
    await controller.close()


async def test_a_restart_re_attaches_instead_of_creating_a_second_room(tmp_path):
    state = tmp_path / "p2wlan.json"

    first = P2WlanController(
        driver=LocalDriver(), state_path=state, display_name="alice"
    )
    room = await first.create_room("7123456789", "radiant")
    invite = room.invite_key
    # Simulate an unclean shutdown: the process dies without leaving the room,
    # so the state file still holds it. No close() call here on purpose.

    second = P2WlanController(
        driver=LocalDriver(), state_path=state, display_name="alice"
    )
    assert "7123456789" in second.rooms
    reloaded = await second.create_room("7123456789", "radiant")
    assert reloaded.invite_key == invite
    assert second.metrics["rooms_created"] == 0
    assert second.metrics["rooms_reused"] == 1
    await second.close()


async def test_a_clean_shutdown_keeps_the_room_for_the_next_launch(tmp_path):
    state = tmp_path / "p2wlan.json"

    first = P2WlanController(
        driver=LocalDriver(), state_path=state, display_name="alice"
    )
    room = await first.create_room("7123456789", "radiant")
    invite = room.invite_key
    await first.close()  # clean exit, e.g. the player quits the app mid-match

    second = P2WlanController(
        driver=LocalDriver(), state_path=state, display_name="alice"
    )
    reloaded = await second.create_room("7123456789", "radiant")
    assert reloaded.invite_key == invite
    assert reloaded.joined is True  # the driver was brought back up
    assert second.metrics["rooms_created"] == 0
    assert second.metrics["rooms_reused"] == 1
    # The roster survives, so addresses are stable across the restart.
    assert "alice" in reloaded.members
    await second.close()


async def test_ending_a_match_forgets_the_room_for_good(tmp_path):
    state = tmp_path / "p2wlan.json"
    controller = _controller(tmp_path)
    await controller.create_room("7123456789", "radiant")
    await controller.close()

    # leave_room is what an ended match calls: it must actually drop the room.
    assert await controller.leave_room("7123456789") is True
    raw = json.loads(state.read_text(encoding="utf-8"))
    assert "7123456789" not in raw["rooms"]


async def test_teammates_sharing_an_invite_land_on_the_same_subnet(tmp_path):
    host = _controller(tmp_path)
    guest = P2WlanController(
        driver=LocalDriver(),
        state_path=tmp_path / "guest.json",
        display_name="bob",
    )
    room = await host.create_room("7123456789", "radiant")
    joined = await guest.join_room(room.invite_key, match_id="7123456789")

    assert joined.subnet == room.subnet
    assert joined.gateway == room.gateway
    assert joined.invite_key == room.invite_key
    await host.close()
    await guest.close()


async def test_members_get_stable_virtual_addresses(tmp_path):
    controller = _controller(tmp_path)
    room = await controller.create_room("7123456789", "radiant")
    alice = controller.add_member("7123456789", "alice", "radiant")
    bob = controller.add_member("7123456789", "bob", "radiant")

    assert alice.virtual_ip != bob.virtual_ip
    again = controller.add_member("7123456789", "alice", "radiant")
    assert again.virtual_ip == alice.virtual_ip
    assert room.address_for("alice") == alice.virtual_ip
    await controller.close()


async def test_removing_a_member_frees_the_slot(tmp_path):
    controller = _controller(tmp_path)
    await controller.create_room("7123456789", "radiant")
    controller.add_member("7123456789", "bob")
    controller.remove_member("7123456789", "bob")
    assert controller.room("7123456789").members.get("bob") is None
    await controller.close()


async def test_leaving_a_room_removes_it(tmp_path):
    controller = _controller(tmp_path)
    await controller.create_room("7123456789", "radiant")
    assert await controller.leave_room("7123456789") is True
    assert controller.room("7123456789") is None
    assert await controller.leave_room("7123456789") is False


async def test_a_failing_driver_raises_unless_the_fallback_is_on(tmp_path):
    class Broken:
        name = "broken"

        async def create(self, room):
            raise P2WlanError("нет сети")

        async def join(self, room, key):
            raise P2WlanError("нет сети")

        async def leave(self, room):
            return None

    strict = P2WlanController(driver=Broken(), state_path=tmp_path / "a.json")
    with pytest.raises(P2WlanError):
        await strict.create_room("7123456789", "radiant")

    lenient = P2WlanController(
        driver=Broken(), state_path=tmp_path / "b.json", fallback_to_sdr=True
    )
    room = await lenient.create_room("7123456789", "radiant")
    assert room.relayed is True


async def test_join_with_an_unknown_invite_is_rejected(tmp_path):
    controller = _controller(tmp_path)
    with pytest.raises(P2WlanError):
        await controller.join_room("p2w-doesnotexist")


async def test_state_file_round_trips(tmp_path):
    state = tmp_path / "p2wlan.json"
    controller = P2WlanController(
        driver=LocalDriver(), state_path=state, display_name="alice"
    )
    await controller.create_room("7123456789", "dire")
    controller.add_member("7123456789", "bob", "dire")
    controller._save()

    raw = json.loads(state.read_text(encoding="utf-8"))
    assert "7123456789" in raw["rooms"]
    assert raw["rooms"]["7123456789"]["team"] == "dire"
    assert "bob" in raw["rooms"]["7123456789"]["members"]
    await controller.close()


async def test_snapshot_reports_rooms_and_metrics(tmp_path):
    controller = _controller(tmp_path)
    await controller.create_room("7123456789", "radiant")
    snapshot = controller.snapshot()
    assert snapshot["driver"] == "local"
    assert snapshot["metrics"]["rooms_created"] == 1
    assert snapshot["rooms"]["7123456789"]["room_id"] == "7123456789"
    await controller.close()
