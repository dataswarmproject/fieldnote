"""Compile a bounded, declarative English grammar into source-backed SQLite records."""

import json
import re
from decimal import Decimal


VERSION = "fieldnote-knowledge-v1"
TABLES = ("knowledge_records", "knowledge_gaps")
MAX_CLAUSES = 100_000
MAX_BLOCK = 128_000
MAX_CLAUSE = 1_024


def initialize(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_records(
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('fact','rule')),
            key TEXT NOT NULL, entity TEXT NOT NULL, column_name TEXT NOT NULL,
            op TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL,
            source TEXT NOT NULL, anchor TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','blocked'))
        );
        CREATE INDEX IF NOT EXISTS knowledge_lookup ON knowledge_records(kind,key,source);
        CREATE INDEX IF NOT EXISTS knowledge_source ON knowledge_records(source);
        CREATE TABLE IF NOT EXISTS knowledge_gaps(source TEXT NOT NULL, anchor TEXT NOT NULL, reason TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS knowledge_gaps_by_location ON knowledge_gaps(source,anchor,reason);
    """)


def normalize(text):
    return " ".join(text.casefold().split())


def available(conn):
    meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_meta'").fetchone()
    return bool(meta and conn.execute("SELECT 1 FROM index_meta WHERE key='compiler' AND value=?", (VERSION,)).fetchone())


def _counts(conn):
    counts = dict(facts=0, rules=0, blocked=0, omissions=0)
    for kind, status, n in conn.execute("SELECT kind,status,COUNT(*) FROM knowledge_records GROUP BY kind,status"):
        counts["blocked" if status == "blocked" else kind + "s"] += n
    counts["omissions"] = conn.execute("SELECT COUNT(*) FROM knowledge_gaps").fetchone()[0]
    return counts


def publish(conn):
    """Compute coverage once, inside the same transaction as the derived data."""
    conn.executemany("INSERT OR REPLACE INTO index_meta(key,value) VALUES(?,?)", (
        ("compiler", VERSION), ("compiler_counts", json.dumps(_counts(conn)))))


def summary(conn=None):
    meta = {}
    if conn is not None and conn.execute("SELECT 1 FROM sqlite_master WHERE name='index_meta'").fetchone():
        meta = dict(conn.execute("SELECT key,value FROM index_meta WHERE key IN ('compiler','compiler_counts','snapshot')"))
    ready = meta.get("compiler") == VERSION
    result = dict(version=VERSION if ready else None, needs_reindex=bool(meta.get("snapshot")) and not ready,
                  facts=0, rules=0, blocked=0, omissions=0, gaps=[])
    if ready:
        counts = json.loads(meta["compiler_counts"]) if "compiler_counts" in meta else _counts(conn)
        result.update(counts)
        result["gaps"] = [dict(row) for row in conn.execute(
            "SELECT source,anchor,reason FROM knowledge_gaps ORDER BY source,anchor,reason LIMIT 50")]
    return result


def records(conn, kind, key=None, include_evidence=True):
    """Enumerate symbols, including blocked symbols; keyed lookups use a B-tree."""
    if kind not in ("fact", "rule"):
        raise ValueError("Unknown knowledge record kind.")
    if not available(conn):
        return
    fields = "key AS subject" if kind == "fact" else "key AS predicate,entity,column_name AS [column],op"
    select = f"SELECT id,{fields},value,unit,source,anchor,status" + (",text" if include_evidence else "")
    query = select + " FROM knowledge_records WHERE kind=?"
    parameters = [kind]
    if key is not None:
        query += " AND key=?"
        parameters.append(normalize(key))
    for row in conn.execute(query + " ORDER BY source,id", parameters):
        yield dict(row)


def evidence(conn, record_id):
    if not available(conn):
        return None
    row = conn.execute("SELECT source,anchor,text FROM knowledge_records WHERE id=?", (record_id,)).fetchone()
    return dict(row) if row else None


WORD = r"[A-Za-z][A-Za-z0-9'-]{0,39}"
LABEL = rf"{WORD}(?: {WORD}){{0,7}}"
NUMBER = r"[+-]?(?:0|[1-9]\d{0,23})(?:\.\d{1,16})?"
UNIT = r"(?:hours?|minutes?|seconds?|days?|weeks?|months?|years?|meters?|metres?|kilograms?|units?|percent|%)"
FACT_HEAD = re.compile(rf"\bThe (?P<subject>{LABEL}?) is (?P<value>.+)", re.I)
FACT_REVISION = re.compile(rf"\bThe (?P<subject>{LABEL}?) (?:was|will be|has (?:changed|been)|"
                           r"changed|became|should be|used to be) .+", re.I)
MEASURE = re.compile(rf"(?P<number>{NUMBER})(?: ?(?P<unit>{UNIT}))?", re.I)
NAME = re.compile(r"(?:the |The )?([A-Z][\w'-]*(?:(?: (?:of|the|and))? [A-Z][\w'-]*){0,5})")
CODE = re.compile(r"[a-z][a-z0-9-]{0,31}")
ACTION = rf"(?:requires|needs|qualifies for|is eligible for) {LABEL}"
RULE_HEAD = re.compile(rf"\b(?:A|An|Each) (?P<entity>{LABEL}?) (?P<predicate>{ACTION}) (?:when|if) (?P<condition>.+)", re.I)
RULE_NEGATION = re.compile(rf"\b(?:A|An|Each) (?P<entity>{LABEL}?) (?:does not|never|no longer) "
                           rf"(?P<action>require|need)s? (?P<goal>{LABEL}?)(?: (?:when|if|unless|because) .+)?$", re.I)
OPS = {"strictly greater than": ">", "greater than": ">", "more than": ">", "above": ">",
       "at least": ">=", "greater than or equal to": ">=", "strictly less than": "<",
       "less than": "<", "below": "<", "at most": "<=", "less than or equal to": "<=", "equal to": "="}
OPERATORS = {"is " + label: op for label, op in OPS.items()}
OPERATORS.update({"exceeds": ">", "equals": "=", "falls below": "<"})
COMPARISON = "|".join(map(re.escape, sorted(OPERATORS, key=len, reverse=True)))
CONDITION = re.compile(rf"(?:its )?(?P<column>{LABEL}?)(?: value)? (?P<op>{COMPARISON}) (?P<n>{NUMBER})"
                       rf"(?: ?(?P<unit>{UNIT}))?(?:, (?:regardless|irrespective) of {WORD}(?: (?:or|and) {WORD})?)?", re.I)
BOUNDARY = re.compile(rf"Exactly (?P<n>{NUMBER})(?: (?P<unit>{UNIT}))? does not (?P<action>require|need) (?P<goal>{LABEL})", re.I)
USAGE = re.compile(rf"The (?P<key>{LABEL}) is used only for (?P<purpose>{LABEL})", re.I)
SCOPE = re.compile(r"\b(?:except\w*|unless|only|excluding|if|when|provided|until|formerly|historical|draft|"
                   r"retired|proposed|obsolete|previous\w*|hypothetical|example|revised|revision|amended|"
                   r"overrid\w*|supersed\w*|temporary|tentative|may|might|sometimes|normally|usually|"
                   r"either|alternatively|but|however|not|never|no|for|from|before|after|in|at|under|during)\b", re.I)
STRUCTURAL = {"policy", "procedures", "procedure", "rules", "rule", "overview", "ownership", "general",
              "notes", "handbook", "handoff", "facts", "terms", "details", "definitions"}


class SourceCompiler:
    """The transaction and file-level savepoint are owned by the indexer."""

    def __init__(self, conn, source):
        self.conn = conn
        self.source = source
        self.count = 0
        self.unsafe = False
        self.section_labels = []
        self.vocabulary = set()
        self.has_rule = False

    def gap(self, anchor, reason, *, global_scope=True):
        self.conn.execute("INSERT INTO knowledge_gaps VALUES(?,?,?)", (self.source, anchor, reason))
        self.unsafe |= global_scope

    def admit(self, anchor):
        self.count += 1
        if self.count == MAX_CLAUSES + 1:
            self.gap(anchor, "Source exceeds the compiler statement limit.")
        return self.count <= MAX_CLAUSES

    def heading(self, anchor, label, *, document_title=False):
        if self.admit(anchor):
            if len(label) > MAX_CLAUSE:
                self.gap(anchor, "Section heading exceeds the compiler limit.")
            else:
                self.section_labels.append((anchor, normalize(label), document_title))

    def add(self, kind, key, anchor, text, value="", unit="", *, entity="", column="", op="", blocked=False):
        key = normalize(key)
        self.has_rule |= kind == "rule"
        self.vocabulary.update(key.split())
        self.vocabulary.update(normalize(entity).split())
        cursor = self.conn.execute("""
            INSERT INTO knowledge_records(kind,key,entity,column_name,op,value,unit,source,anchor,text,status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (kind, key, normalize(entity), column, op, value, normalize(unit), self.source, anchor, text,
              "blocked" if blocked else "active"))
        return cursor.lastrowid

    def unit(self, anchor, text):
        if not text or self.count > MAX_CLAUSES:
            return
        if len(text) > MAX_BLOCK:
            self.gap(anchor, "Text block exceeds the compiler limit.")
            self.admit(anchor)
            return
        text = " ".join(text.split())
        # Plain-text documents sometimes begin with a functional document title.
        # Qualifiers and punctuation exclude a title from this structural form.
        if (self.count == 0 and re.fullmatch(LABEL, text) and not SCOPE.search(text)
                and normalize(text).split()[-1] in STRUCTURAL):
            self.heading(anchor, text, document_title=True)
            return
        previous = None
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if not self.admit(anchor):
                break
            if len(sentence) > MAX_CLAUSE:
                self.gap(anchor, "Statement exceeds the compiler limit.")
                previous = None
            else:
                previous = self.statement(anchor, sentence, previous)

    def statement(self, anchor, sentence, previous):
        body = sentence.removesuffix(".")
        boundary = BOUNDARY.fullmatch(body)
        if boundary and previous and previous["kind"] == "rule":
            unit = normalize(boundary["unit"] or "")
            compatible_unit = previous["unit"] or normalize(previous["column"]).split()[-1]
            if (previous["op"] in (">", "<") and Decimal(previous["value"]) == Decimal(boundary["n"])
                    and previous["key"] == normalize(boundary["action"] + "s " + boundary["goal"])
                    and (not unit or unit == compatible_unit)):
                return None
        usage = USAGE.fullmatch(body)
        if usage and previous and previous["kind"] == "fact":
            if (" " + previous["key"]).endswith(" " + normalize(usage["key"])):
                self.conn.execute("UPDATE knowledge_records SET status='blocked' WHERE id=?", (previous["id"],))
                self.gap(anchor, "The preceding fact has a restricted use.", global_scope=False)
                return None
        rule = RULE_HEAD.fullmatch(body)
        if rule:
            condition = CONDITION.fullmatch(rule["condition"])
            key = normalize(rule["predicate"])
            goal = re.sub(r"^(requires|needs|qualifies for|is eligible for) ", "", key)
            if (condition and not SCOPE.search(rule["entity"]) and not SCOPE.search(goal)
                    and not SCOPE.search(condition["column"])):
                column = condition["column"]
                unit = normalize(condition["unit"] or "")
                op = OPERATORS[normalize(condition["op"])]
                self.add("rule", key, anchor, sentence, condition["n"], unit, entity=rule["entity"], column=column, op=op)
                return dict(kind="rule", key=key, unit=unit, column=column, op=op, value=condition["n"])
            self.add("rule", key, anchor, sentence, entity=rule["entity"], blocked=True)
            self.gap(anchor, "Rule has an unsupported condition or scope.")
            return None
        fact = FACT_HEAD.fullmatch(body)
        if fact:
            key, literal = normalize(fact["subject"]), fact["value"]
            quantity = MEASURE.fullmatch(literal)
            name = NAME.fullmatch(literal)
            if not SCOPE.search(key) and not SCOPE.search(literal) and (quantity or name or CODE.fullmatch(literal)):
                value = quantity["number"] if quantity else (name[1] if name else literal)
                unit = (quantity["unit"] or "") if quantity else ""
                record_id = self.add("fact", key, anchor, sentence, value, unit)
                return dict(kind="fact", key=key, id=record_id)
            self.add("fact", key, anchor, sentence, blocked=True)
        else:
            # A recognizable head buried inside a qualifier or revision can
            # shadow an active record elsewhere. It never creates an operation.
            revision = FACT_REVISION.search(body) or FACT_HEAD.search(body)
            negation = RULE_NEGATION.search(body)
            embedded_rule = RULE_HEAD.search(body)
            if revision:
                self.add("fact", revision["subject"], anchor, sentence, blocked=True)
            if negation:
                predicate = negation["action"] + "s " + negation["goal"]
                self.add("rule", predicate, anchor, sentence, entity=negation["entity"], blocked=True)
            elif embedded_rule:
                self.add("rule", embedded_rule["predicate"], anchor, sentence, entity=embedded_rule["entity"], blocked=True)
        self.gap(anchor, "Statement has unsupported syntax or scope.")
        return None

    def finish(self, partial=False):
        for anchor, label, document_title in self.section_labels:
            words = set(label.split())
            # Titles describe this source. Subheadings consisting of novel scope
            # (a branch name, version, region, etc.) need a richer representation.
            if (SCOPE.search(label) or not re.fullmatch(LABEL, label)
                    or not (words <= self.vocabulary | STRUCTURAL or (
                        document_title and not self.has_rule and words & self.vocabulary
                        and label.split()[-1] in STRUCTURAL))):
                self.gap(anchor, "Section heading has unresolved scope.")
        if self.unsafe or partial:
            self.conn.execute("UPDATE knowledge_records SET status='blocked' WHERE source=?", (self.source,))
