from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from autobot.diffing import render_untracked_diff
from autobot.models import Issue, IssueComment
from autobot.scanner import redact_secret_like_values

DEFAULT_BRANCHES = {"main", "master", "trunk", "develop"}


class GitHubError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubIssueTracker:
    def __init__(self, token: str | None, agent_login: str | None) -> None:
        self.token = token
        self.agent_login = agent_login
        self._last_response_headers: dict[str, str] = {}

    def list_actionable(self, repo: str) -> list[int]:
        if not self.agent_login:
            issues = self._request_all_list_pages(
                f"/repos/{repo}/issues?state=open&labels=agent-ready"
            )
            return [int(item["number"]) for item in issues if "pull_request" not in item]

        numbers: set[int] = set()
        for qualifier in (f"mentions:{self.agent_login}", f"assignee:{self.agent_login}"):
            query = urllib.parse.quote(f"repo:{repo} is:issue is:open {qualifier}")
            for item in self._request_all_search_items(f"/search/issues?q={query}"):
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

    def _request_all_list_pages(self, path: str) -> list[Any]:
        items: list[Any] = []
        for page in self._page_numbers():
            data = self._request("GET", _page_path(path, page))
            if not isinstance(data, list):
                raise GitHubError(f"GitHub pagination expected a list for {path}")
            items.extend(data)
            if self._last_page_reached(page, len(data)):
                break
        return _dedupe_by_id(items)

    def _request_all_search_items(self, path: str) -> list[Any]:
        items: list[Any] = []
        for page in self._page_numbers():
            data = self._request("GET", _page_path(path, page))
            page_items = data.get("items", []) if isinstance(data, dict) else None
            if not isinstance(page_items, list):
                raise GitHubError(f"GitHub search pagination expected items for {path}")
            items.extend(page_items)
            if self._last_page_reached(page, len(page_items)):
                break
        return _dedupe_by_id(items)

    def _page_numbers(self):
        page = 1
        while True:
            yield page
            page += 1

    def _last_page_reached(self, page: int, count: int) -> bool:
        last_page = _last_link_page(self._last_response_headers.get("Link", ""))
        if last_page is not None:
            return page >= last_page
        return count < 100

    def comment(self, repo: str, issue_number: int, text: str) -> int:
        data = self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_number}/comments",
            {"body": redact_secret_like_values(text)},
        )
        return int(data["id"])

    def set_label(self, repo: str, issue_number: int, label: str) -> None:
        try:
            self._request(
                "POST", f"/repos/{repo}/issues/{issue_number}/labels", {"labels": [label]}
            )
        except GitHubError as exc:
            if exc.status_code != 422:
                raise
            try:
                self._request(
                    "POST",
                    f"/repos/{repo}/labels",
                    {"name": label, "color": "ededed"},
                )
            except GitHubError as create_exc:
                if create_exc.status_code != 422:
                    raise
            self._request(
                "POST", f"/repos/{repo}/issues/{issue_number}/labels", {"labels": [label]}
            )

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"https://api.github.com{path}"
        data = json.dumps(_sanitize_body(body)).encode("utf-8") if body is not None else None
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
            message = redact_secret_like_values(
                f"GitHub {method} {path} failed: {exc.code} {payload}"
            )
            raise GitHubError(message, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            message = redact_secret_like_values(f"GitHub {method} {path} failed: {exc.reason}")
            raise GitHubError(message) from exc


def _last_link_page(link: str) -> int | None:
    for part in link.split(","):
        if 'rel="last"' not in part:
            continue
        url = part[part.find("<") + 1 : part.find(">")]
        query = urllib.parse.urlparse(url).query
        page = urllib.parse.parse_qs(query).get("page", [None])[0]
        return int(page) if page else None
    return None


def _page_path(path: str, page: int) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}per_page=100&page={page}"


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


def _sanitize_body(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_body(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_body(item) for item in value]
    if isinstance(value, str):
        return redact_secret_like_values(value)
    return value


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
            self._git(target_dir, ["clean", "-fdx"])
            return
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        cmd = self._auth_git_prefix() + ["clone", f"https://github.com/{repo}.git", str(target_dir)]
        self._run(cmd)

    def create_branch(self, repo_dir: Path, branch: str) -> None:
        if _is_default_like_branch(branch):
            raise GitHubError(f"refusing to work on protected default-like branch: {branch}")
        self._git(repo_dir, ["checkout", "-B", branch])

    def current_diff(self, repo_dir: Path) -> str:
        stat = self._git(repo_dir, ["diff", "--stat", "HEAD"])
        diff = self._git(repo_dir, ["diff", "HEAD"])
        untracked = self._untracked_diff(repo_dir)
        return "\n".join(part for part in (stat, diff, untracked) if part)

    def _untracked_diff(self, repo_dir: Path) -> str:
        paths = self._git(repo_dir, ["ls-files", "--others", "--exclude-standard"]).splitlines()
        return render_untracked_diff(repo_dir, paths)

    def commit_all(self, repo_dir: Path, message: str, paths: list[str] | None = None) -> bool:
        add_args = ["add", "-A"] if paths is None else ["add", "-A", "--", *paths]
        self._git(repo_dir, add_args)
        quiet = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if quiet.returncode == 0:
            return False
        if quiet.returncode > 1:
            message = redact_secret_like_values((quiet.stderr or quiet.stdout).strip())
            raise GitHubError(message or "git diff failed")
        self._git(repo_dir, ["commit", "-m", message])
        return True

    def push(self, repo: str, repo_dir: Path, branch: str) -> None:
        if _is_default_like_branch(branch):
            raise GitHubError(f"refusing to push protected default-like branch: {branch}")
        self._git(repo_dir, self._auth_config() + ["push", "origin", branch])

    def open_draft_pr(self, repo: str, branch: str, title: str, body: str) -> str:
        if _is_default_like_branch(branch):
            raise GitHubError(f"refusing to open PR from protected default-like branch: {branch}")
        default_branch = self._default_branch(repo)
        tracker = GitHubIssueTracker(self.token, None)
        try:
            data = tracker._request(
                "POST",
                f"/repos/{repo}/pulls",
                {
                    "title": redact_secret_like_values(title),
                    "head": branch,
                    "base": default_branch,
                    "body": redact_secret_like_values(body),
                    "draft": True,
                },
            )
        except GitHubError as exc:
            if exc.status_code != 422:
                raise
            existing_url = self._existing_draft_pr_url(tracker, repo, branch, default_branch)
            if existing_url:
                return existing_url
            raise
        return data["html_url"]

    def ci_status(self, repo: str, branch: str) -> dict:
        tracker = GitHubIssueTracker(self.token, None)
        encoded = urllib.parse.quote(branch, safe="")
        errors: list[str] = []
        status: dict[str, Any] = {}
        checks: dict[str, Any] = {}
        try:
            status = tracker._request("GET", f"/repos/{repo}/commits/{encoded}/status")
        except GitHubError as exc:
            errors.append(str(exc))
        try:
            checks = tracker._request("GET", f"/repos/{repo}/commits/{encoded}/check-runs")
        except GitHubError as exc:
            errors.append(str(exc))
        statuses = status.get("statuses", []) if isinstance(status, dict) else []
        check_runs = checks.get("check_runs", []) if isinstance(checks, dict) else []
        result = {
            "state": _combined_ci_state(status.get("state"), statuses, check_runs, errors),
            "statuses": statuses,
            "check_runs": [_summarize_check_run(run) for run in check_runs],
        }
        if errors:
            result["errors"] = errors
        return result

    def _default_branch(self, repo: str) -> str:
        data = GitHubIssueTracker(self.token, None)._request("GET", f"/repos/{repo}")
        return data.get("default_branch") or "main"

    def _existing_draft_pr_url(
        self,
        tracker: GitHubIssueTracker,
        repo: str,
        branch: str,
        default_branch: str,
    ) -> str | None:
        owner = repo.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {"state": "open", "head": f"{owner}:{branch}", "base": default_branch}
        )
        pulls = tracker._request_all_list_pages(f"/repos/{repo}/pulls?{query}")
        for pull in pulls:
            if pull.get("draft") is True and pull.get("html_url"):
                return str(pull["html_url"])
        return None

    def _auth_git_prefix(self) -> list[str]:
        return ["git", *self._auth_config()]

    def _auth_config(self) -> list[str]:
        if not self.token:
            return []
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: bearer {self.token}"]

    def _git(self, repo_dir: Path, args: list[str]) -> str:
        return self._run(["git", *args], cwd=repo_dir)

    def _run(self, cmd: list[str], cwd: Path | None = None) -> str:
        try:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            message = redact_secret_like_values(str(exc))
            raise GitHubError(message or "git command failed") from exc
        if result.returncode != 0:
            message = redact_secret_like_values((result.stderr or result.stdout).strip())
            raise GitHubError(message or "git command failed")
        return result.stdout.strip()


def _combined_ci_state(
    legacy_state: str | None,
    statuses: list[dict],
    check_runs: list[dict],
    errors: list[str],
) -> str:
    status_state = legacy_state if statuses else None
    failed_checks = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
    if status_state in {"failure", "error"} or any(
        run.get("conclusion") in failed_checks for run in check_runs
    ):
        return "failure"
    if status_state == "pending" or any(
        run.get("status") != "completed" or not run.get("conclusion") for run in check_runs
    ):
        return "pending"
    if status_state == "success" or any(
        run.get("conclusion") in {"success", "neutral", "skipped"} for run in check_runs
    ):
        return "success"
    if errors:
        return "unknown"
    return "no_checks"


def _summarize_check_run(run: dict) -> dict:
    return {
        "name": run.get("name"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "html_url": run.get("html_url"),
    }


def _is_default_like_branch(branch: str) -> bool:
    candidates = [branch]
    for separator in (":", "/", "refs/heads/"):
        if separator in branch:
            candidates.append(branch.rsplit(separator, 1)[-1])
    return any(candidate in DEFAULT_BRANCHES for candidate in candidates)
