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
    def __init__(self, status, code=250, message=""):
        self.status = status
        self.code = code
        self.message = message


def make_verify(mapping, default="invalid"):
    """`mapping` values are a status, or a list of statuses consumed one per call."""
    calls = []
    kwargs_seen = []

    def verify(emails, http, **kwargs):
        calls.append(list(emails))
        kwargs_seen.append(kwargs)
        out = {}
        for email in emails:
            status = mapping.get(email, default)
            if isinstance(status, list):
                status = status.pop(0) if len(status) > 1 else status[0]
            out[email] = FakeResult(status)
        return out

    verify.kwargs = kwargs_seen
    return verify, calls


def no_sleep(_seconds):
    pass


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

    def test_exhaustive_mode_tries_every_candidate(self):
        ctx = FakeCtx(permute_max=19, permute_all=True)
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({}, default="invalid")
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)

        self.assertEqual(len(calls), 19)      # every convention, not the default 8
        self.assertEqual(counts["exhausted"], 1)

    def test_dirty_name_has_more_candidates_than_the_ladder(self):
        from ehunter import permute

        clean = permute.candidates("Jane", "Doe", "acme.com")
        dirty = permute.candidates("Jane99", "Doe", "acme.com")
        self.assertEqual(len(clean), 19)
        self.assertGreater(len(dirty), 19)       # clean + raw spelling

    def test_greylisted_is_reported_as_greylisted(self):
        ctx = FakeCtx()
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, _calls = make_verify({}, default="greylisted")
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify,
                             sleep_fn=no_sleep)

        self.assertEqual(record.status, "greylisted")
        self.assertEqual(record.email, "jane.doe@acme.com")

    def test_greylisted_address_is_retried_and_verified(self):
        ctx = FakeCtx(greylist_wait=5, greylist_retries=1)   # retry is opt-in
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({"jane.doe@acme.com": ["greylisted", "valid"]})
        sleeps = []
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify,
                                      sleep_fn=sleeps.append)

        self.assertEqual(record.status, "valid")
        self.assertEqual(record.email, "jane.doe@acme.com")
        self.assertEqual(sleeps, [5])
        self.assertEqual(calls, [["jane.doe@acme.com"], ["jane.doe@acme.com"]])
        self.assertEqual(counts["valid"], 1)
        self.assertEqual(counts["inconclusive"], 0)
        self.assertTrue(any("greylist retry" in n for n in record.notes))

    def test_greylisted_stays_greylisted_when_retry_also_defers(self):
        ctx = FakeCtx(greylist_retries=1)
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({}, default="greylisted")
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify,
                             sleep_fn=no_sleep)

        self.assertEqual(record.status, "greylisted")
        self.assertEqual(len(calls), 2)              # one probe + one retry, no more

    def test_retry_rejection_returns_person_to_name_only(self):
        ctx = FakeCtx(greylist_retries=1)
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, _calls = make_verify({"jane.doe@acme.com": ["greylisted", "invalid"]})
        counts = resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify,
                                      sleep_fn=no_sleep)

        self.assertEqual(record.email, "")
        self.assertEqual(record.status, "name_only")
        self.assertEqual(counts["exhausted"], 1)
        self.assertEqual(counts["inconclusive"], 0)

    def test_no_retry_pass_by_default(self):
        ctx = FakeCtx()                     # default greylist_retries = 0
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, calls = make_verify({}, default="greylisted")
        sleeps = []
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify,
                             sleep_fn=sleeps.append)
        self.assertEqual(len(calls), 1)     # one probe, no retry
        self.assertEqual(sleeps, [])        # and no 60s wait
        self.assertEqual(record.status, "greylisted")
        self.assertEqual(record.email, "jane.doe@acme.com")

    def test_rejection_notes_carry_the_server_reply(self):
        ctx = FakeCtx(permute_max=2)
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")

        def verify(emails, http, **kwargs):
            return {e: FakeResult("invalid", 550, "5.1.1 no such user") for e in emails}

        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)
        self.assertTrue(any("550 5.1.1 no such user" in n for n in record.notes))

    def test_sample_teaches_the_convention_to_everyone_else(self):
        ctx = FakeCtx(permute_max=4)
        firsts = ["Ava", "Ben", "Cara", "Dev", "Eli", "Fay", "Gus", "Hana", "Ian",
                  "Jo", "Kai", "Lea", "Max", "Nia"]
        records = [Record(domain="acme.com", first_name=f, last_name="Person",
                          full_name=f"{f} Person", status="name_only") for f in firsts]
        calls = []

        def verify(emails, http, **kwargs):
            calls.append(list(emails))
            return {e: FakeResult("valid" if "." not in e.split("@")[0]
                                  and "person" not in e else "invalid")
                    for e in emails}

        resolve_permutations(ctx, records, lambda *_: None, verify_fn=verify,
                             sleep_fn=no_sleep)

        self.assertTrue(all(r.status == "valid" for r in records))
        self.assertEqual(records[-1].email, "nia@acme.com")
        # sample: round 1 (first.last, rejected) + round 2 (first, accepted) = 2 calls;
        # everyone else then goes straight to `first` in a single call.
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("." not in e.split("@")[0] for e in calls[2]))

    def test_control_address_is_passed_to_the_verifier(self):
        ctx = FakeCtx(control_address="me@acme.com")
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, _calls = make_verify({"jane.doe@acme.com": "valid"})
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)
        self.assertEqual(verify.kwargs[0]["control"], "me@acme.com")

    def test_control_from_another_domain_is_ignored(self):
        ctx = FakeCtx(control_address="me@elsewhere.org")
        record = Record(domain="acme.com", first_name="Jane", last_name="Doe",
                        full_name="Jane Doe", status="name_only")
        verify, _calls = make_verify({"jane.doe@acme.com": "valid"})
        resolve_permutations(ctx, [record], lambda *_: None, verify_fn=verify)
        self.assertEqual(verify.kwargs[0]["control"], "")

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


class FakeSMTP:
    """Stands in for smtplib.SMTP: `rules(addr)` -> (code, text) for each RCPT."""

    rules = staticmethod(lambda addr: (250, b"ok"))
    sessions = 0

    helo_names: list = []

    def __init__(self, host, port, timeout=None, local_hostname=None):
        FakeSMTP.sessions += 1
        FakeSMTP.helo_names.append(local_hostname)

    def ehlo_or_helo_if_needed(self):
        pass

    def mail(self, _from):
        return 250, b"ok"

    def rcpt(self, addr):
        return FakeSMTP.rules(addr)

    def rset(self):
        return 250, b"ok"

    def quit(self):
        pass

    def close(self):
        pass


class SmtpAuditTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        from ehunter import smtp

        self.smtp = smtp
        FakeSMTP.sessions = 0
        # The fake server listens locally, so a configured SMTP_PROXY (from .env) must not
        # redirect these probes into a tunnel that has nothing to do with the test.
        proxy_patch = mock.patch.object(smtp, "proxy_settings", return_value=None)
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)
        patcher = mock.patch.object(smtp.smtplib, "SMTP", FakeSMTP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = smtp.DomainState(domain="acme.com", mx=["mx.acme.com"], catch_all=False)

    def run_batch(self, emails, control="", delay=0.0):
        return self.smtp.verify_domain(emails, self.state, "me@example.org", 5,
                                       delay=delay, control=control)

    def test_rejections_stand_when_the_control_address_is_accepted(self):
        FakeSMTP.rules = staticmethod(
            lambda a: (250, b"ok") if a == "me@acme.com" else (550, b"5.1.1 no such user"))
        out = self.run_batch(["a@acme.com", "b@acme.com"], control="me@acme.com")
        self.assertEqual({r.status for r in out.values()}, {"invalid"})
        self.assertEqual(out["a@acme.com"].message, "5.1.1 no such user")

    def test_rejections_are_not_trusted_when_the_control_is_refused(self):
        FakeSMTP.rules = staticmethod(lambda a: (550, b"5.1.1 no such user"))
        out = self.run_batch(["a@acme.com", "b@acme.com"], control="me@acme.com")
        self.assertEqual({r.status for r in out.values()}, {"greylisted"})
        self.assertIn("control address me@acme.com was refused", out["a@acme.com"].message)

    def test_without_a_control_rejections_are_taken_at_face_value(self):
        FakeSMTP.rules = staticmethod(lambda a: (550, b"5.1.1 no such user"))
        out = self.run_batch(["a@acme.com"])
        self.assertEqual(out["a@acme.com"].status, "invalid")

    def test_sessions_are_rotated_every_few_probes(self):
        FakeSMTP.rules = staticmethod(lambda a: (250, b"ok"))
        emails = [f"u{i}@acme.com" for i in range(self.smtp.SESSION_PROBES * 2 + 1)]
        self.run_batch(emails)
        self.assertEqual(FakeSMTP.sessions, 3)

    def test_ehlo_announces_the_sender_domain_not_the_machine_name(self):
        FakeSMTP.helo_names = []
        self.run_batch(["a@acme.com"])
        self.assertEqual(FakeSMTP.helo_names, ["example.org"])    # from_addr me@example.org
        self.assertEqual(self.smtp.helo_name("seraj@gmail.com"), "gmail.com")
        self.assertEqual(self.smtp.helo_name(""), "localhost.localdomain")

    def test_greylisted_note_includes_the_server_text(self):
        res = self.smtp.VerifyResult(email="a@acme.com", status="greylisted",
                                     code=451, message="4.7.1 try later")
        self.assertIn("4.7.1 try later", res.note)


if __name__ == "__main__":
    unittest.main()
