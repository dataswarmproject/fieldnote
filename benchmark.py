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
        aliases = case.get("aliases", [])
        if (not isinstance(aliases, list) or len(aliases) > 32
                or not all(isinstance(a, str) and a.strip() and len(a) <= 4000 for a in aliases)):
            raise ValueError("Provide up to 32 nonempty aliases of up to 4,000 characters each.")
    return cases


def run(db_path, cases, provider, snapshot):
    runs = []
    summary = {method: dict(total=len(cases), correct=0, value_matches=0, answered=0, errors=0, calls=0,
                            input_tokens=0, output_tokens=0, unmetered_calls=0)
               for method in ("engine", "rag")}
    start = time.monotonic()
    model = provider.name
    for case in validate(cases):
        pair = engine.compare(str(db_path), case["question"], provider)
        if pair["snapshot"] != snapshot:
            raise ValueError("Dataset changed during evaluation. Rerun on the current index.")
        if provider.name != model or pair.get("provider", model) != model:
            raise ValueError("Model changed during evaluation. Rerun with one selected model.")
        pair.update(expected=case["expected"], aliases=case.get("aliases", []))
        acceptable = {normalize(label) for label in (case["expected"], *case.get("aliases", []))}
        for method in summary:
            result = pair[method]
            matches = normalize(result["answer"]) in acceptable
            score = result["status"] == "answered" and matches
            pair[method + "_score"] = score
            pair[method + "_value_match"] = matches
            stats = summary[method]
            stats["correct"] += int(score)
            stats["value_matches"] += int(matches)
            stats["answered"] += int(result["status"] == "answered")
            stats["errors"] += int(result["status"] == "error")
            calls = result["calls"]
            stats["calls"] += calls
            for key in ("input_tokens", "output_tokens"):
                stats[key] += result.get("metered_" + key, result.get(key)) or 0
            stats["unmetered_calls"] += result.get("unmetered_calls", calls if (
                result.get("input_tokens") is None or result.get("output_tokens") is None) else 0)
        runs.append(pair)
    for stats in summary.values():
        stats["accuracy"] = stats["correct"] / len(cases)
        stats["value_match_rate"] = stats["value_matches"] / len(cases)
    return dict(runs=runs, summary=summary, snapshot=snapshot, model=model,
                generated_at=datetime.now(timezone.utc).isoformat(), seconds=round(time.monotonic()-start,2),
                grading="Correct requires answered status and normalized exact match with a reference or alias. Value matches compare answer text independently of coverage status. All cases stay in both denominators.",
                token_accounting="Backend-reported prompt and generated tokens across calls. Deterministic steps use zero model tokens.")
