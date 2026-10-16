"""Tests for the bounded retry on HTTP 429 (rate limited), with no real Plane.

Background: Plane allows each API key 60 requests per minute. Request 61
gets HTTP 429 "Too Many Requests", usually with a Retry-After header saying
how many seconds to wait (on a burst, Plane said 57). Before this change,
the first 429 failed the whole tool call, so a batch of ordinary reads broke
halfway through.

The rules being tested (see ratelimit.py and docs/decisions.md):
  * only requests that are safe to repeat are retried: GET, HEAD, OPTIONS,
    PUT, DELETE. POST and PATCH (creates, partial updates) never are;
  * wait what Retry-After says, or else back off 1 s, 2 s, 4 s with jitter;
  * at most 3 retries and at most 65 s of waiting in total. Then raise the
    usual RateLimitError.

The "fake HTTP layer" is httpx's MockTransport: a function that answers each
request with whatever response we script, so nothing goes over the network.
``sleep`` is replaced by a function that only records how long it was asked
to wait, so the tests run instantly.
"""

import email.utils
import logging

import httpx
import pytest

from plane_pages_mcp import ratelimit
from plane_pages_mcp.rest import PlaneREST, RateLimitError, RestError


class ScriptedPlane:
    """Answers requests from a list of (status, headers); records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.method)
        status, headers = self.responses.pop(0) if self.responses else (200, {})
        body = {"ok": True} if status == 200 else {"error_message": "RATE_LIMIT_EXCEEDED"}
        return httpx.Response(status, json=body if status != 204 else None, headers=headers)


def _client(plane: ScriptedPlane, rand=lambda: 0.5):
    waits: list[float] = []
    rest = PlaneREST("http://plane.test", "key", transport=httpx.MockTransport(plane),
                     sleep=waits.append, rand=rand)
    return rest, waits


TOO_MANY = 429


# --- which methods are retried --------------------------------------------------


@pytest.mark.parametrize("method, retried", [
    ("GET", True), ("HEAD", True), ("OPTIONS", True), ("PUT", True), ("DELETE", True),
    ("POST", False), ("PATCH", False), ("get", True),
])
def test_method_classification(method, retried):
    # Checks: the exact list of methods that may be retried.
    # Why: retrying a create (POST) could create the same work item twice.
    # This test pins the list so nobody widens it by accident.
    assert ratelimit.is_retryable(method) is retried


def test_get_is_retried_after_waiting_what_retry_after_says():
    # Checks: a read that gets 429 with "Retry-After: 2" waits exactly 2 s,
    # tries again, and returns the successful answer.
    # Why: this is the everyday case. Plane says how long to wait, and we
    # wait exactly that long, no more and no less.
    plane = ScriptedPlane((TOO_MANY, {"Retry-After": "2"}), (200, {}))
    rest, waits = _client(plane)
    assert rest.get("workspaces/ws/projects/") == {"ok": True}
    assert waits == [2.0] and plane.requests == ["GET", "GET"]


def test_without_retry_after_it_backs_off_with_jitter():
    # Checks: with no Retry-After header, the waits grow 1 s, 2 s, 4 s, each
    # scaled by a random factor between 0.5 and 1.5 (here the "random" number
    # is fixed, so the waits are predictable).
    # Why: growing waits give the limit time to recover. The randomness
    # ("jitter") stops several clients from all retrying at the same moment.
    plane = ScriptedPlane((TOO_MANY, {}), (TOO_MANY, {}), (TOO_MANY, {}), (200, {}))
    rest, waits = _client(plane, rand=lambda: 0.25)  # factor 0.5 + 0.25 = 0.75
    rest.get("x/")
    assert waits == [0.75, 1.5, 3.0]


def test_put_and_delete_are_retried():
    # Checks: DELETE (and PUT) are retried like reads.
    # Why: doing them twice leaves the same end state, so a retry is safe.
    plane = ScriptedPlane((TOO_MANY, {"Retry-After": "1"}), (204, {}))
    rest, waits = _client(plane)
    rest._request("DELETE", "x/")
    assert plane.requests == ["DELETE", "DELETE"]


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_post_and_patch_are_never_retried(method):
    # Checks: a POST or PATCH that gets 429 is sent exactly once, nothing
    # waits, and the error says it wasn't retried and why.
    # Why: the task forbids blindly retrying a create, which could make a
    # duplicate. The caller decides what to do.
    plane = ScriptedPlane((TOO_MANY, {"Retry-After": "1"}), (200, {}))
    rest, waits = _client(plane)
    with pytest.raises(RateLimitError, match=f"{method} requests are not retried"):
        rest._request(method, "x/", json={})
    assert plane.requests == [method] and waits == []


# --- the limits ------------------------------------------------------------------


def test_gives_up_after_three_retries_with_the_usual_error():
    # Checks: a read that keeps getting 429 is tried 4 times in total (1 + 3
    # retries), then raises RateLimitError with the familiar message and
    # how many retries were made.
    # Why: retrying forever would hang the tool call. A small, fixed number
    # keeps the worst case predictable.
    plane = ScriptedPlane(*[(TOO_MANY, {"Retry-After": "5"})] * 10)
    rest, waits = _client(plane)
    with pytest.raises(RateLimitError, match=r"Plane REST rate limit hit .*gave up after 3 retries"):
        rest.get("x/")
    assert len(plane.requests) == 4 and waits == [5.0, 5.0, 5.0]


def test_stops_when_the_next_wait_would_pass_the_total_cap():
    # Checks: after waiting 40 s once, a second "wait 40 s" would make 80 s,
    # more than the 65 s cap, so it gives up instead of waiting.
    # Why: the cap bounds how long one tool call can block, whatever Plane says.
    plane = ScriptedPlane(*[(TOO_MANY, {"Retry-After": "40"})] * 5)
    rest, waits = _client(plane)
    with pytest.raises(RateLimitError, match="65 s"):
        rest.get("x/")
    assert waits == [40.0] and len(plane.requests) == 2


def test_a_burst_wait_of_57_seconds_is_honoured():
    # Checks: "Retry-After: 57" (what Plane really sent on the dev stack after
    # a burst of 61 requests) is waited out, and the read then succeeds.
    # Why: this is the case the cap was raised for. Nothing frees up sooner,
    # so a shorter cap would just fail here.
    plane = ScriptedPlane((TOO_MANY, {"Retry-After": "57"}), (200, {}))
    rest, waits = _client(plane)
    assert rest.get("x/") == {"ok": True} and waits == [57.0]


def test_retry_after_longer_than_the_cap_fails_at_once():
    # Checks: if Plane asks for 120 s, more than the whole budget, we don't
    # wait at all. We raise straight away and say how long Plane asked for.
    # Why: waiting 65 s only to fail anyway would waste a minute.
    plane = ScriptedPlane((TOO_MANY, {"Retry-After": "120"}))
    rest, waits = _client(plane)
    with pytest.raises(RateLimitError, match="asked to wait 120 s"):
        rest.get("x/")
    assert waits == []


def test_other_errors_are_not_retried():
    # Checks: an HTTP 500 is raised at once, without any retry.
    # Why: only "too many requests" means "try again later". A server error
    # would most likely just fail again, and retrying would hide it.
    plane = ScriptedPlane((500, {}), (200, {}))
    rest, waits = _client(plane)
    with pytest.raises(RestError, match="500"):
        rest.get("x/")
    assert plane.requests == ["GET"] and waits == []


def test_each_retry_is_logged(caplog):
    # Checks: every retry writes a warning naming the method, the path, the
    # attempt number and the wait.
    # Why: retries hide slowness from the caller, so the logs are where an
    # operator sees that the rate limit is being hit.
    plane = ScriptedPlane((TOO_MANY, {"Retry-After": "3"}), (200, {}))
    rest, _ = _client(plane)
    with caplog.at_level(logging.WARNING, logger="plane_pages_mcp"):
        rest.get("workspaces/ws/projects/")
    assert any(
        "429" in r.message and "GET" in r.message and "workspaces/ws/projects/" in r.message
        and "retry 1/3" in r.message and "3.0 s" in r.message
        for r in caplog.records
    )


# --- reading Retry-After ------------------------------------------------------------


def test_retry_after_as_an_http_date_is_understood():
    # Checks: Retry-After may also be a date ("Wed, 21 Oct 2026 07:28:00
    # GMT"); it is turned into the number of seconds from now.
    # Why: the HTTP standard allows both forms. A proxy in front of Plane
    # could send the date form.
    now = 1_000_000.0
    header = email.utils.formatdate(now + 30, usegmt=True)
    assert ratelimit.retry_after_seconds(header, now=now) == pytest.approx(30, abs=1)


@pytest.mark.parametrize("header", [None, "", "soon", "-5x"])
def test_unreadable_retry_after_falls_back_to_backoff(header):
    # Checks: a missing or nonsense Retry-After is treated as "not given".
    # Why: then the normal backoff applies, instead of crashing or waiting
    # some random amount.
    assert ratelimit.retry_after_seconds(header, now=0.0) is None


def test_retry_after_in_the_past_means_no_wait():
    # Checks: a negative number or a date already in the past becomes 0 s.
    # Why: a clock difference must not produce a negative sleep (an error).
    assert ratelimit.retry_after_seconds("-3", now=0.0) == 0.0
