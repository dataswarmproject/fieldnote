import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import engine
import indexer


class NoModel:
    available = False
    name = "offline"


class RecordedReply:
    """Controlled provider response for testing plan validation and accounting."""
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
        (self.folder / "Dispatch.md").write_text(
            "The dispatch policy requires immediate action when Hours exceeds 15.", encoding="utf-8"
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

    def test_document_condition_controls_a_full_table_scan(self):
        with closing(indexer.connect(self.db)) as conn:
            table_id = indexer.catalog(conn)[0]["id"]
        reply = dict(table=table_id, operation="count", filters=[
            dict(column="Hours", op=">", value=15)], citations=[1])
        result = self.ask("How many vendors require immediate action under the dispatch policy?",
                          RecordedReply(reply))
        self.assertEqual(result["answer"], "2")
        self.assertEqual(result["scope"].split(" · ")[2], "3 indexed rows examined")
        self.assertEqual(result["input_tokens"], 19)
        self.assertEqual({e["source"] for e in result["evidence"]}, {"Dispatch.md", "Vendors.csv"})

    def test_unsupported_model_filter_is_rejected_and_metered(self):
        with closing(indexer.connect(self.db)) as conn:
            table_id = indexer.catalog(conn)[0]["id"]
        reply = dict(table=table_id, operation="count", filters=[
            dict(column="Hours", op=">", value=555)], citations=[1])
        result = self.ask("How many vendors require immediate action under the dispatch policy?",
                          RecordedReply(reply))
        self.assertEqual((result["status"], result["calls"]), ("unanswered", 1))

    def test_operation_and_threshold_direction_are_bound_to_evidence(self):
        with closing(indexer.connect(self.db)) as conn:
            table_id = indexer.catalog(conn)[0]["id"]
        question = "How many vendors require immediate action under the dispatch policy?"
        for op, comparison in (("sum", ">"), ("count", "<="), ("count", "=")):
            with self.subTest(operation=op, comparison=comparison):
                reply = dict(table=table_id, operation=op, column="Hours",
                             filters=[dict(column="Hours", op=comparison, value=15)], citations=[1])
                self.assertEqual(self.ask(question, RecordedReply(reply))["status"], "unanswered")

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
        self.assertEqual(result["rag"]["status"], "unanswered")
        self.assertEqual(result["rag"]["calls"], 0)
        self.assertEqual(result["engine"]["answer"], "Lyon")

    def test_oversize_required_context_stops_before_generation(self):
        model = RecordedReply({})
        model.input_budget = 50
        model.count_prompt = lambda system, user: 51
        result = self.ask("How many vendors require immediate action under the dispatch policy?", model)
        self.assertEqual((result["status"], result["calls"]), ("unanswered", 0))
        self.assertEqual(result["admission"]["measurement"], "tokenizer")

    def test_document_admission_only_removes_whole_passages(self):
        model = RecordedReply(dict(answer="Paris", status="answered", citations=[1]))
        model.input_budget = 50
        model.count_prompt = lambda system, user: 70 if "[2]" in user else 45
        records = [
            dict(source="A.md", anchor="line 1", text="The office is in Paris, except for the East division."),
            dict(source="B.md", anchor="line 4", text="An amendment applies from September.")
        ]
        result = engine._synthesize("Where is the office?", records, engine.Meter(model), "Anchored evidence")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["evidence"], [records[0]])


if __name__ == "__main__":
    unittest.main()
