# Contributing to Fieldnote

Use GitHub Issues for bugs, feature proposals, and questions. Include your operating system, Python version, release tag, reproduction steps on synthetic data, and the expected and observed behavior. Redact folder names, source excerpts, model credentials, pairing URLs, and benchmark answers from screenshots and logs. Use private reporting for security issues. [Security policy](SECURITY.md)

1. Fork the repository, clone your fork, and create a branch from `main`.
2. Create a virtual environment, install the hash-locked requirements, and run the tests from the repository root.
3. Make a focused change with a synthetic regression case. Explain how the change affects existing folders, snapshot coverage, and token accounting when applicable.
4. Open a pull request describing the change and the commands you ran. Include before-and-after results for claims about accuracy or cost.

```sh
python -m unittest discover -s tests -v
python scripts/smoke.py
node --check static/app.js
```

The HTTP smoke test starts Fieldnote with a temporary state directory and an unused loopback port. It exercises browser pairing, indexing, an exact query, and graceful shutdown. The CI suite runs without model downloads. For inference changes, also run a paired benchmark with your own local model and report its name, source snapshot and grading method.

Runtime dependency constraints live in `requirements.in`. To change a dependency, update the constraints and regenerate the universal lock from PyPI with `uv pip compile requirements.in --default-index https://pypi.org/simple --python-version 3.11 --universal --generate-hashes --no-header --output-file requirements.txt`. Reinstall from the lock and run the tests. CI uses read-only repository permissions for pull requests and pins external actions to commit hashes.

By submitting a contribution you agree that it can be distributed under the project's MIT license.
