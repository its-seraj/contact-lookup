"""Tests for the Hugging Face keep-alive / wake helpers (no real network, no real sleeping)."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from enrivo import keepalive


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_ok():
    return _Response(json.dumps({"status": "ok"}).encode())


class PingTests(unittest.TestCase):
    def _ping(self, outcome):
        with mock.patch.object(keepalive.urllib.request, "urlopen", side_effect=[outcome]):
            return keepalive.ping("https://x.hf.space/health")

    def test_json_200_is_awake(self):
        self.assertTrue(self._ping(_json_ok()))

    def test_html_200_is_not_awake(self):
        """A sleeping Space can serve an HTML placeholder with status 200."""
        self.assertFalse(self._ping(_Response(b"<html>This Space is sleeping</html>")))

    def test_errors_are_not_awake(self):
        http = urllib.error.HTTPError("http://x", 503, "unavailable", {}, io.BytesIO(b""))
        for outcome in (http, urllib.error.URLError("refused"), TimeoutError()):
            self.assertFalse(self._ping(outcome))

    def test_non_200_is_not_awake(self):
        self.assertFalse(self._ping(_Response(b"{}", status=204)))


class WakeTests(unittest.TestCase):
    def test_returns_true_as_soon_as_it_answers(self):
        sleeps = []
        with mock.patch.object(keepalive, "ping", side_effect=[False, False, True]):
            woke = keepalive.wake("u", wait=60, interval=5, _sleep=sleeps.append,
                                  _clock=lambda: 0.0)
        self.assertTrue(woke)
        self.assertEqual(sleeps, [5, 5])

    def test_gives_up_after_the_deadline(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        with mock.patch.object(keepalive, "ping", return_value=False) as pinged:
            woke = keepalive.wake("u", wait=20, interval=5, _sleep=sleep, _clock=lambda: now[0])
        self.assertFalse(woke)
        self.assertLessEqual(pinged.call_count, 5)       # 0,5,10,15 (+ final) not unbounded

    def test_zero_wait_does_nothing(self):
        with mock.patch.object(keepalive, "ping") as pinged:
            self.assertFalse(keepalive.wake("u", wait=0))
        pinged.assert_not_called()


class AutostartTests(unittest.TestCase):
    def _autostart(self, **environ):
        clean = {"KEEPALIVE": "", "SPACE_ID": "", "SPACE_HOST": "", "KEEPALIVE_URLS": "",
                 "KEEPALIVE_INTERVAL_MINUTES": ""}
        clean.update(environ)
        with mock.patch.dict("os.environ", clean), \
                mock.patch.object(keepalive.threading, "Thread") as thread:
            stop = keepalive.autostart(["https://auth.hf.space/health"])
        return stop, thread

    def test_off_outside_a_space(self):
        stop, thread = self._autostart()
        self.assertIsNone(stop)
        thread.assert_not_called()

    def test_on_inside_a_space_and_pings_auth_and_self(self):
        stop, thread = self._autostart(SPACE_ID="its-seraj/contact-lookup",
                                       SPACE_HOST="its-seraj-contact-lookup.hf.space")
        self.assertIsNotNone(stop)
        urls, interval = thread.call_args.kwargs["args"][:2]
        self.assertEqual(urls, ["https://auth.hf.space/health",
                                "https://its-seraj-contact-lookup.hf.space/api/health"])
        self.assertEqual(interval, 30 * 60)
        thread.return_value.start.assert_called_once()

    def test_explicit_off_beats_space_detection(self):
        stop, _ = self._autostart(KEEPALIVE="0", SPACE_ID="a/b")
        self.assertIsNone(stop)

    def test_explicit_on_locally_with_extra_urls_and_interval(self):
        stop, thread = self._autostart(KEEPALIVE="1", KEEPALIVE_URLS="https://a/x, https://b/y",
                                       KEEPALIVE_INTERVAL_MINUTES="10")
        self.assertIsNotNone(stop)
        urls, interval = thread.call_args.kwargs["args"][:2]
        self.assertEqual(urls[1:], ["https://a/x", "https://b/y"])
        self.assertEqual(interval, 600)

    def test_duplicate_targets_are_pinged_once(self):
        _, thread = self._autostart(KEEPALIVE="1",
                                    KEEPALIVE_URLS="https://auth.hf.space/health")
        self.assertEqual(thread.call_args.kwargs["args"][0], ["https://auth.hf.space/health"])


if __name__ == "__main__":
    unittest.main()
