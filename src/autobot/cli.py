from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from autobot.audit import AuditLog
from autobot.chat import IssueCommentChat
from autobot.config import (
    Config,
    configured_llm_models,
    incompatible_models_for_provider,
    infer_llm_provider,
    model_provider_mismatch_message,
)
from autobot.doctor import doctor_ok, run_doctor
from autobot.github import GitHubGitHost, GitHubIssueTracker
from autobot.llm import build_llm
from autobot.pipeline import IssueProcessor
from autobot.scanner import redact_secret_like_values
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
        if args.command == "doctor":
            return _doctor(args)
    except Exception as exc:
        print(f"error: {redact_secret_like_values(str(exc))}", file=sys.stderr)
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
        failed = False
        numbers = tracker.list_actionable(args.repo)
        if not numbers:
            print(json.dumps({"repo": args.repo, "state": "idle", "actionable": 0}))
        for number in numbers:
            try:
                result = processor.process(args.repo, number)
                payload = _summary(args.repo, number, result)
            except Exception as exc:
                failed = True
                payload = _error_summary(args.repo, number, exc)
            print(json.dumps(payload, sort_keys=True))
        if args.once:
            return 1 if failed else 0
        time.sleep(args.interval)


def _doctor(args: argparse.Namespace) -> int:
    config = _config(args, require_github=False)
    checks = run_doctor(config, args.repo, args.issue, network=not args.no_network)
    payload = {
        "ok": doctor_ok(checks),
        "checks": [check.to_dict() for check in checks],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


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
        "verification_commands": result.verification_commands,
        "cost": result.cost,
    }


def _error_summary(repo: str, issue: int, exc: Exception) -> dict:
    return {
        "repo": repo,
        "issue": issue,
        "state": "error",
        "message": redact_secret_like_values(str(exc)),
    }


def _config(args: argparse.Namespace, require_github: bool = True) -> Config:
    config = Config.from_env(
        root=Path.cwd(),
        db_path=args.db,
        work_root=args.work_root,
        dry_run=args.dry_run,
        mock_llm=args.mock_llm,
    )
    if require_github and not config.github_token and not config.dry_run:
        raise RuntimeError("GITHUB_TOKEN is required for live runs")
    if require_github:
        _ensure_live_llm_key(config)
    return config


def _ensure_live_llm_key(config: Config) -> None:
    if config.dry_run or config.mock_llm:
        return
    provider = infer_llm_provider(config.llm_provider)
    if provider not in {None, "openai", "anthropic"}:
        raise RuntimeError("LLM_PROVIDER must be openai or anthropic")
    if provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required")
    if provider == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required")
    if provider is None:
        raise RuntimeError("OPENAI_API_KEY or ANTHROPIC_API_KEY is required")
    incompatible = incompatible_models_for_provider(provider, configured_llm_models(config))
    if incompatible:
        raise RuntimeError(model_provider_mismatch_message(provider, incompatible))


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

    doctor = subcommands.add_parser("doctor", help="Check live-run prerequisites")
    doctor.add_argument("--repo", help="GitHub repository in owner/name form")
    doctor.add_argument("--issue", type=int, help="GitHub issue number")
    doctor.add_argument("--no-network", action="store_true", help="Skip GitHub issue readability")
    _common(doctor)
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
