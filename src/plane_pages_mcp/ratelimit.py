"""When and how long to wait after HTTP 429 (rate limited) from Plane's REST API.

Plane limits each API key (API_KEY_RATE_LIMIT, default 60/minute) with Django
REST Framework's SimpleRateThrottle, and answers request 61 with 429 and a
Retry-After header. After a burst that wait is close to a minute (57 s
measured on the dev stack), because nothing frees up until the oldest request
in the window is 60 s old. Retrying sooner just gets another 429.

This module only decides; rest.py does the sending and sleeping. See
docs/decisions.md (2026-10-15, "Bounded retry on HTTP 429").
"""

from __future__ import annotations

import email.utils
from collections.abc import Callable

MAX_RETRIES = 3
# Total seconds one request may spend waiting across all its retries. A little
# over Plane's 60 s window, so even a burst's Retry-After (~57 s) can be
# waited out once. (The original brief said ~30 s; raised with the operator
# after measuring Plane's real Retry-After.)
MAX_TOTAL_WAIT = 65.0
# Backoff when Plane sends no usable Retry-After: 1 s, 2 s, 4 s, each scaled
# by a random factor in [0.5, 1.5) ("jitter") so clients don't retry in step.
BACKOFF_BASE = 1.0

# Which requests may be repeated after a 429, by HTTP method (RFC 9110):
#
#   GET, HEAD, OPTIONS   "safe": they only read. Repeating them changes nothing.
#   PUT, DELETE          "idempotent": doing them twice leaves the same end state
#                        as doing them once (a second DELETE finds nothing more
#                        to delete).
#   POST                 NOT retried: creates (work items, comments, cycles,
#                        projects, members...). Repeating one could create a
#                        duplicate.
#   PATCH                NOT retried: partial updates are not idempotent in
#                        general (and some trigger activity entries each time).
#
# Note: Plane checks the rate limit before running the request, so a POST
# that got 429 did nothing, and repeating it would in fact be safe. We still
# don't retry it automatically: the brief asks never to retry a create
# blindly, and a 429 could also come from a proxy in front of Plane. The
# caller gets the error and decides.
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def is_retryable(method: str) -> bool:
    return method.upper() in RETRYABLE_METHODS


def retry_after_seconds(header: str | None, *, now: float) -> float | None:
    """Seconds to wait according to a Retry-After header, or None if unusable.

    The header is either a number of seconds ("57") or an HTTP date. ``now``
    is the current Unix time (passed in so the date form can be tested).
    """
    if not header or not header.strip():
        return None
    value = header.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now)


def backoff_seconds(retry_number: int, rand: Callable[[], float]) -> float:
    """Wait before retry ``retry_number`` (1-based) when Plane gave no Retry-After."""
    return BACKOFF_BASE * 2 ** (retry_number - 1) * (0.5 + rand())


def next_wait(
    method: str,
    retries_done: int,
    waited_so_far: float,
    retry_after: float | None,
    rand: Callable[[], float],
) -> tuple[float | None, str]:
    """Decide what to do after a 429.

    Returns (seconds to wait, "") to retry, or (None, reason) to give up,
    where the reason is added to the error message.
    """
    if not is_retryable(method):
        return None, (
            f"{method.upper()} requests are not retried automatically "
            "(repeating a create or partial update could duplicate it)"
        )
    if retries_done >= MAX_RETRIES:
        return None, f"gave up after {MAX_RETRIES} retries ({waited_so_far:.0f} s waited)"
    wait = retry_after if retry_after is not None else backoff_seconds(retries_done + 1, rand)
    if waited_so_far + wait > MAX_TOTAL_WAIT:
        asked = "Plane asked to wait" if retry_after is not None else "the next backoff is"
        return None, (
            f"{asked} {wait:.0f} s, which would pass the {MAX_TOTAL_WAIT:.0f} s retry "
            f"budget ({waited_so_far:.0f} s already waited)"
        )
    return wait, ""
