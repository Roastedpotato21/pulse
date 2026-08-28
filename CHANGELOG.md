# Changelog

All notable changes are recorded here. Pulse follows Semantic Versioning while
pre-1.0 APIs may change with release notes and migration guidance.

## [Unreleased]

## [0.1.0] - 2026-08-28

### Added

- Permissioned local coding-agent CLI with repository intelligence, durable
  tasks, conversations, mutation tracking, provider routing, and loopback RPC.
- Docker and authenticated remote sandbox implementations with policy,
  resource, filesystem, network, secret-redaction, and recovery tests.
- Production doctor, correlation IDs, remote health/readiness endpoints,
  operations/security documentation, and explicit deployment boundaries.
- Reproducible wheel/source builds, artifact-content verification, clean-install
  smoke tests, cross-platform CI, coverage gate, dependency audit, secret scan,
  and OIDC trusted-publishing workflow.

### Known limitations

- The supported release boundary is local and single-user.
- The VS Code extension is not included in the Python release and its legacy
  stdio transport is unsupported.
- Hosted tenancy, unified schema migrations/backups, centralized observability,
  signed SBOM/provenance, and the full evaluation gate remain roadmap work.
- No project license has been selected; public distribution requires an owner
  decision before release.
