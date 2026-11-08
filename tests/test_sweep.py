"""Tests for the LinkedIn sweep: query variants, employment classification, records."""

from __future__ import annotations

import pathlib
import unittest

from ehunter import sweep


class VariantTests(unittest.TestCase):
    def test_domain_query_first(self):
        queries = sweep.variants("bamboobox.ai", ["BambooBox"], 14)
        self.assertEqual(queries[0], 'site:linkedin.com/in "bamboobox.ai"')

    def test_variants_are_unique_and_capped(self):
        queries = sweep.variants("acme.com", ["Acme", "Acme Inc"], 10)
        self.assertEqual(len(queries), 10)
        self.assertEqual(len(set(queries)), len(queries))

    def test_modifiers_present(self):
        joined = " ".join(sweep.variants("bamboobox.ai", ["BambooBox"], 30))
        for needle in ('"BambooBox"', '"at BambooBox"', '"BambooBox" India',
                       '"building BambooBox"'):
            self.assertIn(needle, joined)

    def test_former_employer_queries_are_skipped(self):
        joined = " ".join(sweep.variants("acme.com", ["Acme"], 60)).lower()
        self.assertNotIn('"ex-acme"', joined)
        self.assertNotIn('"former acme"', joined)

    def test_company_names_from_title(self):
        names = sweep.company_names("bamboobox.ai", "BambooBox | AI Customer Support")
        self.assertEqual(names[0], "BambooBox")

    def test_company_names_without_title(self):
        names = sweep.company_names("bamboobox.ai")
        self.assertIn("bamboobox", [n.lower() for n in names])


class ClassifyTests(unittest.TestCase):
    NAMES = ["BambooBox", "bamboobox.ai"]

    def test_current_from_at(self):
        employment, reason = sweep.classify(
            "Mohak Mahajan - Design at BambooBox.ai", "", self.NAMES)
        self.assertEqual(employment, "current")
        self.assertIn("at bamboobox", reason.lower())

    def test_current_from_building(self):
        employment, _ = sweep.classify("Niharika Thakur - Building BambooBox", "", self.NAMES)
        self.assertEqual(employment, "current")

    def test_former(self):
        employment, _ = sweep.classify("Jane Doe - Ex-BambooBox | Product", "", self.NAMES)
        self.assertEqual(employment, "former")

    def test_mentioned_without_employer_phrase(self):
        employment, _ = sweep.classify("Jane Doe - Product Manager", "spoke at BambooBox", self.NAMES)
        self.assertEqual(employment, "mentioned")

    def test_open_to_work(self):
        employment, _ = sweep.classify(
            "Jane Doe - Open to work", "BambooBox bamboobox.ai", self.NAMES)
        self.assertIn(employment, ("mentioned", "unclear"))

    def test_no_company_named(self):
        employment, reason = sweep.classify("Jane Doe - Engineer", "", self.NAMES)
        self.assertEqual(employment, "unclear")
        self.assertEqual(reason, "company not named")

    def test_student_is_not_current(self):
        employment, _ = sweep.classify(
            "Anirudh Arrepu - Senior at IIT Tirupati", "BambooBox", self.NAMES)
        self.assertNotEqual(employment, "current")


class HelperTests(unittest.TestCase):
    def test_slug_from_link(self):
        self.assertEqual(
            sweep.slug_from_link("https://in.linkedin.com/in/mohakmahajan9"),
            "mohakmahajan9",
        )
        self.assertEqual(sweep.slug_from_link("https://example.com"), "")

    def test_title_parts(self):
        self.assertEqual(
            sweep.title_parts("Jane Doe - VP Sales - Acme | LinkedIn"),
            ("Jane Doe", "VP Sales"),
        )
        self.assertEqual(sweep.title_parts("no person here"), ("", ""))

    def test_sort_puts_current_first(self):
        profiles = [
            {"employment": "former", "full_name": "Zed"},
            {"employment": "current", "full_name": "Amy"},
            {"employment": "mentioned", "full_name": "Bob"},
        ]
        ordered = [p["full_name"] for p in sweep.sort_profiles(profiles)]
        self.assertEqual(ordered, ["Amy", "Bob", "Zed"])


class BuildRecordsTests(unittest.TestCase):
    def test_current_profile_gets_high_confidence(self):
        from ehunter.context import Options

        class Ctx:
            domain = "bamboobox.ai"
            opts = Options()
            http = None

        records = sweep.build_records(Ctx(), [{
            "url": "https://www.linkedin.com/in/mohakmahajan9",
            "full_name": "Mohak Mahajan",
            "title": "Design at BambooBox.ai",
            "employment": "current",
            "evidence": 'matched "at bamboobox.ai"',
        }])
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.confidence, 60)
        self.assertEqual(record.linkedin, "https://www.linkedin.com/in/mohakmahajan9")
        self.assertEqual(record.full_name, "Mohak Mahajan")
        self.assertIn("linkedin: current", record.notes[0])

    def test_profiles_without_a_name_are_dropped(self):
        from ehunter.context import Options

        class Ctx:
            domain = "bamboobox.ai"
            opts = Options()
            http = None

        records = sweep.build_records(Ctx(), [
            {"url": "https://www.linkedin.com/in/x", "full_name": ""},
        ])
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()


def _workdir() -> "pathlib.Path":
    """Scratch dir inside the project (the sandbox blocks the OS temp dir)."""
    base = pathlib.Path(__file__).resolve().parent / "_tmp_sweep"
    base.mkdir(parents=True, exist_ok=True)
    return base


class ReadProfilesTests(unittest.TestCase):
    def test_csv_roundtrip(self):
        profiles = [{
            "employment": "current", "full_name": "Jane Doe", "title": "CTO",
            "url": "https://www.linkedin.com/in/jane-doe", "slug": "jane-doe",
            "evidence": 'matched "at acme"', "queries": ["q1", "q2"], "snippet": "acme",
        }]
        path = sweep.write_profiles(_workdir() / "_roundtrip.csv", profiles)
        back = sweep.read_profiles(path)
        self.assertEqual(back[0]["full_name"], "Jane Doe")
        self.assertEqual(back[0]["url"], "https://www.linkedin.com/in/jane-doe")
        self.assertEqual(back[0]["queries"], ["q1", "q2"])

    def test_missing_file_raises(self):
        with self.assertRaises(ValueError):
            sweep.read_profiles("does-not-exist.csv")


class LinkedInInputTests(unittest.TestCase):
    def _ctx(self):
        from ehunter.context import Options

        class Ctx:
            domain = "acme.com"
            http = None
            opts = Options()
            cache = {}

            def memo(self, key):
                return self.cache.get(key)

            def remember(self, key, value):
                self.cache[key] = value
                return value

            def info(self, *_a, **_k):
                pass

        return Ctx()

    def test_saved_sweep_becomes_contacts_and_former_is_dropped(self):
        from ehunter.pipeline import _linkedin_records

        profiles = [
            {"employment": "current", "full_name": "Jane Doe", "title": "CTO",
             "url": "https://www.linkedin.com/in/jane-doe", "slug": "jane-doe",
             "queries": ["q"], "evidence": "", "snippet": ""},
            {"employment": "mentioned", "full_name": "Mark Smith", "title": "Engineer",
             "url": "https://www.linkedin.com/in/mark-smith", "slug": "mark-smith",
             "queries": ["q"], "evidence": "", "snippet": ""},
            {"employment": "former", "full_name": "Old Timer", "title": "Ex-Acme",
             "url": "https://www.linkedin.com/in/old-timer", "slug": "old-timer",
             "queries": ["q"], "evidence": "", "snippet": ""},
        ]
        path = sweep.write_profiles(_workdir() / "_contacts.csv", profiles)
        ctx = self._ctx()
        ctx.opts.linkedin_file = str(path)
        records = _linkedin_records(ctx, ctx.opts, lambda *_a, **_k: None)

        names = {r.full_name for r in records}
        self.assertEqual(names, {"Jane Doe", "Mark Smith"})   # former dropped
        self.assertTrue(all(r.linkedin for r in records))
        self.assertTrue(all(r.email == "" for r in records))

    def test_duplicate_slugs_collapse(self):
        from ehunter.pipeline import _linkedin_records

        profiles = [
            {"employment": "current", "full_name": "Jane Doe", "title": "CTO",
             "url": "https://www.linkedin.com/in/jane-doe", "slug": "jane-doe",
             "queries": ["q1"], "evidence": "", "snippet": ""},
            {"employment": "current", "full_name": "Jane Doe", "title": "CTO",
             "url": "https://in.linkedin.com/in/jane-doe", "slug": "jane-doe",
             "queries": ["q2"], "evidence": "", "snippet": ""},
        ]
        path = sweep.write_profiles(_workdir() / "_dupes.csv", profiles)
        ctx = self._ctx()
        ctx.opts.linkedin_file = str(path)
        records = _linkedin_records(ctx, ctx.opts, lambda *_a, **_k: None)
        self.assertEqual(len(records), 1)
