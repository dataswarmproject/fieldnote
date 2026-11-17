import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from unittest.mock import patch
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import engine
import indexer


class NoModel:
    available = False
    name = "offline"


class RecordedReply:
    """A controlled RAG generation response and runtime token receipt."""
    available = True
    name = "fixture"

    def __init__(self, payload):
        self.payload = payload

    def complete(self, system, user):
        return json.dumps(self.payload), dict(input_tokens=19, output_tokens=7, usage_kind="runtime")


class QueryEngineTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.folder = Path(self.scratch.name) / "corpus"
        self.folder.mkdir()
        (self.folder / "Vendors.csv").write_text(
            "Code,City,Status,Hours\nA-01,Paris,Ready,10\nA-02,Lyon,Ready,20\n"
            "A-03,Lyon,Hold,30\n", encoding="utf-8"
        )
        (self.folder / "Dispatch policy.md").write_text(
            "A vendor requires immediate action when its Hours value is greater than 15.\n\n"
            "The dispatch owner is the Blue Team. The dispatch threshold is 15 hours.", encoding="utf-8"
        )
        self.db = Path(self.scratch.name) / "index.sqlite"
        indexer.index_folder(self.folder, self.db)

    def ask(self, question, provider=None):
        return engine.compare(self.db, question, provider or NoModel(), mode="engine")["engine"]

    def test_exact_counts_lookups_and_numeric_operations(self):
        for question, expected in [
            ("How many vendors have Status = Ready?", "2"),
            ("What is the City for Code = A-02?", "Lyon"),
            ("What is the total Hours in vendors where Status = Ready?", "30"),
            ("How many vendors where Hours > 10 and City = Lyon?", "2"),
        ]:
            with self.subTest(question=question):
                result = self.ask(question)
                self.assertEqual((result["answer"], result["calls"]), (expected, 0))
                self.assertTrue(result["evidence"])

    def test_unsupported_disjunction_does_not_return_a_zero_count(self):
        result = self.ask("How many vendors have City = Paris or City = Lyon?")
        self.assertEqual(result["status"], "unanswered")

    def test_compiled_rule_scans_complete_table_without_reading_passages(self):
        def deny_read(action, table, column, db, trigger):
            import sqlite3
            if action == sqlite3.SQLITE_READ and table in {"units", "chunks"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        class ForbiddenModel:
            def __getattribute__(self, attribute):
                raise AssertionError(f"Engine accessed the model's {attribute}")

        with ExitStack() as stack:
            for name in ("document_search", "search", "baseline_chunks"):
                stack.enter_context(patch.object(indexer, name, side_effect=AssertionError("retrieval invoked")))
            question = "How many vendors require immediate action under the dispatch policy?"
            result = self.ask(question, ForbiddenModel())
            self.assertEqual(result["answer"], "2", result)
            self.assertEqual(result["scope"].split(" · ")[2], "3 indexed rows examined")
            self.assertEqual(result["input_tokens"], 0)
            self.assertEqual(result["proof"]["filters"], [dict(column="Hours", op=">", value="15")])
            self.assertEqual({e["source"] for e in result["evidence"]}, {"Dispatch policy.md", "Vendors.csv"})
            with closing(indexer.connect(self.db)) as conn:
                conn.set_authorizer(deny_read)
                actual = engine._engine(conn, "Who owns dispatch?", None)
                self.assertEqual(actual["answer"], "Blue Team")

    def test_bound_rule_preserves_aggregate_and_threshold_direction(self):
        q = "What is the average Hours for vendors that need immediate action under the Dispatch policy?"
        result = self.ask(q)
        self.assertEqual((result["status"], result["answer"]), ("answered", "25"))
        self.assertEqual(result["proof"]["confirmed_rows"], 2)
        self.assertEqual(result["proof"]["column"], "Hours")

    def test_fact_queries_check_source_unit_and_conflicts(self):
        for q, expected in [
            ("What is the dispatch threshold in hours?", "15"),
            ("How many hours is the dispatch threshold?", "15"),
            ("Show me the dispatch threshold", "15 hours"),
            ("What is the name of the dispatch owner?", "Blue Team"),
        ]:
            with self.subTest(q=q):
                r = engine.compare(self.db, q, mode="engine")["engine"]
                self.assertEqual((r["route"], r["answer"], r["calls"]), ("Compiled fact", expected, 0), r)
        for q in ("What is the dispatch threshold in minutes?",
                  "Who owns dispatch according to the Renewal policy?",
                  "Which dispatch updates are important?"):
            with self.subTest(q=q):
                self.assertEqual(self.ask(q)["status"], "unanswered")
        (self.folder / "Alternate.md").write_text("The dispatch owner is the Red Team.", encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        self.assertEqual(self.ask("Who owns dispatch?")["status"], "unanswered")
        self.assertEqual(self.ask("Who owns dispatch according to Alternate?")["answer"], "Red Team")

    def test_rule_scope_lists_and_boundary_restatements(self):
        question = "How many vendors require immediate action under the dispatch policy?"
        base = "A vendor requires immediate action when its Hours value is greater than 20"
        for qualifier, status in (
            (", regardless of City or Status. Exactly 20 hours does not require immediate action.", "answered"),
            (", regardless of City or Status. Exactly 20 hours requires immediate action.", "unanswered"),
            (", regardless of City or Status, except for expedited dispatch.", "unanswered"),
            (" or Status is Hold.", "unanswered"),
        ):
            with self.subTest(qualifier=qualifier):
                (self.folder / "Dispatch policy.md").write_text(base + qualifier, encoding="utf-8")
                indexer.index_folder(self.folder, self.db)
                result = self.ask(question)
                self.assertEqual(result["status"], status, result)
                if status == "answered":
                    self.assertEqual(result["answer"], "1")

    def test_conflicting_rules_and_unbound_qualifiers_abstain(self):
        (self.folder / "Other.md").write_text(
            "A vendor requires immediate action when its Hours value is greater than 20.", encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        for q in ("Count vendors that require immediate action",
                  "Count vendors that require immediate action under another policy",
                  "Count vendors that require immediate action unless City = Paris",
                  "Count vendors that need immediate action in Paris"):
            with self.subTest(q=q):
                self.assertEqual(self.ask(q)["status"], "unanswered")
        self.assertEqual(self.ask("Count vendors that require immediate action under Other")["answer"], "1")

    def test_ambiguous_table_and_headers_never_pick_first_binding(self):
        directory = self.folder / "archive"
        directory.mkdir()
        (directory / "Vendors.csv").write_text("Code,City,Hours\nA-02,Rome,99\n", encoding="utf-8")
        (self.folder / "Inventory.csv").write_text("Item,Item\nS,T\n", encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        for q in ("How many vendors have Hours > 15?", "What is the City for Code = A-02?",
                  "Count vendors that need immediate action", "Count Inventory where Item = S"):
            with self.subTest(q=q):
                self.assertEqual(self.ask(q)["status"], "unanswered")

    def test_provenance_text_cannot_change_a_compiled_answer(self):
        evidence = dict(source="Dispatch policy.md", anchor="line 3", text="Conflicting preview text")
        with patch("knowledge.evidence", return_value=evidence):
            result = self.ask("Who owns dispatch?")
        self.assertEqual(result["evidence"], [evidence])
        self.assertEqual(result["answer"], "Blue Team")

    def test_catalog_examples_are_answerable_bounded_and_snapshot_pinned(self):
        import sqlite3

        (self.folder / "Archive.txt").write_text(
            "The dispatch owner is the Red Team. The meeting target is 8 hours. "
            "The target is used only for special meetings.", encoding="utf-8")
        snapshot = indexer.index_folder(self.folder, self.db)["snapshot"]
        connect = indexer.connect

        def catalog_only(path):
            conn = connect(path)

            def authorize(action, table, column, db, trigger):
                if action == sqlite3.SQLITE_READ and (table in {"units", "chunks", "records"}
                                                      or (table == "knowledge_records" and column == "text")):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            return conn

        with patch.object(indexer, "connect", catalog_only):
            samples = engine.examples(self.db, 99, snapshot=snapshot)
            self.assertEqual(engine.examples(self.db, snapshot="other revision"), [])
        self.assertIn("What is the dispatch threshold?", samples)
        self.assertIn("How many rows in Vendors?", samples)
        self.assertNotIn("What is the dispatch owner?", samples)
        self.assertNotIn("What is the meeting target?", samples)
        self.assertLessEqual(len(samples), 6)
        for question in samples:
            self.assertEqual(self.ask(question)["status"], "answered")
        with closing(connect(self.db)) as conn:
            conn.execute("DELETE FROM index_meta WHERE key='compiler'")
            conn.commit()
        self.assertEqual(engine.examples(self.db), [])
        with self.assertRaisesRegex(ValueError, "Reindex this folder"):
            engine.compare(self.db, "Count vendors", mode="engine")
        result = engine.compare(self.db, "Count vendors", NoModel(), mode="rag")
        self.assertEqual(result["rag"]["status"], "unavailable")

    def test_numeric_rule_equality_uses_decimal_even_for_csv_text(self):
        (self.folder / "Vendors.csv").write_text("ID,Hours\nA,15.0\nB,20\n", encoding="utf-8")
        (self.folder / "Dispatch policy.md").write_text(
            "A vendor needs review when its Hours value is equal to 15.", encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        result = self.ask("Count vendors that need review")
        self.assertEqual((result["answer"], result["status"]), ("1", "answered"), result)

    def test_named_policy_resolves_before_assertion_lookup(self):
        directory = self.folder / "archive"
        directory.mkdir()
        (directory / "Dispatch policy.md").write_text("The duty owner is the Ops Team.", encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        self.assertEqual(self.ask("Who owns dispatch under the dispatch policy?")["status"], "unanswered")
        r = self.ask("Who owns duty under archive/Dispatch policy.md?")
        self.assertEqual((r["status"], r["answer"]), ("answered", "Ops Team"))

    def test_distinct_and_grouped_intents_do_not_degrade_to_row_count(self):
        for question in ("How many distinct vendors are ready?", "Count vendors per city"):
            with self.subTest(question=question):
                result = self.ask(question, RecordedReply({}))
                self.assertEqual((result["status"], result["calls"]), ("unanswered", 0))

    def test_blank_csv_values_are_unknown_for_negative_filters(self):
        (self.folder / "Vendors.csv").write_text("Code,City\nA-01,Paris\nA-02,\n", encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        result = self.ask("Count vendors where City != Paris")
        self.assertEqual((result["status"], result["answer"]), ("partial", "0 confirmed; 1 unresolved"))

    def test_ranked_table_subset_does_not_certify_an_exhaustive_count(self):
        rows = [f"{i},Ready," + "order " * 80 for i in range(40)]
        (self.folder / "Vendors.csv").write_text("Code,Status,Notes\n" + "\n".join(rows), encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        model = RecordedReply(dict(answer="40", status="answered", citations=[1]))
        model.count_prompt = lambda system, user: 500
        result = engine.compare(self.db, "How many vendors have Status = Ready?", model)
        self.assertEqual((result["engine"]["status"], result["rag"]["status"]), ("answered","partial"))
        self.assertEqual(result["rag"]["answer"], "40")
        self.assertIn("coverage",result["rag"]["scope"])

    def test_partial_or_invalid_runtime_usage_stays_visible(self):
        class PartialModel(RecordedReply):
            def complete(self, system, user):
                return "done", dict(input_tokens=12, output_tokens=-1, usage_kind="runtime")
        meter = engine.Meter(PartialModel({}))
        meter.complete("instruction","question")
        self.assertEqual(meter.stats()["input_tokens"],12)
        self.assertIsNone(meter.stats()["output_tokens"])
        self.assertEqual(meter.stats()["unmetered_calls"],1)

    def test_offline_rag_never_fabricates_a_generated_answer(self):
        result = engine.compare(self.db, "What is the City for Code = A-03?", NoModel())
        self.assertEqual(result["rag"]["status"], "unavailable")
        self.assertEqual(result["rag"]["calls"], 0)
        self.assertEqual(result["engine"]["answer"], "Lyon")

    def test_model_protocol_errors_are_distinct_from_supported_abstentions(self):
        for payload in ({}, dict(answer="x", status="answered", citations=[99])):
            with self.subTest(payload=payload):
                result = engine.compare(self.db, "Who owns dispatch?", RecordedReply(payload), mode="rag")["rag"]
                self.assertEqual((result["status"], result["calls"]), ("error", 1))
        result = engine.compare(self.db, "Who owns dispatch?",
                                RecordedReply(dict(answer="Unknown", status="unanswered", citations=[])),
                                mode="rag")["rag"]
        self.assertEqual(result["status"], "unanswered")

    def test_oversize_required_context_stops_before_generation(self):
        model = RecordedReply({})
        model.input_budget = 50
        model.count_prompt = lambda system, user: 51
        result = engine.compare(self.db, "Who is the dispatch owner?", model, mode="rag")["rag"]
        self.assertEqual((result["status"], result["calls"]), ("error", 0))
        self.assertEqual(result["admission"]["measurement"], "tokenizer")

    def test_document_admission_only_removes_whole_passages(self):
        model = RecordedReply(dict(answer="Paris", status="answered", citations=[1]))
        model.input_budget = 50
        model.count_prompt = lambda system, user: 70 if "[2]" in user else 45
        records = [
            dict(source="A.md", anchor="line 1", text="The office is in Paris, except for the East division."),
            dict(source="B.md", anchor="line 4", text="An amendment applies from September.")
        ]
        result = engine._synthesize("Where is the office?", records, engine.Meter(model), "BM25 top-5 RAG")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["evidence"], [records[0]])


if __name__ == "__main__":
    unittest.main()
