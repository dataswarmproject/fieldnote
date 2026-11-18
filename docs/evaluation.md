# Compiled engine evaluation

One paired run compared the compiled engine with the included BM25 top-five RAG baseline. Both received identical questions and a pinned snapshot per question. Only RAG used inference. It ran locally with `Qwen3.5-4B-Q4_K_M`, temperature 0, a 4,096-token input budget and a maximum of 500 output tokens. All model calls reported usage. [Machine-readable per-case results](benchmarks/compiled-preview.json)

| Workload | Local Dataset Query Engine correct | RAG correct | Local Dataset Query Engine calls and tokens | RAG calls | RAG input tokens | RAG output tokens |
|---|---:|---:|---:|---:|---:|---:|
| Bundled supplier demo | 6/6 | 4/6 | 0 | 6 | 10,735 | 135 |
| Seeded shipping documents and 64 spreadsheet rows | 7/10 | 6/10 | 0 | 8 | 5,939 | 170 |
| First 12 WikiTableQuestions unseen-table examples | 0/12 | 5/12 | 0 | 11 | 11,702 | 227 |

**Supplier demo.** This is a development fixture. The compiler answered a named fact, a strict boundary in a policy, and a count across all 96 rows. These examples exercise supported behavior and helped shape the implementation.

**Shipping revisions.** A separate, hand-authored fixture changes the threshold and owner, deletes a source, and introduces a conflicting owner. Each method gave four correct answers to the seven answerable questions. The compiler correctly abstained on three of three unanswerable cases. RAG correctly abstained on two. RAG answered the narrative diversion question. The compiler missed it and both policy counts. The question referenced “the shipping policy” while the source was named `Shipping.md`. Its source binding requires an exact qualifier. The two policy questions share a template across revisions, so they are correlated observations.

**External table questions.** The first 12 test questions were selected in published file order before evaluation, without filtering by grammar, answer type or table size. Both methods got the designated upstream table for each question. The compiler abstained on all 12. RAG returned ten answers, including five correct ones. Every example is present in the report by ID. The data and questions come from Pasupat and Liang’s [WikiTableQuestions](https://github.com/ppasupat/WikiTableQuestions/tree/7d455a5a707b96341ef72aff9428749d443d8aa9).

## Scoring and interpretation

For answerable questions a correct result requires both a whole-value match and an answered status. On a question labeled unanswerable, an explicit abstention earns credit. Errors and partial results earn zero. The external runner compares complete multi-value answers without regard to order. It uses Local Dataset Query Engine’s documented short-answer metric, which differs from the authors’ official evaluator. The reports also expose raw value matches so the effect of the coverage rule can be inspected separately.

Zero inference tokens means the compiled path sent no text to a model. A zero-token abstention is still a miss on an answerable question. Token totals exclude CPU time, disk usage, warm-up, and local tokenizer invocations. Both methods share an extraction and indexing pass that also builds the comparison index. These runs do not measure independent end-to-end infrastructure cost. The sample sizes, one model and one retrieval configuration support a comparison of these implementations on these cases. They cannot establish superiority over RAG in general.

## Reproduce

### Schema Query Compiler

The optional spreadsheet interpreter uses the same local Qwen3.5-4B-Q4_K_M weights with schema-constrained plan output. On the four synthetic cases in `scripts/evaluate_query_compiler.py`, the compiler and RAG each produced three correct values and one correct abstention. The compiler used 1,071 input and 112 output tokens. RAG used 899 input and 73 output tokens. The fixture has only three rows and includes an inventory question similar to the compiler instruction example. It verifies the integration. It provides little evidence about generalization.

On a new paired run of the same 12 external questions, Schema Query Compiler scored **0/12** using 3,528 input and 463 output tokens. It produced one incorrect answered result, seven abstentions, one partial result and three errors. The RAG baseline scored **6/12** using 11,702 input and 223 output tokens in this run. Its score on the earlier run was 5/12. The role used fewer tokens here and showed no accuracy improvement over the deterministic engine. [Per-case results](benchmarks/schema-query-compiler.json)

For a paired comparison on the pinned external slice, use `python scripts/evaluate_wikitables.py WikiTableQuestions --paired --compiler --output tables-compiler.json` with your local model endpoint.

### Deterministic engine

Use the Python environment from the project README. The compiled challenge runs without a model. The paired CLI requires `FIELDNOTE_MODEL_URL` pointing to your local chat service. Configure `FIELDNOTE_MODEL_ID` and `FIELDNOTE_MODEL_KEY` if needed. Run the bundled demo through the workbench’s **Benchmark** view.

```sh
python scripts/evaluate_compiled.py --seed 17331 --output shipping-offline.json
python scripts/evaluate_compiled.py --seed 17331 --paired --output shipping-paired.json
```

For the external slice, obtain the upstream data separately under its own license. The evaluator reads the local checkout and verifies the tested slice against its pinned fingerprint. It creates temporary indexes and leaves your active Local Dataset Query Engine folder alone.

```sh
git clone https://github.com/ppasupat/WikiTableQuestions.git
git -C WikiTableQuestions checkout 7d455a5a707b96341ef72aff9428749d443d8aa9
python scripts/evaluate_wikitables.py WikiTableQuestions --paired --output tables-paired.json
```
