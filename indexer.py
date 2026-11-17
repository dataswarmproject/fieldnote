"""Local, transactionally published SQLite index for documents and worksheets."""
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from zipfile import ZipFile
from defusedxml import ElementTree as ET
from index_worker import wait_for_worker, worker_environment


FORMATS = {".txt", ".md", ".pdf", ".docx", ".csv", ".xlsx"}
SPREADSHEETS = {".csv", ".xlsx"}
PARSER_VERSION = "fieldnote-index-v2"
CHUNK_SIZE = 1200
OVERLAP = 120
MIB = 1024 * 1024
MAX_FILE_BYTES = 64 * MIB
MAX_SOURCE_BYTES = 1024 * MIB
MAX_ENTRIES = 25_000
MAX_ARCHIVE_BYTES = 128 * MIB
MAX_ARCHIVE_MEMBER_BYTES = 48 * MIB
MAX_ARCHIVE_MEMBERS = 4096
MAX_EXTRACTED_CHARS = 48 * MIB
MAX_TABLE_COLUMNS = 1024
MAX_TABLE_ROWS = 250_000
MAX_UNITS = 250_000
MAX_HEADER_CHARS = 4096
MAX_CELL_CHARS = 1024 * 1024
MAX_PDF_PAGES = 10_000
REFRESH_TIMEOUT = 600
STOPWORDS = set("a an and are as at be by can did do does for from how i in is it list me of on or please show tell that the their this to was we were what which who with would".split())


def connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _initialize(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count={(2 * 1024 * MIB) // page_size}")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS index_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(
            source TEXT PRIMARY KEY, digest TEXT, kind TEXT, state TEXT, units INTEGER
        );
        CREATE TABLE IF NOT EXISTS issues(source TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS sheets(
            id INTEGER PRIMARY KEY, source TEXT, sheet TEXT, headers TEXT, row_count INTEGER
        );
        CREATE INDEX IF NOT EXISTS sheets_source ON sheets(source);
        CREATE TABLE IF NOT EXISTS source_ranges(
            source TEXT PRIMARY KEY, first_unit INTEGER, last_unit INTEGER,
            first_chunk INTEGER, last_chunk INTEGER
        );
        CREATE TABLE IF NOT EXISTS records(
            sheet_id INTEGER, row_num INTEGER, anchor TEXT, cells TEXT,
            PRIMARY KEY(sheet_id, row_num)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS units USING fts5(source,anchor,body,kind UNINDEXED);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(source,anchor,body);
    """)


def _walk(folder, database, report):
    excluded = {os.path.normcase(str(database) + suffix) for suffix in ("", "-wal", "-shm", "-journal")}
    count = 0
    for directory, dirs, files in os.walk(folder, followlinks=False,
            onerror=lambda e: report(os.path.relpath(e.filename, folder), str(e))):
        count += len(dirs) + len(files)
        if count > MAX_ENTRIES:
            raise ValueError(f"Folder exceeds the {MAX_ENTRIES:,}-entry limit. Select a smaller folder.")
        dirs.sort()
        for name in dirs[:]:
            entry = Path(directory, name)
            if entry == database.parent or _is_link(entry) or _is_hidden(entry):
                dirs.remove(name)
                report(entry.relative_to(folder).as_posix(), "Hidden, linked, or application-state directory excluded.")
        for name in sorted(files):
            entry = Path(directory, name)
            if os.path.normcase(str(entry)) in excluded:
                continue
            yield entry


def _is_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                              getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


@contextmanager
def _open_source(path, root):
    """Open a regular file and check the opened handle against the selected root."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or _is_link(path):
        raise ValueError("Linked or non-regular file excluded.")
    fd = None
    if os.name == "posix":
        # Anchor every lookup in an opened directory. Refuse symlinks even if a
        # directory entry changes between the inventory pass and the source read.
        parent = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            path.relative_to(root)
            components = path.parts[1:]
            for name in components[:-1]:
                sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = sub
            fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
    with (os.fdopen(fd, "rb") if fd is not None else path.open("rb")) as original:
        opened = os.fstat(original.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise ValueError("File changed while opening. Refresh to retry.")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes
            final_name = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            final_name.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
            output = ctypes.create_unicode_buffer(32768)
            size = final_name(msvcrt.get_osfhandle(original.fileno()), output, len(output), 0)
            if not 0 < size < len(output):
                raise ValueError("Could not verify the opened file's location.")
            physical = Path(output.value.removeprefix("\\\\?\\"))
            if physical != path or not physical.is_relative_to(root):
                raise ValueError("File resolved outside its selected path.")
        yield original


def _is_hidden(path):
    if path.name.startswith((".", "~$")) or path.name == "__pycache__":
        return True
    if os.name == "nt":
        try:
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)
        except OSError:
            pass
    return False


def _windows(text):
    """Character-bounded passages with overlap, retaining each source character."""
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            cut = max(text.rfind(" ", start + CHUNK_SIZE // 2, end),
                      text.rfind("\n", start + CHUNK_SIZE // 2, end))
            if cut > start:
                end = cut
        section = text[start:end].strip()
        if section:
            yield section
        if end == len(text):
            break
        start = end - OVERLAP


class Writer:
    def __init__(self, conn, source):
        self.conn = conn
        self.source = source
        self.n = 0
        self.chars = 0
        self._pending = ""
        self._start = self._end = ""
        self.warnings = set()
        self.first_unit = self.last_unit = self.first_chunk = self.last_chunk = None

    def warn(self, message):
        self.warnings.add(message)

    def emit_chunk(self):
        if self._pending:
            where = self._start if self._start == self._end else f"{self._start} – {self._end}"
            self.last_chunk = self.conn.execute("INSERT INTO chunks VALUES (?,?,?)", (self.source, where, self._pending)).lastrowid
            if self.first_chunk is None:
                self.first_chunk = self.last_chunk
        self._pending = ""

    def unit(self, anchor, text, kind="document"):
        if not text.strip():
            return
        self.n += 1
        self.chars += len(text)
        if self.n > MAX_UNITS or self.chars > MAX_EXTRACTED_CHARS:
            raise ValueError("Source exceeds the per-file extracted text or record limit.")
        for part in _windows(text):
            self.last_unit = self.conn.execute("INSERT INTO units VALUES (?,?,?,?)", (self.source, anchor, part, kind)).lastrowid
            if self.first_unit is None:
                self.first_unit = self.last_unit
            if self._pending and len(self._pending) + len(part) + 1 > CHUNK_SIZE:
                self.emit_chunk()
            if not self._pending:
                self._start = anchor
            self._end = anchor
            self._pending = (self._pending + "\n" + part).lstrip("\n")


def _decode(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _text_units(handle, writer, markdown):
    text = _decode(handle.read())
    heading = ""
    pending = []
    start = end = 0

    def emit():
        if pending:
            anchor = f"line {start}" if start == end else f"lines {start}–{end}"
            writer.unit(anchor, ((heading + "\n") if heading else "") + "\n".join(pending))
            pending.clear()

    for n, line in enumerate(text.splitlines(), 1):
        if markdown and re.match(r"^#{1,6}\s", line):
            emit()
            heading = line.strip()
            writer.unit(f"line {n}", heading)
        elif line.strip():
            if not pending:
                start = n
            end = n
            pending.append(line)
        else:
            emit()
    emit()


def _pdf(handle, writer):
    from pypdf import PdfReader
    reader = PdfReader(handle)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Encrypted PDF requires a password.")
    missing = []
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError("PDF exceeds the page limit.")
    for page, obj in enumerate(reader.pages, 1):
        try:
            text = obj.extract_text() or ""
        except MemoryError:
            raise
        except Exception:
            text = ""
        if text.strip():
            writer.unit(f"page {page}", text)
        else:
            missing.append(page)
    if missing:
        writer.warn(f"No extractable text on {len(missing)} page(s), including page {missing[0]}. OCR is unavailable.")


def _docx(handle, writer):
    # Read OOXML body and notes, including table-cell text in reading order.
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    def content(element):
        return "".join(x.text or "" for x in element.findall(".//w:t", ns))

    with ZipFile(handle) as archive:
        body = ET.fromstring(archive.read("word/document.xml"), forbid_dtd=True).find("w:body", ns)
        if body is None:
            raise ValueError("DOCX has no document body.")
        paragraph = table = 0
        for element in body:
            if element.tag.endswith("}p"):
                paragraph += 1
                writer.unit(f"paragraph {paragraph}", content(element))
            elif element.tag.endswith("}tbl"):
                table += 1
                for row, tr in enumerate(element.findall("w:tr", ns), 1):
                    writer.unit(f"table {table} row {row}", " | ".join(content(c) for c in tr.findall("w:tc", ns)))
            elif content(element).strip():
                writer.unit("document section", content(element))
        for name in archive.namelist():
            if re.fullmatch(r"word/(footnotes|endnotes|header\d+|footer\d+)\.xml", name):
                for n, p in enumerate(ET.fromstring(archive.read(name), forbid_dtd=True).findall(".//w:p", ns), 1):
                    writer.unit(f"{Path(name).stem} paragraph {n}", content(p))
        if any(n.startswith("word/media/") for n in archive.namelist()):
            writer.warn("Embedded media excluded from text extraction.")


def _value(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _sheet(writer, name, rows):
    """Use first nonempty row as header. Keep original row numbers."""
    table_id = None
    count = 0
    headers = []
    writer.emit_chunk()
    for row in rows:
        n, cells, *flags = row
        if n > MAX_TABLE_ROWS or len(cells) > MAX_TABLE_COLUMNS:
            raise ValueError("Worksheet exceeds the row or column limit.")
        limit = MAX_CELL_CHARS if table_id is not None else MAX_HEADER_CHARS
        if any(isinstance(v, str) and len(v) > limit for v in cells):
            raise ValueError("Worksheet cell or header exceeds the text limit.")
        values = [_value(v) for v in cells]
        if not any(flags) and not any(v is not None and str(v).strip() for v in values):
            continue
        if table_id is None:
            seen = set()
            for idx, v in enumerate(values, 1):
                base = str(v).strip() if v is not None and str(v).strip() else f"Column {idx}"
                title = base
                ordinal = 2
                while title.casefold() in seen:
                    title = f"{base} ({ordinal})"
                    ordinal += 1
                seen.add(title.casefold())
                headers.append(title)
            table_id = writer.conn.execute("INSERT INTO sheets(source,sheet,headers,row_count) VALUES(?,?,?,0)",
                (writer.source, name, json.dumps(headers, ensure_ascii=False))).lastrowid
            continue
        if len(values) > len(headers):
            writer.warn(f"{name} has rows wider than its header. Extra columns receive generated names.")
            while len(headers) < len(values):
                title = f"Column {len(headers)+1}"
                while title.casefold() in seen:
                    title += " (extra)"
                headers.append(title)
                seen.add(title.casefold())
        values.extend([None] * (len(headers) - len(values)))
        anchor = f"{name} row {n}"
        writer.conn.execute("INSERT INTO records VALUES(?,?,?,?)", (table_id, n, anchor, json.dumps(values, ensure_ascii=False)))
        body = " | ".join(f"{k}: {v}" for k, v in zip(headers, values) if v is not None)
        writer.unit(anchor, body or "Row has no available values.", "row")
        count += 1
    if table_id is not None:
        writer.conn.execute("UPDATE sheets SET row_count=?, headers=? WHERE id=?",
                            (count, json.dumps(headers, ensure_ascii=False), table_id))
    writer.emit_chunk()
    return table_id is not None


def _csv(handle, writer):
    csv.field_size_limit(MAX_CELL_CHARS)
    data = handle.read(4)
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    handle.seek(0)
    wrapper = io.TextIOWrapper(handle, encoding=encoding, newline="")
    try:
        sample = wrapper.read(8192)
        wrapper.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        _sheet(writer, Path(writer.source).stem, enumerate(csv.reader(wrapper, dialect), 1))
    finally:
        wrapper.detach()


def _xlsx(handle, writer):
    from openpyxl import load_workbook
    formulas = load_workbook(handle, read_only=True, data_only=False, keep_links=False)
    try:
        cached = load_workbook(handle, read_only=True, data_only=True, keep_links=False)
        try:
            for sheet in formulas.worksheets:
                # Some writers declare a single-cell used range for a populated worksheet.
                sheet.reset_dimensions()
                cached_sheet = cached[sheet.title]
                cached_sheet.reset_dimensions()
                def extract():
                    for n, (source, data) in enumerate(zip(sheet.iter_rows(), cached_sheet.iter_rows()), 1):
                        unresolved = False
                        for cell, cached_cell in zip(source, data):
                            if cell.data_type == "f" and cached_cell.value is None:
                                unresolved = True
                                writer.warn(f"{sheet.title} contains formulas without cached values. Affected cells are indexed as unknown.")
                            if cached_cell.data_type == "e":
                                unresolved = True
                                writer.warn(f"{sheet.title} contains spreadsheet errors. Affected cells are indexed as unknown.")
                        yield n, [None if c.data_type == "e" else c.value for c in data], unresolved
                _sheet(writer, sheet.title, extract())
        finally:
            cached.close()
    finally:
        formulas.close()


def _forget(conn, source):
    conn.execute("DELETE FROM records WHERE sheet_id IN (SELECT id FROM sheets WHERE source=?)", (source,))
    span = conn.execute("SELECT * FROM source_ranges WHERE source=?", (source,)).fetchone()
    if span:
        for table, first, last in (("chunks", "first_chunk", "last_chunk"), ("units", "first_unit", "last_unit")):
            if span[first] is not None:
                conn.execute(f"DELETE FROM {table} WHERE rowid BETWEEN ? AND ?", (span[first],span[last]))
    for table in ("sources", "issues", "sheets", "source_ranges"):
        conn.execute(f"DELETE FROM {table} WHERE source=?", (source,))


def _check_archive(handle):
    with ZipFile(handle) as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("Office file exceeds the archive-entry limit.")
        if sum(member.file_size for member in members) > MAX_ARCHIVE_BYTES:
            raise ValueError("Office file exceeds the total expanded-size limit.")
        if any(member.file_size > MAX_ARCHIVE_MEMBER_BYTES for member in members):
            raise ValueError("An Office file part exceeds the expanded-size limit.")
        if any(member.flag_bits & 1 for member in members):
            raise ValueError("Encrypted Office files require an unencrypted export.")
    handle.seek(0)


class FolderLimit(ValueError):
    """A whole-refresh limit; roll back the entire transaction."""


def _read_file(conn, path, root, source, prior, remaining):
    ext = path.suffix.lower()
    kind = "table" if ext in SPREADSHEETS else "document"
    digest = None
    size = 0
    conn.execute("SAVEPOINT input_file")
    try:
        if _is_hidden(path):
            raise ValueError("Hidden or temporary file excluded.")
        if ext not in FORMATS:
            raise ValueError(f"Unsupported format {ext or '(no extension)'}.")
        with _open_source(path, root) as original, tempfile.SpooledTemporaryFile(max_size=MIB) as captured:
            before = os.fstat(original.fileno())
            if before.st_size > MAX_FILE_BYTES:
                raise ValueError(f"Source exceeds the {MAX_FILE_BYTES // MIB} MiB per-file limit.")
            if before.st_size > remaining:
                raise FolderLimit("Dataset exceeds the total source-byte limit. Select a smaller folder.")
            hasher = hashlib.sha256()
            while block := original.read(256 * 1024):
                size += len(block)
                if size > MAX_FILE_BYTES:
                    raise ValueError("Source grew beyond the file-size limit.")
                if size > remaining:
                    raise FolderLimit("Dataset exceeds the total source-byte limit. Select a smaller folder.")
                hasher.update(block)
                captured.write(block)
            after = os.fstat(original.fileno())
            if before.st_mtime_ns != after.st_mtime_ns or before.st_size != size or size != after.st_size:
                raise ValueError("File changed while reading. Refresh to retry.")
            digest = hasher.hexdigest()
            if prior and prior["state"] in ("indexed", "partial") and prior["digest"] == digest:
                conn.execute("RELEASE input_file")
                return size
            if prior:
                _forget(conn, source)
            captured.seek(0)
            writer = Writer(conn, source)
            if ext in (".xlsx", ".docx"):
                _check_archive(captured)
            if ext in (".md", ".txt"):
                _text_units(captured, writer, ext == ".md")
            else:
                {".pdf": _pdf, ".docx": _docx, ".csv": _csv, ".xlsx": _xlsx}[ext](captured, writer)
            writer.emit_chunk()
            conn.execute("INSERT INTO source_ranges VALUES(?,?,?,?,?)",
                (source, writer.first_unit, writer.last_unit, writer.first_chunk, writer.last_chunk))
            has_schema = conn.execute("SELECT 1 FROM sheets WHERE source=? LIMIT 1", (source,)).fetchone()
            if writer.n == 0 and not has_schema:
                writer.warn("Source has no extractable text or tabular header.")
            for reason in sorted(writer.warnings):
                conn.execute("INSERT INTO issues VALUES(?,?)", (source, reason))
            state = "partial" if writer.warnings else "indexed"
            conn.execute("INSERT INTO sources VALUES(?,?,?,?,?)", (source, digest, kind, state, writer.n))
    except (sqlite3.Error, MemoryError, FolderLimit):
        raise
    except Exception as error:
        conn.execute("ROLLBACK TO input_file")
        if prior:
            _forget(conn, source)
        conn.execute("INSERT INTO sources VALUES(?,?,?,?,0)", (source, digest, kind, "skipped"))
        conn.execute("INSERT INTO issues VALUES(?,?)", (source, str(error) or type(error).__name__))
    conn.execute("RELEASE input_file")
    return size


def _meta(conn):
    return dict(conn.execute("SELECT key,value FROM index_meta"))


def index_folder(folder, db_path):
    if not str(folder).strip():
        raise ValueError("Choose an existing local folder.")
    selected = Path(folder).expanduser()
    if os.name == "nt" and selected.drive.startswith("\\\\"):
        raise ValueError("Choose a local drive folder.")
    root = selected.resolve(strict=True)
    if os.name == "nt":
        import ctypes
        if root.drive.startswith("\\\\") or ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) == 4:
            raise ValueError("Choose a local drive folder.")
    if not root.is_dir():
        raise ValueError("Choose an existing local folder.")
    database = Path(db_path).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    # A worker interruption releases the transaction. Keep parser memory and
    # wall time bounded independently from the HTTP server and its readers.
    command = [sys.executable, "-E", "-P", "-B", str(Path(__file__).with_name("index_worker.py"))]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, env=worker_environment(),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as worker:
        try:
            worker.stdin.write(json.dumps({"folder":str(root), "database":str(database)}) + "\n")
            worker.stdin.flush()
            code = wait_for_worker(worker, REFRESH_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Index refresh timed out. Choose a smaller folder and retry.") from exc
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
        if code:
            try:
                error = json.loads(worker.stdout.read(8192))["error"]
            except (KeyError, ValueError, TypeError):
                error = "Index worker could not finish. Check the source sizes, available memory, and disk space."
            raise ValueError(error)
    return status(database)


def _refresh(root, database):
    with closing(connect(database)) as conn:
        _initialize(conn)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            meta = _meta(conn)
            previous = {r["source"]: r for r in conn.execute("SELECT * FROM sources")}
            if meta.get("folder") != str(root) or meta.get("parser") != PARSER_VERSION:
                for table in ("records", "sources", "sheets", "issues", "units", "chunks", "source_ranges"):
                    conn.execute(f"DELETE FROM {table}")
                previous = {}
            else:
                conn.execute("DELETE FROM issues WHERE source NOT IN (SELECT source FROM sources)")
            visited = set()
            def report(source, reason):
                conn.execute("INSERT INTO issues VALUES(?,?)", (source, reason))
            remaining = MAX_SOURCE_BYTES
            for path in _walk(root, database, report):
                source = path.relative_to(root).as_posix()
                visited.add(source)
                remaining -= _read_file(conn, path, root, source, previous.get(source), remaining)
            for source in previous.keys() - visited:
                _forget(conn, source)
            stamp = hashlib.sha256((PARSER_VERSION + str(root)).encode())
            for item in conn.execute("SELECT source,digest,state FROM sources ORDER BY source"):
                stamp.update(json.dumps(tuple(item)).encode())
            for item in conn.execute("SELECT source,reason FROM issues ORDER BY source,reason"):
                stamp.update(json.dumps(tuple(item)).encode())
            conn.executemany("INSERT OR REPLACE INTO index_meta VALUES(?,?)",
                [("folder", str(root)), ("parser", PARSER_VERSION), ("snapshot", stamp.hexdigest()[:20])])


def status_from_connection(conn):
    meta = _meta(conn)
    return dict(folder=meta.get("folder"), snapshot=meta.get("snapshot"),
        files=conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
        documents=conn.execute("SELECT COUNT(*) FROM sources WHERE kind='document' AND units>0").fetchone()[0],
        tables=conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0],
        rows=conn.execute("SELECT COALESCE(SUM(row_count),0) FROM sheets").fetchone()[0],
        issues=[dict(r) for r in conn.execute("SELECT source,reason FROM issues ORDER BY source,reason")],
        inventory=[dict(r) for r in conn.execute("SELECT source,state AS status,units FROM sources ORDER BY source")])


def status(db_path):
    empty = dict(folder=None, snapshot=None, files=0, documents=0, tables=0, rows=0, issues=[], inventory=[])
    if not Path(db_path).is_file():
        return empty
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='index_meta'").fetchone():
            return empty
        return status_from_connection(conn)


def catalog(conn):
    return [{**dict(r), "headers": json.loads(r["headers"])}
            for r in conn.execute("SELECT id,source,sheet,headers,row_count FROM sheets ORDER BY source,sheet")]


def table_rows(conn, table_id):
    head = conn.execute("SELECT headers FROM sheets WHERE id=?", (table_id,)).fetchone()
    if head:
        width = len(json.loads(head[0]))
        for r in conn.execute("SELECT anchor,cells FROM records WHERE sheet_id=? ORDER BY row_num", (table_id,)):
            values = json.loads(r["cells"])
            yield {"anchor": r["anchor"], "values": values + [None] * (width - len(values))}


def _search(conn, query, limit, baseline=False, docs_only=False):
    words = (w.casefold() for w in re.findall(r"[^\W_]+", str(query), flags=re.UNICODE))
    terms = list(dict.fromkeys(w for w in words if w not in STOPWORDS))[:64]
    if not terms:
        return []
    # Quote each token as a literal. Only internally selected table names enter SQL.
    match = " OR ".join(f'"{w}"' for w in terms)
    tab = "chunks" if baseline else "units"
    restriction = " AND kind='document'" if docs_only else ""
    sql = (f"SELECT source,anchor,body AS text FROM {tab} WHERE {tab} MATCH ?{restriction} "
           f"ORDER BY bm25({tab},0.6,0.25,1.0),rowid LIMIT ?")
    return [dict(r) for r in conn.execute(sql, (match, min(max(int(limit), 0), 200)))]


def search(conn, query, limit=5):
    return _search(conn, query, limit)


def document_search(conn, query, limit=5):
    return _search(conn, query, limit, docs_only=True)


def baseline_chunks(conn, query, limit=5):
    return _search(conn, query, limit, baseline=True)
