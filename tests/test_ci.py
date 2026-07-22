import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path
import pytest

from pulse.ci.github_client import GitHubClient
from pulse.ci.runner import CIRunner

# Helper async mock request
async def _mock_request(self, method: str, endpoint: str, **kwargs):
    if "diff" in endpoint:
        return SimpleNamespace(text="diff content", json=lambda: {"diff": "content"})
    else:
        return SimpleNamespace(text="", json=lambda: {"comment": "posted"})

@pytest.fixture
def mock_client(monkeypatch):
    # Patch the _request method
    monkeypatch.setattr(GitHubClient, "_request", _mock_request, raising=False)
    client = GitHubClient(token="dummy", repository="owner/repo")
    return client

def test_github_client_methods(mock_client):
    # Test get_pr_diff
    diff = asyncio.run(mock_client.get_pr_diff(1))
    assert diff == "diff content"
    # Test post_pr_comment returns json dict
    result = asyncio.run(mock_client.post_pr_comment(1, "test comment"))
    assert isinstance(result, dict)
    assert result.get("comment") == "posted"

def test_cirunner_run_pr(mock_client, tmp_path):
    runner = CIRunner(mock_client, workspace=Path(tmp_path))
    comment = asyncio.run(runner.run_pr(5))
    assert "Processed PR #5" in comment
    assert "Diff size: 12" in comment  # "diff content" length is 12

def test_cli_ci_command(monkeypatch, capsys, tmp_path):
    # Replace GitHubClient with mock that records calls
    class DummyClient:
        async def get_pr_diff(self, pr_number: int):
            return "abc"
        async def post_pr_comment(self, pr_number: int, body: str):
            return {"posted": True, "body": body}

    monkeypatch.setattr("pulse.ci.github_client.GitHubClient", DummyClient)
    # Simulate CLI arguments
    monkeypatch.setattr(sys, "argv", ["pulse", "ci", "--pr", "3"])
    # Import and run main
    from pulse.cli import main
    main()
    captured = capsys.readouterr()
    assert "Processed PR #3" in captured.out
    assert "Diff size: 3" in captured.out
