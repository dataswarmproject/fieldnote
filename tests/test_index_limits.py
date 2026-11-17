import tempfile
import unittest
import os
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import indexer


class IndexLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve() / "dataset"
        self.folder.mkdir()
        self.db = Path(self.tmp.name, "test.sqlite")

    def test_worker_inherits_only_runtime_environment(self):
        with patch.dict(os.environ, {"FIELDNOTE_MODEL_KEY":"fixture","GH_TOKEN":"fixture",
                                     "PYTHONPATH":"fixture","TZ":"UTC"}):
            env = indexer.worker_environment()
        self.assertEqual(env["TZ"], "UTC")
        self.assertTrue({"FIELDNOTE_MODEL_KEY","GH_TOKEN","PYTHONPATH"}.isdisjoint(env))

    def test_file_omission_is_explicit_and_stale_content_is_retired(self):
        item = self.folder / "policy.txt"
        item.write_text("registered", encoding="utf-8")
        indexer.index_folder(self.folder, self.db)
        item.write_text("revised policy contents", encoding="utf-8")
        (self.folder / "short.txt").write_text("active", encoding="utf-8")
        with patch.object(indexer, "MAX_FILE_BYTES", 16):
            indexer._refresh(self.folder, self.db)
        report = indexer.status(self.db)
        self.assertEqual(report["inventory"][0]["status"], "skipped")
        self.assertEqual(report["issues"][0]["source"], "policy.txt")
        with closing(indexer.connect(self.db)) as conn:
            self.assertEqual(indexer.search(conn, "registered"), [])
            self.assertEqual(indexer.search(conn, "active")[0]["source"], "short.txt")

    def test_folder_budget_failure_retains_published_generation(self):
        first = self.folder / "alpha.txt"
        first.write_text("older", encoding="utf-8")
        snapshot = indexer.index_folder(self.folder, self.db)["snapshot"]
        first.write_text("newer", encoding="utf-8")
        (self.folder / "second.txt").write_text("extra", encoding="utf-8")
        with patch.object(indexer, "MAX_SOURCE_BYTES", 6), self.assertRaises(indexer.FolderLimit):
            indexer._refresh(self.folder, self.db)
        self.assertEqual(indexer.status(self.db)["snapshot"], snapshot)
        with closing(indexer.connect(self.db)) as conn:
            self.assertTrue(indexer.search(conn, "older"))
            self.assertFalse(indexer.search(conn, "newer"))

    def test_interrupted_worker_preserves_existing_snapshot(self):
        source = self.folder / "policy.txt"
        source.write_text("original", encoding="utf-8")
        snapshot = indexer.index_folder(self.folder, self.db)["snapshot"]
        source.write_text("revision", encoding="utf-8")
        with patch.object(indexer, "REFRESH_TIMEOUT", 0), self.assertRaisesRegex(ValueError, "timed out"):
            indexer.index_folder(self.folder, self.db)
        self.assertEqual(indexer.status(self.db)["snapshot"], snapshot)

    def test_office_size_and_column_limits_skip_whole_source(self):
        with ZipFile(self.folder / "procedure.docx", "w") as doc:
            doc.writestr("word/document.xml", "<document>ordinary text</document>")
        (self.folder / "ledger.csv").write_text("A,B,C\n1,2,3\n", encoding="utf-8")
        with (patch.object(indexer, "MAX_ARCHIVE_MEMBER_BYTES", 12),
              patch.object(indexer, "MAX_TABLE_COLUMNS", 2)):
            indexer._refresh(self.folder, self.db)
        report = indexer.status(self.db)
        self.assertEqual(report["rows"], 0)
        self.assertEqual({i["status"] for i in report["inventory"]}, {"skipped"})
        self.assertTrue(any("expanded-size" in i["reason"] for i in report["issues"]))
        self.assertTrue(any("column limit" in i["reason"] for i in report["issues"]))

    def test_link_excluded_with_intact_source(self):
        original = self.folder / "source.txt"
        original.write_text("authorized content", encoding="utf-8")
        try:
            (self.folder / "pointer.txt").symlink_to(original)
        except OSError:
            self.skipTest("Creating a symbolic link requires an OS privilege on this host.")
        report = indexer.index_folder(self.folder, self.db)
        self.assertEqual(report["issues"][0]["source"], "pointer.txt")
        with closing(indexer.connect(self.db)) as conn:
            self.assertEqual([r["source"] for r in indexer.search(conn,"authorized")], ["source.txt"])
