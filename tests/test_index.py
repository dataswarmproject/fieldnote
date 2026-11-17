"""Run from the application directory with python -m unittest discover -s tests."""
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

from openpyxl import Workbook
from pypdf import PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import indexer


class LocalIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "sources")
        self.root.mkdir()
        self.db = Path(self.tmp.name, "corpus.sqlite")

    def index(self):
        return indexer.index_folder(self.root, self.db)

    def test_documents_workbooks_and_known_omissions(self):
        (self.root / "register.csv").write_text("ID;Region\n0003;West\n0004;West\n", encoding="utf-8")
        (self.root / "Policy.md").write_text("# Renewal\n\nWest uses the signed document.", encoding="utf-8")
        with ZipFile(self.root / "terms.docx", "w") as package:
            package.writestr("word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>All exceptions require written consent.</w:t>'
                '</w:r></w:p></w:body></w:document>')
        with PdfWriter() as writer:
            writer.add_blank_page(96, 96)
            writer.write(self.root / "scan.pdf")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Approvals"
        sheet.append(["Count"])
        sheet.append([10])
        sheet.append(["=5+5"])
        workbook.save(self.root / "approvals.xlsx")
        (self.root / "corrupt.xlsx").write_bytes(b"bad")
        (self.root / ".private.txt").write_text("confidential")
        status = self.index()
        self.assertEqual((status["files"], status["documents"], status["tables"], status["rows"]), (7, 2, 2, 4))
        self.assertEqual({i["source"] for i in status["issues"]},
                         {"scan.pdf", "approvals.xlsx", "corrupt.xlsx", ".private.txt"})
        with closing(indexer.connect(self.db)) as conn:
            tables = {t["sheet"]: t for t in indexer.catalog(conn)}
            self.assertEqual(list(indexer.table_rows(conn,tables["register"]["id"]))[0]["values"], ["0003", "West"])
            self.assertEqual(list(indexer.table_rows(conn,tables["Approvals"]["id"]))[-1]["values"], [None])
            self.assertEqual(indexer.document_search(conn, "written consent")[0]["source"], "terms.docx")
            self.assertEqual(indexer.baseline_chunks(conn, "0003")[0]["source"], "register.csv")
            self.assertEqual(indexer.search(conn, "confidential"), [])
            self.assertIsInstance(indexer.search(conn, '") OR *: ('), list)

    def test_changes_are_pinned_and_stale_hits_removed(self):
        item = self.root / "rule.txt"
        item.write_text("alpha", encoding="utf-8")
        (self.root / "keep.txt").write_text("stable", encoding="utf-8")
        original = self.index()
        self.assertEqual(self.index()["snapshot"], original["snapshot"])
        with closing(indexer.connect(self.db)) as reader:
            reader.execute("BEGIN")
            self.assertTrue(indexer.search(reader, "alpha"))
            previous = item.stat()
            item.write_text("bravo", encoding="utf-8")
            os.utime(item, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            updated = self.index()
            self.assertNotEqual(updated["snapshot"], original["snapshot"])
            self.assertTrue(indexer.search(reader, "alpha"))
        with closing(indexer.connect(self.db)) as reader:
            self.assertFalse(indexer.baseline_chunks(reader, "alpha"))
            self.assertTrue(indexer.baseline_chunks(reader, "bravo"))
        item.unlink()
        self.index()
        with closing(indexer.connect(self.db)) as reader:
            self.assertFalse(indexer.search(reader, "bravo"))
            self.assertTrue(indexer.search(reader, "stable"))

    def test_long_cell_is_complete_and_passages_are_bounded(self):
        value = "echo " * 30000 + "lastword"
        (self.root / "long.csv").write_text("id,notes\n7," + value, encoding="utf-8")
        self.assertEqual(self.index()["issues"], [])
        with closing(indexer.connect(self.db)) as conn:
            table = indexer.catalog(conn)[0]
            self.assertEqual(next(indexer.table_rows(conn, table["id"]))["values"][1], value)
            hits = indexer.baseline_chunks(conn, "lastword")
            self.assertTrue(hits)
            self.assertTrue(all(len(hit["text"]) <= indexer.CHUNK_SIZE for hit in hits))


if __name__ == "__main__":
    unittest.main()
