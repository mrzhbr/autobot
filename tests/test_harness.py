from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autobot.config import Config
from autobot.harness import (
    HarnessResult,
    HarnessTask,
    HarnessTaskKind,
    LegacyLLMHarnessAdapter,
    build_harness_adapter,
)
from autobot.implementation import _apply_harness_result
from autobot.models import ContextFile, FileChange, ImplementationPlan, Issue, Usage
from autobot.pi_harness import (
    PiHarnessAdapter,
    PiHarnessSession,
    _planner_prompt,
    _task_prompt,
    pi_container_env_names,
)


class FakeLLM:
    def __init__(self) -> None:
        self.review_findings: list[str] | None = None

    def write_tests(self, issue: Issue, context: list[ContextFile]) -> ImplementationPlan:
        return ImplementationPlan(
            plan=["Write acceptance coverage."],
            changes=[FileChange("tests/test_issue.py", "def test_ok(): pass\n")],
            test_commands=["python -m pytest"],
            usage=Usage("test", "fake-test", 1, 2, 0.01),
        )

    def implement(
        self,
        issue: Issue,
        context: list[ContextFile],
        review_findings: list[str] | None = None,
    ) -> ImplementationPlan:
        self.review_findings = review_findings
        return ImplementationPlan(
            plan=["Implement behavior."],
            changes=[FileChange("README.md", "done\n")],
            test_commands=["python -m pytest -q"],
            usage=Usage("implement", "fake-implement", 3, 4, 0.02),
        )


class FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> None:
        self.writes.append(text)

    def flush(self) -> None:
        return None


class FakeProcess:
    def __init__(self, stdout_lines: list[dict] | None = None) -> None:
        self.stdin = FakeStdin()
        text = "".join(json.dumps(line) + "\n" for line in stdout_lines or [])
        self.stdout = io.StringIO(text)
        self.stderr = io.StringIO("")
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        self.terminated = True
        return 0

    def kill(self) -> None:
        self.terminated = True


class FakeSandbox:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.args: list[str] | None = None
        self.env: dict[str, str] | None = None

    def popen(self, args: list[str], env: dict[str, str] | None = None):
        self.args = args
        self.env = env
        return self.process


class SyncRecordingSandbox:
    def __init__(self) -> None:
        self.applied: list[FileChange] = []
        self.synced: list[list[str]] = []

    def apply_changes(self, changes: list[FileChange]) -> None:
        self.applied.extend(changes)

    def sync_to_host(self, paths: list[str] | None = None) -> None:
        self.synced.append(paths or [])


class HarnessTests(unittest.TestCase):
    def test_legacy_harness_routes_test_author_task(self) -> None:
        session = LegacyLLMHarnessAdapter(FakeLLM()).start(Path("/tmp/repo"))

        result = session.run(
            HarnessTask(HarnessTaskKind.TEST_AUTHOR, _issue(), [ContextFile("README.md", "# R")])
        )

        self.assertEqual(result.plan, ["Write acceptance coverage."])
        self.assertEqual(result.changed_paths, ["tests/test_issue.py"])
        self.assertEqual(result.test_commands, ["python -m pytest"])
        self.assertFalse(result.applied_in_workspace)
        self.assertEqual(result.usage, Usage("test", "fake-test", 1, 2, 0.01))

    def test_legacy_harness_routes_review_fix_findings(self) -> None:
        llm = FakeLLM()
        session = LegacyLLMHarnessAdapter(llm).start(Path("/tmp/repo"))

        result = session.run(
            HarnessTask(
                HarnessTaskKind.REVIEW_FIX,
                _issue(),
                [ContextFile("README.md", "# R")],
                ["[high] README.md:1 fix this"],
            )
        )

        self.assertEqual(llm.review_findings, ["[high] README.md:1 fix this"])
        self.assertEqual(result.changed_paths, ["README.md"])
        self.assertEqual(result.usage, Usage("implement", "fake-implement", 3, 4, 0.02))

    def test_legacy_harness_routes_verification_fix_findings(self) -> None:
        llm = FakeLLM()
        session = LegacyLLMHarnessAdapter(llm).start(Path("/tmp/repo"))

        result = session.run(
            HarnessTask(
                HarnessTaskKind.VERIFICATION_FIX,
                _issue(),
                [ContextFile("README.md", "# R")],
                ["[high] Verification failed: would reformat tests/test_issue.py"],
            )
        )

        self.assertEqual(
            llm.review_findings,
            ["[high] Verification failed: would reformat tests/test_issue.py"],
        )
        self.assertEqual(result.changed_paths, ["README.md"])
        self.assertEqual(result.usage, Usage("implement", "fake-implement", 3, 4, 0.02))

    def test_build_harness_defaults_to_legacy_adapter(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            config = Config.from_env(Path(tmp))

        self.assertIsInstance(build_harness_adapter(config, FakeLLM()), LegacyLLMHarnessAdapter)

    def test_build_harness_uses_pi_adapter_for_live_pi_config(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {
                    "IMPLEMENT_HARNESS": "pi",
                    "HARNESS_LLM_PROVIDER": "openrouter",
                    "HARNESS_MODEL": "openrouter/anthropic/claude-sonnet-4",
                },
                clear=True,
            ),
        ):
            config = Config.from_env(Path(tmp))

        self.assertIsInstance(build_harness_adapter(config, FakeLLM()), PiHarnessAdapter)

    def test_pi_adapter_starts_rpc_in_sandbox(self) -> None:
        process = FakeProcess()
        sandbox = FakeSandbox(process)
        with (
            TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {
                    "IMPLEMENT_HARNESS": "pi",
                    "HARNESS_LLM_PROVIDER": "openrouter",
                    "HARNESS_MODEL": "openrouter/anthropic/claude-sonnet-4",
                },
                clear=True,
            ),
        ):
            config = Config.from_env(Path(tmp))
            session = PiHarnessAdapter(config).start(Path(tmp), sandbox=sandbox)
            session.close()

        assert sandbox.args is not None
        self.assertEqual(sandbox.args[:5], ["pi", "--mode", "rpc", "--no-session", "--provider"])
        self.assertIn("openrouter", sandbox.args)
        self.assertIn("anthropic/claude-sonnet-4", sandbox.args)
        self.assertNotIn("openrouter/anthropic/claude-sonnet-4", sandbox.args)
        self.assertIn("--no-context-files", sandbox.args)
        self.assertEqual(
            sandbox.env,
            {"PI_CODING_AGENT_DIR": "/tmp/autobot-pi-agent", "PI_OFFLINE": "1"},
        )

    def test_pi_adapter_starts_read_only_planner_rpc_in_sandbox(self) -> None:
        process = FakeProcess()
        sandbox = FakeSandbox(process)
        with (
            TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {
                    "PLANNER_ENABLED": "1",
                    "PLANNER_LLM_PROVIDER": "openrouter",
                    "PLANNER_MODEL": "openrouter/anthropic/claude-opus-4.8",
                },
                clear=True,
            ),
        ):
            config = Config.from_env(Path(tmp))
            session = PiHarnessAdapter(config).start_planner(Path(tmp), sandbox=sandbox)
            session.close()

        assert sandbox.args is not None
        self.assertEqual(sandbox.args[:5], ["pi", "--mode", "rpc", "--no-session", "--provider"])
        self.assertIn("openrouter", sandbox.args)
        self.assertIn("anthropic/claude-opus-4.8", sandbox.args)
        self.assertNotIn("openrouter/anthropic/claude-opus-4.8", sandbox.args)
        tools = sandbox.args[sandbox.args.index("--tools") + 1]
        self.assertEqual(tools, "read,grep,find,ls,bash")

    def test_pi_session_parses_rpc_result_and_usage(self) -> None:
        lines = [
            {"id": "prompt-1", "type": "response", "success": True},
            {"type": "agent_start"},
            {"type": "agent_end", "messages": []},
            {
                "id": "last-1",
                "type": "response",
                "success": True,
                "data": {
                    "text": json.dumps(
                        {
                            "plan": ["Edit README."],
                            "test_commands": ["python -m pytest"],
                            "changed_paths": ["README.md"],
                        }
                    )
                },
            },
            {
                "id": "stats-1",
                "type": "response",
                "success": True,
                "data": {"tokens": {"input": 11, "output": 7}, "cost": 0.03},
            },
        ]
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"HARNESS_TIMEOUT_SECONDS": "5"}, clear=True),
            patch("autobot.pi_harness._request_id", side_effect=["prompt-1", "last-1", "stats-1"]),
            patch("autobot.pi_harness._git_status_paths", side_effect=[set(), {"README.md"}]),
        ):
            repo = Path(tmp)
            config = Config.from_env(repo)
            session = PiHarnessSession(config, repo, FakeProcess(lines), repo / "harness")

            result = session.run(
                HarnessTask(HarnessTaskKind.IMPLEMENT, _issue(), [ContextFile("README.md", "# R")])
            )

        self.assertTrue(result.applied_in_workspace)
        self.assertEqual(result.plan, ["Edit README."])
        self.assertEqual(result.test_commands, ["python -m pytest"])
        self.assertEqual(result.changed_paths, ["README.md"])
        self.assertEqual(result.usage, Usage("implement", "gpt-4.1", 11, 7, 0.03))
        self.assertIsNotNone(result.transcript_path)

    def test_pi_session_parses_planner_result_and_usage(self) -> None:
        lines = [
            {"id": "prompt-1", "type": "response", "success": True},
            {"type": "agent_start"},
            {"type": "agent_end", "messages": []},
            {
                "id": "last-1",
                "type": "response",
                "success": True,
                "data": {
                    "text": json.dumps(
                        {
                            "summary": "Patch router confidence handling.",
                            "target_files": ["src/router.py", "tests/test_router.py"],
                            "constraints": ["Keep public API stable."],
                            "implementation_steps": ["Read RouterResult.", "Add fallback."],
                            "tests_to_add": ["Low-confidence route test."],
                            "verification_commands": ["python -m pytest tests/test_router.py"],
                            "risks": ["Ambiguous router names."],
                            "non_goals": ["No model provider changes."],
                        }
                    )
                },
            },
            {
                "id": "stats-1",
                "type": "response",
                "success": True,
                "data": {"tokens": {"input": 13, "output": 11}, "cost": 0.05},
            },
        ]
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"HARNESS_TIMEOUT_SECONDS": "5"}, clear=True),
            patch("autobot.pi_harness._request_id", side_effect=["prompt-1", "last-1", "stats-1"]),
        ):
            repo = Path(tmp)
            config = Config.from_env(repo)
            session = PiHarnessSession(
                config,
                repo,
                FakeProcess(lines),
                repo / "harness",
                model="openrouter/anthropic/claude-opus-4.8",
            )

            result = session.plan(
                HarnessTask(HarnessTaskKind.PLANNING, _issue(), [ContextFile("README.md", "# R")])
            )

        self.assertEqual(result.summary, "Patch router confidence handling.")
        self.assertEqual(result.target_files, ["src/router.py", "tests/test_router.py"])
        self.assertEqual(result.implementation_steps, ["Read RouterResult.", "Add fallback."])
        self.assertEqual(
            result.usage,
            Usage("planning", "openrouter/anthropic/claude-opus-4.8", 13, 11, 0.05),
        )
        self.assertIsNotNone(result.transcript_path)
        self.assertIn("pi-planning-", Path(result.transcript_path).name)

    def test_pi_verification_fix_uses_distinct_role_and_transcript_name(self) -> None:
        lines = [
            {"id": "prompt-1", "type": "response", "success": True},
            {"type": "agent_start"},
            {"type": "agent_end", "messages": []},
            {
                "id": "last-1",
                "type": "response",
                "success": True,
                "data": {
                    "text": json.dumps(
                        {
                            "plan": ["Run formatter."],
                            "test_commands": ["ruff format --check ."],
                            "changed_paths": ["tests/test_issue.py"],
                        }
                    )
                },
            },
            {
                "id": "stats-1",
                "type": "response",
                "success": True,
                "data": {"tokens": {"input": 5, "output": 3}, "cost": 0.01},
            },
        ]
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"HARNESS_TIMEOUT_SECONDS": "5"}, clear=True),
            patch("autobot.pi_harness._request_id", side_effect=["prompt-1", "last-1", "stats-1"]),
            patch(
                "autobot.pi_harness._git_status_paths", side_effect=[set(), {"tests/test_issue.py"}]
            ),
        ):
            repo = Path(tmp)
            config = Config.from_env(repo)
            session = PiHarnessSession(config, repo, FakeProcess(lines), repo / "harness")

            result = session.run(
                HarnessTask(
                    HarnessTaskKind.VERIFICATION_FIX,
                    _issue(),
                    [ContextFile("README.md", "# R")],
                    ["[high] Verification failed: would reformat tests/test_issue.py"],
                )
            )

        self.assertEqual(result.usage, Usage("verification_fix", "gpt-4.1", 5, 3, 0.01))
        self.assertIsNotNone(result.transcript_path)
        self.assertIn("pi-verification_fix-", Path(result.transcript_path).name)

    def test_pi_task_prompt_labels_verification_feedback_separately_from_review(self) -> None:
        prompt = _task_prompt(
            HarnessTask(
                HarnessTaskKind.VERIFICATION_FIX,
                _issue(),
                [ContextFile("README.md", "# R")],
                ["[high] Verification failed: ruff format --check ."],
            )
        )

        self.assertIn("Task kind: verification_fix", prompt)
        self.assertIn("Verification failure output:", prompt)
        self.assertIn("[high] Verification failed: ruff format --check .", prompt)
        self.assertNotIn("Blocking reviewer findings:", prompt)

    def test_pi_task_prompt_includes_planner_context(self) -> None:
        prompt = _task_prompt(
            HarnessTask(
                HarnessTaskKind.IMPLEMENT,
                _issue(),
                [ContextFile("README.md", "# R")],
                planning_context="Summary: edit router confidence logic.",
            )
        )

        self.assertIn("Planner output:", prompt)
        self.assertIn("Summary: edit router confidence logic.", prompt)

    def test_pi_planner_prompt_forbids_writes_and_requires_json(self) -> None:
        prompt = _planner_prompt(
            HarnessTask(
                HarnessTaskKind.PLANNING,
                _issue(),
                [ContextFile("README.md", "# R")],
            )
        )

        self.assertIn("read-only planning agent", prompt)
        self.assertIn("Do not edit files", prompt)
        self.assertIn("Return one JSON object", prompt)

    def test_pi_container_env_names_passes_only_selected_provider_key(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {
                    "IMPLEMENT_HARNESS": "pi",
                    "HARNESS_MODEL": "openrouter/google/gemini-2.5-pro",
                },
                clear=True,
            ),
        ):
            config = Config.from_env(Path(tmp))

        self.assertEqual(pi_container_env_names(config), ["OPENROUTER_API_KEY"])

    def test_pi_container_env_names_includes_planner_provider_key(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {
                    "IMPLEMENT_HARNESS": "pi",
                    "HARNESS_LLM_PROVIDER": "openai",
                    "HARNESS_MODEL": "gpt-4.1",
                    "PLANNER_ENABLED": "1",
                    "PLANNER_LLM_PROVIDER": "openrouter",
                    "PLANNER_MODEL": "openrouter/anthropic/claude-opus-4.8",
                },
                clear=True,
            ),
        ):
            config = Config.from_env(Path(tmp))

        self.assertEqual(pi_container_env_names(config), ["OPENAI_API_KEY", "OPENROUTER_API_KEY"])

    def test_apply_harness_result_syncs_workspace_changes_to_host(self) -> None:
        sandbox = SyncRecordingSandbox()
        result = HarnessResult(
            plan=["Edit in container."],
            changes=[],
            test_commands=[],
            applied_in_workspace=True,
            changed_paths=["README.md"],
        )

        _apply_harness_result(Path("/tmp/repo"), sandbox, result, dry_run=False)

        self.assertEqual(sandbox.applied, [])
        self.assertEqual(sandbox.synced, [["README.md"]])

    def test_apply_harness_result_syncs_legacy_changes_after_remote_apply(self) -> None:
        sandbox = SyncRecordingSandbox()
        change = FileChange("README.md", "done\n")
        result = HarnessResult(
            plan=["Apply typed change."],
            changes=[change],
            test_commands=[],
            applied_in_workspace=False,
            changed_paths=["README.md"],
        )

        _apply_harness_result(Path("/tmp/repo"), sandbox, result, dry_run=False)

        self.assertEqual(sandbox.applied, [change])
        self.assertEqual(sandbox.synced, [["README.md"]])


def _issue() -> Issue:
    return Issue("owner/repo", 1, "Title", "Body", "alice", [])


if __name__ == "__main__":
    unittest.main()
