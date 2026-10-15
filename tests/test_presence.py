"""Tests for presence.LivePresence: "is this page open in an editor right now?"

How the real thing works: Plane's live server (Hocuspocus) keeps each open page
in memory and listens on a Redis channel named "hocuspocus:<page id>". If we
publish a "who is here?" message (QueryAwareness) on that channel, the server
that has the page loaded answers with an Awareness message. If no server has
it loaded, nobody answers. So: an answer means open, silence means not open.

These tests replace Redis with a small fake, so they need no Plane and no
Redis. The byte layouts below come from Hocuspocus 2.15 (the version Plane
v1.3.1 ships), and were checked against a real Plane dev stack.
"""

import pytest

from plane_pages_mcp import presence
from plane_pages_mcp.presence import LivePresence, PresenceError

PAGE = "6e7c28fc-1cb9-4896-906f-ed85f8e007d4"


def _frame(sender: bytes, body: bytes) -> bytes:
    """How Hocuspocus's Redis extension wraps every message on the channel."""
    return bytes([len(sender)]) + sender + body


def _awareness_reply(page_id: str, client_count: int) -> bytes:
    """What a live server sends back when the page is loaded there."""
    update = presence.varuint(client_count)
    for client_id in range(client_count):
        update += presence.varuint(client_id + 1) + presence.varuint(0) + presence.varstring("{}")
    body = presence.varstring(page_id) + presence.varuint(presence.MESSAGE_AWARENESS)
    body += presence.varuint(len(update)) + update
    return _frame(b"host-live-1", body)


class FakePubSub:
    """Stands in for redis-py's PubSub: replays canned messages once subscribed."""

    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._queue: list[dict] = []

    def subscribe(self, channel: str) -> None:
        self._redis.subscribed.append(channel)
        self._queue.append({"type": "subscribe", "data": 1})

    def get_message(self, timeout: float = 0.0):
        return self._queue.pop(0) if self._queue else None

    def deliver(self, data: bytes) -> None:
        self._queue.append({"type": "message", "data": data})

    def close(self) -> None:
        pass


class FakeRedis:
    def __init__(self, answer_with=None, fail: bool = False) -> None:
        self.answer_with = answer_with  # a function(published_bytes) -> list of replies
        self.fail = fail
        self.subscribed: list[str] = []
        self.published: list[tuple[str, bytes]] = []
        self._pubsub: FakePubSub | None = None

    def pubsub(self) -> FakePubSub:
        if self.fail:
            raise ConnectionError("redis is down")
        self._pubsub = FakePubSub(self)
        return self._pubsub

    def publish(self, channel: str, data: bytes) -> int:
        self.published.append((channel, data))
        # Our own message also comes back to us, as it does in real Redis.
        self._pubsub.deliver(data)
        for reply in (self.answer_with(data) if self.answer_with else []):
            self._pubsub.deliver(reply)
        return 1


def test_query_message_has_the_exact_hocuspocus_layout():
    # Checks: the bytes we publish are exactly what Hocuspocus expects:
    # [sender-length][sender] then [page id as varstring][3 = QueryAwareness],
    # on channel "hocuspocus:<page id>".
    # Why: if one byte is off, the live server silently ignores the message,
    # and every page would then look "not open". That would quietly disable
    # the protection, so the layout is pinned exactly.
    redis = FakeRedis()
    LivePresence(redis, sender_id="mcp-test", wait_seconds=0.05).is_open(PAGE)
    channel, data = redis.published[0]
    assert channel == f"hocuspocus:{PAGE}"
    assert data == bytes([8]) + b"mcp-test" + bytes([36]) + PAGE.encode() + bytes([3])


def test_page_is_open_when_a_live_server_answers():
    # Checks: an Awareness answer from a live server means "open".
    # Why: this is the case where an MCP write would be lost, so it must be
    # detected.
    redis = FakeRedis(answer_with=lambda _: [_awareness_reply(PAGE, 1)])
    assert LivePresence(redis, wait_seconds=0.05).is_open(PAGE) is True


def test_answer_with_zero_clients_still_counts_as_open():
    # Checks: an answer listing no client states still means "open".
    # Why: on the dev stack, headless browser tabs had the page open but
    # reported no awareness state. What matters is that the live server has
    # the page loaded, because then its copy is the one that gets saved.
    redis = FakeRedis(answer_with=lambda _: [_awareness_reply(PAGE, 0)])
    assert LivePresence(redis, wait_seconds=0.05).is_open(PAGE) is True


def test_page_is_not_open_when_nobody_answers():
    # Checks: silence (only our own message echoing back) means "not open".
    # Why: our own message always comes back on the channel. It must not be
    # mistaken for an answer, or every page would look open and every update
    # would be refused.
    redis = FakeRedis()
    assert LivePresence(redis, wait_seconds=0.05).is_open(PAGE) is False


def test_answers_about_a_different_message_type_are_ignored():
    # Checks: other traffic on the channel (e.g. a sync message, type 0)
    # doesn't count as an answer.
    # Why: pages that are being edited send sync messages all the time. Only
    # the awareness answer to our question is evidence.
    sync = _frame(b"host-live-1", presence.varstring(PAGE) + presence.varuint(0) + b"\x00")
    redis = FakeRedis(answer_with=lambda _: [sync])
    assert LivePresence(redis, wait_seconds=0.05).is_open(PAGE) is False


def test_redis_failure_raises_a_clear_error():
    # Checks: if Redis can't be reached, is_open raises PresenceError instead
    # of answering "not open".
    # Why: guessing "not open" when we can't tell would let a write through
    # that the live server might then overwrite. The caller refuses the
    # write with a clear message instead.
    with pytest.raises(PresenceError, match="could not check"):
        LivePresence(FakeRedis(fail=True), wait_seconds=0.05).is_open(PAGE)


@pytest.mark.parametrize("value", [0, 1, 127, 128, 300, 16384])
def test_varuint_round_trips(value):
    # Checks: our encoder and decoder for lib0 "varuint" numbers agree,
    # including the 1-byte/2-byte/3-byte boundaries (127/128, 16383/16384).
    # Why: every length and type in the protocol is a varuint. A page id is
    # 36 characters (1 byte), but awareness payloads can exceed 127 bytes.
    decoded, end = presence.read_varuint(presence.varuint(value), 0)
    assert decoded == value and end == len(presence.varuint(value))
