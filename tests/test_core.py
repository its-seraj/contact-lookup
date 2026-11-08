"""Unit tests for the name/pattern/parsing core. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import unittest

from ehunter import patterns
from ehunter.names import (
    email_localpart,
    looks_like_name,
    looks_like_title,
    normalize_person,
    slug,
    split_name,
    split_name_title,
    strip_accents,
)
from ehunter.pipeline import generate_emails, infer_pattern, merge_records
from ehunter.sources.base import rec
from ehunter.sources.pgp import parse_mr_keys, unescape_mr
from ehunter.sources.web import is_generic, parse_html
from ehunter.net import decode_body
from ehunter.sources.web import company_tail, is_external_title
from ehunter.util import find_emails, normalize_domain, registrable


class FakeCtx:
    def __init__(self, domain="acme.com"):
        self.domain = domain
        self.cache = {}
        self.opts = _Opts()
        self.lock = None

    def memo(self, key):
        return self.cache.get(key)

    def remember(self, key, value):
        self.cache[key] = value
        return value

    def info(self, *_a, **_k):
        pass

    def debug(self, *_a, **_k):
        pass


class _Opts:
    max_patterns = 2

    def get(self, key, default=None):
        return getattr(self, key, default)


class NameTests(unittest.TestCase):
    def test_split_simple(self):
        self.assertEqual(split_name("Jane Doe"), ("Jane", "Doe"))

    def test_split_last_first(self):
        self.assertEqual(split_name("Doe, Jane"), ("Jane", "Doe"))

    def test_split_suffix(self):
        self.assertEqual(split_name("John Smith Jr."), ("John", "Smith"))

    def test_split_middle_name(self):
        self.assertEqual(split_name("Mary Jane Watson"), ("Mary", "Watson"))

    def test_split_particles(self):
        self.assertEqual(split_name("Jan van der Berg"), ("Jan", "van der Berg"))

    def test_split_single_token(self):
        self.assertEqual(split_name("Cher"), ("Cher", ""))

    def test_split_honorific(self):
        self.assertEqual(split_name("Dr. Ana Ruiz"), ("Ana", "Ruiz"))

    def test_normalize_fills_from_full(self):
        first, last, full = normalize_person("", "", "Jane Doe")
        self.assertEqual((first, last), ("Jane", "Doe"))
        self.assertEqual(full, "Jane Doe")

    def test_normalize_builds_full(self):
        first, last, full = normalize_person("jane", "doe", "")
        self.assertEqual(first, "Jane")
        self.assertEqual(last, "Doe")
        self.assertEqual(full, "Jane Doe")

    def test_accents_and_slug(self):
        self.assertEqual(strip_accents("Łukasz Müller"), "Lukasz Muller")
        self.assertEqual(slug("Müller"), "muller")
        self.assertEqual(slug("O'Brien"), "obrien")
        self.assertEqual(slug("Jean-Luc"), "jeanluc")

    def test_looks_like_name(self):
        self.assertTrue(looks_like_name("Jane Doe"))
        self.assertTrue(looks_like_name("Ana Maria Ruiz"))
        self.assertTrue(looks_like_name("Jan van der Berg"))
        self.assertFalse(looks_like_name("Read More"))
        self.assertFalse(looks_like_name("Chief Technology Officer"))
        self.assertFalse(looks_like_name("Senior Software Engineer"))
        self.assertFalse(looks_like_name("Our Team"))
        self.assertFalse(looks_like_name("jane@acme.com"))
        self.assertFalse(looks_like_name("Acme Group"))
        self.assertFalse(looks_like_name("2024"))

    def test_looks_like_title(self):
        self.assertTrue(looks_like_title("Chief Technology Officer"))
        self.assertTrue(looks_like_title("Co-founder"))
        self.assertTrue(looks_like_title("Head of Sales"))
        self.assertTrue(looks_like_title("Software Engineer"))
        self.assertFalse(looks_like_title("Session Replay"))
        self.assertFalse(looks_like_title("Free Bios"))
        self.assertFalse(looks_like_title("Jane Doe"))

    def test_split_name_title(self):
        self.assertEqual(
            split_name_title("James Hawkins Co-founder"),
            ("James Hawkins", "Co-founder"),
        )
        self.assertEqual(
            split_name_title("Ana Maria Ruiz Head of Sales"),
            ("Ana Maria Ruiz", "Head of Sales"),
        )
        self.assertIsNone(split_name_title("Session Replay"))
        self.assertIsNone(split_name_title("Jane Doe"))

    def test_email_localpart(self):
        self.assertEqual(email_localpart("Jane.Doe+tag@Acme.com"), "jane.doe+tag")


class PatternTests(unittest.TestCase):
    PAIRS = [
        ("Jane", "Doe", "jane.doe@acme.com"),
        ("Mark", "Smith", "mark.smith@acme.com"),
        ("Ana", "Ruiz", "ana.ruiz@acme.com"),
    ]

    def test_render(self):
        self.assertEqual(patterns.render("{first}.{last}", "Jane", "Doe"), "jane.doe")
        self.assertEqual(patterns.render("{f}{last}", "Jane", "Doe"), "jdoe")
        self.assertEqual(patterns.render("{first}{l}", "Jane", "Doe"), "janed")
        self.assertEqual(patterns.render("{last}", "Jane", "Doe"), "doe")
        self.assertEqual(patterns.render("{first}.{last}", "Cher", ""), "")

    def test_render_strips_punctuation_and_accents(self):
        self.assertEqual(patterns.render("{first}.{last}", "O'Brien", "Müller"), "obrien.muller")
        self.assertEqual(patterns.render("{first}_{last}", "Jean-Luc", "Picard"), "jeanluc_picard")

    def test_infer_picks_dominant_pattern(self):
        hits = patterns.infer(self.PAIRS)
        self.assertEqual(hits[0].pattern, "{first}.{last}")
        self.assertEqual(hits[0].matched, 3)
        self.assertGreater(hits[0].confidence, 50)

    def test_infer_ignores_freemail(self):
        pairs = [("Jane", "Doe", "jane.doe@gmail.com")]
        self.assertEqual(patterns.infer(pairs), [])

    def test_infer_fno_last(self):
        pairs = [("Jane", "Doe", "jdoe@acme.com"), ("Mark", "Smith", "msmith@acme.com")]
        self.assertEqual(patterns.infer(pairs)[0].pattern, "{f}{last}")

    def test_build(self):
        self.assertEqual(patterns.build("{f}{last}", "Jane", "Doe", "acme.com"), "jdoe@acme.com")

    def test_run_together_names_disambiguate_patterns(self):
        # "johnsmith@acme.com" fits both {first}{last} and {last}{first}; ordering resolves it
        hits = patterns.infer([("John", "Smith", "johnsmith@acme.com")])
        self.assertEqual(hits[0].pattern, "{first}{last}")


class UtilTests(unittest.TestCase):
    def test_normalize_domain(self):
        self.assertEqual(normalize_domain("https://www.Acme.com/team?x=1"), "acme.com")
        self.assertEqual(normalize_domain("acme.com"), "acme.com")
        self.assertEqual(normalize_domain("WWW.ACME.CO.UK"), "acme.co.uk")
        self.assertEqual(normalize_domain("user@acme.com"), "acme.com")

    def test_normalize_domain_rejects_junk(self):
        with self.assertRaises(ValueError):
            normalize_domain("not a domain")
        with self.assertRaises(ValueError):
            normalize_domain("")

    def test_registrable(self):
        self.assertEqual(registrable("careers.acme.co.uk"), "acme.co.uk")
        self.assertEqual(registrable("blog.acme.com"), "acme.com")

    def test_find_emails_filters_domain(self):
        text = "mail jane@acme.com or sales@other.com or jane@acme.com"
        self.assertEqual(find_emails(text, "acme.com"), ["jane@acme.com"])

    def test_find_emails_skips_assets(self):
        self.assertEqual(find_emails("logo@2x.png", ""), [])


class MergeTests(unittest.TestCase):
    def test_merge_deduplicates_and_enriches(self):
        ctx = FakeCtx()
        rows = [
            rec(ctx, "hunter", first="Jane", last="Doe", email="jane.doe@acme.com", confidence=90),
            rec(ctx, "web", full="Jane Doe", title="CTO", email="JANE.DOE@acme.com"),
        ]
        by_email, name_only = merge_records(rows)
        self.assertEqual(len(by_email), 1)
        record = by_email["jane.doe@acme.com"]
        self.assertEqual(record.title, "CTO")
        self.assertEqual(sorted(record.sources), ["hunter", "web"])
        self.assertEqual(name_only, {})

    def test_handle_only_row_survives_merge(self):
        ctx = FakeCtx()
        rows = [
            rec(ctx, "github", full="", github="harsh-b2", status="name_only", confidence=30),
        ]
        by_email, name_only = merge_records(rows)
        self.assertEqual(by_email, {})
        self.assertEqual(len(name_only), 1)
        record = next(iter(name_only.values()))
        self.assertEqual(record.github, "harsh-b2")
        self.assertEqual(record.full_name, "")
        self.assertEqual(record.email, "")

    def test_merge_folds_name_only_into_email_row(self):
        ctx = FakeCtx()
        rows = [
            rec(ctx, "hunter", first="Jane", last="Doe", email="jane.doe@acme.com"),
            rec(ctx, "web", full="Jane Doe", title="CTO"),
        ]
        by_email, name_only = merge_records(rows)
        self.assertEqual(len(by_email), 1)
        self.assertEqual(len(name_only), 0)
        self.assertEqual(by_email["jane.doe@acme.com"].title, "CTO")

    def test_generate_infers_for_named_person(self):
        ctx = FakeCtx()
        rows = [
            rec(ctx, "hunter", first="Jane", last="Doe", email="jane.doe@acme.com", confidence=90),
            rec(ctx, "hunter", first="Ana", last="Ruiz", email="ana.ruiz@acme.com", confidence=90),
            rec(ctx, "web", full="Mark Smith", title="CTO"),
        ]
        by_email, name_only = merge_records(rows)
        hits = infer_pattern(ctx, by_email)
        generated = generate_emails(ctx, name_only, by_email, hits)
        emails = {r.email for r in generated}
        self.assertIn("mark.smith@acme.com", emails)
        record = next(r for r in generated if r.email == "mark.smith@acme.com")
        self.assertEqual(record.status, "pattern")
        self.assertEqual(record.first_name, "Mark")
        # converted people must not be emitted twice as name-only rows
        self.assertEqual(name_only, {})

    def test_generate_refuses_single_example(self):
        ctx = FakeCtx()
        rows = [
            rec(ctx, "hunter", first="Jane", last="Doe", email="jane.doe@acme.com"),
            rec(ctx, "web", full="Mark Smith", title="CTO"),
        ]
        by_email, name_only = merge_records(rows)
        hits = infer_pattern(ctx, by_email)
        self.assertEqual(generate_emails(ctx, name_only, by_email, hits), [])

    def test_generate_never_duplicates_existing_email(self):
        ctx = FakeCtx()
        rows = [
            rec(ctx, "hunter", first="Jane", last="Doe", email="jane.doe@acme.com"),
            rec(ctx, "web", full="Jane Doe"),
        ]
        by_email, _ = merge_records(rows)
        hits = infer_pattern(ctx, by_email)
        generated = generate_emails(ctx, {}, by_email, hits)
        self.assertEqual(generated, [])


class CharsetTests(unittest.TestCase):
    def test_cp1252_body(self):
        raw = "Sales \u2013 Rootstock".encode("cp1252")
        self.assertEqual(decode_body(raw), "Sales \u2013 Rootstock")

    def test_meta_charset(self):
        raw = b"<meta charset=iso-8859-1>Zo\xeb Kooyman"
        self.assertIn("Zo\xeb Kooyman", decode_body(raw))

    def test_header_charset_wins(self):
        raw = "Gr\xfc\xdfe".encode("utf-8")
        text = decode_body(raw, {"Content-Type": "text/html; charset=utf-8"})
        self.assertEqual(text, "Gr\xfc\xdfe")

    def test_utf8_default(self):
        self.assertEqual(decode_body("Zo\xeb".encode("utf-8")), "Zo\xeb")


class ExternalTitleTests(unittest.TestCase):
    def test_company_tail_extracted(self):
        self.assertEqual(company_tail("CMO, Acalvio"), "Acalvio")
        self.assertEqual(company_tail("CMO \u2013 Airtel Business"), "Airtel Business")
        self.assertEqual(company_tail("Head of Sales - Rootstock Software"),
                         "Rootstock Software")

    def test_internal_scope_is_not_a_company(self):
        self.assertEqual(company_tail("Head of Sales - EMEA"), "")
        self.assertEqual(company_tail("Director of Engineering - Platform"), "")
        self.assertEqual(company_tail("Head of Sales - Sales"), "")
        self.assertEqual(company_tail("Chief Technology Officer"), "")

    def test_external_detection(self):
        self.assertTrue(is_external_title("CMO, Acalvio", "bamboobox.ai"))
        self.assertTrue(is_external_title("Head of Sales - Rootstock", "bamboobox.ai"))
        self.assertFalse(is_external_title("CMO at Bamboobox", "bamboobox.ai"))
        self.assertFalse(is_external_title("Chief Technology Officer", "bamboobox.ai"))
        self.assertFalse(is_external_title("Head of Sales - EMEA", "bamboobox.ai"))


class PgpTests(unittest.TestCase):
    def test_unescape(self):
        self.assertEqual(unescape_mr(r"Jane\x3aDoe"), "Jane:Doe")
        self.assertEqual(unescape_mr(r"100\x25"), "100%")

    def test_parse_mr(self):
        text = "\n".join([
            "info:1:1",
            "pub:AAAAAAAAAAAAAAAA:1:4096:1600000000::",
            "uid:AAAAAAAAAAAAAAAA:1600000000::Jane Doe <jane@acme.com>",
            "uid:AAAAAAAAAAAAAAAA:1600000001::Bob (work) <bob\\x40acme.com>",
        ])
        parsed = parse_mr_keys(text)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0], ("Jane Doe", "jane@acme.com"))
        self.assertEqual(parsed[1][0], "Bob")
        self.assertEqual(parsed[1][1], "bob@acme.com")


TEAM_HTML = """
<html><head><title>Acme - Our Team</title></head><body>
<nav><a href="/">Home</a><a href="/about">About Us</a><a href="/contact">Contact</a></nav>
<h1>Leadership</h1>
<div class="card">
  <h3>Jane Doe</h3>
  <p>Chief Technology Officer</p>
  <a href="mailto:jane.doe@acme.com">Jane Doe</a>
</div>
<div class="card">
  <h3>Mark Smith</h3>
  <p>Head of Sales</p>
</div>
<a href="mailto:info@acme.com">Contact us</a>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Person","name":"Ana Ruiz","jobTitle":"Engineering Manager","email":"ana.ruiz@acme.com"}
</script>
</body></html>
"""


class WebParseTests(unittest.TestCase):
    def test_parse_html_extracts_blocks_links_emails(self):
        page = parse_html(TEAM_HTML, "https://acme.com/team")
        self.assertIn("Leadership", page["blocks"])
        self.assertIn("Jane Doe", page["blocks"])
        self.assertIn("jane.doe@acme.com", page["emails"])
        self.assertTrue(any(href == "/about" for href, _ in page["links"]))
        self.assertEqual(page["people"][0]["name"], "Ana Ruiz")

    def test_is_generic(self):
        self.assertTrue(is_generic("info@acme.com"))
        self.assertTrue(is_generic("sales.de@acme.com"))
        self.assertFalse(is_generic("jane.doe@acme.com"))
        self.assertTrue(is_generic("connect@acme.com"))
        self.assertTrue(is_generic("connections@acme.com"))

    def test_parse_html_survives_garbage(self):
        page = parse_html("<div><p>unclosed <b>tags", "https://acme.com")
        self.assertIn("unclosed tags", page["text"])


if __name__ == "__main__":
    unittest.main()
