"""Tests for the HTTP API layer: request parsing, cache policy, streaming events."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from ehunter import api
from ehunter.service import MAX_DOMAINS, ScanFlags, build_doc, cache_covers, normalize_domains


class NormalizeTests(unittest.TestCase):
    def test_list_input(self):
        domains, errors = normalize_domains(["https://www.Acme.com/team", "beta.io"])
        self.assertEqual(domains, ["acme.com", "beta.io"])
        self.assertEqual(errors, [])

    def test_string_input(self):
        domains, _ = normalize_domains("acme.com, beta.io;gamma.dev\nacme.com")
        self.assertEqual(domains, ["acme.com", "beta.io", "gamma.dev"])

    def test_invalid_entries_are_reported_not_raised(self):
        domains, errors = normalize_domains(["nope", "acme.com"])
        self.assertEqual(domains, ["acme.com"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["input"], "nope")

    def test_limit(self):
        many = [f"site{i}.com" for i in range(MAX_DOMAINS + 3)]
        domains, errors = normalize_domains(many)
        self.assertEqual(len(domains), MAX_DOMAINS)
        self.assertEqual(len(errors), 3)

    def test_empty(self):
        self.assertEqual(normalize_domains(None), ([], []))
        self.assertEqual(normalize_domains(""), ([], []))


class CachePolicyTests(unittest.TestCase):
    DOC = {"flags": {"isLinkedin": True, "isVerify": False}, "rows": [{"x": 1}]}

    def test_cover_when_flags_match(self):
        self.assertTrue(cache_covers(self.DOC, ScanFlags(is_verify=False, is_linkedin=True)))

    def test_linkedin_request_needs_a_linkedin_run(self):
        thin = {"flags": {"isLinkedin": False, "isVerify": False}, "rows": []}
        self.assertFalse(cache_covers(thin, ScanFlags(is_linkedin=True)))

    def test_verify_request_needs_a_verify_run(self):
        self.assertFalse(cache_covers(self.DOC, ScanFlags(is_verify=True, is_linkedin=True)))

    def test_verified_cache_serves_both(self):
        rich = {"flags": {"isLinkedin": True, "isVerify": True}, "rows": [{}]}
        self.assertTrue(cache_covers(rich, ScanFlags(is_verify=False, is_linkedin=True)))
        self.assertTrue(cache_covers(rich, ScanFlags(is_verify=True, is_linkedin=True)))

    def test_missing_doc(self):
        self.assertFalse(cache_covers(None, ScanFlags()))


class BuildDocTests(unittest.TestCase):
    def test_shape(self):
        result = {"rows": [{"full_name": "Jane"}], "meta": {"pattern": "{first}"},
                  "counts": {"people": 1}}
        doc = build_doc("acme.com", result, ScanFlags(is_verify=True, is_linkedin=False))
        self.assertEqual(doc["domain"], "acme.com")
        self.assertEqual(doc["rows"][0]["full_name"], "Jane")
        self.assertEqual(doc["flags"], {"isVerify": True, "isLinkedin": False})
        self.assertIn("scanned_at", doc)
        self.assertIn("scan_ms", doc)


class DowngradedVerifyTests(unittest.TestCase):
    """isVerify requested, but port 25 was blocked so verification never ran."""

    RESULT = {"rows": [{"full_name": "Jane"}], "meta": {}, "counts": {"people": 1},
              "verify_ran": False, "warnings": ["port 25 is blocked"]}

    def test_doc_records_what_actually_ran(self):
        doc = build_doc("acme.com", self.RESULT, ScanFlags(is_verify=True, is_linkedin=True))
        self.assertEqual(doc["flags"], {"isVerify": False, "isLinkedin": True})
        self.assertEqual(doc["warnings"], ["port 25 is blocked"])

    def test_downgraded_doc_does_not_satisfy_a_later_verify_request(self):
        doc = build_doc("acme.com", self.RESULT, ScanFlags(is_verify=True, is_linkedin=True))
        self.assertFalse(cache_covers(doc, ScanFlags(is_verify=True, is_linkedin=True)))
        self.assertTrue(cache_covers(doc, ScanFlags(is_verify=False, is_linkedin=True)))

    def test_stream_emits_a_warning_event(self):
        events = _collect(["acme.com"], ScanFlags(is_verify=True), FakeStore(doc=None),
                          run_result=self.RESULT)
        warning = next(e for e in events if e["type"] == "warning")
        self.assertEqual(warning["message"], "port 25 is blocked")
        result = next(e for e in events if e["type"] == "result")
        self.assertEqual(result["warnings"], ["port 25 is blocked"])


class FakeStore:
    def __init__(self, doc=None):
        self.doc = doc
        self.upserts = []

    def get(self, domain):
        return self.doc

    def upsert(self, domain, doc):
        self.upserts.append(doc)
        return True


def _collect(domain_list, flags, store, run_result=None, force=False):
    """Drive the streaming generator without a server."""
    events = []

    def fake_run(domain, scan_flags, log=None, progress=None):
        if log:
            log("  [i] fake scan started")
        return run_result or {"rows": [{"full_name": "Jane Doe"}], "meta": {}, "counts": {"people": 1}}

    with mock.patch.object(api, "store", return_value=store), \
         mock.patch.object(api, "run_domain", side_effect=fake_run):
        async def go():
            async for event in api._stream(domain_list, flags, force, lambda e: e):
                events.append(event)
        asyncio.run(go())
    return events


class StreamTests(unittest.TestCase):
    def test_uncached_domain_streams_start_log_result_done(self):
        store = FakeStore(doc=None)
        events = _collect(["acme.com"], ScanFlags(is_linkedin=False), store)
        kinds = [e["type"] for e in events]

        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("log", kinds)
        result = next(e for e in events if e["type"] == "result")
        self.assertEqual(result["domain"], "acme.com")
        self.assertEqual(result["rows"][0]["full_name"], "Jane Doe")
        self.assertTrue(result["cached"])            # upsert reported True
        self.assertEqual(len(store.upserts), 1)

    def test_cached_domain_skips_scanning(self):
        store = FakeStore(doc={
            "flags": {"isLinkedin": True, "isVerify": False},
            "rows": [{"full_name": "Cached"}],
            "counts": {"people": 1},
            "meta": {},
            "scanned_at": "2026-01-01T00:00:00Z",
        })
        events = _collect(["acme.com"], ScanFlags(is_linkedin=True), store,
                          run_result={"rows": [], "meta": {}, "counts": {}})
        kinds = [e["type"] for e in events]

        self.assertEqual(kinds[:2], ["cached", "result"])
        self.assertNotIn("start", kinds)
        self.assertTrue(next(e for e in events if e["type"] == "result")["cached"])
        self.assertEqual(store.upserts, [])

    def test_force_rescans_even_when_cached(self):
        store = FakeStore(doc={"flags": {"isLinkedin": True, "isVerify": False},
                               "rows": [{"full_name": "Cached"}], "counts": {}, "meta": {}})
        events = _collect(["acme.com"], ScanFlags(is_linkedin=True), store, force=True)
        kinds = [e["type"] for e in events]
        self.assertIn("start", kinds)
        self.assertNotIn("cached", kinds)

    def test_multiple_domains_stream_one_result_each(self):
        store = FakeStore(doc=None)
        events = _collect(["a.com", "b.io"], ScanFlags(is_linkedin=False), store)
        results = [e for e in events if e["type"] == "result"]
        self.assertEqual([r["domain"] for r in results], ["a.com", "b.io"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["domains"], 2)


class PermuteMaxTests(unittest.TestCase):
    def test_defaults_are_nineteen_everywhere(self):
        import inspect

        from ehunter.service import ScanFlags
        from ehunter.webapp import run_scan

        self.assertEqual(ScanFlags().permute_max, 19)
        self.assertEqual(
            inspect.signature(run_scan).parameters["permute_max"].default, 19
        )

    def test_body_accepts_a_lower_value(self):
        body = api.ScanBody(domains=["acme.com"], permuteMax=6)
        self.assertEqual(body.permuteMax, 6)

    def test_body_rejects_out_of_range(self):
        with self.assertRaises(Exception):
            api.ScanBody(domains=["acme.com"], permuteMax=99)


class PartialStreamTests(unittest.TestCase):
    def test_partial_rows_are_forwarded(self):
        events = []

        def fake_run(domain, flags, log=None, progress=None):
            if progress:
                progress({"stage": "discovered", "rows": [{"full_name": "Jane",
                                                           "email": ""}],
                          "removed": [], "total": 1})
            return {"rows": [{"full_name": "Jane", "email": "jane@acme.com"}],
                    "meta": {}, "counts": {"people": 1}}

        with mock.patch.object(api, "store", return_value=FakeStore(doc=None)), \
             mock.patch.object(api, "run_domain", side_effect=fake_run):
            async def go():
                async for event in api._stream(["acme.com"], ScanFlags(is_linkedin=False),
                                               False, lambda e: e):
                    events.append(event)
            asyncio.run(go())

        partials = [e for e in events if e["type"] == "partial"]
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0]["stage"], "discovered")
        self.assertEqual(partials[0]["domain"], "acme.com")
        self.assertEqual(partials[0]["rows"][0]["full_name"], "Jane")
        self.assertEqual(partials[0]["total"], 1)


class RouteTests(unittest.TestCase):
    def test_routes_registered(self):
        paths = {route.path for route in api.app.routes if getattr(route, "path", "")}
        for expected in ("/api/scan", "/api/domains", "/api/domains/{domain}",
                         "/api/health"):
            self.assertIn(expected, paths)

    def test_encoder_formats(self):
        ndjson_encode, ndjson_type = api._encoder("ndjson")
        self.assertTrue(ndjson_encode({"type": "x"}).endswith("\n"))
        self.assertEqual(ndjson_type, "application/x-ndjson")

        sse_encode, sse_type = api._encoder("sse")
        self.assertTrue(sse_encode({"type": "x"}).startswith("data: "))
        self.assertTrue(sse_encode({"type": "x"}).endswith("\n\n"))
        self.assertEqual(sse_type, "text/event-stream")


if __name__ == "__main__":
    unittest.main()
