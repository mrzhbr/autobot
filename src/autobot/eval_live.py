from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from autobot.config import Config
from autobot.context import gather_context
from autobot.cost import CostLedger
from autobot.harness import HarnessResult, HarnessTask, HarnessTaskKind, build_harness_adapter
from autobot.llm import build_llm
from autobot.models import Issue, IssueComment
from autobot.pi_harness import PiHarnessAdapter, pi_container_env_names
from autobot.sandbox import (
    DockerSandbox,
    LocalSandbox,
    detect_setup_command,
    run_verification_allow_failure,
)


def run_provider_eval(
    root: Path,
    fixture_name: str,
    harness: str,
    *,
    config: Config,
    output_dir: Path | None = None,
):
    from autobot.eval_harness import (
        EvalCost,
        EvalModelRef,
        EvalVerificationResult,
        HarnessEvalResult,
        _implement_ref,
        _review_refs,
        _summarize_output,
        append_result,
        load_fixture,
        score_eval,
    )

    started = time.monotonic()
    fixture = load_fixture(root, fixture_name)
    live_config = replace(config, implement_harness=harness, dry_run=False, mock_llm=False)
    base_output = output_dir or root / ".autobot" / "evals"
    repo_dir = _prepare_workspace(root, fixture_name, harness, base_output)
    issue = _fixture_issue(fixture)
    ledger = CostLedger()
    sandbox = _docker_sandbox(repo_dir, live_config) if harness == "pi" else None
    session = None
    try:
        if sandbox is not None:
            sandbox.prepare()
        llm = build_llm(live_config)
        session = build_harness_adapter(live_config, llm).start(
            repo_dir,
            sandbox=sandbox,
            dry_run=False,
        )
        planner = None
        if live_config.planner_enabled:
            planner_session = PiHarnessAdapter(live_config).start_planner(
                repo_dir,
                _require_sandbox(sandbox),
            )
            try:
                planner = planner_session.plan(
                    HarnessTask(HarnessTaskKind.PLANNING, issue, gather_context(repo_dir, issue))
                )
                ledger.add(planner.usage)
            finally:
                planner_session.close()
        task = HarnessTask(
            HarnessTaskKind.IMPLEMENT,
            issue,
            gather_context(repo_dir, issue),
            planning_context=planner.as_prompt_context() if planner is not None else None,
        )
        result = session.run(task)
        ledger.add(result.usage)
        _apply_result(repo_dir, sandbox, result)
        commands = _verification_commands(fixture, result)
        verification = run_verification_allow_failure(LocalSandbox(repo_dir), commands, False)
        touched = _changed_paths(result)
        score = score_eval(repo_dir, fixture.expectations, touched, bool(verification["ok"]))
        transcript_path = result.transcript_path or str(_write_transcript(base_output, result))
        state = "passed" if score.passed else "failed"
        eval_result = HarnessEvalResult(
            fixture_name=fixture.name,
            harness=harness,
            mode="live",
            planner_enabled=live_config.planner_enabled,
            planner=EvalModelRef(
                provider=live_config.planner_llm_provider if live_config.planner_enabled else None,
                model=live_config.planner_model if live_config.planner_enabled else None,
            ),
            implement=_implement_ref(live_config, harness),
            reviewers=_review_refs(live_config),
            state=state,
            result="pass" if score.passed else "fail",
            files_touched=touched,
            verification=EvalVerificationResult(
                commands=commands,
                ok=bool(verification["ok"]),
                output_summary=_summarize_output(str(verification["output"])),
            ),
            review_rounds=0,
            blockers=[],
            cost=EvalCost(
                input_tokens=ledger.input_tokens,
                output_tokens=ledger.output_tokens,
                dollars=ledger.dollars,
            ),
            wall_seconds=round(time.monotonic() - started, 3),
            transcript_path=transcript_path,
            log_paths=[transcript_path],
            score=score,
        )
        append_result(base_output, eval_result)
        return eval_result
    finally:
        if session is not None:
            session.close()
        if sandbox is not None:
            sandbox.close()


def _prepare_workspace(root: Path, fixture_name: str, harness: str, output_dir: Path) -> Path:
    source = root / "evals" / "harness" / fixture_name / "repo"
    target = output_dir / "work" / f"{fixture_name}-{harness}-live"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    _init_git(target)
    return target


def _init_git(repo_dir: Path) -> None:
    _run_git(repo_dir, ["init"])
    _run_git(repo_dir, ["add", "."])
    _run_git(
        repo_dir,
        [
            "-c",
            "user.name=Autobot Eval",
            "-c",
            "user.email=autobot-eval@example.invalid",
            "commit",
            "-m",
            "fixture baseline",
        ],
    )


def _fixture_issue(fixture) -> Issue:
    comments = [
        IssueComment(index + 1, comment.author, comment.body, "2026-01-01T00:00:00+00:00")
        for index, comment in enumerate(fixture.issue.comments)
    ]
    return Issue("eval/fixture", 1, fixture.issue.title, fixture.issue.body, "eval", [], comments)


def _docker_sandbox(repo_dir: Path, config: Config) -> DockerSandbox:
    setup = detect_setup_command(repo_dir, config.sandbox_setup_command)
    return DockerSandbox(
        repo_dir,
        config.sandbox_image,
        setup,
        config.sandbox_network,
        env_names=pi_container_env_names(config),
        mode="copy" if config.sandbox_backend == "docker-copy" else "bind",
    )


def _require_sandbox(sandbox: DockerSandbox | None) -> DockerSandbox:
    if sandbox is None:
        raise RuntimeError("planner-backed evals require a Docker sandbox")
    return sandbox


def _apply_result(repo_dir: Path, sandbox: DockerSandbox | None, result: HarnessResult) -> None:
    if result.applied_in_workspace:
        if sandbox is not None:
            sandbox.sync_to_host(_changed_paths(result))
        return
    LocalSandbox(repo_dir).apply_changes(result.changes)


def _verification_commands(fixture, result: HarnessResult) -> list[str]:
    commands = [*result.test_commands, *fixture.expectations.verification_commands]
    return list(dict.fromkeys(command for command in commands if command.strip()))


def _changed_paths(result: HarnessResult) -> list[str]:
    paths = result.changed_paths or [change.path for change in result.changes]
    return list(dict.fromkeys(paths))


def _write_transcript(output_dir: Path, result: HarnessResult) -> Path:
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"live-{time.time_ns()}-transcript.json"
    path.write_text(
        json.dumps({"plan": result.plan, "test_commands": result.test_commands}, indent=2),
        encoding="utf-8",
    )
    return path


def _run_git(repo_dir: Path, args: list[str]) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
