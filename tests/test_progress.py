"""Tests for incremental row snapshots (the `partial` stream events)."""

from __future__ import annotations

import unittest

from ehunter.progress import RowEmitter, row_key


class RowKeyTests(unittest.TestCase):
    def test_name_wins_so_a_later_email_updates_the_row(self):
        before = {"first_name": "Jane", "last_name": "Doe", "email": ""}
        after = {"first_name": "Jane", "last_name": "Doe", "email": "jane@acme.com"}
        self.assertEqual(row_key(before), row_key(after))
        self.assertEqual(row_key(before), "n:jane.doe")

    def test_email_only_row(self):
        self.assertEqual(row_key({"email": "connect@acme.com"}), "e:connect@acme.com")

    def test_handle_only_row(self):
        self.assertEqual(row_key({"github": "harsh-b2"}), "g:harsh-b2")

    def test_accents_and_symbols_collapse(self):
        self.assertEqual(row_key({"first_name": "Zoë", "last_name": "O'Brien"}),
                         row_key({"first_name": "Zoe", "last_name": "OBrien"}))


class EmitterTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.emitter = RowEmitter(self.events.append)

    def test_first_snapshot_carries_everything(self):
        rows = [
            {"first_name": "Jane", "last_name": "Doe", "email": ""},
            {"first_name": "Mark", "last_name": "Smith", "email": ""},
        ]
        self.emitter.snapshot(rows, "discovered")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["stage"], "discovered")
        self.assertEqual(len(self.events[0]["rows"]), 2)
        self.assertEqual(self.events[0]["total"], 2)
        self.assertEqual(self.events[0]["removed"], [])

    def test_unchanged_rows_are_not_resent(self):
        rows = [{"first_name": "Jane", "last_name": "Doe", "email": ""}]
        self.emitter.snapshot(rows, "discovered")
        self.emitter.snapshot(rows, "generated")
        self.assertEqual(len(self.events), 1)          # second call had nothing new

    def test_new_and_changed_rows_are_sent(self):
        rows = [{"first_name": "Jane", "last_name": "Doe", "email": ""}]
        self.emitter.snapshot(rows, "discovered")
        rows.append({"first_name": "Mark", "last_name": "Smith", "email": ""})
        rows[0]["email"] = "jane@acme.com"
        self.emitter.snapshot(rows, "linkedin")

        second = self.events[1]
        self.assertEqual(second["stage"], "linkedin")
        self.assertEqual(len(second["rows"]), 2)       # changed Jane + new Mark
        self.assertEqual(second["total"], 2)

    def test_removed_rows_are_reported(self):
        rows = [
            {"first_name": "Jane", "last_name": "Doe", "email": ""},
            {"first_name": "Mark", "last_name": "Smith", "email": ""},
        ]
        self.emitter.snapshot(rows, "discovered")
        self.emitter.snapshot(rows[:1], "filtered")
        self.assertEqual(self.events[1]["removed"], ["n:mark.smith"])

    def test_nothing_new_emits_nothing(self):
        self.assertIsNone(self.emitter.snapshot([], "discovered"))
        self.assertEqual(self.events, [])

    def test_no_emit_callback_is_harmless(self):
        RowEmitter(None).snapshot([{"first_name": "Jane"}], "discovered")


if __name__ == "__main__":
    unittest.main()
