from __future__ import annotations
from contextlib import contextmanager
import importlib
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from urapi import ApiResult, UrapiClient, UrapiApiError, UrapiClientError, UrapiWaitError, wait_for_job
wait_module = importlib.import_module("urapi.wait_for_job")

JOB_ID = "job_persisted_fixture"


def result(state, job_id=JOB_ID, status=200):
    return ApiResult({"id": job_id, "state": state}, status, "request_fixture")


class FixtureClient(UrapiClient):
    def __init__(self, responses):
        super().__init__(base_url="https://api.example.invalid", api_key="synthetic_fixture_key")
        self.responses = iter(responses)
        self.calls = []
    def request(self, operation_id, input=None, options=None):
        self.calls.append((operation_id, input, options))
        response = next(self.responses)
        if callable(response):
            response = response()
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []
    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


@contextmanager
def clocked():
    clock = Clock()
    with patch.object(wait_module.time, "monotonic", lambda: clock.now), patch.object(wait_module.time, "sleep", clock.sleep):
        yield clock


class WaitTests(unittest.TestCase):
    def test_all_terminal_states_return_without_sleep(self):
        for state in ("succeeded", "failed", "cancelled", "timed_out"):
            with self.subTest(state=state), clocked() as clock:
                response = result(state)
                client = FixtureClient([response])
                self.assertIs(wait_for_job(client, JOB_ID), response)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(clock.sleeps, [])

    def test_nonterminal_lifecycle_uses_only_same_get(self):
        with clocked() as clock:
            client = FixtureClient([result(state) for state in ("queued", "running", "retry_wait", "succeeded")])
            self.assertEqual(wait_for_job(client, JOB_ID, timeout_seconds=5, poll_interval_seconds=0.5).data["state"], "succeeded")
            self.assertEqual(clock.sleeps, [0.5]*3)
            for index, call in enumerate(client.calls):
                self.assertEqual(call, ("getJob", {"path": {"job_id": JOB_ID}}, {"timeout_seconds": 5 - index*0.5}))

    def test_invalid_options_and_ids_do_not_request(self):
        values = [True, False, float("nan"), float("inf"), -1, 0, 10**1000, "1", None]
        for option in ("timeout_seconds", "poll_interval_seconds", "max_polls"):
            for value in values:
                with self.subTest(option=option, kind=type(value).__name__):
                    client = FixtureClient([])
                    with self.assertRaises(UrapiWaitError) as caught:
                        wait_for_job(client, JOB_ID, **{option: value})
                    self.assertEqual(caught.exception.code, "invalid_wait_options")
                    self.assertEqual(client.calls, [])
        for options in ({"timeout_seconds": 601}, {"poll_interval_seconds": 61}, {"max_polls": 10001}, {"max_polls": 1.5}, {"cancel_event": object()}):
            with self.assertRaises(UrapiWaitError):
                wait_for_job(FixtureClient([]), JOB_ID, **options)
        for job_id in ("", "..", "tenant/other", "x"*81, "url?secret", True, None, "\ud800"):
            with self.assertRaises(UrapiWaitError) as caught:
                wait_for_job(FixtureClient([]), job_id)
            self.assertEqual(caught.exception.code, "invalid_job_id")
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(UrapiWaitError):
            wait_for_job(object(), JOB_ID)

    def test_poll_bound_returns_last_valid_state(self):
        with clocked() as clock:
            client = FixtureClient([result("queued"), result("running")])
            with self.assertRaises(UrapiWaitError) as caught:
                wait_for_job(client, JOB_ID, max_polls=2)
            self.assertEqual((caught.exception.code, caught.exception.polls, caught.exception.last_state), ("poll_limit_exceeded", 2, "running"))
            self.assertEqual(clock.sleeps, [1])

    def test_wrong_identity_shape_state_and_status(self):
        invalid = [result("succeeded", "other_tenant_job"), result("unknown_private_state"), result("succeeded", status=202), result("succeeded", status=True), ApiResult([], 200, None), ApiResult({"id": JOB_ID}, 200, None), ApiResult({"id": JOB_ID, "state": []}, 200, None), {"id": JOB_ID, "state": "succeeded"}]
        for response in invalid:
            with clocked(), self.subTest(kind=type(response).__name__):
                client = FixtureClient([result("running"), response])
                with self.assertRaises(UrapiWaitError) as caught:
                    wait_for_job(client, JOB_ID)
                self.assertEqual((caught.exception.code, caught.exception.polls, caught.exception.last_state), ("invalid_job_response", 2, "running"))
                self.assertNotIn("private", str(caught.exception))

    def test_deadline_prevents_early_poll_when_interval_exceeds_budget(self):
        with clocked() as clock:
            client = FixtureClient([result("queued")])
            with self.assertRaises(UrapiWaitError) as caught:
                wait_for_job(client, JOB_ID, timeout_seconds=0.5, poll_interval_seconds=1)
            self.assertEqual(caught.exception.code, "wait_timeout")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(clock.sleeps, [0.5])

    def test_late_terminal_response_does_not_override_budget(self):
        with clocked() as clock:
            def late():
                clock.now += 2
                return result("succeeded")
            client = FixtureClient([late])
            with self.assertRaises(UrapiWaitError) as caught:
                wait_for_job(client, JOB_ID, timeout_seconds=1)
            self.assertEqual((caught.exception.code, caught.exception.polls, caught.exception.last_state), ("wait_timeout", 1, None))

    def test_less_than_transport_minimum_stops_before_next_get(self):
        with clocked() as clock:
            client = FixtureClient([result("queued")])
            with self.assertRaises(UrapiWaitError) as caught:
                wait_for_job(client, JOB_ID, timeout_seconds=0.015, poll_interval_seconds=0.01)
            self.assertEqual(caught.exception.code, "wait_timeout")
            self.assertEqual(len(client.calls), 1)

    def test_long_budget_caps_individual_socket_timeout(self):
        with clocked():
            client = FixtureClient([result("succeeded")])
            wait_for_job(client, JOB_ID, timeout_seconds=600)
            self.assertEqual(client.calls[0][2], {"timeout_seconds": 120})

    def test_early_sleep_wakeup_does_not_accelerate_polling(self):
        with clocked() as clock:
            def early(duration):
                clock.sleeps.append(duration)
                clock.now += duration/2 if len(clock.sleeps) == 1 else duration
            with patch.object(wait_module.time, "sleep", early):
                client = FixtureClient([result("queued"), result("succeeded")])
                wait_for_job(client, JOB_ID, poll_interval_seconds=1)
            self.assertEqual(clock.sleeps, [1, 0.5])
            self.assertEqual(clock.now, 101)

    def test_cancel_before_get_after_get_and_during_sleep(self):
        for phase in ("before", "after", "sleep"):
            with clocked(), self.subTest(phase=phase):
                event = threading.Event()
                if phase == "before":
                    event.set()
                def response():
                    if phase == "after":
                        event.set()
                    return result("succeeded" if phase == "after" else "queued")
                client = FixtureClient([response])
                def cancel_sleep(duration):
                    event.set()
                    return True
                with patch.object(event, "wait", cancel_sleep), self.assertRaises(UrapiWaitError) as caught:
                    wait_for_job(client, JOB_ID, cancel_event=event)
                self.assertEqual(caught.exception.code, "wait_aborted")
                self.assertEqual(len(client.calls), 0 if phase == "before" else 1)

    def test_api_and_transport_errors_propagate_once(self):
        errors = [UrapiApiError(status, "fixture_error") for status in (401, 403, 404, 429, 503)] + [UrapiClientError("request_timeout"), UrapiClientError("transport_error")]
        for error in errors:
            with clocked() as clock:
                client = FixtureClient([error])
                with self.assertRaises(UrapiClientError) as caught:
                    wait_for_job(client, JOB_ID)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(clock.sleeps, [])

    def test_late_error_reports_elapsed_wait(self):
        with clocked() as clock:
            def late():
                clock.now += 2
                return UrapiClientError("request_timeout")
            with self.assertRaises(UrapiWaitError) as caught:
                wait_for_job(FixtureClient([late]), JOB_ID, timeout_seconds=1)
            self.assertEqual(caught.exception.code, "wait_timeout")


if __name__ == "__main__":
    unittest.main()
