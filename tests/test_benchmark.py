import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
import indexer


class NoInference:
    name = "fixture"
    available = True

    def start(self):
        pass

    def info(self):
        return {"name":self.name, "available":True, "detail":"test"}


class EvaluationTests(unittest.TestCase):
    def test_answers_are_paired_and_expected_labels_stay_out_of_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "dataset"
            data.mkdir()
            (data / "items.csv").write_text("ID,Label\n001,north\n",encoding="utf-8")
            app = create_app(root, NoInference(), 18741, launch_token="test-launch")
            client = app.test_client()
            host = "http://127.0.0.1:18741"
            def post(route, payload):
                return client.post("/api/" + route, base_url=host, json=payload)
            paired = post("session", {"token":"test-launch"})
            self.assertEqual(paired.status_code, 200)
            client.environ_base["HTTP_X_FIELDNOTE_SESSION"] = paired.get_json()["session"]
            snapshot = post("index", {"folder":str(data)}).get_json()["snapshot"]
            self.assertEqual(post("benchmark",{}).status_code,400)
            question = "What is the label?"
            def fixture(db, query, provider):
                self.assertEqual(query, question)
                result = dict(answer=" Northern. ", status="answered", calls=1, input_tokens=20, output_tokens=8)
                return dict(question=query, snapshot=snapshot,engine=result,rag=dict(result, status="partial"))
            with patch("benchmark.engine.compare",side_effect=fixture):
                resp = post("benchmark",{"cases":[dict(question=question,expected="North",aliases=["northern"])]})
            self.assertEqual(resp.status_code,200)
            summary = resp.get_json()["summary"]
            self.assertEqual(summary["engine"]["correct"],1)
            self.assertEqual(summary["rag"]["correct"],0)
            self.assertEqual(summary["rag"]["value_matches"],1)
            self.assertEqual(summary["rag"]["output_tokens"],8)
            self.assertEqual(client.get("/api/results",base_url=host).get_json()["report"]["snapshot"],snapshot)
            (data / "items.csv").write_text("ID,Label\n001,south\n",encoding="utf-8")
            post("index",{"folder":str(data)})
            self.assertIsNone(client.get("/api/results",base_url=host).get_json()["report"])
            denied = client.post("/api/index",base_url=host,headers={"Origin":"http://unrelated.invalid"},
                                 json={"folder":str(data)})
            self.assertEqual(denied.status_code,403)
