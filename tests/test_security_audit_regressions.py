from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pulse.audit import AuditLog
from pulse.auth import AuthError, SecureTokenStore, TokenSet, UserProfile
from pulse.config import ModelConfig, load_env_file
from pulse.conversations import ConversationManager
from pulse.memory import LongTermMemory
from pulse.mutations import MutationTracker
from pulse.providers.openai import OpenAIProvider
from pulse.repository import RepositoryIndex
from pulse.rpc import JsonRpcDispatcher, _authorized_rpc_header, _valid_rpc_token
from pulse.sandbox.process import ProcessResult
from pulse.sandbox.remote.models import SubmitExecutionRequest
from pulse.sandbox.remote.server import _extract_remote_artifact
from pulse.sandbox.remote.worker import RemoteWorker
from pulse.sandbox.secrets import SecretScrubber
from pulse.subprocesses import isolated_subprocess_environment
from pulse.telemetry import TelemetryLogger
from pulse.verification import VerificationEngine

# Controlled synthetic fixtures. The final audit scan permits these values only
# in this file and rejects them in every generated/runtime artifact.
AUDIT_SECRET = "PULSE_" + "AUDIT_SECRET_" + "7f13c9"
AUDIT_API_KEY = "PULSE_" + "AUDIT_API_KEY_" + "8d21e4"
INTERNAL_PROMPT = "PULSE_" + "INTERNAL_PROMPT_" + "51ac77"
RELEASE_SECRET = "PULSE_" + "RELEASE_SECRET_" + "7F13C9"
INTERNAL_TRACE = "PULSE_" + "INTERNAL_TRACE_" + "51AC77"


def test_redactor_handles_serialized_variants_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubber = SecretScrubber([AUDIT_SECRET])
    serialized = (
        AUDIT_SECRET.lower()
        + " "
        + AUDIT_SECRET.replace("_", "%5F")
        + " secret="
        + AUDIT_API_KEY
        + " "
        + INTERNAL_PROMPT
        + " "
        + RELEASE_SECRET
        + " "
        + INTERNAL_TRACE
    )
    redacted = scrubber.redact(serialized)
    assert AUDIT_SECRET.lower() not in redacted.lower()
    assert "%5F" not in redacted
    assert AUDIT_API_KEY not in redacted
    assert INTERNAL_PROMPT not in redacted
    assert RELEASE_SECRET not in redacted
    assert INTERNAL_TRACE not in redacted

    def explode(_text: str) -> str:
        raise RuntimeError(AUDIT_SECRET)

    monkeypatch.setattr(scrubber, "_redact_impl", explode)
    assert scrubber.redact(AUDIT_SECRET) == "[REDACTION_FAILED]"


def test_audit_telemetry_memory_and_conversations_redact_credentials(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "actions.jsonl"
    telemetry_path = tmp_path / "telemetry.jsonl"
    AuditLog(audit_path).record("question", ".", f"secret={AUDIT_SECRET}")
    telemetry = TelemetryLogger(telemetry_path)
    telemetry.add_secret(AUDIT_API_KEY)
    telemetry.log_event("failure", error=AUDIT_API_KEY)

    memory = LongTermMemory(tmp_path, secrets=[AUDIT_API_KEY])
    asyncio.run(memory.remember_task(f"secret={AUDIT_SECRET}", AUDIT_API_KEY))
    conversations = ConversationManager(tmp_path)
    conversation = conversations.create(INTERNAL_PROMPT)
    conversations.add_turn(conversation.id, "user", f"api_key={AUDIT_API_KEY}")

    persisted = b"\n".join(
        path.read_bytes()
        for path in (audit_path, telemetry_path, memory.database_path, conversations.database_path)
    )
    for marker in (AUDIT_SECRET, AUDIT_API_KEY, INTERNAL_PROMPT):
        assert marker.encode() not in persisted


def test_provider_error_body_and_rpc_exception_are_not_reflected(tmp_path: Path) -> None:
    provider = OpenAIProvider(
        ModelConfig(provider="openai", name="audit-model", temperature=0.2),
        tmp_path / ".env",
        api_key=AUDIT_API_KEY,
    )
    request = httpx.Request("POST", provider.endpoint)
    response = httpx.Response(
        500,
        json={"error": {"message": AUDIT_SECRET}},
        request=request,
    )
    error = httpx.HTTPStatusError("provider failure", request=request, response=response)
    assert AUDIT_SECRET not in provider._safe_error_detail(error)

    class FailingAgent:
        async def respond_remote(self, _prompt: str, _context: list[str]) -> str:
            raise RuntimeError(INTERNAL_PROMPT)

    dispatcher = JsonRpcDispatcher(SimpleNamespace(agent=FailingAgent(), tools=None))
    rpc_response = asyncio.run(
        dispatcher.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "pulse.ask",
                "params": {"prompt": "hello"},
            }
        )
    )
    assert rpc_response["error"]["message"] == "Internal Pulse error."
    assert INTERNAL_PROMPT not in json.dumps(rpc_response)


def test_local_fake_provider_receives_key_only_in_authorization_header(
    tmp_path: Path,
) -> None:
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            captured["path"] = self.path
            captured["authorization"] = self.headers.get("Authorization", "")
            captured["body"] = self.rfile.read(length).decode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = OpenAIProvider(
            ModelConfig(provider="openai", name="audit-model", temperature=0.2),
            tmp_path / ".env",
            api_key=AUDIT_API_KEY,
        )
        provider.endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        assert provider.stream_chat([{"role": "user", "content": "audit prompt"}]) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert captured["authorization"] == f"Bearer {AUDIT_API_KEY}"
    assert AUDIT_API_KEY not in captured["path"]
    assert AUDIT_API_KEY not in captured["body"]


def test_rpc_token_validation_and_authorization() -> None:
    assert _valid_rpc_token(AUDIT_API_KEY * 2)
    assert not _valid_rpc_token("replace_me")
    assert _authorized_rpc_header(f"Bearer {AUDIT_API_KEY * 2}", AUDIT_API_KEY * 2)
    assert not _authorized_rpc_header("Bearer wrong-token", AUDIT_API_KEY * 2)


def test_env_loading_and_subprocesses_do_not_inherit_repository_or_host_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable = "PULSE_AUDIT_INHERITED_VALUE"
    monkeypatch.delenv(variable, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{variable}={AUDIT_SECRET}\n", encoding="utf-8")
    assert load_env_file(env_file)[variable] == AUDIT_SECRET
    assert variable not in os.environ

    monkeypatch.setenv(variable, AUDIT_SECRET)
    assert variable not in isolated_subprocess_environment()
    command = (
        sys.executable,
        "-c",
        f"import os; print(os.environ.get('{variable}', 'missing'))",
    )
    code, stdout, stderr = asyncio.run(VerificationEngine._run_command(command, tmp_path))
    assert code == 0 and stdout.strip() == "missing" and not stderr


def test_env_loader_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside.env"
    target.write_text(f"secret={AUDIT_SECRET}\n", encoding="utf-8")
    link = tmp_path / "linked.env"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symbolic links are unavailable on this host")
    with pytest.raises(ValueError, match="symbolic-link"):
        load_env_file(link)


def test_index_and_mutation_snapshot_skip_symlinks_and_secret_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text(AUDIT_SECRET, encoding="utf-8")
    link = workspace / "linked.txt"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("Symbolic links are unavailable on this host")
    (workspace / ".env").write_text(f"API_KEY={AUDIT_API_KEY}\n", encoding="utf-8")
    (workspace / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")

    index = RepositoryIndex(workspace)
    asyncio.run(index.index())
    assert asyncio.run(index.files()) == ["safe.py"]
    assert AUDIT_SECRET not in index.index_path.read_text(encoding="utf-8")

    tracker = MutationTracker(workspace)
    with tracker.transaction():
        (workspace / "safe.py").write_text("VALUE = 2\n", encoding="utf-8")
    mutation_log = tracker.log_path.read_text(encoding="utf-8")
    assert AUDIT_SECRET not in mutation_log
    assert AUDIT_API_KEY not in mutation_log


def test_remote_request_and_archive_paths_are_bounded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="execution_id"):
        SubmitExecutionRequest(
            protocol_version="1.0",
            execution_id="../escape",
            idempotency_key="audit-request",
            command=["echo", "safe"],
        )

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        payload = AUDIT_SECRET.encode()
        member = tarfile.TarInfo("../exec-evil/leak.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    workspace = tmp_path / "tenant" / "exec"
    with pytest.raises(RuntimeError, match="artifact"):
        _extract_remote_artifact(archive_bytes.getvalue(), workspace)
    assert not workspace.exists()
    assert not (workspace.parent / "exec-evil" / "leak.txt").exists()


def test_remote_worker_redacts_streamed_output_before_logging(tmp_path: Path) -> None:
    class FakeBackend:
        async def execute(self, **kwargs: object) -> ProcessResult:
            callback = kwargs["output_callback"]
            assert callback is not None
            await callback("stdout", f"secret={AUDIT_SECRET}".encode())
            return ProcessResult(
                command=f"echo {AUDIT_SECRET}",
                exit_code=0,
                stdout=f"done {AUDIT_SECRET}",
                stderr="",
                duration_ms=1.0,
            )

    worker = RemoteWorker(tmp_path)
    worker.backend = FakeBackend()  # type: ignore[assignment]
    chunks: list[bytes] = []

    async def capture(_stream: str, data: bytes) -> None:
        chunks.append(data)

    result = asyncio.run(
        worker.execute_request(
            SubmitExecutionRequest(
                protocol_version="1.0",
                execution_id="exec-audit-1",
                idempotency_key="audit-request",
                command=["echo", AUDIT_SECRET],
                env={"AUDIT_TOKEN": AUDIT_SECRET},
            ),
            tenant_id="tenant-audit",
            output_callback=capture,
        )
    )

    assert chunks
    assert AUDIT_SECRET.encode() not in b"".join(chunks)
    assert AUDIT_SECRET not in result.command
    assert AUDIT_SECRET not in result.stdout


def test_auth_storage_fails_closed_without_plaintext_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulse import auth

    class FailingKeyring:
        @staticmethod
        def set_password(_service: str, _account: str, _payload: str) -> None:
            raise RuntimeError("unavailable")

    monkeypatch.setattr(auth, "keyring", FailingKeyring())
    store = SecureTokenStore(tmp_path)
    with pytest.raises(AuthError, match="no plaintext fallback"):
        store.store_session(
            UserProfile(email="audit@example.invalid", sub="audit"),
            TokenSet(access_token=AUDIT_SECRET),
        )
    assert not store.fallback_file.exists()
