# Changelog

All notable changes are recorded here. Pulse follows Semantic Versioning while
pre-1.0 APIs may change with release notes and migration guidance.

## [Unreleased]

## [0.1.2] - 2026-09-05

### Fixed

- Google authorization-code exchange and token refresh now send the Desktop
  client credential required by Google's token endpoint. Google documents that
  installed applications cannot keep this value confidential; PKCE remains the
  authorization-code interception protection.
- Packaging now fails before creating a wheel or source distribution when the
  product Google Desktop OAuth credentials are missing or malformed, preventing
  the placeholder configuration shipped by the invalid 0.1.1 PyPI artifacts.
- The release workflow validates the configured credential pair against
  Google's token endpoint before building.

### Security

- Release operators must publish only through the protected trusted-publisher
  workflow; direct uploads bypass provenance and release verification.

## [0.1.1] - 2026-09-04

### Added

- Product-owned Google Desktop OAuth configuration is embedded in official
  builds, so installed users can sign in without creating a `.env` file.
- Release verification now rejects missing/placeholder OAuth configuration,
  confidential client secrets, developer-only source content, and local home
  directory paths in published artifacts.
- Post-login BYOK onboarding now guides provider and model selection before
  collecting the selected provider key through hidden terminal input.
- Provider keys are stored in workspace-scoped native OS credential-vault
  entries, with safe status, rotation, and removal through `pulse keys`.

### Changed

- Google login uses PKCE with a dynamically allocated `127.0.0.1` callback port
  and no confidential client secret in the distributed application.
- Source distributions contain only the files needed to build and understand
  the product, rather than repository tests, scripts, evals, and local templates.
- Successful key set or rotation migrates that provider away from a legacy
  plaintext workspace `.env` entry without ever displaying the secret.

## [0.1.0] - 2026-08-29

### Added

- Permissioned local coding-agent CLI with repository intelligence, durable
  tasks, conversations, mutation tracking, provider routing, and loopback RPC.
- Docker and authenticated remote sandbox implementations with policy,
  resource, filesystem, network, secret-redaction, and recovery tests.
- Configured remote execution is preferred on client machines, with local
  Docker/Podman used as the secure fallback and no implicit host execution.
- Production doctor, correlation IDs, remote health/readiness endpoints,
  operations/security documentation, and explicit deployment boundaries.
- Reproducible wheel/source builds, artifact-content verification, clean-install
  smoke tests, cross-platform CI, coverage gate, dependency audit, secret scan,
  OIDC trusted-publishing workflow, release checksums, source manifest, and
  CycloneDX SBOM.
- Direct application dependencies pinned to the versions exercised by the
  release suite, preventing unreviewed major-version drift at installation.
- Explicit live-Docker test markers, a single no-skip Docker security gate, and
  stable Windows pytest teardown behavior in hosted CI.
- Public `version` and secure provider-key rotation commands, simplified OAuth
  login/logout UX, authenticated Google userinfo verification, and literal-safe
  terminal rendering for untrusted text.
- Non-root Docker overlay export that avoids archive metadata preservation on
  bind mounts, preventing successful sandbox commands from exiting with 125.

### Known limitations

- The supported release boundary is local and single-user.
- Hosted tenancy, centralized observability, and independent multi-tenant
  security validation are outside the supported beta boundary.
- The VS Code extension is not included in the Python release; it remains
  source-only and evaluation-only, and its legacy stdio transport is unsupported.
