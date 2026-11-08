"""Tests for the email permutation ladder and the SMTP-driven resolver."""

from __future__ import annotations

import unittest

from ehunter import permute
from ehunter.context import Options
from ehunter.models import Record
from ehunter.pipeline import resolve_permutations


class FakeCtx:
    def __init__(self, domain="acme.com", **opts):
        self.domain = domain
        self.opts = Options(**opts)
        self.http = None
        self.cache = {}

    def memo(self, key):
        return self.cache.get(key)

    def remember(self, key, value):
        self.cache[key] = value
        return value


class FakeResult:
    def __init__(self, status):
        self.status = status
        self.code = 250


def make_verify(mapping, default="invalid"):
    calls = []

    def verify(emails, http, **kwargs):
        calls.append(list(emails))
        return {email: FakeResult(mapping.get(email, default)) for email in emails}

    return verify, calls


class LadderTests(unittest.TestCase):
    def test_first_eight_conventions_in_order(self):
        locals_ = [local for _label, local in permute.local_candidates("Jane", "Doe")]
        self.assertEqual(locals_[:9], [
            "jane.doe", "jane", "janedoe", "j.doe", "jdoe",
            "janed", "jane_doe", "doe.jane", "jd",
        ])

    def test_full_ladder_has_more_conventions(self):
        labels = [label for label, _ in permute.local_candidates("Jane", "Doe")]
        self.assertGreaterEqual(len(labels), 15)
        for extra in ("last", "lastfirst", "first.l", "first-last", "last.f"):
            self.assertIn(extra, labels)

    def test_preferred_pattern_comes_first(self):
        pairs = permute.candidates("Jane", "Doe", "acme.com", ["{f}{last}"])
        self.assertEqual(pairs[0][1], "jdoe@acme.com")
        self.assertEqual(len({email for _label, email in pairs}), len(pairs))

    def test_no_duplicates_in_ladder(self):
        locals_ = [local for _label, local in permute.local_candidates("Ana", "Ana")]
        self.assertEqual(len(locals_), len(set(locals_)))

    def test_accents_and_punctuation(self):
        labels = dict(permute.local_candidates("Zoë", "O'Brien"))
        self.assertEqual(labels["first.last"], "zoe.obrien")

    def test_particles(self):
        labels = dict(permute.local_candidates("Jan", "van der Berg"))
        self.assertEqual(labels["first.last"], "jan.vanderberg")

    def test_surname_only_skips_last_dependent_variants(self):
        emails = [email for _label, email in permute.candidates("Cher", "", "acme.com")]
        self.assertEqual(emails, ["cher@acme.com"])


class NameVariantTests(unittest.TestCase):
    def test_alpha_and_truncation(self):
        self.assertEqual(permute.alpha("Jane99"), "jane")
        self.assertEqual(permute.alpha("Zoë"), "zoe")
        self.assertEqual(permute.truncate_at_special("jane_doe99"), "jane")
        self.assertEqual(permute.split_parts("jane_doe99"), ["jane", "doe99"])

    def test_digits_are_cleaned_first(self):
        self.assertEqual(permute.name_variants("Jane99", "Doe")[0],
                         ("clean", "jane", "doe"))
        emails = [email for _label, email in
                  permute.candidates("Jane99", "Doe", "acme.com")]
        self.assertEqual(emails[0], "jane.doe@acme.com")

    def test_login_without_surname_is_split(self):
        emails = [email for _label, email in
                  permute.candidates("jane_doe", "", "acme.com")]
        self.assertEqual(emails[0], "jane.doe@acme.com")

    def test_surname_baked_into_given_name(self):
        self.assertEqual(permute.name_variants("Janesmith", "Smith")[0],
                         ("surname-split", "jane", "smith"))

    def test_digits_in_surname(self):
        self.assertEqual(permute.name_variants("Jane", "Doe99")[0],
                         ("clean", "jane", "doe"))

    def test_clean_names_carry_no_variant_suffix(self):
        labels = [label for label, _ in permute.candidates("Jane", "Doe", "acme.com")]
        self.assertFalse(any("(" in label for label in labels))

    def test_single_clean_token_offers_first_only(self):
        emails = [email for _label, email in permute.candidates("Jane", "", "acme.com")]
        self.assertEqual(emails, ["jane@acme.com"])

    def test_unusable_name_returns_nothing(self):
        # a dirty single token is not worth guessing at
        self.assertEqual(permute.candidates("s3raj", "", "acme.com"), [])


class ResolverTests(unittest.TestCase):
    def test_dirty_github_name_is_cleaned_before_probing(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane99", last_name="Doe",
                        full_name="Jane99 Doe", status="name_only")
        verify, calls = make_verify({"jane.doe@acme.com": "valid"})
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "jane.doe@acme.com")
        self.assertEqual(record.status, "valid")
        self.assertEqual(len(calls), 1)

    def test_resolves_on_third_step(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({"janedoe@acme.com": "valid"})
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "janedoe@acme.com")
        self.assertEqual(record.status, "valid")
        self.assertEqual(counts["valid"], 1)
        self.assertEqual(len(calls), 3)          # stopped right after the hit
        self.assertTrue(any("firstlast" in note for note in record.notes))

    def test_replaces_an_invalid_discovered_address(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", email="jane@acme.com", status="invalid")
        verify, calls = make_verify({"jane.doe@acme.com": "valid"})
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "jane.doe@acme.com")
        self.assertEqual(record.status, "valid")
        self.assertEqual(len(calls), 1)

    def test_catch_all_stops_after_first_candidate(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({}, default="catch_all")
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "jane.doe@acme.com")
        self.assertEqual(record.status, "catch_all")
        self.assertEqual(counts["catch_all"], 1)
        self.assertEqual(len(calls), 1)

    def test_inconclusive_stops_and_keeps_best_guess(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({}, default="unknown")
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "jane.doe@acme.com")
        self.assertEqual(record.status, "unverified")
        self.assertEqual(counts["inconclusive"], 1)
        self.assertEqual(len(calls), 1)

    def test_stops_a_person_after_eight_rejections(self):
        ctx = FakeCtx(permute_max=19)
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({}, default="invalid")
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(len(calls), 8)          # not 19: eight rejections is enough
        self.assertEqual(counts["exhausted"], 1)
        self.assertTrue(any("ladder stopped" in n for n in record.notes))

    def test_greylisted_is_reported_as_greylisted(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, _calls = make_verify({}, default="greylisted")
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.status, "greylisted")
        self.assertEqual(record.email, "jane.doe@acme.com")

    def test_exhausts_the_ladder(self):
        ctx = FakeCtx(permute_max=4)
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({}, default="invalid")
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "")
        self.assertEqual(counts["exhausted"], 1)
        self.assertEqual(len(calls), 4)
        self.assertTrue(any("no permutation" in note for note in record.notes))

    def test_inconclusive_replaces_a_known_bad_address(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", email="jane@acme.com", status="invalid")
        verify, _calls = make_verify({}, default="unknown")
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "jane.doe@acme.com")
        self.assertEqual(record.status, "unverified")
        self.assertTrue(any("previous address rejected" in n for n in record.notes))

    def test_exhausted_invalid_becomes_name_only(self):
        ctx = FakeCtx(permute_max=2)
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", email="jane@acme.com", status="invalid")
        verify, _calls = make_verify({}, default="invalid")
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(record.email, "")
        self.assertEqual(record.status, "name_only")
        self.assertTrue(any("dropped rejected address" in n for n in record.notes))

    def test_never_reuses_an_address_verified_for_someone_else(self):
        ctx = FakeCtx()
        taken = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                       full_name="Jane Doe", email="jane.doe@acme.com", status="valid")
        pending = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                         full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({"jane@acme.com": "valid"})
        resolve_permutations(ctx, [taken, pending], lambda *_: None, verify_fn=verify)

        self.assertNotIn("jane.doe@acme.com", calls[0])
        self.assertEqual(pending.email, "jane@acme.com")

    def test_single_token_name_tries_first_only(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Cher", last_name="",
                        full_name="Cher", status="name_only")
        verify, _calls = make_verify({"cher@acme.com": "valid"})
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)
        self.assertEqual(record.email, "cher@acme.com")
        self.assertEqual(counts["valid"], 1)


if __name__ == "__main__":
    unittest.main()
