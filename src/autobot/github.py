from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from autobot.models import Issue, IssueComment

DEFAULT_BRANCHES = {"main", "master", "trunk", "develop"}


class GitHubError(RuntimeError):
    pass


class GitHubIssueTracker:
    def __init__(self, token: str | None, agent_login: str | None) -> None:
        self.token = token
        self.agent_login = agent_login
        self._last_response_headers: dict[str, str] = {}

    def list_actionable(self, repo: str) -> list[int]:
        if not self.agent_login:
            issues = self._request("GET", f"/repos/{repo}/issues?state=open&labels=agent-ready")
            return [int(item["number"]) for item in issues if "pull_request" not in item]

        numbers: set[int] = set()
        for qualifier in (f"mentions:{self.agent_login}", f"assignee:{self.agent_login}"):
            query = urllib.parse.quote(f"repo:{repo} is:issue is:open {qualifier}")
            data = self._request("GET", f"/search/issues?q={query}")
            for item in data.get("items", []):
                if "pull_request" not in item:
                    numbers.add(int(item["number"]))
        return sorted(numbers)

    def get(self, repo: str, issue_number: int) -> Issue:
        issue = self._request("GET", f"/repos/{repo}/issues/{issue_number}")
        comments = self._request_pages(f"/repos/{repo}/issues/{issue_number}/comments")
        return Issue(
            repo=repo,
            number=int(issue["number"]),
            title=issue.get("title") or "",
            body=issue.get("body") or "",
            author=issue.get("user", {}).get("login") or "unknown",
            labels=[label.get("name", "") for label in issue.get("labels", [])],
            comments=[
                IssueComment(
                    id=int(comment["id"]),
                    author=comment.get("user", {}).get("login") or "unknown",
                    body=comment.get("body") or "",
                    created_at=comment.get("created_at") or "",
                )
                for comment in comments
            ],
        )

    def _request_pages(self, path: str) -> list[Any]:
        first = self._request("GET", f"{path}?per_page=100&page=1")
        if not isinstance(first, list):
            raise GitHubError(f"GitHub pagination expected a list for {path}")
        page = _last_link_page(self._last_response_headers.get("Link", ""))
        if page is None and len(first) == 100:
            page = 2
        if page is None or page == 1:
            return first
        last = self._request("GET", f"{path}?per_page=100&page={page}")
        if not isinstance(last, list):
            raise GitHubError(f"GitHub pagination expected a list for {path}")
        return _dedupe_by_id([*first, *last])

    def comment(self, repo: str, issue_number: int, text: str) -> int:
        data = self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            {"body": text},
        )
        return int(data["id"])

    def set_label(self, repo: str, issue_number: int, label: str) -> None:
        self._request("POST", f"/repos/{repo}/issues/{issue_number}/labels", {"labels": [label]})

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"https://api.github.com{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                self._last_response_headers = dict(response.headers.items())
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise GitHubError(f"GitHub {method} {path} failed: {exc.code} {payload}") from exc


def _last_link_page(link: str) -> int | None:
    for part in link.split(","):
        if 'rel="last"' not in part:
            continue
        url = part[part.find("<") + 1 : part.find(">")]
        query = urllib.parse.urlparse(url).query
        page = urllib.parse.parse_qs(query).get("page", [None])[0]
        return int(page) if page else None
    return None


def _dedupe_by_id(items: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    deduped: list[Any] = []
    for item in items:
        key = item.get("id") if isinstance(item, dict) else id(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


class GitHubGitHost:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def clone(self, repo: str, target_dir: Path) -> None:
        if target_dir.exists() and (target_dir / ".git").exists():
            default_branch = self._default_branch(repo)
            self._git(target_dir, self._auth_config() + ["fetch", "origin", "--prune"])
            self._git(
                target_dir,
                ["checkout", "-B", default_branch, f"origin/{default_branch}"],
            )
            self._git(target_dir, ["reset", "--hard", f"origin/{default_branch}"])
            self._git(target_dir, ["clean", "-fd"])
            return
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        cmd = self._auth_git_prefix() + ["clone", f"https://github.com/{repo}.git", str(target_dir)]
        self._run(cmd)

    def create_branch(self, repo_dir: Path, branch: str) -> None:
        if branch in DEFAULT_BRANCHES:
            raise GitHubError(f"refusing to work on protected default-like branch: {branch}")
        self._git(repo_dir, ["checkout", "-B", branch])

    def current_diff(self, repo_dir: Path) -> str:
        stat = self._git(repo_dir, ["diff", "--stat", "HEAD"])
        diff = self._git(repo_dir, ["diff", "HEAD"])
        return stat + "\n" + diff

    def commit_all(self, repo_dir: Path, message: str) -> bool:
        self._git(repo_dir, ["add", "-A"])
        quiet = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if quiet.returncode == 0:
            return False
        self._git(repo_dir, ["commit", "-m", message])
        return True

    def push(self, repo: str, repo_dir: Path, branch: str) -> None:
        if branch in DEFAULT_BRANCHES:
            raise GitHubError(f"refusing to push protected default-like branch: {branch}")
        self._git(repo_dir, self._auth_config() + ["push", "origin", branch])

    def open_draft_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        default_branch = self._default_branch(repo)
        data = GitHubIssueTracker(self.token, None)._request(
            "POST",
            f"/repos/{repo}/pulls",
            {
                "title": title,
                "head": branch,
                "base": default_branch,
                "body": body,
                "draft": True,
            },
        )
        return data["html_url"]

    def ci_status(self, repo: str, branch: str) -> dict:
        tracker = GitHubIssueTracker(self.token, None)
        encoded = urllib.parse.quote(branch, safe="")
        try:
            status = tracker._request("GET", f"/repos/{repo}/commits/{encoded}/status")
        except GitHubError as exc:
            return {"state": "unknown", "error": str(exc)}
        return {"state": status.get("state"), "statuses": status.get("statuses", [])}

    def _default_branch(self, repo: str) -> str:
        data = GitHubIssueTracker(self.token, None)._request("GET", f"/repos/{repo}")
        return data.get("default_branch") or "main"

    def _auth_git_prefix(self) -> list[str]:
        return ["git", *self._auth_config()]

    def _auth_config(self) -> list[str]:
        if not self.token:
            return []
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: bearer {self.token}"]

    def _git(self, repo_dir: Path, args: list[str]) -> str:
        return self._run(["git", *args], cwd=repo_dir)

    def _run(self, cmd: list[str], cwd: Path | None = None) -> str:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            raise GitHubError(message or "git command failed")
        return result.stdout.strip()
