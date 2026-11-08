"""Tests for LinkedIn URL extraction (web pages, humans.txt, keyless SERP parsers)."""

from __future__ import annotations

import base64
import unittest

from ehunter import gravatar
from ehunter.net import HttpError
from ehunter.sources.search import Search, parse_bing, parse_ddg, parse_searx
from ehunter.sources.web import parse_humans_txt, parse_html
from ehunter.util import linkedin_matches_name, linkedin_slug, linkedin_url


class LinkedInUtilTests(unittest.TestCase):
    def test_url_variants(self):
        for raw in [
            "https://www.linkedin.com/in/jane-doe/",
            "http://linkedin.com/in/jane-doe?trk=public",
            "linkedin.com/in/jane-doe",
            "//www.linkedin.com/in/jane-doe",
            "<a href=\"https://www.linkedin.com/in/jane-doe/\">in</a>",
        ]:
            self.assertEqual(
                linkedin_url(raw), "https://www.linkedin.com/in/jane-doe", msg=raw
            )

    def test_not_a_profile(self):
        self.assertEqual(linkedin_url("https://www.linkedin.com/company/acme"), "")
        self.assertEqual(linkedin_url(""), "")

    def test_slug(self):
        self.assertEqual(linkedin_slug("https://www.linkedin.com/in/jane-doe-1a2b3c"), "jane-doe-1a2b3c")
        self.assertEqual(linkedin_slug("nonsense"), "")

    def test_matches_name(self):
        url = "https://www.linkedin.com/in/jane-doe-1a2b3c"
        self.assertTrue(linkedin_matches_name(url, "Jane", "Doe"))
        self.assertTrue(linkedin_matches_name(url, "jane", "doe"))
        self.assertFalse(linkedin_matches_name(url, "John", "Smith"))
        self.assertFalse(linkedin_matches_name(url, "", ""))


class HumansTxtTests(unittest.TestCase):
    TEXT = """
/* TEAM */
    Name: Jane Doe
    Role: Chief Technology Officer
    Contact: jane@acme.com
    LinkedIn: linkedin.com/in/janedoe

/* TEAM */
    Name: Mark Smith
    Contact: mark@acme.com
    Twitter: @marksmith

/* SITE */
    Standards: HTML5
"""

    def test_parses_team_blocks(self):
        people = parse_humans_txt(self.TEXT)
        self.assertEqual(len(people), 2)
        self.assertEqual(people[0]["name"], "Jane Doe")
        self.assertEqual(people[0]["email"], "jane@acme.com")
        self.assertEqual(people[0]["title"], "Chief Technology Officer")
        self.assertEqual(people[0]["linkedin"], "https://www.linkedin.com/in/janedoe")
        self.assertEqual(people[1]["linkedin"], "")

    def test_ignores_absent_team(self):
        self.assertEqual(parse_humans_txt("/* SITE */ Standards: HTML5"), [])


TEAM_HTML = """
<html><body>
<div class="card">
  <h3>Jane Doe</h3><p>Chief Technology Officer</p>
  <a href="https://www.linkedin.com/in/jane-doe">LinkedIn</a>
</div>
<div class="card">
  <h3>Mark Smith</h3><p>Head of Sales</p>
  <a href="https://www.linkedin.com/company/acme">Acme on LinkedIn</a>
</div>
<script type="application/ld+json">
{"@type":"Person","name":"Ana Ruiz","jobTitle":"Engineering Manager",
 "sameAs":["https://www.linkedin.com/in/ana-ruiz","https://twitter.com/anaruiz"]}
</script>
</body></html>
"""


class WebLinkedInTests(unittest.TestCase):
    def test_page_exposes_profiles(self):
        page = parse_html(TEAM_HTML, "https://acme.com/team")
        self.assertIn("https://www.linkedin.com/in/jane-doe", page["linkedin"])
        kinds = [kind for kind, _ in page["events"]]
        self.assertIn("linkedin", kinds)

    def test_jsonld_sameas_is_used(self):
        page = parse_html(TEAM_HTML, "https://acme.com/team")
        ana = [p for p in page["people"] if p.get("name") == "Ana Ruiz"][0]
        urls = [linkedin_url(u) for u in ana["sameAs"]]
        self.assertIn("https://www.linkedin.com/in/ana-ruiz", urls)


class FakeHttp:
    def __init__(self, payload=None, status=0):
        self.payload = payload
        self.status = status
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        if self.status:
            raise HttpError(self.status, url, "not found")
        return self.payload


class FakeCtx:
    def __init__(self, http):
        self.http = http
        self.cache = {}

    def memo(self, key):
        return self.cache.get(key)

    def remember(self, key, value):
        self.cache[key] = value
        return value


class GravatarTests(unittest.TestCase):
    def test_hash_is_lowercased_md5(self):
        import hashlib

        self.assertEqual(gravatar.email_hash("  Zoe@FSF.org "),
                         hashlib.md5(b"zoe@fsf.org").hexdigest())

    def test_linkedin_from_accounts(self):
        entry = {"accounts": [{"domain": "twitter.com", "username": "zoe"},
                              {"domain": "linkedin.com", "username": "zoe-kooyman"}]}
        self.assertEqual(gravatar.linkedin_from_entry(entry),
                         "https://www.linkedin.com/in/zoe-kooyman")

    def test_linkedin_from_urls(self):
        entry = {"urls": [{"value": "https://www.linkedin.com/in/zoe-kooyman/"}]}
        self.assertEqual(gravatar.linkedin_from_entry(entry),
                         "https://www.linkedin.com/in/zoe-kooyman")

    def test_no_profile(self):
        ctx = FakeCtx(FakeHttp(status=404))
        self.assertEqual(gravatar.lookup(ctx, "nobody@example.com"), "")
        # cached: a second lookup must not hit the network again
        ctx.http.calls.clear()
        self.assertEqual(gravatar.lookup(ctx, "nobody@example.com"), "")

    def test_profile_found_and_cached(self):
        payload = {"entry": [{"accounts": [{"domain": "linkedin.com",
                                            "username": "zoe-kooyman"}]}]}
        ctx = FakeCtx(FakeHttp(payload=payload))
        self.assertEqual(gravatar.lookup(ctx, "zoe@fsf.org"),
                         "https://www.linkedin.com/in/zoe-kooyman")
        ctx.http.calls.clear()
        gravatar.lookup(ctx, "zoe@fsf.org")
        self.assertEqual(ctx.http.calls, [])


class KeylessSerpParserTests(unittest.TestCase):
    def test_parse_ddg_redirect(self):
        body = (
            '<a rel="nofollow" class="result__a" '
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fjane-doe'
            '&amp;rut=abc">Jane Doe - VP Sales - Acme | LinkedIn</a>'
            '<a class="result__snippet" href="#">Acme &middot; janedoe</a>'
        )
        results = parse_ddg(body)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["link"], "https://www.linkedin.com/in/jane-doe")
        self.assertIn("Jane Doe", results[0]["title"])

    def test_parse_ddg_challenge_page_yields_nothing(self):
        self.assertEqual(parse_ddg("<html>Select all squares containing a duck</html>"), [])

    def test_parse_bing_base64_redirect(self):
        target = "https://www.linkedin.com/in/mark-smith"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        body = (
            '<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1'
            + encoded
            + '">Mark Smith - LinkedIn</a></h2><p>Head of Sales at Acme</p></li>'
        )
        results = parse_bing(body)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["link"], target)

    def test_parse_searx(self):
        body = (
            '<article class="result"><h3><a href="https://www.linkedin.com/in/jane-doe">'
            "Jane Doe - LinkedIn</a></h3><p class=\"content\">Acme</p></article>"
        )
        results = parse_searx(body)
        self.assertEqual(results[0]["link"], "https://www.linkedin.com/in/jane-doe")
        self.assertEqual(results[0]["snippet"], "Acme")

    def test_relevance_filter_drops_cached_junk(self):
        junk = {"title": "YouTube", "link": "https://youtube.com/watch?v=1", "snippet": ""}
        self.assertFalse(Search._relevant(junk, "posthog.com"))
        good = {"title": "Jane Doe - LinkedIn",
                "link": "https://www.linkedin.com/in/jane-doe", "snippet": ""}
        self.assertTrue(Search._relevant(good, "posthog.com"))
        self.assertTrue(Search._relevant(
            {"title": "Acme team", "link": "https://acme.com/team",
             "snippet": "write to jane@acme.com"}, "acme.com"))


if __name__ == "__main__":
    unittest.main()


class GitHubHandleTests(unittest.TestCase):
    def test_member_without_display_name_is_kept_as_handle_row(self):
        from ehunter.context import Options
        from ehunter.net import HttpError
        from ehunter.sources.github import GitHub

        class FakeHttp:
            def get_json(self, url, **kwargs):
                if "/members" in url:
                    return [{"login": "harsh-b2"}]
                if "/users/harsh-b2" in url:
                    return {"login": "harsh-b2", "name": None, "email": None,
                            "blog": "", "bio": "", "company": ""}
                raise HttpError(404, url)

        class Ctx:
            domain = "acme.com"
            opts = Options(github_org="acme")
            http = FakeHttp()
            cache = {}

            def memo(self, key):
                return self.cache.get(key)

            def remember(self, key, value):
                self.cache[key] = value
                return value

            def info(self, *_a, **_k):
                pass

            def debug(self, *_a, **_k):
                pass

        records = GitHub().run(Ctx())
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.github, "harsh-b2")
        self.assertEqual(record.full_name, "")
        self.assertEqual(record.email, "")
        self.assertEqual(record.status, "name_only")
        self.assertTrue(any("no display name" in n for n in record.notes))
