"""Paired evaluation against user-supplied short answers on one index snapshot."""

import time
import unicodedata
from datetime import datetime, timezone

import engine


def normalize(value):
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    return " ".join(text.rstrip(".!?").split())


def validate(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("Supply an array of 1–100 questions with expected answers.")
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each case must be an object.")
        for key in ("question", "expected"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"Each case requires a nonempty string for {key}.")
        if len(case["question"]) > 4000 or len(case["expected"]) > 4000:
            raise ValueError("Questions and expected answers may be up to 4,000 characters.")
        if type(case.get("answerable", True)) is not bool:
            raise ValueError("answerable must be true or false.")
        aliases = case.get("aliases", [])
        if (not isinstance(aliases, list) or len(aliases) > 32
                or not all(isinstance(a, str) and a.strip() and len(a) <= 4000 for a in aliases)):
            raise ValueError("Provide up to 32 nonempty aliases of up to 4,000 characters each.")
    return cases


def summarize(runs, methods):
    summary = {}
    for method in methods:
        stats = dict(total=len(runs), correct=0, answerable=0, unanswerable=0, correct_answers=0,
                     correct_abstentions=0, value_matches=0, answered=0, answered_answerable=0,
                     false_answers=0, unanswered=0, unavailable=0, partial=0, errors=0, calls=0, input_tokens=0,
                     output_tokens=0, unmetered_calls=0)
        for pair in runs:
            result = pair[method]
            answered = result["status"] == "answered"
            stats["answered"] += int(answered)
            stats["unanswered"] += int(result["status"] == "unanswered")
            stats["unavailable"] += int(result["status"] == "unavailable")
            stats["partial"] += int(result["status"] == "partial")
            stats["errors"] += int(result["status"] == "error")
            correct = pair[method + "_score"]
            stats["correct"] += int(correct)
            if pair["answerable"]:
                stats["answerable"] += 1
                stats["answered_answerable"] += int(answered)
                stats["correct_answers"] += int(correct)
                stats["value_matches"] += int(pair[method + "_value_match"])
            else:
                stats["unanswerable"] += 1
                stats["correct_abstentions"] += int(correct)
                stats["false_answers"] += int(answered)
            calls = result["calls"]
            stats["calls"] += calls
            for key in ("input_tokens", "output_tokens"):
                stats[key] += result.get("metered_" + key, result.get(key)) or 0
            stats["unmetered_calls"] += result.get("unmetered_calls", calls if (
                result.get("input_tokens") is None or result.get("output_tokens") is None) else 0)
        for metric, numerator, denominator in (
            ("accuracy", "correct", "total"), ("answer_coverage", "answered", "total"),
            ("answerable_accuracy", "correct_answers", "answerable"),
            ("answerable_coverage", "answered_answerable", "answerable"),
            ("abstention_accuracy", "correct_abstentions", "unanswerable"),
            ("value_match_rate", "value_matches", "answerable"),
        ):
            stats[metric] = stats[numerator] / stats[denominator] if stats[denominator] else None
        summary[method] = stats
    return summary


def run(db_path, cases, provider, snapshot, mode="both"):
    if mode not in {"engine", "both", "rag"}:
        raise ValueError("Choose engine, rag, or both.")
    methods = ("engine", "rag") if mode == "both" else (mode,)
    runs = []
    start = time.monotonic()
    model = "local execution" if mode == "engine" else provider.name
    for case in validate(cases):
        args = str(db_path), case["question"], provider
        pair = engine.compare(*args) if mode == "both" else engine.compare(*args, mode=mode)
        if pair["snapshot"] != snapshot:
            raise ValueError("Dataset changed during evaluation. Rerun on the current index.")
        if mode != "engine" and (provider.name != model or pair.get("provider", model) != model):
            raise ValueError("Model changed during evaluation. Rerun with one selected model.")
        pair.update(expected=case["expected"], aliases=case.get("aliases", []),
                    answerable=case.get("answerable", True))
        acceptable = {normalize(label) for label in (case["expected"], *case.get("aliases", []))}
        for method in methods:
            result = pair[method]
            if pair["answerable"]:
                matches = normalize(result["answer"]) in acceptable
                correct = result["status"] == "answered" and matches
            else:
                matches = None
                correct = result["status"] == "unanswered"
            pair[method + "_value_match"] = matches
            pair[method + "_score"] = correct
        runs.append(pair)
    summary = summarize(runs, methods)
    return dict(runs=runs, summary=summary, snapshot=snapshot, model=model,
                generated_at=datetime.now(timezone.utc).isoformat(), seconds=round(time.monotonic()-start,2),
                grading="Accuracy counts exact answered values on answerable cases and explicit unanswered status on unanswerable cases. Errors and partial results are incorrect. Raw value matches ignore status on answerable cases. Undefined rates are null.",
                token_accounting="Input and output totals cover backend-metered usage. Unmetered calls are listed separately. Deterministic operations use zero model tokens.")
