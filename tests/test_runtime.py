import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from app import ROOT, create_app
from provider import LocalModel


class LocalRuntimeTests(unittest.TestCase):
    def test_expired_readiness_is_rechecked_after_a_stopped_child_or_connection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            model = LocalModel(directory)
            model.available = model.started = True
            model.child = Mock()
            model.child.poll.return_value = 1
            with patch("provider.read_local_config", return_value={}):
                model.start()
            self.assertFalse(model.info()["available"])
            self.assertIsNone(model.child)
            model.available = True
            with patch.object(model, "_request", side_effect=ConnectionRefusedError), self.assertRaises(OSError):
                model.count_prompt("system", "question")
            self.assertFalse(model.info()["available"])

    def test_pairing_and_origin_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(directory, None, 18741, launch_token="test-session")
            client = app.test_client()
            base = "http://127.0.0.1:18741"
            self.assertEqual(client.get("/api/status", base_url=base).status_code, 401)
            self.assertEqual(client.post("/api/session", base_url=base, json={"token":"wrong"}).status_code, 403)
            paired = client.post("/api/session", base_url=base, json={"token":"test-session"})
            self.assertEqual(paired.status_code, 200)
            self.assertIn("HttpOnly", paired.headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", paired.headers["Set-Cookie"])
            self.assertEqual(client.get("/api/cases", base_url=base).status_code, 401)
            client.environ_base["HTTP_X_FIELDNOTE_SESSION"] = paired.get_json()["session"]
            self.assertEqual(client.get("/api/cases", base_url=base).status_code, 200)
            self.assertEqual(client.get("/api/cases", base_url=base,
                                        headers={"Sec-Fetch-Site":"cross-site"}).status_code, 403)
            self.assertEqual(client.post("/api/index", base_url=base, json={},
                                         headers={"Origin":"http://another.invalid"}).status_code, 403)
            self.assertEqual(client.get("/api/health", base_url="http://another.invalid").status_code, 403)

    def test_no_unsolicited_model_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True), \
                patch("provider.read_local_config", return_value={}), patch.object(LocalModel, "_probe") as probe:
            model = LocalModel(directory)
            model.start()
            self.assertFalse(model.info()["available"])
            probe.assert_not_called()

    def test_real_server_pair_ask_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as free:
                free.bind(("127.0.0.1", 0))
                port = free.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            state = Path(directory)
            cmd = [sys.executable, str(ROOT / "app.py"), "--port", str(port), "--state", str(state)]
            # Reserve an unused model endpoint throughout startup; the deterministic path uses no model.
            with socket.socket() as unused:
                unused.bind(("127.0.0.1", 0))
                env = dict(os.environ, FIELDNOTE_MODEL_URL=f"http://127.0.0.1:{unused.getsockname()[1]}/v1")
                process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    keyfile = state / f"server-{port}.key"
                    deadline = time.monotonic() + 25
                    while not keyfile.is_file() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if not keyfile.is_file():
                        self.fail("Startup failed: " + (process.stdout.read() if process.poll() is not None else "timed out"))
                    if os.name == "posix":
                        self.assertEqual(keyfile.stat().st_mode & 0o077, 0)
                    client = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                         urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                    def post(path, payload):
                        req = urllib.request.Request(url + path, data=json.dumps(payload).encode(),
                                                     headers={"Content-Type":"application/json"})
                        with client.open(req, timeout=10) as response:
                            return json.load(response)
                    with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                        client.open(url + "/api/status", timeout=10)
                    self.assertEqual(unauthorized.exception.code, 401)
                    unauthorized.exception.close()
                    credentials = json.loads(keyfile.read_text(encoding="utf-8"))
                    paired = post("/api/session", {"token":credentials["launch"]})
                    self.assertTrue(paired["paired"])
                    client.addheaders = [("X-Fieldnote-Session", paired["session"])]
                    pair = post("/api/ask", {"question":"How many suppliers have Status = Eligible?"})
                    self.assertEqual(pair["engine"]["answer"], "32")
                    self.assertEqual(pair["engine"]["calls"], 0)
                    status = subprocess.run([*cmd, "--status"], env=env, timeout=10, capture_output=True, text=True)
                    self.assertIn("is running", status.stdout)
                    stop = subprocess.run([*cmd, "--stop"], env=env, timeout=10, capture_output=True, text=True)
                    self.assertEqual(stop.returncode, 0, stop.stderr)
                    self.assertEqual(process.wait(timeout=10), 0)
                    self.assertFalse(keyfile.exists())
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=10)
                    process.stdout.close()
