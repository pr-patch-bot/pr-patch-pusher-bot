#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from typing import Any

import httpx


def _req_headers_github(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _req_headers_codeberg(token: str) -> dict[str, str]:
    return {"Authorization": f"token {token}"}


def _print_comment(prefix: str, c: dict[str, Any]) -> None:
    cid = c.get("id")
    path = c.get("path")
    position = c.get("position")
    line = c.get("line")
    original_line = c.get("original_line")
    commit_id = c.get("commit_id")
    created_at = c.get("created_at")
    in_reply_to = c.get("in_reply_to") or c.get("in_reply_to_id") or c.get("reply")
    html_url = c.get("html_url")
    user = (c.get("user") or {}).get("login")
    print(
        f"{prefix} id={cid} user={user} path={path} line={line} original_line={original_line} "
        f"position={position} in_reply_to={in_reply_to} commit_id={commit_id} created_at={created_at} url={html_url}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codeberg-repo", required=True, help="owner/repo on Codeberg")
    ap.add_argument("--codeberg-pr", type=int, required=True)
    ap.add_argument("--github-repo", required=True, help="owner/repo on GitHub")
    ap.add_argument("--github-pr", type=int, required=True)
    args = ap.parse_args()

    gh_token = os.environ.get("GITHUB_TOKEN") or ""
    cb_token = os.environ.get("CODEBERG_TOKEN") or ""
    if not gh_token:
        raise SystemExit("Missing GITHUB_TOKEN in env")
    if not cb_token:
        raise SystemExit("Missing CODEBERG_TOKEN in env")

    cb_owner, cb_name = args.codeberg_repo.split("/", 1)
    gh_owner, gh_name = args.github_repo.split("/", 1)

    async with httpx.AsyncClient(timeout=30) as client:
        # GitHub PR details
        r = await client.get(
            f"https://api.github.com/repos/{gh_owner}/{gh_name}/pulls/{args.github_pr}",
            headers=_req_headers_github(gh_token),
        )
        r.raise_for_status()
        gh_pr = r.json()
        print("GitHub PR:")
        print(f"  html_url={gh_pr.get('html_url')}")
        print(f"  base.ref={((gh_pr.get('base') or {}).get('ref'))}")
        print(f"  head.ref={((gh_pr.get('head') or {}).get('ref'))}")
        print(f"  head.sha={((gh_pr.get('head') or {}).get('sha'))}")

        # GitHub review comments
        print("GitHub review comments:")
        page = 1
        while True:
            r = await client.get(
                f"https://api.github.com/repos/{gh_owner}/{gh_name}/pulls/{args.github_pr}/comments",
                headers=_req_headers_github(gh_token),
                params={"page": page, "per_page": 100},
            )
            r.raise_for_status()
            items = r.json() or []
            if not items:
                break
            for c in items:
                _print_comment("  GH", c)
            if len(items) < 100:
                break
            page += 1

        # Codeberg reviews
        print("Codeberg reviews + review comments:")
        page = 1
        while True:
            r = await client.get(
                f"https://codeberg.org/api/v1/repos/{cb_owner}/{cb_name}/pulls/{args.codeberg_pr}/reviews",
                headers=_req_headers_codeberg(cb_token),
                params={"page": page, "limit": 50},
            )
            r.raise_for_status()
            reviews = r.json() or []
            if not reviews:
                break
            for review in reviews:
                rid = review.get("id")
                if not isinstance(rid, int):
                    continue
                print(f"  Review id={rid} type={review.get('type')} submitted_at={review.get('submitted_at')}")
                cr = await client.get(
                    f"https://codeberg.org/api/v1/repos/{cb_owner}/{cb_name}/pulls/{args.codeberg_pr}/reviews/{rid}/comments",
                    headers=_req_headers_codeberg(cb_token),
                )
                cr.raise_for_status()
                rcomments = cr.json() or []
                for c in rcomments:
                    _print_comment("    CB", c)
            if len(reviews) < 50:
                break
            page += 1


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

