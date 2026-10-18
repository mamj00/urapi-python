"""Bounded observation of one persisted job; stopping a wait never mutates it."""
from __future__ import annotations

import math
import re
import threading
import time

from .client import ApiResult, UrapiClient, UrapiClientError

_STATES = frozenset({"queued", "running", "retry_wait", "succeeded", "failed", "cancelled", "timed_out"})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})


class UrapiWaitError(Exception):
    """Local wait stopped; retain the caller's saved job ID for later observation."""
    def __init__(self, code: str, *, polls: int = 0, last_state: str | None = None):
        super().__init__("URAPI job wait: " + code)
        self.code = code
        self.polls = polls
        self.last_state = last_state


def _bounded(value: object, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def wait_for_job(
    client: UrapiClient,
    job_id: str,
    *,
    timeout_seconds: float = 60,
    poll_interval_seconds: float = 1,
    max_polls: int = 60,
    cancel_event: threading.Event | None = None,
) -> ApiResult:
    """GET only, no retries of failed requests. All four terminal states return.

    The elapsed budget is checked around requests and sleeps, not enforced by
    interrupting DNS or socket I/O. Each GET receives min(remaining, 120) seconds
    as its per-socket timeout. Cancellation interrupts sleep, not active I/O.
    Validates job identity/lifecycle, not the complete Job JSON Schema.
    """
    if (not isinstance(client, UrapiClient)
            or not _bounded(timeout_seconds, 0.01, 600)
            or not _bounded(poll_interval_seconds, 0.001, 60)
            or type(max_polls) is not int or not 1 <= max_polls <= 10000
            or cancel_event is not None and not isinstance(cancel_event, threading.Event)):
        raise UrapiWaitError("invalid_wait_options")
    if type(job_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id) is None:
        raise UrapiWaitError("invalid_job_id")
    deadline = time.monotonic() + timeout_seconds
    polls = 0
    last_state = None

    def budget() -> float:
        if cancel_event is not None and cancel_event.is_set():
            raise UrapiWaitError("wait_aborted", polls=polls, last_state=last_state)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UrapiWaitError("wait_timeout", polls=polls, last_state=last_state)
        return remaining

    while polls < max_polls:
        remaining = budget()
        # The transport accepts a minimum timeout of 10ms. Never round the
        # remaining budget upward or issue an invalid request near the deadline.
        if remaining < 0.01:
            raise UrapiWaitError("wait_timeout", polls=polls, last_state=last_state)
        polls += 1
        try:
            result = client.request("getJob", {"path": {"job_id": job_id}},
                                    {"timeout_seconds": min(remaining, 120)})
        except UrapiClientError:
            budget()
            raise
        budget()
        if not isinstance(result, ApiResult) or type(result.status) is not int or result.status != 200:
            raise UrapiWaitError("invalid_job_response", polls=polls, last_state=last_state)
        data = result.data
        if (type(data) is not dict or type(data.get("id")) is not str or data["id"] != job_id
                or type(data.get("state")) is not str or data["state"] not in _STATES):
            raise UrapiWaitError("invalid_job_response", polls=polls, last_state=last_state)
        last_state = data["state"]
        if last_state in _TERMINAL:
            return result
        if polls >= max_polls:
            raise UrapiWaitError("poll_limit_exceeded", polls=polls, last_state=last_state)
        next_poll_at = time.monotonic() + poll_interval_seconds
        while True:
            remaining = budget()
            delay = min(remaining, next_poll_at - time.monotonic())
            if delay <= 0:
                break
            if cancel_event is not None:
                cancel_event.wait(delay)
            else:
                time.sleep(delay)
            budget()
    raise UrapiWaitError("poll_limit_exceeded", polls=polls, last_state=last_state)
