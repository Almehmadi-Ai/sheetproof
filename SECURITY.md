# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.2.x | Yes: the latest release on `main` receives security fixes. |
| 1.1.0 and earlier | **No.** Affected by code execution through `filters`, cross-user file access, and (on Open WebUI 0.5 and 0.6) model-callable internal methods; see [`CHANGELOG.md`](CHANGELOG.md). Upgrade both plugins. |

## Reporting a vulnerability

Please report vulnerabilities **privately** through GitHub: open this repository's **Security** tab
and choose **Report a vulnerability**. Do not open a public issue for a vulnerability. If private
reporting is unavailable, open an issue that only asks for a private contact, without details.

Include a **minimal reproduction using synthetic data** (see `examples/generate_samples.py`). Do not
include private, production, or personal data in a report.

## Security model

SheetProof treats every tool argument and every attached-file id as untrusted, because a model can be
steered by prompt injection and Open WebUI copies the attachment list from the client request.
Expressions are checked against an AST allow-list and never evaluated by Python or pandas, file ids
must pass Open WebUI's read rule for files, and parsing is bounded by size limits.
[`docs/security.md`](docs/security.md) describes each property, its limits and the residual risks.
