from __future__ import annotations

from pathlib import Path

from pulse.ci.github_client import GitHubClient


class CIRunner:
    """Headless CI engine for GitHub Actions.

    It downloads a PR diff, optionally runs analysis (e.g., PatchVerifier),
    and posts a summary comment to the PR.
    """

    def __init__(self, client: GitHubClient, workspace: Path) -> None:
        self.client = client
        self.workspace = workspace

    async def run_pr(self, pr_number: int) -> str:
        """Process a pull request and post a comment.

        Returns the comment body that was posted.
        """
        diff = await self.client.get_pr_diff(pr_number)
        # Placeholder for analysis – in real implementation we would invoke AutonomousLoop etc.
        comment_body = f"Processed PR #{pr_number}. Diff size: {len(diff)} characters."
        await self.client.post_pr_comment(pr_number, comment_body)
        return comment_body
