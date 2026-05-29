from __future__ import annotations

from dataclasses import dataclass

import httpx
import logging
from urllib.parse import quote


log = logging.getLogger("codeberg_bridge.clients")


@dataclass(frozen=True)
class CodebergPRInfo:
    number: int
    title: str
    html_url: str
    body: str
    author: str
    head_repo_clone_url: str
    head_ref: str
    head_sha: str
    base_ref: str
    state: str


@dataclass(frozen=True)
class CodebergPRListItem:
    number: int
    author: str
    state: str


@dataclass(frozen=True)
class CodebergIssueComment:
    id: int
    html_url: str
    author: str
    body: str


class CodebergClient:
    def __init__(self, *, base_url: str, token: str | None):
        self._base_url = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"token {self._token}"}

    async def get_authenticated_user_login(self) -> str:
        if not self._token:
            raise RuntimeError("Codeberg token not configured")
        url = f"{self._base_url}/api/v1/user"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        login = data.get("login") or data.get("username")
        if not isinstance(login, str) or not login:
            raise RuntimeError("Codeberg /user response missing login")
        return login

    async def get_pull_request(self, *, repo: str, number: int) -> CodebergPRInfo:
        owner, name = repo.split("/", 1)
        url = f"{self._base_url}/api/v1/repos/{owner}/{name}/pulls/{number}"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self._headers())
            r.raise_for_status()
            data = r.json()
        return CodebergPRInfo(
            number=int(data["number"]),
            title=data["title"],
            html_url=data["html_url"],
            body=data.get("body") or "",
            author=data["user"]["login"],
            head_repo_clone_url=data["head"]["repo"]["clone_url"],
            head_ref=data["head"]["ref"],
            head_sha=data["head"]["sha"],
            base_ref=data["base"]["ref"],
            state=data.get("state") or "unknown",
        )

    async def list_pull_requests(
        self, *, repo: str, state: str = "open", page: int = 1, limit: int = 50
    ) -> list[CodebergPRListItem]:
        owner, name = repo.split("/", 1)
        url = f"{self._base_url}/api/v1/repos/{owner}/{name}/pulls"
        params = {"state": state, "page": page, "limit": limit}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self._headers(), params=params)
            r.raise_for_status()
            data = r.json()
        items: list[CodebergPRListItem] = []
        for pr in data or []:
            try:
                number = int(pr["number"])
                author = pr["user"]["login"]
                pr_state = pr.get("state") or "unknown"
            except Exception:
                continue
            if isinstance(author, str) and author:
                items.append(CodebergPRListItem(number=number, author=author, state=pr_state))
        return items

    async def update_pull_request_state(self, *, repo: str, number: int, state: str) -> None:
        owner, name = repo.split("/", 1)
        url = f"{self._base_url}/api/v1/repos/{owner}/{name}/pulls/{number}"
        payload = {"state": state}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(url, headers=self._headers(), json=payload)
            r.raise_for_status()

    async def create_issue_comment(
        self, *, repo: str, issue_number: int, body: str
    ) -> CodebergIssueComment:
        # In Gitea, pull requests are issues; PR comments use the issues comments API.
        owner, name = repo.split("/", 1)
        url = f"{self._base_url}/api/v1/repos/{owner}/{name}/issues/{issue_number}/comments"
        payload = {"body": body}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, headers=self._headers(), json=payload)
            r.raise_for_status()
            data = r.json()
        return CodebergIssueComment(
            id=int(data["id"]),
            html_url=data.get("html_url") or "",
            author=((data.get("user") or {}).get("login")) or "",
            body=data.get("body") or "",
        )

    async def list_issue_comments(
        self, *, repo: str, issue_number: int, page: int = 1, limit: int = 50
    ) -> list[CodebergIssueComment]:
        owner, name = repo.split("/", 1)
        url = f"{self._base_url}/api/v1/repos/{owner}/{name}/issues/{issue_number}/comments"
        params = {"page": page, "limit": limit}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers=self._headers(), params=params)
            r.raise_for_status()
            data = r.json()
        out: list[CodebergIssueComment] = []
        for c in data or []:
            try:
                cid = int(c["id"])
                author = c["user"]["login"]
                html_url = c.get("html_url") or ""
                body = c.get("body") or ""
            except Exception:
                continue
            if isinstance(author, str) and author:
                out.append(
                    CodebergIssueComment(id=cid, html_url=html_url, author=author, body=body)
                )
        return out


@dataclass(frozen=True)
class GitHubPR:
    number: int
    html_url: str
    state: str


@dataclass(frozen=True)
class GitHubIssueComment:
    id: int
    html_url: str
    author: str
    body: str


@dataclass(frozen=True)
class GitHubReviewComment:
    id: int
    html_url: str
    author: str
    body: str
    path: str | None
    line: int | None
    position: int | None


@dataclass(frozen=True)
class GitHubPRDetails:
    number: int
    html_url: str
    state: str
    merged_at: str | None
    merge_commit_sha: str | None


class GitHubClient:
    def __init__(self, *, token: str):
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_authenticated_user_login(self) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.github.com/user", headers=self._headers())
            r.raise_for_status()
            data = r.json()
        login = data.get("login")
        if not isinstance(login, str) or not login:
            raise RuntimeError("GitHub /user response missing login")
        return login

    async def ensure_fork(self, *, upstream_repo: str, bot_username: str) -> str:
        upstream_owner, upstream_name = upstream_repo.split("/", 1)
        login = await self.get_authenticated_user_login()
        if bot_username and bot_username != login:
            log.warning(
                "github_bot_username_mismatch",
                extra={"config_bot_username": bot_username, "token_login": login},
            )
        fork_repo = f"{login}/{upstream_name}"
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(
                f"https://api.github.com/repos/{fork_repo}", headers=self._headers()
            )
            if r.status_code == 404:
                create = await client.post(
                    f"https://api.github.com/repos/{upstream_owner}/{upstream_name}/forks",
                    headers=self._headers(),
                    json={},
                )
                create.raise_for_status()
            elif r.status_code >= 400:
                r.raise_for_status()
        return fork_repo

    async def find_pr_by_head(
        self, *, upstream_repo: str, head: str, state: str = "open"
    ) -> GitHubPR | None:
        params = {"state": state, "head": head, "per_page": 1}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.github.com/repos/{upstream_repo}/pulls",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            items = r.json()
        if not items:
            return None
        return GitHubPR(
            number=int(items[0]["number"]),
            html_url=items[0]["html_url"],
            state=items[0].get("state") or "unknown",
        )

    async def create_pr(
        self,
        *,
        upstream_repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> GitHubPR:
        payload = {"title": title, "body": body, "head": head, "base": base}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"https://api.github.com/repos/{upstream_repo}/pulls",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        return GitHubPR(
            number=int(data["number"]),
            html_url=data["html_url"],
            state=data.get("state") or "unknown",
        )

    async def update_pr_body(
        self, *, upstream_repo: str, number: int, title: str, body: str, base: str | None = None
    ) -> None:
        payload: dict[str, str] = {"title": title, "body": body}
        if base:
            payload["base"] = base
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.patch(
                f"https://api.github.com/repos/{upstream_repo}/pulls/{number}",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()

    async def update_pr_state(self, *, upstream_repo: str, number: int, state: str) -> None:
        payload = {"state": state}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.patch(
                f"https://api.github.com/repos/{upstream_repo}/pulls/{number}",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()

    async def get_pr(self, *, upstream_repo: str, number: int) -> GitHubPRDetails:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.github.com/repos/{upstream_repo}/pulls/{number}",
                headers=self._headers(),
            )
            r.raise_for_status()
            data = r.json()
        return GitHubPRDetails(
            number=int(data["number"]),
            html_url=data["html_url"],
            state=data.get("state") or "unknown",
            merged_at=data.get("merged_at"),
            merge_commit_sha=data.get("merge_commit_sha"),
        )

    async def repo_exists(self, *, repo: str) -> bool:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"https://api.github.com/repos/{repo}", headers=self._headers())
            if r.status_code == 404:
                return False
            r.raise_for_status()
        return True

    async def delete_branch(self, *, repo: str, branch: str) -> None:
        # DELETE /repos/{owner}/{repo}/git/refs/{ref}
        # branch may contain slashes; the API path must be URL-encoded.
        ref = f"heads/{branch}"
        ref_enc = quote(ref, safe="")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(
                f"https://api.github.com/repos/{repo}/git/refs/{ref_enc}",
                headers=self._headers(),
            )
            if r.status_code in {404, 422}:
                return
            r.raise_for_status()

    async def list_issue_comments(
        self, *, repo: str, issue_number: int, page: int = 1, per_page: int = 100
    ) -> list[GitHubIssueComment]:
        params = {"page": page, "per_page": per_page}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            items = r.json()
        out: list[GitHubIssueComment] = []
        for c in items or []:
            try:
                cid = int(c["id"])
                author = c["user"]["login"]
                html_url = c["html_url"]
                body = c.get("body") or ""
            except Exception:
                continue
            if isinstance(author, str) and author:
                out.append(GitHubIssueComment(id=cid, html_url=html_url, author=author, body=body))
        return out

    async def create_issue_comment(
        self, *, repo: str, issue_number: int, body: str
    ) -> GitHubIssueComment:
        payload = {"body": body}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        return GitHubIssueComment(
            id=int(data["id"]),
            html_url=data["html_url"],
            author=(data.get("user") or {}).get("login") or "",
            body=data.get("body") or "",
        )

    async def list_review_comments(
        self, *, repo: str, pull_number: int, page: int = 1, per_page: int = 100
    ) -> list[GitHubReviewComment]:
        params = {"page": page, "per_page": per_page}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/pulls/{pull_number}/comments",
                headers=self._headers(),
                params=params,
            )
            r.raise_for_status()
            items = r.json()
        out: list[GitHubReviewComment] = []
        for c in items or []:
            try:
                cid = int(c["id"])
                author = c["user"]["login"]
                html_url = c["html_url"]
                body = c.get("body") or ""
            except Exception:
                continue
            if not isinstance(author, str) or not author:
                continue
            path = c.get("path")
            line = c.get("line")
            position = c.get("position")
            out.append(
                GitHubReviewComment(
                    id=cid,
                    html_url=html_url,
                    author=author,
                    body=body,
                    path=path if isinstance(path, str) else None,
                    line=int(line) if isinstance(line, int) else None,
                    position=int(position) if isinstance(position, int) else None,
                )
            )
        return out

    async def create_review_comment_reply(
        self, *, repo: str, pull_number: int, in_reply_to: int, body: str
    ) -> GitHubReviewComment:
        payload = {"body": body, "in_reply_to": int(in_reply_to)}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo}/pulls/{pull_number}/comments",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        return GitHubReviewComment(
            id=int(data["id"]),
            html_url=data["html_url"],
            author=((data.get("user") or {}).get("login")) or "",
            body=data.get("body") or "",
            path=data.get("path") if isinstance(data.get("path"), str) else None,
            line=int(data["line"]) if isinstance(data.get("line"), int) else None,
            position=int(data["position"]) if isinstance(data.get("position"), int) else None,
        )

    async def create_review_comment(
        self,
        *,
        repo: str,
        pull_number: int,
        commit_id: str,
        path: str,
        position: int,
        body: str,
    ) -> GitHubReviewComment:
        payload = {
            "body": body,
            "commit_id": commit_id,
            "path": path,
            "position": int(position),
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"https://api.github.com/repos/{repo}/pulls/{pull_number}/comments",
                headers=self._headers(),
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        return GitHubReviewComment(
            id=int(data["id"]),
            html_url=data["html_url"],
            author=((data.get("user") or {}).get("login")) or "",
            body=data.get("body") or "",
            path=data.get("path") if isinstance(data.get("path"), str) else None,
            line=int(data["line"]) if isinstance(data.get("line"), int) else None,
            position=int(data["position"]) if isinstance(data.get("position"), int) else None,
        )
