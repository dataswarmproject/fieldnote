"""Exercise the schema-only model role on a synthetic, isolated inventory."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import indexer
from provider import LocalModel


CASES = (
    ("Across the Oslo depots, how much inventory is on hand altogether?", "answered", "37"),
    ("Which depot has the identifier WH-3?", "answered", "Lima"),
    ("How many entries show stock below 15?", "answered", "2"),
    ("Which depot should we close under the board policy?", "unanswered", None),
)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        source = state / "inventory"
        source.mkdir()
        (source / "stock.csv").write_text(
            "Warehouse ID,Depot,Stock\nWH-1,Oslo,13\nWH-2,Oslo,24\nWH-3,Lima,8\n", encoding="utf-8")
        db = state / "index.sqlite"
        indexer.index_folder(source, db)
        model = LocalModel(state)
        try:
            model.start()
            if not model.available:
                raise RuntimeError(model.detail)
            for question, status, value in CASES:
                pair = engine.compare(db, question, model, mode="compiler_rag")
                for method in ("compiler", "rag"):
                    result = pair[method]
                    print(json.dumps(dict(model=model.name,question=question,method=method,
                          correct=result["status"]==status and (value is None or result["answer"]==value),
                          **{k:result[k] for k in ("status","answer","calls","input_tokens","output_tokens")})), flush=True)
        finally:
            model.close()


if __name__ == "__main__":
    main()
