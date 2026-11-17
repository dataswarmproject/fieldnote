# Security

Fieldnote 0.2 is intended for one trusted OS account on a workstation. Install the latest release and select source folders you are authorized to read. The service listens on IPv4 loopback. Remote or multi-tenant hosting requires a different authentication, authorization and isolation design. Running inside the original user's session gives the program access to that account's files.

## Report privately

Use [GitHub private vulnerability reporting](https://github.com/dataswarmproject/fieldnote/security/advisories/new). Provide the affected version, platform, potential impact and the minimum information required to understand the issue. Use synthetic data. Never upload credentials, real documents, private benchmark exports or your state directory.

## Data boundaries

- A per-instance launch secret pairs the browser. Sensitive API calls require a strict HttpOnly cookie and an origin-scoped session secret. Treat private browser tabs and the state directory as trusted. Session credentials rotate on restart.
- The parser worker runs under the same OS identity as the app, with resource limits and restricted environment inheritance. It provides process and resource isolation. The OS account still determines filesystem permissions. Process arbitrary hostile collections in a separate low-privilege account or disposable OS environment.
- Selected sources may contain untrusted prose. The model interface accepts structured proposals for a bounded set of operators. Local execution validates fields, operators and cited material. Review interpretations and citations before consequential decisions.
- State files contain extracted text, answers, and local session material. Use a private profile with suitable filesystem permissions. Fieldnote does not provide application-level encryption, secure erasure, or per-document access control for shared accounts.
- A configured local inference service receives prompts and selected evidence. Its implementation and any forwarding, telemetry, model files or extensions are part of the trusted environment.

Security fixes target the current release. The prototype 0.1 release predates browser pairing and isolated ingestion. [Architecture and verification](docs/architecture.md)
