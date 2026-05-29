from __future__ import annotations

import logging
import re

import httpx


log = logging.getLogger("codeberg_bridge.codeberg_web")

_CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')


class CodebergWebClient:
    """
    Non-API integration for Codeberg/Gitea UI routes.

    This is intentionally optional and cookie-based: it is only used to create
    true inline *reply* comments in the same thread, which the public REST API
    does not expose.
    """

    def __init__(self, *, base_url: str, cookie: str):
        self._base_url = base_url.rstrip("/")
        self._cookie = cookie

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": self._cookie,
            "User-Agent": "codeberg-bridge (web client)",
            "Accept": "*/*",
            "Origin": self._base_url,
        }

    async def _get_csrf(self, *, repo: str, pull_number: int) -> str:
        # Fetch a page that contains an _csrf token input. The PR files page includes it.
        url = f"{self._base_url}/{repo}/pulls/{pull_number}/files"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(url, headers=self._headers())
            r.raise_for_status()
            html = r.text or ""
        m = _CSRF_RE.search(html)
        if not m:
            raise RuntimeError("Unable to find _csrf token in Codeberg UI HTML")
        return m.group(1)

    async def create_inline_reply_comment(
        self,
        *,
        repo: str,
        pull_number: int,
        path: str,
        line: int,
        side: str,
        content: str,
        reply_to: int,
    ) -> None:
        """
        Create an inline reply comment via the UI route:
          POST /{repo}/pulls/{pull_number}/files/reviews/comments
        """
        csrf = await self._get_csrf(repo=repo, pull_number=pull_number)
        url = f"{self._base_url}/{repo}/pulls/{pull_number}/files/reviews/comments"

        data = {
            "origin": "timeline",
            "latest_commit_id": "",
            "side": side,
            "line": str(int(line)),
            "path": path,
            "diff_start_cid": "",
            "diff_end_cid": "",
            "diff_base_cid": "",
            "content": content,
            "reply": str(int(reply_to)),
            "_csrf": csrf,
        }

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.post(url, headers=self._headers(), data=data)
            if r.status_code >= 400:
                log.error(
                    "codeberg_web_inline_reply_failed",
                    extra={"status": r.status_code, "body": (r.text or "")[:300]},
                )
            r.raise_for_status()

