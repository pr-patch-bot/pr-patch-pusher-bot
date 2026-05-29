import json
import unittest

import httpx


class TestGitHubInlinePayload(unittest.IsolatedAsyncioTestCase):
    async def test_github_inline_comment_defaults_side_right(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal captured
            if request.method == "POST" and request.url.path.endswith("/pulls/8/comments"):
                captured = json.loads(request.content.decode("utf-8"))
                return httpx.Response(
                    201,
                    json={
                        "id": 123,
                        "html_url": "https://github.com/x/y/pull/8#discussion_r123",
                        "user": {"login": "bot"},
                        "body": captured.get("body") or "",
                        "path": captured.get("path"),
                        "line": captured.get("line"),
                        "position": captured.get("position"),
                    },
                )
            return httpx.Response(404, json={"message": "not found"})

        transport = httpx.MockTransport(handler)

        # Patch only within codeberg_bridge.clients module.
        import codeberg_bridge.clients as clients

        real_async_client = clients.httpx.AsyncClient

        def patched_async_client(*args, **kwargs):
            kwargs = dict(kwargs)
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        clients.httpx.AsyncClient = patched_async_client  # type: ignore[assignment]
        try:
            gh = clients.GitHubClient(token="t")
            await gh.create_review_comment(
                repo="plowsof/test-comments",
                pull_number=8,
                commit_id="deadbeef",
                path="README.md",
                position=None,
                line=13,
                body="hi",
            )
        finally:
            clients.httpx.AsyncClient = real_async_client  # type: ignore[assignment]

        self.assertEqual(captured.get("line"), 13)
        self.assertEqual(captured.get("side"), "RIGHT")
        self.assertNotIn("position", captured)


class TestThreadRootInference(unittest.TestCase):
    def test_infer_codeberg_thread_root_id_groups_by_metadata(self) -> None:
        from codeberg_bridge.app import _infer_codeberg_thread_root_id

        review_id = 1435652
        # same review_id/path/position/commit_id/diff_hunk => same thread
        comments = [
            {
                "id": 10,
                "path": "README.md",
                "position": 14,
                "commit_id": "c",
                "diff_hunk": "@@ -1 +1 @@",
                "created_at": "2026-05-30T14:28:04+02:00",
            },
            {
                "id": 11,
                "path": "README.md",
                "position": 14,
                "commit_id": "c",
                "diff_hunk": "@@ -1 +1 @@",
                "created_at": "2026-05-30T17:39:01+02:00",
            },
        ]
        self.assertEqual(
            _infer_codeberg_thread_root_id(review_id=review_id, all_review_comments=comments, comment_id=11),
            10,
        )

