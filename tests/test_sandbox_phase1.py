"""Unit tests for Phase 1 of Pulse Sandbox: Policy Engine, Path Validator, Secrets Scrubber, and Audit Logger."""

import json
from pathlib import Path

import pytest

from pulse.sandbox.audit import StructuredAuditLogger
from pulse.sandbox.path_validator import PathValidationError, PathValidator
from pulse.sandbox.policy import ActionType, PolicyDecision, PolicyRule, SandboxPolicy
from pulse.sandbox.secrets import SecretScrubber

# ---------------------------------------------------------------------------
# 1. Policy Engine Tests
# ---------------------------------------------------------------------------


def test_sandbox_policy_defaults():
    policy = SandboxPolicy()
    assert policy.evaluate(ActionType.READ) == PolicyDecision.ALLOW
    assert policy.evaluate(ActionType.WRITE) == PolicyDecision.DENY
    assert policy.evaluate(ActionType.DELETE) == PolicyDecision.DENY
    assert policy.evaluate(ActionType.SHELL) == PolicyDecision.DENY
    assert policy.evaluate(ActionType.GIT) == PolicyDecision.ASK
    assert policy.evaluate(ActionType.NETWORK) == PolicyDecision.DENY


def test_sandbox_policy_rule_precedence():
    policy = SandboxPolicy()
    # General rule: write is ask
    policy.add_rule(PolicyRule(action="write", target_pattern="*.tmp", decision=PolicyDecision.ALLOW))
    policy.add_rule(PolicyRule(action="write", target_pattern="secrets/*", decision=PolicyDecision.DENY))

    assert policy.evaluate("write", "foo.tmp") == PolicyDecision.ALLOW
    assert policy.evaluate("write", "secrets/env.json") == PolicyDecision.DENY
    assert policy.evaluate("write", "src/main.py") == PolicyDecision.DENY


def test_sandbox_policy_inheritance():
    parent = SandboxPolicy(default_decisions={"shell": PolicyDecision.DENY})
    child = SandboxPolicy(parent_policy=parent)

    assert child.evaluate("shell") == PolicyDecision.DENY

    child.add_rule(PolicyRule(action="shell", target_pattern="pytest*", decision=PolicyDecision.ALLOW))
    assert child.evaluate("shell", "pytest tests/") == PolicyDecision.ALLOW
    assert child.evaluate("shell", "rm -rf /") == PolicyDecision.DENY


def test_sandbox_policy_json_roundtrip(tmp_path: Path):
    policy = SandboxPolicy()
    policy.add_rule(PolicyRule(action="read", target_pattern="*.json", decision=PolicyDecision.ALLOW))

    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy.to_dict()), encoding="utf-8")

    loaded = SandboxPolicy.from_file(policy_file)
    assert loaded.evaluate("read", "config.json") == PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# 2. Path Validator Tests
# ---------------------------------------------------------------------------


def test_path_validator_workspace_containment(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    validator = PathValidator(workspace)

    valid_file = workspace / "src" / "main.py"
    valid_file.parent.mkdir(parents=True)
    valid_file.write_text("print('hello')", encoding="utf-8")

    resolved = validator.validate_path("src/main.py")
    assert resolved == valid_file.resolve()
    assert validator.is_inside_workspace("src/main.py")


def test_path_validator_traversal_prevention(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    validator = PathValidator(workspace)

    with pytest.raises(PathValidationError) as exc_info:
        validator.validate_path("../../../etc/passwd")
    assert "outside workspace boundary" in str(exc_info.value)


def test_path_validator_allowed_external_reads(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_docs = tmp_path / "shared_docs"
    external_docs.mkdir()

    validator = PathValidator(workspace, allowed_external_reads=[external_docs])

    external_file = external_docs / "guide.md"
    external_file.write_text("Doc", encoding="utf-8")

    # Disallowed by default
    with pytest.raises(PathValidationError):
        validator.validate_path(external_file, allow_read_only_external=False)

    # Allowed when explicitly permitted
    resolved = validator.validate_path(external_file, allow_read_only_external=True)
    assert resolved == external_file.resolve()


# ---------------------------------------------------------------------------
# 3. Secret Protection Tests
# ---------------------------------------------------------------------------


def test_secret_scrubber_exact_values():
    scrubber = SecretScrubber(secrets=["my_super_secret_token_12345"])
    text = "Connecting with token my_super_secret_token_12345 to server"
    cleaned = scrubber.redact(text)
    assert "my_super_secret_token_12345" not in cleaned
    assert "[REDACTED_SECRET]" in cleaned


def test_secret_scrubber_builtin_patterns():
    scrubber = SecretScrubber()

    # SSH key
    ssh_key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
    assert scrubber.redact(ssh_key) == "[REDACTED_SECRET]"

    # OpenAI API Key
    openai_key = "sk-proj-1234567890abcdef1234567890abcdef"
    redacted_openai = scrubber.redact(f"KEY = {openai_key}")
    assert openai_key not in redacted_openai

    # Bearer Token
    # Bearer Token — the hardened scrubber matches the key:value pattern
    bearer = "auth_token: secret_bearer_token_xyz_12345"
    assert "secret_bearer_token_xyz_12345" not in scrubber.redact(bearer)


# ---------------------------------------------------------------------------
# 4. Audit Logger Tests
# ---------------------------------------------------------------------------


def test_structured_audit_logger(tmp_path: Path):
    log_file = tmp_path / "audit.jsonl"
    scrubber = SecretScrubber(secrets=["secret_key_999"])
    logger = StructuredAuditLogger(log_file, scrubber=scrubber)

    logger.record(
        action="shell-exec",
        target="python test.py --token secret_key_999",
        decision="allow",
        exit_code=0,
        duration_ms=12.5,
        detail="Ran test command with secret_key_999",
    )

    assert len(logger.entries) == 1
    last = logger.last_entry()
    assert last is not None
    assert last.redacted is True
    assert "secret_key_999" not in last.target
    assert "secret_key_999" not in last.detail

    content = log_file.read_text(encoding="utf-8")
    assert "secret_key_999" not in content
    parsed = json.loads(content.strip())
    assert parsed["action"] == "shell-exec"
    assert parsed["decision"] == "allow"
