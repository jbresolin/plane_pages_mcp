"""Ask Plane's live server whether a page is open in an editor right now.

While a page is open, Plane's live server (Hocuspocus) holds the page's Yjs
document in memory, and its copy wins: anything we write to Postgres
meanwhile is overwritten on the server's next save. So update_page asks first,
and refuses if the page is open.

How we ask: Plane's live server runs Hocuspocus's Redis extension, which
subscribes to the Redis channel ``hocuspocus:<page id>`` while the page is
loaded (several live servers use it to share edits). Any instance can publish
a QueryAwareness ("who is here?") message on that channel, and the server that
has the page loaded answers with an Awareness message. No answer within a
short wait means no live server has the page loaded, i.e. nobody has it open.

Notes:
  * The extension leaves a subscription behind on the channel after the page
    closes, so "the channel has a subscriber" is NOT a usable signal. Only an
    answer is.
  * Redis pub/sub ignores the logical database number, so the same Redis URL
    the OAuth storage uses (``redis://plane-redis:6379/1``) reaches Plane's
    channels.
  * Message layouts follow Hocuspocus 2.15 (shipped in Plane v1.3.1) and the
    lib0 encoding it uses. The editor harness (tests/e2e) checks this against
    a real Plane after upgrades.
"""

from __future__ import annotations

import time
import uuid

CHANNEL_PREFIX = "hocuspocus:"
MESSAGE_AWARENESS = 1
MESSAGE_QUERY_AWARENESS = 3

# Long enough for a live server on the same network to answer (a few ms in
# practice), short enough not to slow every update noticeably.
DEFAULT_WAIT_SECONDS = 1.0


class PresenceError(RuntimeError):
    """Raised when we can't tell whether the page is open (e.g. Redis down)."""


# --- lib0 encoding (what Yjs and Hocuspocus use on the wire) -----------------


def varuint(value: int) -> bytes:
    """Unsigned int, 7 bits per byte, high bit = "more bytes follow"."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def varstring(text: str) -> bytes:
    """UTF-8 bytes prefixed with their length as a varuint."""
    encoded = text.encode("utf-8")
    return varuint(len(encoded)) + encoded


def read_varuint(buffer: bytes, position: int) -> tuple[int, int]:
    """Decode a varuint at ``position``; return (value, next position)."""
    value = shift = 0
    while True:
        byte = buffer[position]
        position += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, position


def read_varstring(buffer: bytes, position: int) -> tuple[str, int]:
    length, position = read_varuint(buffer, position)
    end = position + length
    return buffer[position:end].decode("utf-8"), end


# --- the check ----------------------------------------------------------------


class LivePresence:
    """Answers "is this page open in a Plane editor right now?".

    ``redis_client`` is a redis-py ``Redis`` (anything with ``pubsub()`` and
    ``publish()``); it is passed in so tests can use a fake.
    """

    def __init__(self, redis_client, *, sender_id: str | None = None,
                 wait_seconds: float = DEFAULT_WAIT_SECONDS) -> None:
        self._redis = redis_client
        # Every message on the channel starts with its sender's id. Ours is
        # unique so we can skip our own message when it echoes back.
        self._sender = (sender_id or f"plane-pages-mcp-{uuid.uuid4()}").encode("utf-8")
        self._wait = wait_seconds

    def is_open(self, page_id: str) -> bool:
        channel = CHANNEL_PREFIX + page_id
        try:
            subscription = self._redis.pubsub()
            try:
                # Subscribe before asking, or a fast answer could be missed.
                subscription.subscribe(channel)
                self._redis.publish(channel, self._query_message(page_id))
                return self._answer_arrives(subscription, page_id)
            finally:
                subscription.close()
        except Exception as exc:  # any Redis/network failure means "unknown"
            raise PresenceError(
                f"could not check whether page {page_id} is open in the editor: {exc}"
            ) from exc

    def _query_message(self, page_id: str) -> bytes:
        # [sender length][sender] is the Redis extension's envelope; inside
        # it is a normal Hocuspocus message: [page id][message type].
        body = varstring(page_id) + varuint(MESSAGE_QUERY_AWARENESS)
        return bytes([len(self._sender)]) + self._sender + body

    def _answer_arrives(self, subscription, page_id: str) -> bool:
        deadline = time.monotonic() + self._wait
        while (remaining := deadline - time.monotonic()) > 0:
            message = subscription.get_message(timeout=remaining)
            if message and message.get("type") == "message":
                if self._is_awareness_answer(message["data"], page_id):
                    return True
        return False

    def _is_awareness_answer(self, data: bytes, page_id: str) -> bool:
        sender_length = data[0]
        if data[1 : 1 + sender_length] == self._sender:
            return False  # our own question, echoed back by Redis
        body = data[1 + sender_length :]
        try:
            document, position = read_varstring(body, 0)
            message_type, _ = read_varuint(body, position)
        except (IndexError, UnicodeDecodeError):
            return False  # not a message we understand; keep waiting
        # Any awareness answer counts, even one listing zero client states:
        # what matters is that a live server has the page loaded.
        return document == page_id and message_type == MESSAGE_AWARENESS
