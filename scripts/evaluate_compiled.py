"""Rebuild a seeded synthetic corpus and score both paths through the public engine."""

import argparse
import csv
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import benchmark
import indexer
from provider import LocalModel


DEFAULT_SEED = 17331
SUITE_VERSION = "shipping-revisions-v1"
PHASES = ("original", "amended", "conflicting")


class OfflineProvider:
    name = "offline"
    available = False

    def complete(self, *args, **kwargs):
        raise AssertionError("The offline evaluation attempted model inference.")


class Challenge:
    """The generator and oracle are ordinary Python; the engine only sees the folder and query."""

    def __init__(self, seed):
        rng = random.Random(seed)
        self.owners = rng.sample(["Lena Voss", "Nadia Lake", "Dara Grey", "Iris Winn",
                                  "Ari Martel", "Remy Moss", "Sol Arden"], 3)
        self.port = rng.choice(["Elm Quay", "Fern Quay", "Coral Wharf", "Aster Pier"])
        self.office = rng.choice(["Rose Yard", "Birch Hall", "East Annex", "Cedar Court"])
        self.thresholds = rng.randint(14, 25), rng.randint(32, 46)
        # Each threshold has entries just below, on and just above it.
        masses = [weight + offset for weight in self.thresholds for offset in (-1, 0, 1)]
        masses.extend(rng.randint(2, 63) for _ in range(58))
        self.rows = [(f"CR-{i:03}", rng.choice(["Amber", "Teal", "Violet"]), mass)
                     for i, mass in enumerate(masses, 1)]

    def write_phase(self, folder, phase):
        if phase not in PHASES:
            raise ValueError("Unknown challenge phase.")
        amended = phase != "original"
        owner = self.owners[int(amended)]
        threshold = self.thresholds[int(amended)]
        if phase == "original":
            with (folder / "crates.csv").open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(["Crate ID", "Zone", "Mass kg"])
                writer.writerows(self.rows)
            (folder / "Dispatch.md").write_text(f"The dispatch office is {self.office}.\n", encoding="utf-8")
            (folder / "Diary.txt").write_text(
                f"After the north road closed, the crew diverted the shipment to {self.port}.\n",
                encoding="utf-8")
        elif phase == "amended":
            (folder / "Dispatch.md").unlink()
        else:
            (folder / "Handover.md").write_text(
                f"The transfer coordinator is {self.owners[2]}.\n", encoding="utf-8")
        (folder / "Shipping.md").write_text(
            f"The transfer coordinator is {owner}.\n\n"
            f"The inspection threshold is {threshold} kilograms.\n\n"
            f"A crate requires inspection when its Mass kg value is strictly greater than {threshold}.\n",
            encoding="utf-8")

    def cases(self, phase):
        def case(category, question, answer=None):
            return dict(category=category, question=question,
                        expected=str(answer) if answer is not None else "[unanswerable]",
                        answerable=answer is not None)

        coordinator = "Who is the transfer coordinator?"
        office = "What is the dispatch office?"
        inspection = "How many crates require inspection under the shipping policy?"
        if phase == "conflicting":
            return [case("conflicting sources", coordinator)]
        amended = phase == "amended"
        cases = [
            case("document fact", coordinator, self.owners[int(amended)]),
            case("document rule and table count", inspection,
                 sum(mass > self.thresholds[int(amended)] for _, _, mass in self.rows)),
            case("deleted source" if amended else "document fact", office,
                 None if amended else self.office),
        ]
        if not amended:
            cases.extend([
                case("table filter", "How many crates have Zone = Amber?",
                     sum(zone == "Amber" for _, zone, _ in self.rows)),
                case("narrative prose", "Where did the crew divert the shipment after the north road closed?",
                     self.port),
                case("missing fact", "Who is the insurance underwriter?"),
            ])
        return cases


def corpus_digest(folder):
    """Fingerprint the public files independent of their temporary installation path."""
    digest = hashlib.sha256()
    for path in sorted(folder.iterdir()):
        digest.update(path.name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def evaluate(provider=None, mode="engine", seed=DEFAULT_SEED):
    if mode not in {"engine", "both"}:
        raise ValueError("Challenge mode must be engine or both.")
    if mode == "both" and (provider is None or not provider.available):
        raise ValueError("Paired evaluation requires one available local model for RAG.")
    provider = provider or OfflineProvider()
    methods = ("engine", "rag") if mode == "both" else ("engine",)
    challenge = Challenge(seed)
    phases = []
    with tempfile.TemporaryDirectory(prefix="fieldnote-challenge-") as temp:
        root = Path(temp)
        folder = root / "sources"
        folder.mkdir()
        database = root / "challenge.sqlite3"
        for phase in PHASES:
            challenge.write_phase(folder, phase)
            start = time.monotonic()
            indexed = indexer.index_folder(folder, database)
            indexing_ms = round((time.monotonic() - start) * 1000, 1)
            if indexed["issues"]:
                raise RuntimeError(f"Challenge indexing reported issues in {phase}: {indexed['issues']}")
            cases = challenge.cases(phase)
            result = benchmark.run(database, cases, provider, indexed["snapshot"], mode=mode)
            if phases and result["model"] != phases[0]["model"]:
                raise ValueError("Model changed between challenge revisions.")
            for row, case in zip(result["runs"], cases, strict=True):
                row["category"] = case["category"]
            phases.append(dict(phase=phase, corpus_sha256=corpus_digest(folder),
                               indexing_ms=indexing_ms, files=indexed["files"], rows=indexed["rows"], **result))
    all_runs = [run for phase in phases for run in phase["runs"]]
    return dict(suite=SUITE_VERSION, seed=seed, mode=mode, model=phases[0]["model"],
                design="Hand-authored synthetic workload with seeded values. Repeated questions test updates, deletion and conflicts. No corpus-specific labels are sent to either method. This suite does not establish accuracy on other corpora or against other RAG configurations.",
                summary=benchmark.summarize(all_runs, methods), phases=phases)


def command_provider(parser, paired, state):
    if not paired:
        return OfflineProvider()
    if not os.environ.get("FIELDNOTE_MODEL_URL"):
        parser.error("--paired requires FIELDNOTE_MODEL_URL pointing at an existing loopback model /v1 service.")
    provider = LocalModel(state)
    provider.start()
    if not provider.available:
        parser.error(provider.detail)
    return provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--paired", action="store_true",
                        help="Measure both methods against the same existing FIELDNOTE_MODEL_URL.")
    parser.add_argument("--output", type=Path, help="Write full JSON report (default: stdout).")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="fieldnote-provider-") as state:
        provider = command_provider(parser, args.paired, state)
        report = evaluate(provider, mode="both" if args.paired else "engine", seed=args.seed)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(report["summary"], indent=2))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
