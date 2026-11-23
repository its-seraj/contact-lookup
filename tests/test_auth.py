"""Tests for /api/token: the caller is verified against the auth service before any token
is generated or returned."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from ehunter import api, auth

USER = {"id": "u1", "email": "a@b.co", "name": "Seraj", "picture": "", "roles": ["user"]}
ME_OK = {"success": True, "data": {"user": {
    "id": "u1", "email": "a@b.co", "name": "Seraj", "picture": "p", "roles": ["user"]}}}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload):
    return mock.patch.object(auth.urllib.request, "urlopen",
                             return_value=_Response(json.dumps(payload).encode()))


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"{}"))


class FetchUserTests(unittest.TestCase):
    def test_success_is_normalised(self):
        with _urlopen_returning(ME_OK):
            user = auth.fetch_user("Bearer abc")
        self.assertEqual(user["id"], "u1")
        self.assertEqual(user["roles"], ["user"])

    def test_header_is_forwarded_to_the_me_endpoint(self):
        with _urlopen_returning(ME_OK) as opened:
            auth.fetch_user("Bearer abc")
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, "https://its-seraj-auth-service.hf.space/api/auth/me")
        self.assertEqual(request.get_header("Authorization"), "Bearer abc")

    def test_auth_url_is_configurable(self):
        with mock.patch.dict("os.environ", {"AUTH_SERVICE_URL": "https://auth.example/"}), \
                _urlopen_returning(ME_OK) as opened:
            auth.fetch_user("Bearer abc")
        self.assertEqual(opened.call_args[0][0].full_url, "https://auth.example/api/auth/me")

    def test_rejected_token_is_401(self):
        for code in (401, 403):
            with mock.patch.object(auth.urllib.request, "urlopen", side_effect=_http_error(code)):
                with self.assertRaises(auth.AuthError) as ctx:
                    auth.fetch_user("Bearer bad")
            self.assertEqual(ctx.exception.status, 401)

    def test_upstream_failures_are_502_not_401(self):
        for failure in (_http_error(500), urllib.error.URLError("down"), TimeoutError()):
            with mock.patch.object(auth.urllib.request, "urlopen", side_effect=failure), \
                    mock.patch.object(auth, "wake", return_value=False):
                with self.assertRaises(auth.AuthError) as ctx:
                    auth.fetch_user("Bearer abc")
            self.assertEqual(ctx.exception.status, 502)

    def test_sleeping_auth_service_is_woken_then_retried(self):
        """First call fails like a cold Space; wake() succeeds; the retry returns the user."""
        outcomes = [urllib.error.URLError("sleeping"), _Response(json.dumps(ME_OK).encode())]
        with mock.patch.object(auth.urllib.request, "urlopen", side_effect=outcomes) as opened, \
                mock.patch.object(auth, "wake", return_value=True) as woken:
            user = auth.fetch_user("Bearer abc")
        self.assertEqual(user["id"], "u1")
        self.assertEqual(opened.call_count, 2)
        woken.assert_called_once()
        self.assertEqual(woken.call_args[0][0],
                         "https://its-seraj-auth-service.hf.space/health")

    def test_wake_failure_is_502_without_a_second_verify_call(self):
        with mock.patch.object(auth.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("sleeping")) as opened, \
                mock.patch.object(auth, "wake", return_value=False):
            with self.assertRaises(auth.AuthError) as ctx:
                auth.fetch_user("Bearer abc")
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(opened.call_count, 1)

    def test_definite_answers_never_trigger_a_wake(self):
        """A 401 or a bad payload is the auth service speaking: do not poll it."""
        with mock.patch.object(auth, "wake") as woken:
            with mock.patch.object(auth.urllib.request, "urlopen", side_effect=_http_error(401)):
                with self.assertRaises(auth.AuthError):
                    auth.fetch_user("Bearer bad")
            with _urlopen_returning({"success": False}):
                with self.assertRaises(auth.AuthError):
                    auth.fetch_user("Bearer bad")
        woken.assert_not_called()

    def test_wake_can_be_disabled(self):
        with mock.patch.dict("os.environ", {"AUTH_WAKE_SECONDS": "0"}), \
                mock.patch.object(auth.urllib.request, "urlopen",
                                  side_effect=urllib.error.URLError("down")), \
                mock.patch.object(auth, "wake", return_value=False) as woken:
            with self.assertRaises(auth.AuthError):
                auth.fetch_user("Bearer abc")
        self.assertEqual(woken.call_args.kwargs["wait"], 0.0)

    def test_unsuccessful_or_userless_payloads_are_401(self):
        for payload in ({"success": False}, {"success": True, "data": {}},
                        {"success": True, "data": {"user": {"email": "x"}}}, []):
            with _urlopen_returning(payload):
                with self.assertRaises(auth.AuthError) as ctx:
                    auth.fetch_user("Bearer abc")
            self.assertEqual(ctx.exception.status, 401, payload)


class FakeTokens:
    """Same contract as TokenStore, in memory."""

    def __init__(self):
        self.docs = {}
        self.issued = 0

    def get(self, user_id):
        return self.docs.get(user_id)

    def find_user(self, token):
        for uid, doc in self.docs.items():
            if doc["token"] == token:
                return {"id": uid, "email": "a@b.co", "name": "Seraj", "picture": "", "roles": []}
        return None

    def issue(self, user, rotate=False):
        existing = self.docs.get(user["id"])
        if existing and not rotate:
            return existing, False
        self.issued += 1
        doc = {"token": f"eh_{self.issued}", "created_at": "2026-11-23T00:00:00+00:00"}
        self.docs[user["id"]] = doc
        return doc, True


try:
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001 - httpx is a test-only dependency
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi TestClient needs httpx")
class TokenRouteTests(unittest.TestCase):
    def setUp(self):
        self.tokens = FakeTokens()
        patcher = mock.patch.object(api, "tokens", return_value=self.tokens)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)

    def _verified(self, user=USER):
        return mock.patch.object(auth, "fetch_user", return_value=user)

    def test_missing_or_malformed_header_is_401_and_issues_nothing(self):
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
            for method in (self.client.post, self.client.get):
                response = method("/api/token", headers=headers)
                self.assertEqual(response.status_code, 401, headers)
        self.assertEqual(self.tokens.issued, 0)

    def test_failed_verification_issues_nothing(self):
        with mock.patch.object(auth, "fetch_user", side_effect=auth.AuthError(401, "bad")):
            response = self.client.post("/api/token", headers={"Authorization": "Bearer x"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.tokens.issued, 0)

    def test_auth_service_outage_is_502(self):
        with mock.patch.object(auth, "fetch_user", side_effect=auth.AuthError(502, "down")):
            response = self.client.get("/api/token", headers={"Authorization": "Bearer x"})
        self.assertEqual(response.status_code, 502)

    def test_post_generates_then_returns_the_same_token(self):
        with self._verified() as fetch:
            first = self.client.post("/api/token", headers={"Authorization": "Bearer jwt"})
            second = self.client.post("/api/token", headers={"Authorization": "Bearer jwt"})
        fetch.assert_called_with("Bearer jwt")
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json()["created"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["token"], second.json()["token"])
        self.assertEqual(first.json()["user"]["email"], "a@b.co")
        self.assertNotIn("roles", first.json()["user"])

    def test_rotate_replaces_the_token(self):
        headers = {"Authorization": "Bearer jwt"}
        with self._verified():
            old = self.client.post("/api/token", headers=headers).json()["token"]
            rotated = self.client.post("/api/token?rotate=true", headers=headers)
        self.assertEqual(rotated.status_code, 201)
        self.assertNotEqual(rotated.json()["token"], old)

    def test_get_returns_existing_or_404(self):
        headers = {"Authorization": "Bearer jwt"}
        with self._verified():
            self.assertEqual(self.client.get("/api/token", headers=headers).status_code, 404)
            created = self.client.post("/api/token", headers=headers).json()["token"]
            fetched = self.client.get("/api/token", headers=headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["token"], created)

    def test_tokens_are_per_user(self):
        other = dict(USER, id="u2", email="c@d.co")
        headers = {"Authorization": "Bearer jwt"}
        with self._verified():
            mine = self.client.post("/api/token", headers=headers).json()["token"]
        with self._verified(other):
            theirs = self.client.post("/api/token", headers=headers).json()["token"]
        self.assertNotEqual(mine, theirs)

    def test_storage_outage_is_503(self):
        broken = mock.Mock()
        broken.issue.side_effect = auth.TokenStoreError("MongoDB is not connected")
        with mock.patch.object(api, "tokens", return_value=broken), self._verified():
            response = self.client.post("/api/token", headers={"Authorization": "Bearer jwt"})
        self.assertEqual(response.status_code, 503)

    def test_routes_registered(self):
        paths = {route.path for route in api.app.routes if getattr(route, "path", "")}
        self.assertIn("/api/token", paths)


class _CachedStore:
    """DomainStore stand-in: one cached domain, so /api/scan answers without scanning."""

    DOC = {"flags": {"isLinkedin": True, "isVerify": False},
           "rows": [{"full_name": "Jane Doe"}], "meta": {}, "counts": {"people": 1}}

    def get(self, domain):
        return dict(self.DOC)

    def upsert(self, domain, doc):
        return True

    def list_domains(self, limit=200):
        return []

    def health(self):
        return {"enabled": True, "from": "test", "db": "t", "collection": "t", "error": ""}


@unittest.skipIf(TestClient is None, "fastapi TestClient needs httpx")
class ScanAuthTests(unittest.TestCase):
    """/api/scan and /api/domains accept a JWT, an eh_ token or X-API-Key, and nothing else."""

    SCAN = {"domains": "acme.com"}

    def setUp(self):
        self.tokens = FakeTokens()
        for patcher in (mock.patch.object(api, "tokens", return_value=self.tokens),
                        mock.patch.object(api, "store", return_value=_CachedStore())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)
        self.token = self.tokens.issue(USER)[0]["token"]

    def test_no_credentials_is_401_on_every_protected_route(self):
        for call in (lambda: self.client.post("/api/scan", json=self.SCAN),
                     lambda: self.client.get("/api/domains"),
                     lambda: self.client.get("/api/domains/acme.com"),
                     lambda: self.client.delete("/api/domains/acme.com")):
            self.assertEqual(call().status_code, 401)

    def test_api_token_is_accepted_without_calling_the_auth_service(self):
        with mock.patch.object(auth, "fetch_user") as fetch:
            response = self.client.post("/api/scan", json=self.SCAN,
                                        headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('"type": "result"', response.text)
        fetch.assert_not_called()

    def test_unknown_api_token_is_401_and_never_goes_to_the_auth_service(self):
        with mock.patch.object(auth, "fetch_user") as fetch:
            response = self.client.get("/api/domains",
                                       headers={"Authorization": "Bearer eh_forged"})
        self.assertEqual(response.status_code, 401)
        fetch.assert_not_called()

    def test_jwt_is_verified_against_the_auth_service(self):
        with mock.patch.object(auth, "fetch_user", return_value=USER) as fetch:
            response = self.client.post("/api/scan", json=self.SCAN,
                                        headers={"Authorization": "Bearer some.jwt.value"})
        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once_with("Bearer some.jwt.value")

    def test_bad_jwt_is_rejected(self):
        with mock.patch.object(auth, "fetch_user", side_effect=auth.AuthError(401, "bad")):
            response = self.client.post("/api/scan", json=self.SCAN,
                                        headers={"Authorization": "Bearer some.jwt.value"})
        self.assertEqual(response.status_code, 401)

    def test_x_api_key_still_works_only_when_configured_and_matching(self):
        headers = {"X-API-Key": "s3cret"}
        self.assertEqual(self.client.get("/api/domains", headers=headers).status_code, 401)
        with mock.patch.dict("os.environ", {"API_KEY": "s3cret"}):
            self.assertEqual(self.client.get("/api/domains", headers=headers).status_code, 200)
            self.assertEqual(self.client.get("/api/domains",
                                             headers={"X-API-Key": "nope"}).status_code, 401)

    def test_token_storage_outage_is_503_not_401(self):
        broken = mock.Mock()
        broken.find_user.side_effect = auth.TokenStoreError("MongoDB is not connected")
        with mock.patch.object(api, "tokens", return_value=broken):
            response = self.client.get("/api/domains",
                                       headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 503)

    def test_token_routes_stay_jwt_only(self):
        """An API token must not be able to read or rotate itself via /api/token."""
        with mock.patch.object(auth, "fetch_user",
                               side_effect=auth.AuthError(401, "bad")) as fetch:
            response = self.client.post("/api/token?rotate=true",
                                        headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 401)
        fetch.assert_called_once()
        self.assertEqual(self.tokens.issued, 1)       # the one from setUp; nothing rotated

    def test_health_stays_public(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
