import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import engine
import indexer
import query_compiler
from app import create_app


class CompilerTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        source = root / "folder"
        source.mkdir()
        (source / "items.csv").write_text("Depot,Stock,Note\nOslo,3,PRIVATE-CELL\nLima,8,OTHER-CELL\n")
        (source / "memo.txt").write_text("PRIVATE-DOCUMENT")
        self.db = root / "index.sqlite"
        indexer.index_folder(source, self.db)
        self.model = Mock(available=True, input_budget=4096)
        self.model.name = "role-test"
        self.model.count_prompt.return_value = 90
        self.plan = dict(operation="sum",table=1,project=2,filters=[dict(column=1,op="=",value="Oslo")])

    def run_plan(self, plan):
        self.model.complete.return_value = json.dumps(plan), dict(input_tokens=90,output_tokens=31,usage_kind="runtime")
        with patch("indexer.baseline_chunks", side_effect=AssertionError("unexpected retrieval")):
            return engine.compare(self.db, "How much stock does Oslo carry?", self.model, mode="compiler")["compiler"]

    def test_scope_metering_and_provenance(self):
        result = self.run_plan(self.plan)
        self.assertEqual((result["status"],result["answer"],result["input_tokens"],result["output_tokens"]),
                         ("answered","3",90,31))
        args, kwargs = self.model.complete.call_args
        self.assertEqual(kwargs["response_schema"], query_compiler.SCHEMA)
        self.assertNotIn("PRIVATE-", str(args))
        self.assertNotIn("Lima", str(args))
        self.assertEqual(result["proof"]["rows_examined"], 2)

    def test_invalid_plans_cannot_reach_executor(self):
        with patch("engine._execute") as execute:
            for change in ({"filters":[dict(column=1,op="=",value="Lima")]},
                           {"table": True}, {"operation":"join"}, {"sql":"SELECT"}, {"project":99}):
                with self.subTest(change=change):
                    self.assertEqual(self.run_plan({**self.plan,**change})["status"],"error")
            self.assertEqual(self.run_plan(dict(operation="abstain",table=0,project=0,filters=[]))["status"],"unanswered")
            execute.assert_not_called()

    def test_paired_browser_api_exposes_model_role(self):
        question = "How much stock does Oslo carry?"
        self.model.info.return_value = {"available":True,"name":self.model.name}
        usage = dict(input_tokens=90,output_tokens=31,usage_kind="runtime")
        self.model.complete.side_effect = [
            (json.dumps(self.plan), usage), (json.dumps(self.plan), usage),
            (json.dumps(dict(answer="3",status="answered",citations=[1])), usage)]
        app = create_app(self.db.parent, self.model, 18741, launch_token="unit-test")
        client = app.test_client()
        def post(route, data):
            return client.post("/api/"+route,base_url="http://127.0.0.1:18741",json=data)
        client.environ_base["HTTP_X_FIELDNOTE_SESSION"] = post("session", {"token":"unit-test"}).get_json()["session"]
        answer = post("ask", dict(mode="compiler",question=question)).get_json()
        self.assertEqual(answer["compiler"]["answer"], "3")
        report = post("benchmark", dict(mode="compiler_rag",cases=[dict(question=question,expected="3")])).get_json()
        self.assertEqual(report["summary"]["compiler"]["correct"], 1)
        self.assertEqual(report["summary"]["rag"]["correct"], 1)
