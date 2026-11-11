"""Tests for the team chat and the post-match discussion window.

ТЗ §8.3/§8.4: two private team rooms while the match runs, a shared room for a
few minutes after it ends. The rules that matter are access (a side can never
read the other side's room) and lifetime (the shared room exists only during the
post-match window, and voice never crosses the boundary).
"""

from __future__ import annotations

import pytest

from dota_voice.chat import ChatError, ChatService
from dota_voice.models import ChatChannel, Team


@pytest.fixture
def chat() -> ChatService:
    return ChatService()


# ---------------------------------------------------------------------------
# access rules (pure)
# ---------------------------------------------------------------------------


def test_a_side_can_read_only_its_own_room(chat):
    assert chat.can_read(Team.RADIANT, ChatChannel.RADIANT)
    assert not chat.can_read(Team.RADIANT, ChatChannel.DIRE)
    assert chat.can_read(Team.DIRE, ChatChannel.DIRE)
    assert not chat.can_read(Team.DIRE, ChatChannel.RADIANT)


def test_the_post_game_room_is_shared(chat):
    assert chat.can_read(Team.RADIANT, ChatChannel.POST_GAME)
    assert chat.can_read(Team.DIRE, ChatChannel.POST_GAME)
    assert chat.can_write(Team.RADIANT, ChatChannel.POST_GAME)
    assert chat.can_write(Team.DIRE, ChatChannel.POST_GAME)


def test_an_unknown_side_only_reaches_the_shared_room(chat):
    assert not chat.can_write(Team.UNKNOWN, ChatChannel.RADIANT)
    assert not chat.can_write(Team.UNKNOWN, ChatChannel.DIRE)
    assert chat.can_write(Team.UNKNOWN, ChatChannel.POST_GAME)


def test_a_side_never_sees_the_other_sides_room(chat):
    chat.post(
        "m1",
        channel=ChatChannel.RADIANT,
        author_id="101",
        author_name="Radiant one",
        team=Team.RADIANT,
        text="push top",
    )
    chat.post(
        "m1",
        channel=ChatChannel.DIRE,
        author_id="201",
        author_name="Dire one",
        team=Team.DIRE,
        text="gank mid",
    )
    radiant_sees = {m.text for m in chat.history("m1", team=Team.RADIANT)}
    dire_sees = {m.text for m in chat.history("m1", team=Team.DIRE)}
    assert radiant_sees == {"push top"}
    assert dire_sees == {"gank mid"}


def test_writing_to_the_other_sides_room_is_refused(chat):
    with pytest.raises(ChatError):
        chat.post(
            "m1",
            channel=ChatChannel.DIRE,
            author_id="101",
            author_name="Radiant one",
            team=Team.RADIANT,
            text="sneaky",
        )


def test_an_empty_message_is_refused(chat):
    with pytest.raises(ChatError):
        chat.post(
            "m1",
            channel=ChatChannel.RADIANT,
            author_id="101",
            author_name="Radiant one",
            team=Team.RADIANT,
            text="   ",
        )


def test_a_long_message_is_clipped(chat):
    message = chat.post(
        "m1",
        channel=ChatChannel.RADIANT,
        author_id="101",
        author_name="Radiant one",
        team=Team.RADIANT,
        text="x" * 2000,
    )
    assert len(message.text) == 500


def test_duplicate_message_ids_are_ignored(chat):
    first = chat.post(
        "m1",
        channel=ChatChannel.RADIANT,
        author_id="101",
        author_name="Radiant one",
        team=Team.RADIANT,
        text="hello",
        message_id="dup",
    )
    chat.post(
        "m1",
        channel=ChatChannel.RADIANT,
        author_id="101",
        author_name="Radiant one",
        team=Team.RADIANT,
        text="hello again",
        message_id="dup",
    )
    assert chat.count("m1") == 1
    assert chat.has_message("m1", ChatChannel.RADIANT, first.id)


def test_dropping_a_match_forgets_its_chat(chat):
    chat.post(
        "m1",
        channel=ChatChannel.RADIANT,
        author_id="101",
        author_name="Radiant one",
        team=Team.RADIANT,
        text="hi",
    )
    chat.drop("m1")
    assert chat.count("m1") == 0


def test_the_buffer_is_capped(chat):
    small = ChatService(max_messages=3)
    for i in range(5):
        small.post(
            "m1",
            channel=ChatChannel.RADIANT,
            author_id="101",
            author_name="Radiant one",
            team=Team.RADIANT,
            text=f"msg {i}",
        )
    assert small.count("m1") == 3
    assert [m.text for m in small.history("m1", team=Team.RADIANT)] == [
        "msg 2",
        "msg 3",
        "msg 4",
    ]
