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
    LLM_KEY_ENV,
    Config,
    configured_llm_models,
    infer_llm_provider,
    invalid_price_vars,
    missing_model_keys,
    missing_model_keys_message,
    missing_price_vars,
)
from autobot.doctor import doctor_ok, run_doctor
from autobot.github import GitHubGitHost, GitHubIssueTracker
from autobot.llm import build_llm
from autobot.models import IssueRecord
from autobot.pipeline import IssueProcessor
from autobot.scanner import redact_secret_like_values
from autobot.state import StateStore
from autobot.workflow_models import WorkflowStep


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
        if args.command == "state":
            return _state(args)
    except Exception as exc:
        print(f"error: {redact_secret_like_values(str(exc))}", file=sys.stderr)
        return 1
    return 2


def _run(args: argparse.Namespace) -> int:
    config = _config(args)
    progress = None if args.quiet else _print_run_progress
    processor = _processor(config, progress=progress)
    result = processor.process(args.repo, int(args.issue))
    print(json.dumps(_summary(args.repo, int(args.issue), result), indent=2, sort_keys=True))
    return 0


def _watch(args: argparse.Namespace) -> int:
    config = _config(args)
    tracker = GitHubIssueTracker(config.github_token, config.agent_login)
    processor = _processor(config, tracker=tracker)
    while True:
        failed = _watch_poll(args.repo, tracker, processor)
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


def _state(args: argparse.Namespace) -> int:
    if args.state_command == "show":
        return _state_show(args)
    if args.state_command == "clear":
        return _state_clear(args)
    raise RuntimeError("state subcommand is required")


def _state_show(args: argparse.Namespace) -> int:
    config = _config(args, require_github=False)
    record = StateStore(config.db_path).get(args.repo, int(args.issue))
    if record is None:
        payload = {
            "repo": args.repo,
            "issue": int(args.issue),
            "state": "not_found",
            "found": False,
        }
    else:
        payload = _state_record_summary(record)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _state_clear(args: argparse.Namespace) -> int:
    config = _config(args, require_github=False)
    deleted = StateStore(config.db_path).delete(args.repo, int(args.issue))
    payload = {
        "repo": args.repo,
        "issue": int(args.issue),
        "state": "cleared" if deleted else "not_found",
        "deleted": deleted,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _state_record_summary(record: IssueRecord) -> dict:
    return {
        "repo": record.repo,
        "issue": record.issue_number,
        "state": record.state.value,
        "found": True,
        "branch": record.branch,
        "blocked_on": record.blocked_on,
        "pr_url": record.pr_url,
        "review_rounds": record.review_rounds,
        "files_touched": record.files_touched,
        "verification_commands": list(record.plan.get("verification_commands") or []),
        "cost": record.cost,
        "latest_learning": _latest_learning(record),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _latest_learning(record: IssueRecord) -> dict | None:
    learnings = record.conversation.get("run_learnings")
    if not isinstance(learnings, list) or not learnings:
        return None
    latest = learnings[-1]
    return latest if isinstance(latest, dict) else None


def _processor(
    config: Config,
    tracker: GitHubIssueTracker | None = None,
    progress=None,
) -> IssueProcessor:
    store = StateStore(config.db_path)
    tracker = tracker or GitHubIssueTracker(config.github_token, config.agent_login)
    git_host = GitHubGitHost(config.github_token)
    chat = IssueCommentChat(tracker)
    llm = build_llm(config)
    audit = AuditLog(config.audit_path)
    return IssueProcessor(config, store, tracker, git_host, chat, llm, audit, progress=progress)


def _print_run_progress(step: WorkflowStep) -> None:
    payload = {
        "state": "progress",
        "step": step.value,
        "message": _RUN_PROGRESS_MESSAGES[step],
    }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)


_RUN_PROGRESS_MESSAGES = {
    WorkflowStep.LOAD_RECORD: "loading local state",
    WorkflowStep.TERMINAL_CHECK: "checking terminal state",
    WorkflowStep.BUDGET_GATE: "checking budget",
    WorkflowStep.READ_ISSUE: "reading GitHub issue",
    WorkflowStep.RESUME_WAITING: "checking waiting resume",
    WorkflowStep.GUARDRAIL: "checking guardrails",
    WorkflowStep.PREPARE_WORKSPACE: "preparing workspace",
    WorkflowStep.TRIAGE: "running triage LLM",
    WorkflowStep.IMPLEMENT_REVIEW_FINALIZE: "implementing, reviewing, and opening draft PR",
    WorkflowStep.FINISH: "finishing result",
}


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


def _watch_poll(repo: str, tracker, processor: IssueProcessor) -> bool:
    failed = False
    try:
        numbers = tracker.list_actionable(repo)
    except Exception as exc:
        print(json.dumps(_watch_error_summary(repo, "list_actionable", exc), sort_keys=True))
        return True
    if not numbers:
        print(json.dumps({"repo": repo, "state": "idle", "actionable": 0}))
    for number in numbers:
        try:
            result = processor.process(repo, number)
            payload = _summary(repo, number, result)
        except Exception as exc:
            failed = True
            payload = _error_summary(repo, number, exc)
        print(json.dumps(payload, sort_keys=True))
    return failed


def _error_summary(repo: str, issue: int, exc: Exception) -> dict:
    return {
        "repo": repo,
        "issue": issue,
        "state": "error",
        "message": redact_secret_like_values(str(exc)),
    }


def _watch_error_summary(repo: str, phase: str, exc: Exception) -> dict:
    return {
        "repo": repo,
        "phase": phase,
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
        _ensure_live_prereqs(config)
    return config


def _ensure_live_llm_key(config: Config) -> None:
    if config.dry_run or config.mock_llm:
        return
    provider = infer_llm_provider(config.llm_provider)
    if provider not in {None, *LLM_KEY_ENV}:
        raise RuntimeError("LLM_PROVIDER must be openai, anthropic, or openrouter")
    if provider and not os.getenv(LLM_KEY_ENV[provider]):
        raise RuntimeError(f"{LLM_KEY_ENV[provider]} is required")
    if provider is None:
        raise RuntimeError("OPENAI_API_KEY, ANTHROPIC_API_KEY, or OPENROUTER_API_KEY is required")
    missing = missing_model_keys(provider, configured_llm_models(config))
    if missing:
        raise RuntimeError(missing_model_keys_message(missing))
    if config.implement_harness == "pi":
        harness_provider = config.harness_llm_provider
        if harness_provider not in LLM_KEY_ENV:
            raise RuntimeError("HARNESS_LLM_PROVIDER must be openai, anthropic, or openrouter")
        key = LLM_KEY_ENV[harness_provider]
        if not os.getenv(key):
            raise RuntimeError(f"{key} is required for Pi harness")
    invalid = invalid_price_vars()
    if invalid:
        raise RuntimeError("LLM pricing env vars must be numeric: " + ", ".join(invalid))
    missing_prices = missing_price_vars()
    if config.max_issue_dollars is not None and missing_prices:
        raise RuntimeError(
            "MAX_ISSUE_DOLLARS requires LLM pricing env vars: " + ", ".join(missing_prices)
        )


def _ensure_live_prereqs(config: Config) -> None:
    if config.dry_run:
        return
    failures = [check for check in run_doctor(config, network=False) if check.status == "fail"]
    if not failures:
        return
    details = "; ".join(f"{check.name}: {check.message}" for check in failures)
    raise RuntimeError("live prerequisite check failed: " + details)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli")
    subcommands = parser.add_subparsers(dest="command")

    run = subcommands.add_parser("run", help="Process one GitHub issue")
    run.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    run.add_argument("--issue", required=True, type=int, help="GitHub issue number")
    run.add_argument("--quiet", action="store_true", help="Suppress progress output")
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

    state = subcommands.add_parser("state", help="Inspect or modify local issue state")
    state_subcommands = state.add_subparsers(dest="state_command")
    show = state_subcommands.add_parser("show", help="Show one local issue state record")
    show.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    show.add_argument("--issue", required=True, type=int, help="GitHub issue number")
    _common(show)
    clear = state_subcommands.add_parser("clear", help="Delete one local issue state record")
    clear.add_argument("--repo", required=True, help="GitHub repository in owner/name form")
    clear.add_argument("--issue", required=True, type=int, help="GitHub issue number")
    _common(clear)
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
