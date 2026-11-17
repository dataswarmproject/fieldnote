# Fieldnote

[![CI](https://github.com/dataswarmproject/fieldnote/actions/workflows/ci.yml/badge.svg)](https://github.com/dataswarmproject/fieldnote/actions/workflows/ci.yml)

Ask questions across a local folder of documents and spreadsheets, inspect the evidence, and compare accuracy and token use with a BM25 retrieval-augmented generation (RAG) baseline. Fieldnote runs on a single-user workstation. Exact table queries work immediately. Connect a local chat model to interpret documents, apply a document rule to a spreadsheet, or run both methods side by side.

## Run it

Use Python 3.11–3.14 with SQLite FTS5. Download and extract the source archive from [GitHub Releases](https://github.com/dataswarmproject/fieldnote/releases) or clone the repository. Consult the release notes for the platforms verified for that build.

```sh
git clone https://github.com/dataswarmproject/fieldnote.git
cd fieldnote
git checkout v0.2.0
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

The demo contains a 96-row supplier workbook, a renewal policy, and a handoff note. Ask **How many suppliers have Status = Eligible?** The evidence engine scans the workbook and returns **32** with zero inference tokens. With a model connected, try **How many suppliers require escalation under the renewal policy?** The expected result is **38**. Expand the evidence to inspect the policy passage, spreadsheet filter, and example rows.

To use your own data, choose **Browse → Index folder**. The **Sources** view shows files and extraction issues. Select **Index folder** after editing files. Fieldnote supports CSV, XLSX, UTF-8/UTF-16 text, Markdown, text-bearing PDF and DOCX. Prepare scanned PDFs with OCR first. Tabular files should have one header row per sheet. Excel formulas rely on saved values.

### Connect a local model

Run a chat-completions-compatible service that you trust on loopback, with a chat model loaded. Set the endpoint before launching Fieldnote. Stop the existing instance before changing its configuration.

```powershell
$env:FIELDNOTE_MODEL_URL = 'http://127.0.0.1:1234/v1'
.\.venv\Scripts\python.exe app.py --open
```

On Linux and macOS use `export FIELDNOTE_MODEL_URL=http://127.0.0.1:1234/v1`. Optional settings include `FIELDNOTE_MODEL_ID` and `FIELDNOTE_MODEL_KEY`. Secrets belong in your local environment. Fieldnote can also start a compatible llama.cpp server when you set `FIELDNOTE_LLAMA_SERVER` to its executable and `FIELDNOTE_GGUF` to an installed model file. No model weights are included. Discovery of local servers can be explicitly enabled with `FIELDNOTE_DISCOVER_MODELS=1`.

## Compare accuracy and token use

Open **Benchmark** to run the six synthetic demo cases, or enter your own questions, expected short answers, and accepted alternatives. Reference answers are passed only to the grader. Both methods use the same indexed snapshot and the same configured model when inference is needed.

The report includes raw value matches, coverage-qualified accuracy, errors, abstentions, elapsed time, calls, and backend-reported input and output tokens. Download the report to inspect individual answers and evidence. Short-answer grading uses normalized exact match. Review nuanced answers and citation quality yourself. The small demo exercises specific behaviors and is unsuitable for estimating accuracy on an unfamiliar corpus.

The baseline retrieves up to five BM25 passages, including serialized spreadsheet rows. Fieldnote can execute whole-table counts, numeric aggregates, and single-value lookups. For supported mixed questions the model proposes a structured filter over one table. The engine validates and executes it. The two approaches have different evidence-selection strategies and share an input admission budget. For compatible llama.cpp servers the budget uses the native tokenizer and chat template. Other services use a labeled estimate before generation. Missing runtime usage is reported separately.

## Data and operating limits

The selected documents, extracted text, answers, and prompts stay on the computer when your configured model service processes them locally. Choose a trusted service that meets your data requirements. Keep the app bound to loopback, use a private OS account and protect its state directory. The local index and saved report contain readable data. File permissions and operating-system disk encryption protect them at rest.

Default state directories are `%LOCALAPPDATA%\Fieldnote\state` on Windows, `~/Library/Application Support/Fieldnote` on macOS, and `$XDG_STATE_HOME/fieldnote` or `~/.local/state/fieldnote` on Linux. Use `--state` for a separate private profile. To recover from an interrupted refresh, relaunch and reindex. To roll back a software update, stop the app, return to a known tag, reinstall that tag's requirements in a clean virtual environment, and use a fresh state directory to reindex the source files. Keep exported reports you need.

For architecture, query semantics, data boundaries, and resource limits, read [Architecture](docs/architecture.md). For reporting and the supported operating model, read [Security](SECURITY.md). Contributions are welcome under the [MIT license](LICENSE). See [Contributing](CONTRIBUTING.md).
