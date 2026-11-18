"""The compiler emits scoped operations in exactly one published index generation."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import indexer
import knowledge


class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name, "corpus").resolve()
        self.root.mkdir()
        self.db = self.root.parent / "test.sqlite"

    def write(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")

    def refresh(self):
        indexer._refresh(self.root, self.db)
        return indexer.status(self.db)["knowledge"]

    def rows(self, kind, key=None):
        with closing(indexer.connect(self.db)) as conn:
            return list(knowledge.records(conn, kind, key))

    def test_bundled_corpus_and_artifact_provenance(self):
        demo = Path(indexer.__file__).resolve().parent / "Demo dataset"
        report = indexer.index_folder(demo, self.db)["knowledge"]
        self.assertEqual(report.pop("gaps"), [{"source": "Handoff notes.txt", "anchor": "line 3",
                                              "reason": "The preceding fact has a restricted use."}])
        self.assertEqual(report, dict(version=knowledge.VERSION, needs_reindex=False,
                                      facts=3, rules=1, blocked=1, omissions=1))
        with closing(indexer.connect(self.db)) as conn:
            rule, = knowledge.records(conn, "rule", " Requires  ESCALATION ", include_evidence=False)
            self.assertNotIn("text", rule)
            self.assertEqual((rule["entity"], rule["column"], rule["op"], rule["value"]),
                             ("supplier", "Review hours", ">", "48"))
            proof = knowledge.evidence(conn, rule["id"])
            self.assertEqual((proof["source"], proof["anchor"]), ("Renewal policy.md", "line 5"))
            self.assertTrue(proof["text"].endswith("regardless of site or status."))
            fact, = knowledge.records(conn, "fact", "receiving office")
            self.assertEqual((fact["status"], fact["value"]), ("active", "Harbor House"))

    def test_operands_units_and_decimal_cutoffs(self):
        for wording, op in (("is less than", "<"), ("is at least", ">="), ("falls below", "<"),
                            ("is at most", "<="), ("equals", "="), ("exceeds", ">")):
            with self.subTest(op=wording):
                self.write("note.txt", f"An order needs attention if its Delay hours {wording} -0.25 hours, irrespective of City or Status.")
                self.refresh()
                row, = self.rows("rule", "needs attention")
                self.assertEqual((row["status"], row["entity"], row["column"], row["op"], row["value"], row["unit"]),
                                 ("active", "order", "Delay hours", op, "-0.25", "hours"))

    def test_unknown_context_conditions_and_mismatched_restatements_withhold_rule(self):
        base = "A parcel requires review if its Delay hours exceeds 3."
        for template in (
            "{}\n\nThis excludes frozen parcels.",
            "{} Exactly 4 hours does not require review.",
            "{} Exactly 3 minutes does not require review.",
            "{} Exactly 3 hours requires review.",
            "# Draft\n\n{}", "## West parcel policy\n\n{}",
            "Only in July.\n\n{}", "{}\n\nEffective next month.",
        ):
            with self.subTest(template=template):
                self.write("rules.md", template.format(base))
                report = self.refresh()
                self.assertEqual(report["rules"], 0)
                self.assertGreater(report["omissions"], 0)
                self.assertEqual(self.rows("rule")[0]["status"], "blocked")

    def test_retractions_and_qualified_bodies_remain_visible_by_symbol(self):
        self.write("base.txt", "The dispatch office is Cedar House. A parcel needs attention when Hours exceeds 1.")
        self.write("revision.txt", "The dispatch office has changed from Cedar House. A parcel does not need attention when Hours exceeds 1.")
        self.refresh()
        self.assertEqual([r["status"] for r in self.rows("fact", "dispatch office")], ["active", "blocked"])
        self.assertEqual([r["status"] for r in self.rows("rule", "needs attention")], ["active", "blocked"])
        self.write("revision.txt", "During winter, the dispatch office is Juniper. In West, a parcel needs attention when Hours exceeds 2.")
        self.refresh()
        self.assertEqual([r["status"] for r in self.rows("fact", "dispatch office")], ["active", "blocked"])
        self.assertEqual([r["status"] for r in self.rows("rule", "needs attention")], ["active", "blocked"])
        self.write("revision.txt", "The dispatch office is Cedar House unless closed. A parcel needs attention if Hours exceeds 1 or City is Green.")
        self.refresh()
        self.assertEqual([r["status"] for r in self.rows("fact", "dispatch office")], ["active", "blocked"])
        self.assertEqual([r["status"] for r in self.rows("rule", "needs attention")], ["active", "blocked"])

    def test_unknown_statements_in_tables_cannot_issue_rules(self):
        self.write("table.csv", "Notes\nA parcel needs review if Hours exceeds 1.\n")
        report = self.refresh()
        self.assertEqual((report["rules"], report["omissions"]), (0, 0))

    def test_unchanged_refresh_snapshot_isolation_and_retirement(self):
        self.write("office.txt", "The dispatch office is Cedar House.")
        self.refresh()
        old, = self.rows("fact")
        self.refresh()
        self.assertEqual(self.rows("fact"), [old])
        with closing(indexer.connect(self.db)) as reader:
            reader.execute("BEGIN")
            self.assertEqual(list(knowledge.records(reader, "fact")), [old])
            self.write("office.txt", "The dispatch office is River House.")
            self.refresh()
            self.assertEqual(self.rows("fact")[0]["value"], "River House")
            self.assertEqual(list(knowledge.records(reader, "fact")), [old])
        with patch.object(indexer, "MAX_SOURCE_BYTES", 1), self.assertRaises(indexer.FolderLimit):
            indexer._refresh(self.root, self.db)
        self.assertEqual(self.rows("fact")[0]["value"], "River House")
        (self.root / "office.txt").unlink()
        self.refresh()
        self.assertEqual(self.rows("fact"), [])

    def test_upgrade_from_v2_recompiles_unchanged_sources(self):
        self.assertFalse(indexer.status(self.db)["knowledge"]["needs_reindex"])
        self.write("threshold.txt", "The alert threshold is 15 minutes.")
        self.refresh()
        with closing(indexer.connect(self.db)) as conn, conn:
            conn.execute("DROP TABLE knowledge_records")
            conn.execute("DROP TABLE knowledge_gaps")
            conn.execute("DELETE FROM index_meta WHERE key='compiler'")
            conn.execute("UPDATE index_meta SET value='fieldnote-index-v2' WHERE key='parser'")
        report = indexer.status(self.db)["knowledge"]
        self.assertTrue(report["needs_reindex"])
        self.assertEqual(self.rows("fact"), [])
        self.assertEqual(self.refresh()["facts"], 1)
        self.assertEqual(self.rows("fact")[0]["unit"], "minutes")

    def test_statement_budget_invalidates_accepted_prefix(self):
        self.write("many.txt", "The office is Red. The editor is Green. The owner is Blue.")
        with patch.object(knowledge, "MAX_CLAUSES", 2):
            report = self.refresh()
        self.assertEqual((report["facts"], report["blocked"], report["omissions"]), (0, 2, 1))

    def test_coverage_examples_are_bounded_and_full_counts_are_published(self):
        self.write("notes.txt", "\n\n".join(f"Something happened at {n}." for n in range(60)))
        report = self.refresh()
        self.assertEqual(report["omissions"], 60)
        self.assertEqual(len(report["gaps"]), 50)
        self.write("notes.txt", "The owner is Juniper.")
        report = self.refresh()
        self.assertEqual((report["facts"], report["omissions"], report["gaps"]), (1, 0, []))


if __name__ == "__main__":
    unittest.main()
