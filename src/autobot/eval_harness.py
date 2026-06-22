from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from autobot.config import Config, infer_llm_provider, model_provider_hint
from autobot.models import FileChange
from autobot.sandbox import LocalSandbox, run_verification_allow_failure
from autobot.schemas import FileChangePayload, StrictPayload

EvalHarness = Literal["legacy", "pi"]
EvalMode = Literal["mock", "live"]
EvalState = Literal["passed", "failed"]


class EvalIssueComment(StrictPayload):
    author: str = "user"
    body: str = Field(min_length=1)


class EvalIssue(StrictPayload):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    comments: list[EvalIssueComment] = Field(default_factory=list)


class EvalPattern(StrictPayload):
    path: str = Field(min_length=1)
    pattern: str = Field(min_length=1)


class EvalExpectations(StrictPayload):
    changed_files: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    allowed_patterns: list[EvalPattern] = Field(default_factory=list)
    forbidden_patterns: list[EvalPattern] = Field(default_factory=list)
    behavioral_assertions: list[str] = Field(default_factory=list)


class EvalMockRun(StrictPayload):
    plan: list[str] = Field(min_length=1)
    changes: list[FileChangePayload] = Field(min_length=1)
    test_commands: list[str] = Field(default_factory=list)
    review_rounds: int = Field(default=1, ge=0)
    blockers: list[str] = Field(default_factory=list)
    transcript: list[str] = Field(default_factory=list)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    dollars: float | None = Field(default=None, ge=0)


class EvalFixture(StrictPayload):
    name: str
    issue: EvalIssue
    expectations: EvalExpectations
    mock_runs: dict[EvalHarness, EvalMockRun]

    @model_validator(mode="after")
    def require_mock_runs(self) -> EvalFixture:
        if not self.mock_runs:
            raise ValueError("fixture must define at least one mock harness run")
        return self


class EvalModelRef(StrictPayload):
    provider: str | None = None
    model: str | None = None


class EvalReviewRef(StrictPayload):
    provider: str | None = None
    model: str


class EvalVerificationResult(StrictPayload):
    commands: list[str]
    ok: bool
    output_summary: str


class EvalCost(StrictPayload):
    input_tokens: int | None = None
    output_tokens: int | None = None
    dollars: float | None = None


class EvalScore(StrictPayload):
    passed: bool
    reasons: list[str]


class HarnessEvalResult(StrictPayload):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    fixture_name: str
    harness: EvalHarness
    mode: EvalMode
    planner_enabled: bool
    planner: EvalModelRef
    implement: EvalModelRef
    reviewers: list[EvalReviewRef]
    state: EvalState
    result: str
    files_touched: list[str]
    verification: EvalVerificationResult
    review_rounds: int
    blockers: list[str]
    cost: EvalCost
    wall_seconds: float
    transcript_path: str | None = None
    log_paths: list[str] = Field(default_factory=list)
    score: EvalScore


def load_fixture(root: Path, name: str) -> EvalFixture:
    fixture_dir = root / "evals" / "harness" / name
    config_path = fixture_dir / "fixture.json"
    if not config_path.exists():
        raise FileNotFoundError(f"eval fixture not found: {name}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data.setdefault("name", name)
    fixture = EvalFixture.model_validate(data)
    if not (fixture_dir / "repo").is_dir():
        raise FileNotFoundError(f"eval fixture repo template missing: {fixture_dir / 'repo'}")
    return fixture


def run_harness_eval(
    root: Path,
    fixture_name: str,
    harness: EvalHarness,
    *,
    mock_llm: bool,
    config: Config,
    output_dir: Path | None = None,
) -> HarnessEvalResult:
    if not mock_llm:
        from autobot.eval_live import run_provider_eval

        return run_provider_eval(
            root,
            fixture_name,
            harness,
            config=config,
            output_dir=output_dir,
        )
    started = time.monotonic()
    fixture = load_fixture(root, fixture_name)
    if harness not in fixture.mock_runs:
        raise RuntimeError(f"fixture {fixture_name} has no mock run for harness {harness}")
    base_output = output_dir or root / ".autobot" / "evals"
    repo_dir = _prepare_workspace(root, fixture_name, harness, base_output)
    mock = fixture.mock_runs[harness]
    LocalSandbox(repo_dir).apply_changes(_file_changes(mock.changes))
    commands = _verification_commands(fixture, mock)
    verification = run_verification_allow_failure(LocalSandbox(repo_dir), commands, dry_run=False)
    transcript_path = _write_transcript(base_output, fixture_name, harness, mock)
    score = score_eval(
        repo_dir,
        fixture.expectations,
        _changed_paths(mock),
        verification["ok"],
    )
    state: EvalState = "passed" if score.passed else "failed"
    result = HarnessEvalResult(
        fixture_name=fixture.name,
        harness=harness,
        mode="mock",
        planner_enabled=config.planner_enabled,
        planner=EvalModelRef(
            provider=config.planner_llm_provider if config.planner_enabled else None,
            model=config.planner_model if config.planner_enabled else None,
        ),
        implement=_implement_ref(config, harness),
        reviewers=_review_refs(config),
        state=state,
        result="pass" if score.passed else "fail",
        files_touched=_changed_paths(mock),
        verification=EvalVerificationResult(
            commands=commands,
            ok=bool(verification["ok"]),
            output_summary=_summarize_output(str(verification["output"])),
        ),
        review_rounds=mock.review_rounds,
        blockers=mock.blockers,
        cost=EvalCost(
            input_tokens=mock.input_tokens,
            output_tokens=mock.output_tokens,
            dollars=mock.dollars,
        ),
        wall_seconds=round(time.monotonic() - started, 3),
        transcript_path=str(transcript_path),
        log_paths=[str(transcript_path)],
        score=score,
    )
    append_result(base_output, result)
    return result


def score_eval(
    repo_dir: Path,
    expectations: EvalExpectations,
    files_touched: list[str],
    verification_ok: bool,
) -> EvalScore:
    reasons: list[str] = []
    if verification_ok:
        reasons.append("verification passed")
    else:
        reasons.append("verification failed")
    expected = set(expectations.changed_files)
    touched = set(files_touched)
    missing = sorted(expected - touched)
    unexpected = sorted(touched - expected) if expected else []
    if missing:
        reasons.append("missing expected files: " + ", ".join(missing))
    if unexpected:
        reasons.append("unexpected files touched: " + ", ".join(unexpected))
    for item in expectations.allowed_patterns:
        if item.pattern not in _read_text(repo_dir, item.path):
            reasons.append(f"missing required pattern in {item.path}: {item.pattern}")
    for item in expectations.forbidden_patterns:
        if item.pattern in _read_text(repo_dir, item.path):
            reasons.append(f"forbidden pattern found in {item.path}: {item.pattern}")
    failed = [reason for reason in reasons if not _passing_reason(reason)]
    return EvalScore(passed=not failed, reasons=reasons)


def append_result(output_dir: Path, result: HarnessEvalResult) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "harness-results.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result.model_dump(mode="json"), sort_keys=True) + "\n")
    return path


def _prepare_workspace(root: Path, fixture_name: str, harness: str, output_dir: Path) -> Path:
    source = root / "evals" / "harness" / fixture_name / "repo"
    target = output_dir / "work" / f"{fixture_name}-{harness}-mock"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target


def _write_transcript(
    output_dir: Path,
    fixture_name: str,
    harness: str,
    mock: EvalMockRun,
) -> Path:
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{fixture_name}-{harness}-mock-transcript.txt"
    lines = mock.transcript or mock.plan
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def _verification_commands(fixture: EvalFixture, mock: EvalMockRun) -> list[str]:
    commands = [*mock.test_commands, *fixture.expectations.verification_commands]
    return list(dict.fromkeys(command for command in commands if command.strip()))


def _file_changes(changes: list[FileChangePayload]) -> list[FileChange]:
    return [FileChange(change.path, change.content, change.action) for change in changes]


def _changed_paths(mock: EvalMockRun) -> list[str]:
    return list(dict.fromkeys(change.path for change in mock.changes))


def _read_text(repo_dir: Path, path: str) -> str:
    target = repo_dir / path
    if not target.exists() or not target.is_file():
        return ""
    return target.read_text(encoding="utf-8")


def _passing_reason(reason: str) -> bool:
    return reason == "verification passed"


def _summarize_output(output: str, limit: int = 1200) -> str:
    normalized = output.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "...[truncated]"


def _implement_ref(config: Config, harness: EvalHarness) -> EvalModelRef:
    if harness == "pi":
        return EvalModelRef(provider=config.harness_llm_provider, model=config.harness_model)
    provider = model_provider_hint(config.implement_model) or infer_llm_provider(
        config.llm_provider
    )
    return EvalModelRef(provider=provider, model=config.implement_model)


def _review_refs(config: Config) -> list[EvalReviewRef]:
    default_provider = infer_llm_provider(config.llm_provider)
    return [
        EvalReviewRef(provider=model_provider_hint(model) or default_provider, model=model)
        for model in config.review_models
    ]
