"""Text chat for a match: a channel per team, plus a post-match discussion.

Two channels exist while a match is on (ТЗ §8.3):

* **Свет** (Radiant) - private to Radiant;
* **Тьма** (Dire) - private to Dire.

After the match a third, **shared** channel opens for the 10-15 minute window -
the post-match discussion - and closes with the match. That is the only place
text crosses the team boundary; voice never does.

Access is the whole point, so it is enforced in one place - :meth:`ChatService.can_read`
and :meth:`ChatService.can_write`. A Radiant player can never read or write the
Dire channel. This mirrors the voice isolation (the mesh refuses to link the
other side) on the text path.

The service is pure and in-memory per match: messages are capped so a long match
cannot grow without bound, and the store of record for anything durable is the
match session, not this buffer.
"""

from __future__ import annotations

import logging
import time
import uuid

from .models import ChatChannel, ChatMessage, Team

logger = logging.getLogger(__name__)

# How many messages we keep per channel. Enough to scroll back through a match,
# bounded so a spammy or long match cannot exhaust memory.
MAX_MESSAGES = 500
# A single message is clipped to this length (chat, not an essay).
MAX_TEXT = 500


class ChatError(Exception):
    """Raised when a message is rejected (bad channel, empty text, no access)."""


class ChatService:
    """Per-match chat buffers with team isolation and a post-match channel."""

    def __init__(self, *, max_messages: int = MAX_MESSAGES) -> None:
        self._max = max_messages
        # match_id -> channel -> [ChatMessage]
        self._messages: dict[str, dict[ChatChannel, list[ChatMessage]]] = {}
        # match_id -> channel -> set(message_id) for de-duplication across peers
        self._seen: dict[str, dict[ChatChannel, set[str]]] = {}

    # ------------------------------------------------------------------
    # access rules
    # ------------------------------------------------------------------

    @staticmethod
    def can_read(team: Team, channel: ChatChannel) -> bool:
        """Whether a player on ``team`` may read ``channel``."""
        if channel.shared:
            return True
        if channel is ChatChannel.RADIANT:
            return team is Team.RADIANT
        if channel is ChatChannel.DIRE:
            return team is Team.DIRE
        return False

    @staticmethod
    def can_write(team: Team, channel: ChatChannel) -> bool:
        """Whether a player on ``team`` may write to ``channel``.

        A player with no known side (``UNKNOWN``) may only use the shared
        channel: we will not guess a side and let them into a team room.
        """
        if channel.shared:
            return True
        return ChatService.can_read(team, channel)

    def channels_for(self, team: Team) -> list[ChatChannel]:
        """The channels a player on ``team`` can see, in display order."""
        return [c for c in ChatChannel if self.can_read(team, c)]

    # ------------------------------------------------------------------
    # writing / reading
    # ------------------------------------------------------------------

    def post(
        self,
        match_id: str,
        *,
        channel: ChatChannel,
        author_id: str,
        author_name: str,
        team: Team,
        text: str,
        message_id: str = "",
        ts: float | None = None,
    ) -> ChatMessage:
        """Append a message. Raises :class:`ChatError` if it is not allowed."""
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ChatError("пустое сообщение")
        if not self.can_write(team, channel):
            raise ChatError(
                f"нет доступа к каналу «{channel.label}» для стороны {team.label}"
            )
        message = ChatMessage(
            id=message_id or uuid.uuid4().hex[:12],
            channel=channel,
            author_id=author_id,
            author_name=author_name or author_id,
            team=team,
            text=text,
            ts=ts if ts is not None else time.time(),
        )
        bucket = self._messages.setdefault(match_id, {}).setdefault(channel, [])
        seen = self._seen.setdefault(match_id, {}).setdefault(channel, set())
        if message.id in seen:
            return message  # already have it (a peer re-sent, or a local echo)
        seen.add(message.id)
        bucket.append(message)
        if len(bucket) > self._max:
            dropped = bucket.pop(0)
            seen.discard(dropped.id)
        return message

    def history(
        self,
        match_id: str,
        *,
        team: Team,
        channel: ChatChannel | None = None,
        limit: int = 100,
    ) -> list[ChatMessage]:
        """Recent messages a player on ``team`` is allowed to see."""
        rooms = self._messages.get(match_id, {})
        channels = [channel] if channel is not None else list(ChatChannel)
        out: list[ChatMessage] = []
        for candidate in channels:
            if not self.can_read(team, candidate):
                continue
            out.extend(rooms.get(candidate, []))
        out.sort(key=lambda m: m.ts)
        return out[-limit:]

    def has_message(self, match_id: str, channel: ChatChannel, message_id: str) -> bool:
        return message_id in self._seen.get(match_id, {}).get(channel, set())

    # ------------------------------------------------------------------

    def drop(self, match_id: str) -> None:
        """Forget a match's chat (called when its server closes)."""
        self._messages.pop(match_id, None)
        self._seen.pop(match_id, None)

    def count(self, match_id: str) -> int:
        return sum(len(bucket) for bucket in self._messages.get(match_id, {}).values())
