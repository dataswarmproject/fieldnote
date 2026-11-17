"""Run a local index refresh with OS resource limits outside the web process."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import subprocess
import time


MEMORY_BYTES = 1536 * 1024 * 1024


def _limit_memory():
    if sys.platform == "darwin":
        # dyld reserves a large address range. The parent measures resident
        # bytes on macOS rather than imposing a virtual-address-space ceiling.
        return
    if os.name != "nt":
        import resource
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = MEMORY_BYTES if hard == resource.RLIM_INFINITY else min(MEMORY_BYTES, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        return

    # Job memory is charged to this child alone. Windows releases the job at exit.
    import ctypes as ct
    from ctypes import wintypes as wt

    class BasicLimits(ct.Structure):
        _fields_ = [("process_time", ct.c_int64), ("job_time", ct.c_int64),
                    ("flags", wt.DWORD), ("min_working_set", ct.c_size_t),
                    ("max_working_set", ct.c_size_t), ("active_processes", wt.DWORD),
                    ("affinity", ct.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD)]

    class Limits(ct.Structure):
        _fields_ = [("basic", BasicLimits), ("io", ct.c_uint64 * 6),
                    ("process_memory", ct.c_size_t), ("job_memory", ct.c_size_t),
                    ("peak_process_memory", ct.c_size_t), ("peak_job_memory", ct.c_size_t)]

    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ct.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.GetCurrentProcess.restype = wt.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = Limits()
    limits.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limits.process_memory = MEMORY_BYTES
    if (not job or not kernel.SetInformationJobObject(job, 9, ct.byref(limits), ct.sizeof(limits))
            or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())):
        raise ct.WinError(ct.get_last_error())


def worker_environment():
    keys = ("HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH",
            "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env["PATH"] = (os.path.join(env.get("SYSTEMROOT", r"C:\Windows"), "System32")
                   if os.name == "nt" else os.defpath)
    return env


def wait_for_worker(worker, timeout):
    if sys.platform != "darwin":
        return worker.wait(timeout)
    import psutil
    deadline = time.monotonic() + timeout
    try:
        process = psutil.Process(worker.pid)
    except psutil.NoSuchProcess:
        return worker.wait(1)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(worker.args, timeout)
        try:
            return worker.wait(min(remaining, 0.2))
        except subprocess.TimeoutExpired:
            try:
                if process.memory_info().rss > MEMORY_BYTES:
                    raise ValueError("Index refresh exceeded its resident-memory limit.")
            except psutil.NoSuchProcess:
                return worker.wait(1)


def main():
    _limit_memory()
    os.environ["OPENPYXL_DEFUSEDXML"] = "True"
    os.environ["OPENPYXL_LXML"] = "False"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import indexer
    job = json.loads(sys.stdin.buffer.readline(65536))
    # The parent keeps this pipe open while the refresh is authorized. If it
    # dies, exit promptly and let SQLite roll back any open transaction.
    def watch_parent():
        if not sys.stdin.buffer.read(1):
            os._exit(2)
    threading.Thread(target=watch_parent, daemon=True).start()
    indexer._refresh(Path(job["folder"]), Path(job["database"]))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, MemoryError, sqlite3.Error) as exc:
        # Keep even escaped Unicode errors smaller than a Windows pipe buffer.
        print(json.dumps({"error":str(exc)[:240] or "Index refresh exceeded its memory limit."}), flush=True)
        os._exit(1)
    # A daemon thread is waiting on stdin. Avoid interpreter shutdown waiting
    # for its buffered-reader lock; all SQLite connections were explicitly closed.
    os._exit(0)
