import asyncio
from pathlib import Path

from pulse.git import GitIntelligence


def test_git_insight_tracks_branch_status_diff_and_commit_suggestion(tmp_path: Path) -> None:
    async def runner(arguments, workspace):
        responses = {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n", ""),
            ("branch", "--show-current"): (0, "feature/pulse\n", ""),
            ("rev-parse", "--short", "HEAD"): (0, "abc123\n", ""),
            ("status", "--porcelain=v1"): (0, " M src/pulse/git.py\n?? tests/test_git.py\n", ""),
            ("diff", "--numstat"): (0, "10\t2\tsrc/pulse/git.py\n", ""),
            ("diff", "--cached", "--numstat"): (0, "", ""),
        }
        return responses[arguments]

    insight = asyncio.run(GitIntelligence(tmp_path, runner=runner).inspect())

    assert insight.status.is_repository and insight.status.branch == "feature/pulse"
    assert insight.diff.files_changed == 2 and insight.diff.additions == 10 and insight.diff.deletions == 2
    assert insight.commit_suggestion == "feat: update 2 files"


def test_git_intelligence_handles_a_workspace_outside_a_repository(tmp_path: Path) -> None:
    async def runner(arguments, workspace):
        return 128, "", "not a git repository"

    insight = asyncio.run(GitIntelligence(tmp_path, runner=runner).inspect())

    assert not insight.status.is_repository
    assert insight.commit_suggestion is None
