# Pulse Production Checkpoints

Last audited: 2026-08-28

## Release Decision

Pulse is suitable for a **local, single-user preview release** after the
release-blocking checkpoints below are closed. It is not yet suitable for a
multi-tenant hosted service or for unattended execution of untrusted code.
Those modes remain blocked until the isolation, identity, operations, and
evaluation checkpoints in later phases are complete.

The deployment target for today is the `pulse-coding-agent` Python
distribution (which installs the `pulse` commands) plus its
local loopback RPC service. Publishing to a package registry is a separate,
explicit release action requiring registry credentials.

## Audit Snapshot

| Area | Current evidence | Status | Release action |
| --- | --- | --- | --- |
| Python tests | 346 passed, 16 environment-dependent sandbox tests skipped | Partial | Keep unit suite green and run secure-backend tests in CI |
| Lint | Ruff passes for `src` and `tests` | Pass | Make it a required release gate |
| Packaging | Explicit Hatchling build, clean wheel/sdist verifier, isolated install smoke test | Pass | Keep artifact verification release-blocking |
| CI | Cross-platform tests, coverage, build, audit, secret scan, extension compile, and Docker jobs exist | Partial | Confirm required checks on the pushed release commit |
| Security | Extensive sandbox policy tests and a security note exist | Partial | Add a root threat model, supported deployment boundary, and response process |
| Runtime safety | Secure Docker/remote paths exist; host fallback is explicitly unsafe | Partial | Fail closed in production guidance and verify container tests |
| Configuration | Example environment file exists | Partial | Validate deployment configuration without exposing secrets |
| Release operations | Changelog, checklist, OIDC workflow, production doctor, and rollback runbook exist | Pass for local | Configure external release environments before publishing |
| API compatibility | CLI/RPC are unversioned and pre-1.0 | Gap | Declare compatibility policy and version RPC envelopes |
| Type safety | Type hints exist but no type-checking gate | Gap | Introduce a type checker incrementally with a checked-core baseline |
| Observability | Local structured telemetry/cost tracking exists | Partial | Add correlation, OpenTelemetry export, dashboards, and alert thresholds |
| Evaluation | A verifier/trajectory harness and focused tests exist | Partial | Add versioned task corpus, quality thresholds, and provider E2E jobs |
| Data lifecycle | Multiple SQLite stores exist | Gap | Define schemas, migrations, backup, retention, and corruption recovery |
| Multi-user hosting | Token auth exists for remote execution | Blocked | Add real identity/authorization, tenant boundaries, rotation, and abuse controls |
| Supply chain | Lockfile exists | Partial | Add SBOM, provenance, signed artifacts, and vulnerability policy |
| Project license | No project license has been selected | Blocked for public distribution | Owner must choose and add a license before public publishing |
| Documentation accuracy | Supported and evaluation-only boundaries are explicit | Pass | Review claims with every release |

## Phased Implementation Plan

### Phase 0 — Baseline and release boundary

- [x] Run the complete local Python test suite.
- [x] Run the configured linter.
- [x] Record an evidence-based gap inventory.
- [x] Define today's supported deployment boundary.
- [ ] Confirm the live Docker security suite on a Docker-capable runner.

Exit: release scope and non-goals are explicit; the existing baseline is
reproducible.

### Phase 1 — Reproducible distribution

- [x] Add an explicit PEP 517 build backend and package discovery.
- [x] Add project URLs, classifiers, Python compatibility, and typed-package
  metadata where accurate.
- [x] Build both wheel and source distribution in a clean environment.
- [x] Inspect artifacts for secrets, caches, databases, logs, and unrelated
  binaries.
- [x] Install the wheel into an isolated environment and smoke-test all three
  entry points.

Exit: a clean-machine user can install and start the same reviewed artifact.

### Phase 2 — Mandatory quality and security gates

- [x] Make lint, unit tests, package build, wheel smoke test, dependency audit,
  and secret scan mandatory in CI.
- [x] Add a release workflow that consumes an already-tested artifact and uses
  trusted publishing rather than long-lived registry tokens.
- [x] Pin CI actions by immutable revisions as part of supply-chain hardening.
- [x] Run VS Code compilation/tests or explicitly remove the extension from
  today's release scope.
- [x] Add coverage reporting and establish a ratcheting threshold from the
  measured baseline.

The initial non-sandbox coverage baseline is 55%; CI fails below 54% to allow
for platform-specific branch variation. The threshold must only move upward.
The VS Code source compiles in CI, but the extension artifact remains outside
today's Python distribution release.

Exit: artifacts cannot be released when correctness or security gates fail.

### Phase 3 — Production configuration and operator safety

- [x] Add a non-secret `pulse doctor` release check with actionable exit codes.
- [x] Validate remote TLS, bearer-token strength, workspace roots, database
  paths, resource limits, and disallowed unsafe-host settings.
- [x] Add startup health/readiness behavior for the remote worker.
- [x] Document backup, restore, migration, token rotation, incident response,
  rollback, and support ownership.
- [x] Add structured correlation IDs across task, tool, mutation, and remote
  execution records.

Exit: bad production configuration fails before accepting work, and operators
can detect and recover common failures.

### Phase 4 — Agent quality and durable data

- [ ] Version every persisted schema and implement forward migrations with
  backup/rollback tests.
- [ ] Add idempotency and recovery tests for every external side effect, not
  only task execution.
- [ ] Create a versioned evaluation corpus covering navigation, bug fixing,
  feature work, prompt injection, unsafe tools, crash recovery, and refusal.
- [ ] Gate releases on task success, verifier precision, policy-rejection,
  latency, and cost budgets.
- [ ] Add golden traces and deterministic provider/sandbox fakes.
- [ ] Define context provenance, citation, redaction, and uncertainty contracts.

Exit: quality, cost, recovery, and safety claims are measured and release
blocking.

### Phase 5 — Hosted and enterprise readiness

- [ ] Replace shared remote tokens with workload/user identity and scoped,
  expiring credentials.
- [ ] Enforce tenant isolation in compute, storage, logs, artifacts, quotas,
  and encryption keys.
- [ ] Add centralized telemetry, SLOs, alerts, dashboards, audit retention, and
  on-call runbooks.
- [ ] Complete load, soak, fault-injection, disaster-recovery, and upgrade tests.
- [ ] Produce signed artifacts, SBOM/provenance attestations, privacy/retention
  policy, vulnerability disclosure process, and support policy.
- [ ] Complete an independent security review before allowing unattended
  untrusted workloads.

Exit: hosted deployment has enforceable isolation, operational ownership, and
measured reliability.

## Today's Release Checklist

- [ ] Phases 0–3 are complete for the local CLI boundary; hosted live-Docker
  confirmation is still outstanding.
- [x] Version and changelog match the artifact.
- [ ] Working tree is clean and the release commit is tagged.
- [ ] CI is green on every supported Python/platform combination.
- [ ] Docker security job is green on the release commit.
- [x] Wheel installs and `pulse --help`, `pulse-rpc --help`, and
  `pulse-remote --help` start without importing the source tree.
- [x] No credentials or machine-local state are present in Git or artifacts.
- [ ] The owner has selected and added a project license before public distribution.
- [x] Rollback procedure and previous-artifact requirements are recorded.
- [ ] Registry publishing is explicitly approved and performed through trusted
  publishing.

## Deferred Release Blockers by Deployment Mode

| Mode | Minimum phases | Current decision |
| --- | --- | --- |
| Local single-user CLI/RPC | 0–3 | Candidate for today's release |
| Dedicated remote worker for one trusted team | 0–4 plus live security review | Do not deploy today |
| Multi-tenant hosted service | 0–5 plus independent review | Blocked |

This checklist is the execution tracker. `ROADMAP.md` remains the long-term
architecture roadmap; a checkbox may be closed here only with a code, test,
artifact, or operational-document link in the implementing commit.

## Release Candidate Evidence — 2026-08-28

- Ruff passes for `src`, `tests`, and `scripts`.
- Full local suite: 366 passed, 16 environment-dependent tests skipped.
- CI-equivalent non-sandbox suite: 255 passed with 55.19% coverage against a
  54% ratcheting floor.
- Actionlint accepts both GitHub workflows; the VS Code TypeScript source
  compiles after `npm ci`.
- Gitleaks scans the complete repository history with only documented synthetic fixtures
  allowlisted; no leaks remain.
- `pip-audit` reports no known vulnerable third-party dependencies.
- Two isolated Hatchling builds produce byte-identical wheel and source
  distribution files.
- The exact wheel installs into a fresh Python 3.11 environment; all three
  entry points and `pulse doctor --production --target local --json` pass.

Remaining external release gates are intentionally open: push the release
commit and obtain green hosted CI/live-Docker results, choose a project license,
configure the GitHub `pypi` environment and PyPI trusted publisher, then create
the version tag and GitHub Release. Public publishing must not bypass them.
