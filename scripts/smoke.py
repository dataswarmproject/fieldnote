"""Exercise installation, browser pairing, indexing, local QA and graceful stop."""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp, socket.socket() as port_selector:
        port_selector.bind(("127.0.0.1", 0))
        port = port_selector.getsockname()[1]
        port_selector.close()
        state = Path(tmp, "state")
        args = [sys.executable, str(root / "app.py"), "--port", str(port), "--state", str(state)]
        # Avoid touching a developer's configured inference service during the smoke test.
        env = dict(os.environ, FIELDNOTE_MODEL_URL="http://127.0.0.1:1/v1")
        child = subprocess.Popen(args, env=env)
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))
        base = f"http://127.0.0.1:{port}"
        headers = {"Content-Type": "application/json"}

        def api(route, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + "/api/" + route, data=data, headers=headers)
            with client.open(req, timeout=30) as reply:
                return json.load(reply)

        try:
            key = state / f"server-{port}.key"
            for _ in range(240):
                if child.poll() is not None:
                    raise RuntimeError(f"Local Dataset Query Engine exited during startup with status {child.returncode}")
                if key.exists():
                    break
                time.sleep(.25)
            assert api("health")["version"] == "0.3.0-dev.2"
            try:
                api("status")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError("An unpaired client was given access")
            launch = json.loads(key.read_text(encoding="utf-8"))["launch"]
            headers["X-Fieldnote-Session"] = api("session", {"token":launch})["session"]
            first = api("status")
            assert first["index"]["rows"] == 96 and first["model"]["on_demand"]
            folder = Path(tmp, "counts")
            folder.mkdir()
            (folder / "items.csv").write_text("ID,Site\n1,West\n2,West\n3,East\n", encoding="utf-8")
            assert api("index", {"folder":str(folder)})["rows"] == 3
            response = api("ask", {"question":"How many items have Site = West?"})
            assert "rag" not in response and api("status")["model"]["on_demand"]
            result = response["engine"]
            assert (result["answer"], result["status"], result["input_tokens"]) == ("2","answered",0)
            subprocess.run([*args, "--stop"], check=True, timeout=15, env=env)
            assert child.wait(timeout=15) == 0 and not key.exists()
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
    print("Passed local HTTP pairing, synthetic index, zero-token count and shutdown.")


if __name__ == "__main__":
    main()
