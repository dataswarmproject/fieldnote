import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark
from scripts import evaluate_compiled as challenge
from scripts import evaluate_wikitables as wikitables


class ChallengeTests(unittest.TestCase):
    def test_scoring_separates_coverage_abstentions_partial_answers_and_unknown_cost(self):
        cases = [dict(question="known", expected="North"), dict(question="also known", expected="East"),
                 dict(question="missing", expected="[unanswerable]", answerable=False),
                 dict(question="conflict", expected="[unanswerable]", answerable=False)]
        answers = {"known": (" North. ", "answered"), "also known": ("East", "partial"),
                   "missing": ("insufficient evidence", "unanswered"), "conflict": ("West", "answered")}

        def compare(db, query, provider):
            answer, status = answers[query]
            result = dict(answer=answer, status=status, calls=1, input_tokens=8, output_tokens=2)
            return dict(snapshot="demo", engine=result,
                        rag=dict(result, status="error", input_tokens=None, output_tokens=None))

        with patch("benchmark.engine.compare", side_effect=compare):
            report = benchmark.run("db", cases, challenge.OfflineProvider(), "demo")
        e, r = report["summary"]["engine"], report["summary"]["rag"]
        self.assertEqual((e["accuracy"], e["answerable_coverage"], e["abstention_accuracy"]), (.5, .5, .5))
        self.assertEqual((e["correct_answers"], e["correct_abstentions"], e["false_answers"]), (1, 1, 1))
        self.assertEqual((e["value_matches"], e["answered"], e["partial"]), (2, 2, 1))
        self.assertEqual((e["calls"], e["input_tokens"], e["output_tokens"]), (4, 32, 8))
        self.assertEqual((r["correct"], r["errors"], r["unmetered_calls"]), (0, 4, 4))

    def test_generated_labels_change_at_each_revision_and_content_is_reproducible(self):
        fixture = challenge.Challenge(77)
        first, second, third = [fixture.cases(phase) for phase in challenge.PHASES]
        self.assertNotEqual(first[0]["expected"], second[0]["expected"])
        self.assertGreater(int(first[1]["expected"]), int(second[1]["expected"]))
        self.assertTrue(first[2]["answerable"])
        self.assertFalse(second[2]["answerable"])
        self.assertFalse(third[0]["answerable"])

        def digests():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = []
                for phase in challenge.PHASES:
                    fixture.write_phase(root, phase)
                    self.assertFalse(any(p.suffix == ".json" for p in root.iterdir()))
                    result.append(challenge.corpus_digest(root))
                self.assertFalse((root / "Dispatch.md").exists())
                return result

        self.assertEqual(digests(), digests())
        self.assertEqual(len(set(digests())), 3)
        self.assertNotEqual(fixture.rows, challenge.Challenge(78).rows)

    def test_offline_runner_exercises_real_indexer_and_queries_without_rag(self):
        report = challenge.evaluate()
        self.assertEqual(set(report["summary"]), {"engine"})
        stats = report["summary"]["engine"]
        self.assertEqual((stats["answerable"], stats["unanswerable"]), (7, 3))
        self.assertEqual((stats["errors"], stats["calls"], stats["input_tokens"], stats["output_tokens"]),
                         (0, 0, 0, 0))
        snapshots = {phase["snapshot"] for phase in report["phases"]}
        self.assertEqual(len(snapshots), 3)
        for phase in report["phases"]:
            self.assertEqual(phase["rows"], 64)
            self.assertTrue(all(row["snapshot"] == phase["snapshot"] for row in phase["runs"]))

    def test_paired_mode_requires_working_provider(self):
        with self.assertRaisesRegex(ValueError, "available local model"):
            challenge.evaluate(mode="both")

    def test_external_multi_value_metric_requires_all_values_with_multiplicity(self):
        gold = ["North", "South"]
        self.assertTrue(wikitables.values_match('["South", " NORTH."]', gold))
        self.assertTrue(wikitables.values_match("North and South", gold))
        self.assertFalse(wikitables.values_match("North", gold))
        self.assertFalse(wikitables.values_match("North|North|South", gold))
        self.assertTrue(wikitables.values_match("City, State", ["City, State"]))
        self.assertEqual(wikitables.unescape(r"a\pb\\n\nc"), "a|b\\n\nc")

    def test_external_runner_rejects_modified_or_shortened_suite_before_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "csv" / "201-csv").mkdir(parents=True)
            table = root / "csv" / "201-csv" / "1.csv"
            table.write_text("A,B\n2,3\n", encoding="utf-8")
            tsv = root / "data" / "pristine-unseen-tables.tsv"
            header = "id\tutterance\tcontext\ttargetValue\n"
            row = "x\tWho?\tcsv/201-csv/1.csv\tNorth\n"
            tsv.write_text(header + row * 11, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "first 12"):
                wikitables.load_slice(root)
            tsv.write_text(header + row * 12, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                wikitables.load_slice(root)
