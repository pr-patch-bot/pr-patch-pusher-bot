from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class GitCommandError(RuntimeError):
    def __init__(self, *, args: list[str], cwd: Path, output: str):
        super().__init__(f"git failed (cwd={cwd}): git {' '.join(args)}\n{output}".rstrip())
        self.args_list = args
        self.cwd = cwd
        self.output = output


def _run_git(args: list[str], *, cwd: Path) -> None:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise GitCommandError(args=args, cwd=cwd, output=proc.stdout or "")


def _run_git_with_retries(
    args: list[str],
    *,
    cwd: Path,
    attempts: int = 5,
    base_sleep_s: float = 1.0,
) -> None:
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            _run_git(args, cwd=cwd)
            return
        except GitCommandError as e:
            last_err = e
            output = (e.output or "").lower()
            retriable = any(
                needle in output
                for needle in [
                    "the requested url returned error: 502",
                    "the requested url returned error: 503",
                    "the requested url returned error: 504",
                    "connection timed out",
                    "operation timed out",
                    "could not resolve host",
                    "failed to connect",
                    "connection reset by peer",
                    "remote end hung up unexpectedly",
                ]
            )
            if not retriable or i == attempts - 1:
                raise
            time.sleep(base_sleep_s * (2**i))
        except Exception as e:
            last_err = e
            if i == attempts - 1:
                raise
            time.sleep(base_sleep_s * (2**i))
    if last_err:
        raise last_err


def _github_https_url(repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{repo}.git"


def _codeberg_https_url(clone_url: str, token: str | None) -> str:
    return clone_url


def _codeberg_push_https_url(repo: str, token: str) -> str:
    # Gitea supports HTTP basic auth with token as password.
    # Using 'oauth2' as a conventional username avoids requiring the actual account name.
    return f"https://oauth2:{token}@codeberg.org/{repo}.git"


@dataclass(frozen=True)
class RepoPaths:
    path: Path


def ensure_repo(
    *,
    working_dir: str,
    upstream_repo: str,
    fork_repo: str,
    github_token: str,
) -> RepoPaths:
    wd = Path(working_dir).expanduser().resolve()
    wd.mkdir(parents=True, exist_ok=True)
    safe = upstream_repo.replace("/", "__")
    repo_path = wd / safe
    if not repo_path.exists():
        upstream_url = f"https://github.com/{upstream_repo}.git"
        _run_git_with_retries(["clone", "--quiet", upstream_url, str(repo_path)], cwd=wd)
        _run_git_with_retries(
            ["remote", "add", "fork", _github_https_url(fork_repo, github_token)],
            cwd=repo_path,
        )
    else:
        _run_git_with_retries(
            ["remote", "set-url", "fork", _github_https_url(fork_repo, github_token)],
            cwd=repo_path,
        )
    return RepoPaths(path=repo_path)


def ensure_upstream_clone(*, working_dir: str, upstream_repo: str) -> RepoPaths:
    wd = Path(working_dir).expanduser().resolve()
    wd.mkdir(parents=True, exist_ok=True)
    safe = upstream_repo.replace("/", "__")
    repo_path = wd / safe
    if not repo_path.exists():
        upstream_url = f"https://github.com/{upstream_repo}.git"
        _run_git_with_retries(["clone", "--quiet", upstream_url, str(repo_path)], cwd=wd)
    return RepoPaths(path=repo_path)


def sync_branch(
    *,
    repo_path: Path,
    upstream_base_branch: str,
    mirror_branch: str,
    codeberg_clone_url: str,
    codeberg_ref: str,
) -> None:
    _run_git_with_retries(
        ["fetch", "--quiet", "origin", upstream_base_branch], cwd=repo_path
    )
    codeberg_url = _codeberg_https_url(codeberg_clone_url, None)
    _run_git_with_retries(["fetch", "--quiet", codeberg_url, codeberg_ref], cwd=repo_path)
    _run_git_with_retries(
        ["checkout", "--quiet", "-B", mirror_branch, "FETCH_HEAD"], cwd=repo_path
    )
    _run_git_with_retries(
        ["push", "--quiet", "--force", "fork", f"{mirror_branch}:{mirror_branch}"],
        cwd=repo_path,
    )


def sync_upstream_to_codeberg(
    *,
    working_dir: str,
    upstream_repo: str,
    upstream_branch: str,
    codeberg_repo: str,
    codeberg_branch: str,
    codeberg_token: str,
) -> None:
    repo_paths = ensure_upstream_clone(working_dir=working_dir, upstream_repo=upstream_repo)
    repo_path = repo_paths.path
    _run_git_with_retries(["fetch", "--quiet", "origin", upstream_branch], cwd=repo_path)
    try:
        _run_git_with_retries(
            ["remote", "set-url", "codeberg", _codeberg_push_https_url(codeberg_repo, codeberg_token)],
            cwd=repo_path,
        )
    except GitCommandError:
        _run_git_with_retries(
            ["remote", "add", "codeberg", _codeberg_push_https_url(codeberg_repo, codeberg_token)],
            cwd=repo_path,
        )
    _run_git_with_retries(
        ["push", "--quiet", "--force", "codeberg", f"origin/{upstream_branch}:{codeberg_branch}"],
        cwd=repo_path,
    )


def get_head_sha(*, repo_path: Path) -> str:
    out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_path))
    return out.decode("utf-8").strip()
