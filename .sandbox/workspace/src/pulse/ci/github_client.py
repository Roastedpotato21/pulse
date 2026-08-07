from __future__ import annotations

import os
from typing import Any

import httpx


class GitHubClient:
    """Async wrapper around the GitHub REST API for CI operations."""

    def __init__(self, token: str | None = None, repository: str | None = None) -> None:
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise RuntimeError("GitHub token not provided via argument or GITHUB_TOKEN env var")
        self.repository = repository or os.getenv("GITHUB_REPOSITORY")
        if not self.repository:
            raise RuntimeError("GitHub repository not provided via argument or GITHUB_REPOSITORY env var")
        self.owner, self.repo = self.repository.split('/')
        self.base_url = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self.client = httpx.AsyncClient(headers=self.headers, timeout=30.0)

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{endpoint}"
        response = await self.client.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    async def get_pr_diff(self, pr_number: int) -> str:
        """Return the unified diff of a pull request as a string."""
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/diff"
        resp = await self._request(
            "GET",
            endpoint,
            headers={**self.headers, "Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    async def post_pr_comment(self, pr_number: int, body: str) -> dict[str, Any]:
        """Create a top‑level comment on the PR."""
        endpoint = f"/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments"
        payload = {"body": body}
        resp = await self._request("POST", endpoint, json=payload)
        return resp.json()

    async def post_inline_review(self, pr_number: int, path: str, line: int, body: str) -> dict[str, Any]:
        """Create an inline review comment on a specific file/line.

        GitHub expects a review object with an array of comments. For simplicity we create a
        single‑comment review using the "COMMENT" event.
        """
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/reviews"
        review_payload = {
            "event": "COMMENT",
            "body": body,
            "comments": [{"path": path, "position": line, "body": body}],
        }
        resp = await self._request("POST", endpoint, json=review_payload)
        return resp.json()

    async def close(self) -> None:
        await self.client.aclose()
