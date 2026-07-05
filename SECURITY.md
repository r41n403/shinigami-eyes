# Security Policy

## Supported Versions

Only the latest release is actively maintained. Security fixes are made against `main` and released under the next version tag.

| Version | Supported |
| ------- | --------- |
| 3.0.x   | ✅ |
| < 3.0   | ❌ |

## Reporting a Vulnerability

If you find a security issue in Shinigami Eyes — the app itself, the build pipeline, or the release process — please report it privately rather than opening a public issue.

**Preferred: [GitHub Security Advisories](https://github.com/r41n403/shinigami-eyes/security/advisories/new)** — lets you submit a private report directly on the repo.

**Alternative:** email connor@cmitservices.com with a description of the issue, steps to reproduce, and any relevant logs. Please don't include real credentials, bucket contents, or other sensitive data in the report itself.

You should expect an initial response within a few days. If the issue is confirmed, a fix will be prioritized and a new release cut; you'll be credited in the release notes unless you'd prefer to stay anonymous.

## Scope

This covers:
- The application code (`nas_migrate_gui.py`) — e.g. path traversal, unsafe deserialization, credential handling bugs, injection via filenames/volume labels.
- The CI/release pipeline (`.github/workflows/build-executables.yml`) — e.g. supply-chain risks, workflow injection, secret exposure.
- The signing and notarization setup (Azure Artifact Signing, Apple Developer ID / notarization) — e.g. anything that could let a malicious build get signed under this project's identity.

Out of scope: vulnerabilities in third-party dependencies (rclone, PyInstaller, Python itself) — please report those upstream. Issues that require physical access to a machine already running the app, or that rely on a user disabling OS-level security prompts, are lower priority but still welcome.

## Notes on Secrets

Signing credentials (Apple certificate/notarization credentials, Azure signing credentials) are stored only as encrypted GitHub Actions secrets and are never exposed to workflow runs triggered from forks. See the [README](README.md) for how the CI pipeline is structured.
