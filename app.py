"""Local Dataset Query Engine single-user loopback application."""

import argparse
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import benchmark
import engine
import indexer
from provider import LocalModel, NoRedirect, read_local_config

ROOT = Path(__file__).resolve().parent
DEMO = ROOT / "Demo dataset"
CASES = json.loads((ROOT / "demo-cases.json").read_text(encoding="utf-8"))
VERSION = "0.3.0-dev.2"


def create_app(state, provider, port, stop_token=None, launch_token=None):
    state = Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = state / "index.sqlite"
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    changes = threading.Lock()
    activity = threading.Lock()
    names = {f"localhost:{port}", f"127.0.0.1:{port}"}
    launch_token = launch_token or secrets.token_urlsafe(32)
    browser_session = secrets.token_urlsafe(32)
    origin_key = secrets.token_urlsafe(32)
    cookie = f"fieldnote_{port}"

    @app.before_request
    def check_request():
        if request.host.lower() not in names:
            abort(403)
        if request.path.startswith("/api/"):
            origin = request.headers.get("Origin")
            if origin and origin != "http://" + request.host:
                abort(403)
            if request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                abort(403)
            if request.path not in {"/api/health", "/api/session", "/api/stop"}:
                supplied_cookie = request.cookies.get(cookie, "")
                supplied_key = request.headers.get("X-Fieldnote-Session", "")
                if not (supplied_cookie.isascii() and supplied_key.isascii()
                        and secrets.compare_digest(supplied_cookie, browser_session)
                        and secrets.compare_digest(supplied_key, origin_key)):
                    abort(401, "Open Local Dataset Query Engine with the launcher or run python app.py --open to pair this browser.")
        if request.method == "POST":
            if not request.is_json:
                abort(415)

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.errorhandler(Exception)
    def failed(err):
        code = (err.code if isinstance(err, HTTPException) else
                400 if isinstance(err, (ValueError, FileNotFoundError, PermissionError)) else 500)
        if code >= 500:
            app.logger.exception("Request failed")
            return jsonify(error="The request failed. See the local Local Dataset Query Engine console for details."), code
        return jsonify(error=str(err)), code

    def body():
        obj = request.get_json()
        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object.")
        return obj

    @app.get("/")
    def home():
        return send_from_directory(ROOT / "static", "index.html")

    @app.get("/static/<path:name>")
    def assets(name):
        return send_from_directory(ROOT / "static", name)

    @app.get("/api/health")
    def health():
        challenge = request.args.get("challenge", "")
        proof = hmac.digest(launch_token.encode("ascii"), challenge.encode("utf-8"), "sha256").hex()
        return jsonify(application="Local Dataset Query Engine", version=VERSION, proof=proof)

    @app.post("/api/session")
    def pair():
        token = body().get("token")
        if not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, launch_token):
            abort(403)
        response = jsonify(paired=True, session=origin_key)
        response.set_cookie(cookie, browser_session, httponly=True, samesite="Strict", path="/api/")
        return response

    @app.post("/api/stop")
    def stop():
        supplied = body().get("token")
        if not stop_token or not isinstance(supplied, str) or not supplied.isascii() or not secrets.compare_digest(stop_token, supplied):
            abort(403)
        if not changes.acquire(blocking=False):
            return jsonify(error="An index update or benchmark is still running."), 409
        if not activity.acquire(blocking=False):
            changes.release()
            return jsonify(error="A question or folder picker is still running."), 409
        response = jsonify(stopping=True)
        response.call_on_close(app.config["STOP_SERVER"])
        return response

    @app.get("/api/status")
    def status():
        snapshot = indexer.status(db)
        examples = []
        if snapshot["snapshot"] and not snapshot["knowledge"]["needs_reindex"]:
            examples = ([c["question"] for c in CASES] if snapshot["folder"] == str(DEMO)
                        else engine.examples(db, snapshot=snapshot["snapshot"]))
        return jsonify(index=snapshot, model=provider.info(), demo_folder=str(DEMO), examples=examples)

    @app.get("/api/cases")
    def cases():
        return jsonify(cases=CASES)

    @app.get("/api/results")
    def results():
        report_path = state / "benchmark.json"
        if report_path.exists():
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            snapshot = indexer.status(db)
            if not snapshot["knowledge"]["needs_reindex"] and saved.get("snapshot") == snapshot["snapshot"]:
                return jsonify(report=saved)
        return jsonify(report=None)

    @app.post("/api/browse")
    def browse():
        picker = ("import json,tkinter as tk;from tkinter import filedialog;"
                  "r=tk.Tk();r.withdraw();r.attributes('-topmost',True);"
                  "print(json.dumps(filedialog.askdirectory(title='Choose a dataset folder',parent=r)));r.destroy()")
        if not activity.acquire(blocking=False):
            return jsonify(error="A question or folder picker is already running."), 409
        try:
            proc = subprocess.run([sys.executable, "-c", picker], capture_output=True, text=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            activity.release()
        if proc.returncode:
            raise ValueError("Folder picker unavailable. Paste a local folder path instead.")
        return jsonify(folder=json.loads(proc.stdout.strip()))

    @app.post("/api/index")
    def index():
        folder = body().get("folder")
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("Choose a local folder.")
        if not changes.acquire(blocking=False):
            return jsonify(error="A benchmark or index update is running. Try again when it finishes."), 409
        try:
            return jsonify(indexer.index_folder(folder, db))
        finally:
            changes.release()

    @app.post("/api/ask")
    def ask():
        obj = body()
        question = obj.get("question")
        mode = obj.get("mode", "engine")
        if mode not in ("both", "engine", "compiler", "compiler_rag"):
            raise ValueError("Unknown query mode.")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 4000:
            raise ValueError("Enter a question of up to 4,000 characters.")
        if not activity.acquire(blocking=False):
            return jsonify(error="A question or folder picker is already running."), 409
        try:
            if mode != "engine":
                provider.start()
            result = engine.compare(str(db), question.strip(), provider if mode != "engine" else None, mode=mode)
            result["model"] = provider.name if mode != "engine" else None
            return jsonify(result)
        finally:
            activity.release()

    @app.post("/api/benchmark")
    def evaluate():
        obj = body()
        if not changes.acquire(blocking=False):
            return jsonify(error="A benchmark or index update is running."), 409
        try:
            info = indexer.status(db)
            if not info["snapshot"]:
                raise ValueError("Index a folder first.")
            if info["knowledge"]["needs_reindex"]:
                return jsonify(error="Click Index folder to compile the selected folder for this version."), 409
            if "cases" not in obj and info["folder"] != str(DEMO):
                raise ValueError("Supply expected answers for this folder, or index the bundled demo.")
            selected = benchmark.validate(obj.get("cases", CASES))
            mode = obj.get("mode", "both")
            if mode not in ("both", "compiler_rag"):
                raise ValueError("Choose a paired benchmark mode.")
            provider.start()
            if not provider.info()["available"]:
                return jsonify(error="A local model must be ready for a paired RAG benchmark."), 503
            report = benchmark.run(db, selected, provider, info["snapshot"], mode=mode)
            report["suite"] = "custom" if "cases" in obj else "bundled demo"
            temporary = state / "benchmark.tmp"
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(state / "benchmark.json")
            return jsonify(report)
        finally:
            changes.release()

    return app


def state_directory():
    if os.environ.get("FIELDNOTE_STATE"):
        return Path(os.environ["FIELDNOTE_STATE"]).expanduser()
    configured = read_local_config().get("state")
    if configured:
        return (ROOT / Path(configured).expanduser()).resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        legacy, current = base / "Fieldnote/state", base / "Local Dataset Query Engine/state"
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
        legacy, current = base / "Fieldnote", base / "Local Dataset Query Engine"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        legacy, current = base / "fieldnote", base / "local-dataset-query-engine"
    return legacy if legacy.is_dir() and not current.exists() else current


def verify_running(http, url, keyfile, challenge):
    try:
        with http.open(url + "/api/health?challenge=" + challenge, timeout=5) as response:
            health = json.loads(response.read(8192))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return None
        raise RuntimeError("Could not verify the service on the selected port.") from exc
    if not isinstance(health, dict) or health.get("application") not in ("Local Dataset Query Engine", "Fieldnote") or not keyfile.is_file():
        raise RuntimeError("Port is occupied by another service or Local Dataset Query Engine state directory.")
    try:
        credentials = json.loads(keyfile.read_text(encoding="utf-8"))
        expected = hmac.digest(credentials["launch"].encode("ascii"), challenge.encode("ascii"), "sha256").hex()
        matches = hmac.compare_digest(expected, health.get("proof", ""))
    except (ValueError, KeyError, TypeError, AttributeError):
        matches = False
    if not matches:
        raise RuntimeError("Existing server belongs to a different session or version. Stop its original launcher or choose another port and state folder.")
    return credentials, health.get("version", "unknown")


def serve_local(app, port, state, credentials, provider, open_browser):
    import sqlite3
    from waitress import create_server, wasyncore

    url = f"http://127.0.0.1:{port}"
    keyfile = state / f"server-{port}.key"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        st = state.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("Choose a private state folder owned by your account with mode 0700.")

    # Keep two local servers from replacing one another's reports or model logs.
    lease = sqlite3.connect(state / ".runtime.lock", timeout=0)
    sockets = {}
    server = None
    registered = False
    try:
        try:
            lease.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Another Local Dataset Query Engine process is using this state directory.") from exc
        server = create_server(app, host="127.0.0.1", port=port, map=sockets,
                               threads=6, connection_limit=64, max_request_body_size=2 * 1024 * 1024,
                               max_request_header_size=32768, expose_tracebacks=False)
        stopping = threading.Event()
        app.config["STOP_SERVER"] = stopping.set
        if not indexer.status(state / "index.sqlite")["snapshot"]:
            indexer.index_folder(DEMO, state / "index.sqlite")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state, delete=False) as handle:
            json.dump(credentials, handle)
        os.replace(handle.name, keyfile)
        registered = True
        print(f"Local Dataset Query Engine is running at {url}. Use --open to pair a browser.", flush=True)
        if open_browser:
            webbrowser.open(url + "/#pair=" + credentials["launch"])
        while not stopping.is_set():
            wasyncore.loop(timeout=0.2, count=1, map=sockets)
        # Flush the stop response before closing idle keepalive connections.
        deadline = time.monotonic() + 2
        while any(c.requests or c.total_outbufs_len for c in server.active_channels.values()) and time.monotonic() < deadline:
            wasyncore.loop(timeout=0.1, count=1, map=sockets)
    finally:
        if server is not None:
            server.task_dispatcher.shutdown()
        wasyncore.close_all(sockets, ignore_all=True)
        provider.close()
        if registered:
            keyfile.unlink(missing_ok=True)
        lease.close()


def main():
    parser = argparse.ArgumentParser(description="Local Dataset Query Engine local evidence workbench")
    parser.add_argument("--port", type=int, default=os.environ.get("FIELDNOTE_PORT", "18741"))
    parser.add_argument("--state", type=Path, help="Private directory for this instance's index and session")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--open", action="store_true", help="Open and pair a browser")
    action.add_argument("--stop", action="store_true", help="Stop the local server and its managed model")
    action.add_argument("--status", action="store_true", help="Check this instance")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be in the range 1–65535.")
    if os.name == "posix":
        os.umask(0o077)
    url = f"http://127.0.0.1:{args.port}"
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        state = (args.state or state_directory()).expanduser().resolve()
        keyfile = state / f"server-{args.port}.key"
        running = verify_running(http, url, keyfile, secrets.token_hex(16))
        credentials, server_version = running if running else (None, None)
        if args.status:
            print(f"Local Dataset Query Engine is running. Version {server_version}." if credentials else "Local Dataset Query Engine is stopped.")
            return
        if args.stop:
            if credentials:
                payload = json.dumps({"token":credentials["stop"]}).encode()
                req = urllib.request.Request(url + "/api/stop", data=payload, headers={"Content-Type":"application/json"})
                with http.open(req, timeout=5):
                    print("Stopping Local Dataset Query Engine.")
            else:
                print("Local Dataset Query Engine is stopped.")
            return
        if credentials:
            if server_version != VERSION:
                raise RuntimeError(f"Local Dataset Query Engine {server_version} is running. Use app.py --stop with the same port and state, then app.py --open to launch {VERSION}.")
            if args.open:
                webbrowser.open(url + "/#pair=" + credentials["launch"])
            else:
                print("Local Dataset Query Engine is running. Use --open to pair a browser.")
            return
        credentials = {key: secrets.token_urlsafe(32) for key in ("launch", "stop")}
        provider = LocalModel(state)
        app = create_app(state, provider, args.port, credentials["stop"], credentials["launch"])
        serve_local(app, args.port, state, credentials, provider, args.open)
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, f"Local Dataset Query Engine: {exc}\n")


if __name__ == "__main__":
    main()
