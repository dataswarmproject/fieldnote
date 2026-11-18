"""Evaluate the frozen first 12 questions in the WikiTableQuestions unseen test split."""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import benchmark
import indexer
from scripts.evaluate_compiled import OfflineProvider, command_provider


REVISION = "7d455a5a707b96341ef72aff9428749d443d8aa9"
SOURCE = "https://github.com/ppasupat/WikiTableQuestions/tree/" + REVISION
SHA256 = "80a742b1dad246eebe26c2e643ce8cbe9733727a891a317e73616f4348d37fff"
COUNT = 12


def unescape(field):
    """Decode the upstream TSV escaping after separating list items at raw pipes."""
    return re.sub(r"\\([\\np])", lambda m: {"n": "\n", "p": "|", "\\": "\\"}[m[1]], field)


def load_slice(dataset_root):
    root = Path(dataset_root).resolve(strict=True)
    question_file = (root / "data" / "pristine-unseen-tables.tsv").resolve(strict=True)
    if not question_file.is_relative_to(root):
        raise ValueError("Question file must be inside the dataset root.")
    with question_file.open(encoding="utf-8") as source:
        lines = []
        for _ in range(COUNT + 1):
            line = source.readline(8193)
            if len(line) > 8192:
                raise ValueError("Question line exceeds the size of the pinned slice.")
            if not line:
                break
            lines.append(line.rstrip("\r\n"))
    if len(lines) != COUNT + 1:
        raise ValueError("The frozen slice requires the first 12 questions in upstream file order.")
    rows = list(csv.DictReader(lines, delimiter="\t", quoting=csv.QUOTE_NONE))
    if not rows or not {"id", "utterance", "targetValue", "context"}.issubset(rows[0]):
        raise ValueError("Unexpected WikiTableQuestions split format.")
    if any(not re.fullmatch(r"csv/\d+-csv/\d+\.csv", row["context"]) for row in rows):
        raise ValueError("Unexpected WikiTableQuestions CSV reference.")
    fingerprint = hashlib.sha256(("\n".join(lines) + "\n").encode())
    tables = {}
    for reference in sorted({row["context"] for row in rows}):
        path = (root / reference).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("Referenced table must be inside the dataset root.")
        # A checkout may normalize line endings. Compare the pinned LF bytes and
        # keep the verified inputs in memory for the whole run.
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("CSV exceeds the size of the pinned slice.")
        content = path.read_bytes().replace(b"\r\n", b"\n")
        fingerprint.update(reference.encode() + b"\0" + hashlib.sha256(content).digest())
        tables[reference] = content.decode("utf-8")
    if fingerprint.hexdigest() != SHA256:
        raise ValueError("Dataset fingerprint mismatch. Use WikiTableQuestions revision " + REVISION)
    return rows, tables


def values_match(prediction, expected):
    """Order-independent whole-value equality for a multi-valued denotation."""
    gold = sorted(benchmark.normalize(value) for value in expected)
    text = str(prediction).strip()
    candidates = [[text]]
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list) and all(type(v) in (int, float, str) for v in parsed):
        candidates.append(parsed)
    if len(gold) > 1:
        for delimiter in ("|", ";", ",", "\n", " and "):
            candidates.append(text.split(delimiter))
    return any(sorted(benchmark.normalize(v) for v in items) == gold for items in candidates)


def evaluate(dataset_root, provider=None, mode="engine"):
    if mode not in {"both", "engine", "compiler", "compiler_rag"}:
        raise ValueError("Unknown external benchmark mode.")
    if mode != "engine" and (provider is None or not provider.available):
        raise ValueError("This evaluation requires an available local model.")
    provider = provider or OfflineProvider()
    methods = benchmark.engine.METHODS[mode]
    model = provider.name if mode != "engine" else "local execution"
    rows, tables = load_slice(dataset_root)
    cases = []
    start = time.monotonic()
    for row in rows:
        with tempfile.TemporaryDirectory(prefix="fieldnote-wikitable-") as temp:
            root = Path(temp)
            folder = root / "table"
            folder.mkdir()
            table = folder / Path(row["context"]).name
            # The upstream CSV dialect uses backslash to escape quotes and backslashes.
            reader = csv.reader(io.StringIO(tables[row["context"]], newline=""),
                                escapechar="\\", doublequote=False, strict=True)
            with table.open("w", encoding="utf-8", newline="") as destination:
                csv.writer(destination).writerows(reader)
            database = root / "corpus.sqlite3"
            index_start = time.monotonic()
            indexed = indexer.index_folder(folder, database)
            index_ms = round((time.monotonic() - index_start) * 1000, 1)
            expected = [unescape(value) for value in row["targetValue"].split("|")]
            question = unescape(row["utterance"])
            result = benchmark.run(database, [{"question": question, "expected": "|".join(expected)}],
                                   provider, indexed["snapshot"], mode=mode)
            if result["model"] != model:
                raise ValueError("Model changed during the external evaluation.")
            pair = result["runs"][0]
            case = dict(id=row["id"], table=row["context"], answerable=True, target_values=len(expected),
                        snapshot=indexed["snapshot"], indexing_ms=index_ms, rows=indexed["rows"],
                        indexing_issues=len(indexed["issues"]))
            for method in methods:
                answer = pair[method]
                matches = values_match(answer["answer"], expected)
                case[method + "_value_match"] = matches
                case[method + "_score"] = answer["status"] == "answered" and matches
                case[method] = {key: answer[key] for key in
                                ("status", "route", "calls", "input_tokens", "output_tokens",
                                 "metered_input_tokens", "metered_output_tokens", "unmetered_calls",
                                 "elapsed_ms") if key in answer}
            cases.append(case)
    return dict(suite="WikiTableQuestions unseen first 12", version=1, source=SOURCE,
                authors="Panupong Pasupat and Percy Liang", revision=REVISION, slice_sha256=SHA256,
                mode=mode, model=model, seconds=round(time.monotonic() - start, 2),
                design="First 12 examples in pristine-unseen-tables.tsv, original order. Each question uses only its designated table. All 12 cases are included.",
                grading="Project short-value metric: answered status and exact whole values after Unicode NFKC, case folding, whitespace collapse and trailing .!? removal. For multi-answer cases, compare all values as a multiset, including multiplicity, accepting a JSON array or items separated by pipe, semicolon, comma, newline or ' and '. This differs from the official WikiTableQuestions evaluator.",
                token_accounting="Backend-metered input and output tokens summed separately; unmetered calls listed separately.",
                summary=benchmark.summarize(cases, methods), cases=cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_root", type=Path, help="Local upstream checkout at " + REVISION)
    parser.add_argument("--paired", action="store_true", help="Use an existing FIELDNOTE_MODEL_URL for RAG.")
    parser.add_argument("--compiler", action="store_true", help="Use the schema-only model role for spreadsheet questions.")
    parser.add_argument("--output", type=Path, help="Write per-ID JSON report (default: stdout).")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fieldnote-provider-") as state:
        provider = command_provider(parser, args.paired or args.compiler, state)
        mode = ("compiler_rag" if args.paired else "compiler") if args.compiler else ("both" if args.paired else "engine")
        report = evaluate(args.dataset_root, provider, mode=mode)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report["summary"], indent=2))
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
