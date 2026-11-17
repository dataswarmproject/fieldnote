"""Use a loopback chat service, optionally starting a locally installed llama.cpp."""

import atexit
import ipaddress
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def read_local_config():
    path = Path(__file__).with_name("local-runtime.json")
    if not path.is_file():
        return {}
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("local-runtime.json must contain an object.")
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Local model redirects are disabled")


class LocalModel:
    def __init__(self, state_dir):
        self.available = False
        self.detail = "The local model starts when you request a RAG comparison."
        self.started = False
        self.name = "Local model"
        self.base = ""
        self.key = ""
        self.child = None
        self.state_dir = Path(state_dir)
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self._lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self.input_budget = int(os.environ.get("FIELDNOTE_INPUT_BUDGET", "4096"))
        if self.input_budget < 1:
            raise ValueError("FIELDNOTE_INPUT_BUDGET must be a positive integer.")
        self._native_template = None
        atexit.register(self.close)

    def _request(self, route, data=None, timeout=2, native=False):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        base = self.base.removesuffix("/v1") if native else self.base
        request = urllib.request.Request(base + route, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        with self.http.open(request, timeout=timeout) as reply:
            return json.load(reply)

    def _set_endpoint(self, base):
        base = base.rstrip("/")
        url = urllib.parse.urlsplit(base)
        try:
            local = url.hostname == "localhost" or ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            local = False
        if (url.scheme != "http" or not local or url.username or url.password or url.query
                or url.fragment or url.path != "/v1"):
            raise ValueError("FIELDNOTE_MODEL_URL must be an HTTP loopback URL ending in /v1")
        self.base = base

    def _probe(self):
        try:
            ids = [m["id"] for m in self._request("/models")["data"] if "embed" not in m["id"].lower()]
            requested = os.environ.get("FIELDNOTE_MODEL_ID")
            if ids and (not requested or requested in ids):
                self.name = requested or ids[0]
                self.detail = "Local inference ready"
                self.available = True
                return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            pass
        self.available = False
        return False

    def start(self):
        with self._lock:
            if self.child is not None and self.child.poll() is not None:
                self.child = None
                self.available = False
            if self.started and self.available:
                return
            if self.child and self.child.poll() is None and self._probe():
                return
            self.close()
            self.started = True
            try:
                endpoint = os.environ.get("FIELDNOTE_MODEL_URL")
                if endpoint:
                    self._set_endpoint(endpoint)
                    self.key = os.environ.get("FIELDNOTE_MODEL_KEY", "")
                    if not self._probe():
                        self.detail = "Configured model service is unavailable or the model ID does not match."
                    return
                config = read_local_config()
                binary = Path(os.environ.get("FIELDNOTE_LLAMA_SERVER") or config.get("server", ""))
                weights = Path(os.environ.get("FIELDNOTE_GGUF") or config.get("weights", ""))
                if not (binary.is_file() and weights.is_file()):
                    if os.environ.get("FIELDNOTE_DISCOVER_MODELS") == "1":
                        for base in ("http://127.0.0.1:1234/v1", "http://127.0.0.1:11434/v1"):
                            self._set_endpoint(base)
                            if self._probe():
                                return
                    self.detail = "Start a local chat server and set FIELDNOTE_MODEL_URL, or configure local-runtime.json."
                    return
                import socket
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                self._set_endpoint(f"http://127.0.0.1:{port}/v1")
                self.key = secrets.token_urlsafe(30)
                args = [str(binary), "-m", str(weights), "--host", "127.0.0.1", "--port", str(port),
                        "-c", "8192", "-np", "1", "--reasoning", "off", "--jinja",
                        "--sleep-idle-seconds", "300", "--alias", weights.stem]
                with (self.state_dir / "model.log").open("w", encoding="utf-8") as log:
                    self.child = subprocess.Popen(args, cwd=binary.parent, stdout=log, stderr=log,
                                                  env={**os.environ, "LLAMA_API_KEY": self.key},
                                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for _ in range(90):
                    if self._probe():
                        return
                    if self.child.poll() is not None:
                        break
                    time.sleep(1)
                self.detail = "Local model startup did not complete. See model.log in the state folder."
                self.close()
            except Exception as exc:
                self.detail = str(exc)

    def info(self):
        return {"available": self.available, "on_demand": not self.started,
                "name": self.name, "detail": self.detail}

    def count_prompt(self, system, user):
        if self._native_template is False:
            return None
        messages = [{"role":"system","content":system},{"role":"user","content":user}]
        try:
            rendered = self._request("/apply-template", {"model":self.name, "messages":messages},
                                     timeout=30, native=True)["prompt"]
            tokens = self._request("/tokenize", {"content":rendered, "add_special":True,
                                                 "parse_special":True}, timeout=30, native=True)["tokens"]
            if not isinstance(tokens, list):
                raise ValueError("Unexpected tokenizer reply")
            self._native_template = True
            return len(tokens)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405, 501):
                self._native_template = False
                return None
            self.available = False
            self.detail = f"Local tokenizer returned HTTP {exc.code}."
            raise
        except OSError:
            self.available = False
            self.detail = "Local model connection failed during token counting."
            raise
        except (KeyError, ValueError):
            self._native_template = False
            return None

    def complete(self, system, user):
        if not self.available:
            raise RuntimeError(self.detail)
        data = {"model": self.name, "temperature": 0, "max_tokens": 500, "stream": False,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        with self._inference_lock:
            try:
                result = self._request("/chat/completions", data, timeout=180)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"Local model returned HTTP {exc.code}. Check the model service.") from exc
            except OSError:
                self.available = False
                self.detail = "Local model connection failed. Check the configured service."
                raise
        usage = result.get("usage") or {}
        inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        text = result["choices"][0]["message"].get("content")
        if not isinstance(text, str):
            text = ""
        return text, {"input_tokens": inp, "output_tokens": out,
                      "usage_kind": "runtime" if isinstance(inp,int) and isinstance(out,int) else "unreported"}

    def close(self):
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
