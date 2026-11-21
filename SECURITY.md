# Security Policy

## Threat Model & Security Architecture

Jarviee is a **local-only** command center designed to run on a single developer's workstation under their own OS user account.

- **Loopback-only binding:** The HTTP server binds strictly to `127.0.0.1` (`hub/server.py` asserts `host == "127.0.0.1"`). It is not intended to be exposed on a LAN or the public internet.
- **DNS-rebinding and CSRF protections:** Incoming requests must pass `Host` (`127.0.0.1:<port>` or `localhost:<port>`), `Origin`, and `Sec-Fetch-Site` (`!= "cross-site"`) checks. State-mutating `POST` requests require `application/json` (or an allowed image MIME type on `/api/attach`).
- **Per-install API token:** On first startup, Jarviee generates a random 48-character hex token (`secrets.token_hex(24)`) stored at `~/.jarviee/token` with `0600` file permissions (`hub/auth.py`). Every `POST` endpoint and `GET /api/run-spec` requires this token (verified via `hmac.compare_digest` in `X-Jarviee-Token`).
- **No shipped credentials or user data:** No API keys, OAuth secrets, accounts, or private project data ship with this repository. Users run Jarviee locally with their own installed agent CLIs and local credentials.
- **Local execution boundary:** Jarviee launches local agent CLIs (such as `claude` and `agy`) in the configured workspace and permissions mode (`--mode auto` / `--permission-mode auto` by default in `config.json`). Only run trusted prompts and local repositories under your user session.

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Report vulnerabilities privately through GitHub Security Advisories:

- https://github.com/Mclein31/jarviee/security/advisories/new

Include as much detail as possible to help reproduce and assess the issue:
- Affected file(s), endpoint(s), or CLI command(s)
- Reproduction steps or proof-of-concept
- Potential impact and suggested remediation (if known)
