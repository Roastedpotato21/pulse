# Changelog

All notable changes are recorded here. Pulse follows Semantic Versioning while
pre-1.0 APIs may change with release notes and migration guidance.

## [Unreleased]

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

### Known limitations

- The supported release boundary is local and single-user.
- Hosted tenancy, centralized observability, and independent multi-tenant
  security validation are outside the supported beta boundary.
- The VS Code extension is not included in the Python release; it remains
  source-only and evaluation-only, and its legacy stdio transport is unsupported.
