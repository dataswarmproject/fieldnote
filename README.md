# Fieldnote

[![CI](https://github.com/dataswarmproject/fieldnote/actions/workflows/ci.yml/badge.svg)](https://github.com/dataswarmproject/fieldnote/actions/workflows/ci.yml)

Fieldnote compiles supported statements in local documents into facts and rules, then answers questions by executing those rules against workbook rows. Each answer carries its source and derivation. The default path runs entirely on a single-user workstation with zero model calls during indexing and querying. A separate BM25 retrieval-augmented generation (RAG) baseline lets you measure what each method gets right on your own questions.

## Run it

Use Python 3.11–3.14 with SQLite FTS5. The compiled engine is a development preview on the branch below. The [tagged releases](https://github.com/dataswarmproject/fieldnote/releases) document earlier versions and their verified platforms.

```sh
git clone -b feature/compiled-knowledge https://github.com/dataswarmproject/fieldnote.git
cd fieldnote
python -m venv .venv
```

**Windows PowerShell**

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe app.py --open
```

**Linux or macOS**

```sh
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
.venv/bin/python app.py --open
```

The launcher opens and pairs your browser. Use the same command again to pair another tab. Keep the terminal open while using Fieldnote. To check or stop it, run the same interpreter with `app.py --status` or `app.py --stop`. The Windows start and stop launchers also recognize the repository's virtual environment.

### Your first question

The demo contains a 96-row supplier workbook, a renewal policy, and a handoff note. Ask **How many suppliers require escalation under the renewal policy?** Fieldnote binds the compiled policy to the workbook’s review-hours column and counts the values strictly above 48. The result is **38**. Expand **Derivation** for the operator and row counts, then **Evidence** for the policy and sample rows. Try **Who is the escalation owner?** to inspect a document-only fact.

To use your own data, choose **Browse → Index folder**. The **Sources** view shows compilation counts and extraction issues. Select **Index folder** after editing files or opening an index created by an older version. Fieldnote reads CSV, XLSX, UTF-8/UTF-16 text, Markdown, text-bearing PDF and DOCX. Prepare scanned PDFs with OCR first. Tabular files should have one header row per sheet. Excel formulas rely on saved values.

The compiler understands a bounded English grammar. It can bind “The escalation owner is the Service Desk” and “A supplier requires escalation when its Review hours value is strictly greater than 48.” Narrative explanations, paraphrases, cross-table joins and many real policy clauses are outside its coverage. An unsupported question yields an abstention. The **Compare with RAG** checkbox runs the independent baseline for the same question.

### Connect a local model

Run a chat-completions-compatible service that you trust on loopback, with a chat model loaded. Set the endpoint before launching Fieldnote. Stop the existing instance before changing its configuration.

```powershell
$env:FIELDNOTE_MODEL_URL = 'http://127.0.0.1:1234/v1'
.\.venv\Scripts\python.exe app.py --open
```

On Linux and macOS use `export FIELDNOTE_MODEL_URL=http://127.0.0.1:1234/v1`. Optional settings include `FIELDNOTE_MODEL_ID` and `FIELDNOTE_MODEL_KEY`. Secrets belong in your local environment. Fieldnote can also start a compatible llama.cpp server when you set `FIELDNOTE_LLAMA_SERVER` to its executable and `FIELDNOTE_GGUF` to an installed model file. No model weights are included. Discovery of local servers can be explicitly enabled with `FIELDNOTE_DISCOVER_MODELS=1`.

## Compare accuracy and token use

Open **Benchmark** to run the six synthetic demo cases, or enter your own questions, expected short answers, accepted alternatives and expected abstentions. Only the grader receives reference answers. Both methods use the same indexed snapshot. The model connects on the first request for a RAG comparison.

The report includes raw value matches, coverage-qualified accuracy, errors, abstentions, elapsed time, calls, and backend-reported input and output tokens. Download the report to inspect individual answers and evidence. Short-answer grading uses normalized exact match. Review nuanced answers and citation quality yourself. The small demo exercises specific behaviors and is unsuitable for estimating accuracy on an unfamiliar corpus.

The baseline retrieves up to five BM25 passages, including serialized spreadsheet rows. It uses the local model to synthesize an answer. For compatible llama.cpp servers the input budget uses the model’s native tokenizer and chat template. Other services use a labeled estimate for admission. Generation usage comes from the backend. Fieldnote looks up symbols and scans typed rows. Both approaches spend CPU time and disk space indexing the folder. [Benchmark results and reproduction](docs/evaluation.md)

## Data and operating limits

The selected documents, extracted text, answers, and prompts stay on the computer when your configured model service processes them locally. Choose a trusted service that meets your data requirements. Keep the app bound to loopback, use a private OS account and protect its state directory. The local index and saved report contain readable data. File permissions and operating-system disk encryption protect them at rest.

Default state directories are `%LOCALAPPDATA%\Fieldnote\state` on Windows, `~/Library/Application Support/Fieldnote` on macOS, and `$XDG_STATE_HOME/fieldnote` or `~/.local/state/fieldnote` on Linux. Use `--state` for a separate private profile. To recover from an interrupted refresh, relaunch and reindex. To roll back a software update, stop the app, return to a known tag, reinstall that tag's requirements in a clean virtual environment, and use a fresh state directory to reindex the source files. Keep exported reports you need.

For architecture, query semantics, data boundaries, and resource limits, read [Architecture](docs/architecture.md). For reporting and the supported operating model, read [Security](SECURITY.md). Contributions are welcome under the [MIT license](LICENSE). See [Contributing](CONTRIBUTING.md).
