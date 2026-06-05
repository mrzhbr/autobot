from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from autobot.audit import AuditLog
from autobot.chat import IssueCommentChat
from autobot.config import Config
from autobot.github import GitHubGitHost, GitHubIssueTracker
from autobot.llm import build_llm
from autobot.pipeline import IssueProcessor
from autobot.state import StateStore


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "watch":
            return _watch(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


def _run(args: argparse.Namespace) -> int:
    config = _config(args)
    processor = _processor(config)
    result = processor.process(args.repo, int(args.issue))
    print(json.dumps(_summary(args.repo, int(args.issue), result), indent=2, sort_keys=True))
    return 0


def _watch(args: argparse.Namespace) -> int:
    config = _config(args)
    tracker = GitHubIssueTracker(config.github_token, config.agent_login)
    processor = _processor(config, tracker=tracker)
    while True:
        numbers = tracker.list_actionable(args.repo)
        if not numbers:
            print(json.dumps({"repo": args.repo, "state": "idle", "actionable": 0}))
        for number in numbers:
            result = processor.process(args.repo, number)
            print(json.dumps(_summary(args.repo, number, result), sort_keys=True))
        if args.once:
            return 0
        time.sleep(args.interval)


def _processor(config: Config, tracker: GitHubIssueTracker | None = None) -> IssueProcessor:
    store = StateStore(config.db_path)
    tracker = tracker or GitHubIssueTracker(config.github_token, config.agent_login)
    git_host = GitHubGitHost(config.github_token)
    chat = IssueCommentChat(tracker)
    llm = build_llm(config)
    audit = AuditLog(config.audit_path)
    return IssueProcessor(config, store, tracker, git_host, chat, llm, audit)


def _summary(repo: str, issue: int, result) -> dict:
    return {
        "repo": repo,
        "issue": issue,
        "state": result.state.value,
        "message": result.message,
        "pr_url": result.pr_url,
        "branch": result.branch,
        "blocked_on": result.blocked_on,
        "review_rounds": result.review_rounds,
        "files_touched": result.files_touched,
        "cost": result.cost,
    }


def _config(args: argparse.Namespace) -> Config:
    config = Config.from_env(
        root=Path.cwd(),
        db_path=args.db,
        work_root=args.work_root,
        dry_run=args.dry_run,
        mock_llm=args.mock_llm,
    )
    if not config.github_token and not config.dry_run:
        raise RuntimeError("GITHUB_TOKEN is required for live runs")
    return config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli")
    subcommands = parser.add_subparsers(dest="command")

    run = subcommands.add_parser("run", help="Process one GitHub issue")
    run.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    run.add_argument("--issue", required=True, type=int, help="GitHub issue number")
    _common(run)

    watch = subcommands.add_parser("watch", help="Poll for assigned or @-mentioned issues")
    watch.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    watch.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")
    watch.add_argument("--once", action="store_true", help="Poll once and exit")
    _common(watch)
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="Path to the SQLite state database")
    parser.add_argument("--work-root", help="Directory for per-issue worktrees")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip outward writes and use a local demo repo",
    )
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="Use deterministic mock LLM responses",
    )
