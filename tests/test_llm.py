from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from autobot.config import Config
from autobot.llm import HttpLLM, LLMError, MockLLM, _parse_json, _post_json, _priced
from autobot.models import ContextFile, Issue, IssueComment, Usage


class CapturingLLM(HttpLLM):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.calls: list[tuple[str, str, str]] = []

    def _json_call(self, role: str, model: str, prompt: str):
        self.calls.append((role, model, prompt))
        if role == "review":
            return {"findings": []}, Usage(role, model, 1, 1, 0.001)
        return (
            {
                "plan": ["Do the work."],
                "changes": [{"path": "README.md", "content": "# Demo\n"}],
                "test_commands": ["python -m pytest"],
            },
            Usage(role, model, 1, 1, 0.001),
        )


class RoutingLLM(HttpLLM):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.providers: list[tuple[str, str]] = []

    def _openai_json(self, role: str, model: str, prompt: str):
        self.providers.append(("openai", model))
        return {"findings": []}, Usage(role, model, 1, 1, 0.001)

    def _anthropic_json(self, role: str, model: str, prompt: str):
        self.providers.append(("anthropic", model))
        return {"findings": []}, Usage(role, model, 1, 1, 0.001)


class LLMTests(unittest.TestCase):
    def test_mock_llm_reports_usage_for_each_phase(self) -> None:
        llm = MockLLM()
        issue = _issue()
        context = [ContextFile("README.md", "# Demo\n")]

        triage = llm.triage(issue, context)
        test_plan = llm.write_tests(issue, context)
        plan = llm.implement(issue, context)
        review = llm.review("correctness", issue, "diff --git a/app.py b/app.py")

        self.assertEqual(triage.usage, Usage("triage", "mock-triage", 0, 0, 0))
        self.assertEqual(test_plan.usage, Usage("test", "mock-test", 0, 0, 0))
        self.assertEqual(plan.usage, Usage("implement", "mock-implement", 0, 0, 0))
        self.assertEqual(review.usage, Usage("review", "mock-review", 0, 0, 0))

    def test_implement_prompt_encodes_engineering_discipline(self) -> None:
        llm = _llm()

        llm.implement(
            _issue(),
            [ContextFile("README.md", "# Demo\n")],
            ["[high] app.py:12 fix behavior"],
        )

        role, _, prompt = llm.calls[-1]
        self.assertEqual(role, "implement")
        self.assertIn("Plan before writing", prompt)
        self.assertIn("Reuse existing project patterns", prompt)
        self.assertIn("source file at or below 400 lines", prompt)
        self.assertIn("include test, lint, or type commands", prompt)
        self.assertIn("[high] app.py:12 fix behavior", prompt)

    def test_test_author_prompt_keeps_tests_spec_derived(self) -> None:
        llm = _llm()

        llm.write_tests(_issue(), [ContextFile("README.md", "# Demo\n")])

        role, _, prompt = llm.calls[-1]
        self.assertEqual(role, "test")
        self.assertIn("Write acceptance tests derived only from the issue", prompt)
        self.assertIn("Do not implement product code", prompt)
        self.assertIn("Plan before writing", prompt)
        self.assertIn("source file at or below 400 lines", prompt)

    def test_review_prompt_includes_issue_comments(self) -> None:
        llm = _llm()
        issue = Issue(
            "owner/repo",
            1,
            "Add filter",
            "Use a filter control.",
            "alice",
            [],
            [IssueComment(7, "alice", "Use a dropdown, not radio buttons.", "2026-06-05")],
        )

        llm.review("correctness", issue, "diff --git a/app.py b/app.py")

        role, _, prompt = llm.calls[-1]
        self.assertEqual(role, "review")
        self.assertIn("Comments:\nalice: Use a dropdown, not radio buttons.", prompt)
        self.assertIn("Diff:\ndiff --git", prompt)

    def test_prompt_comments_are_truncated(self) -> None:
        llm = _llm()
        body = "start " + ("x" * 2200) + " tail"
        issue = Issue(
            "owner/repo",
            1,
            "Add filter",
            "Use a filter control.",
            "alice",
            [],
            [IssueComment(7, "alice", body, "2026-06-05")],
        )

        llm.review("correctness", issue, "diff --git a/app.py b/app.py")

        _, _, prompt = llm.calls[-1]
        self.assertIn("start ", prompt)
        self.assertIn("...[truncated]", prompt)
        self.assertNotIn(" tail", prompt)

    def test_http_llm_infers_anthropic_provider_from_only_anthropic_key(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "x"}, clear=True),
        ):
            llm = HttpLLM(Config.from_env(Path(tmp)))

        self.assertEqual(llm.provider, "anthropic")

    def test_http_llm_rejects_model_when_matching_key_is_missing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"OPENAI_API_KEY": "x"}, clear=True),
            patch("autobot.llm.urllib.request.urlopen") as urlopen,
            self.assertRaises(LLMError) as raised,
        ):
            llm = HttpLLM(Config.from_env(Path(tmp)))
            llm.review(
                "correctness",
                _issue(),
                "diff --git a/app.py b/app.py",
                model="claude-sonnet-4-20250514",
            )

        urlopen.assert_not_called()
        self.assertIn("ANTHROPIC_API_KEY", str(raised.exception))

    def test_http_llm_routes_model_hint_to_matching_provider(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "x"},
                clear=True,
            ),
        ):
            llm = RoutingLLM(Config.from_env(Path(tmp)))
            llm.review(
                "correctness",
                _issue(),
                "diff --git a/app.py b/app.py",
                model="claude-sonnet-4-20250514",
            )

        self.assertEqual(llm.providers, [("anthropic", "claude-sonnet-4-20250514")])

    def test_pricing_uses_role_specific_env_vars(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "REVIEW_INPUT_PRICE_PER_1K": "0.001",
                "REVIEW_OUTPUT_PRICE_PER_1K": "0.002",
            },
            clear=True,
        ):
            self.assertEqual(_priced("review", 1000, 500), 0.002)

    def test_pricing_returns_none_when_prices_are_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_priced("triage", 1000, 1000))

    def test_test_author_pricing_falls_back_to_implement_prices(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "IMPLEMENT_INPUT_PRICE_PER_1K": "0.003",
                "IMPLEMENT_OUTPUT_PRICE_PER_1K": "0.006",
            },
            clear=True,
        ):
            self.assertEqual(_priced("test", 2000, 500), 0.009)

    def test_pricing_rejects_invalid_env_var(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "REVIEW_INPUT_PRICE_PER_1K": "not-a-number",
                    "REVIEW_OUTPUT_PRICE_PER_1K": "0.002",
                },
                clear=True,
            ),
            self.assertRaises(LLMError) as raised,
        ):
            _priced("review", 1000, 1000)

        self.assertIn("REVIEW_INPUT_PRICE_PER_1K", str(raised.exception))
        self.assertIn("must be a number", str(raised.exception))

    def test_parse_json_redacts_non_json_model_text(self) -> None:
        token = "ghp_" + ("A" * 36)

        with self.assertRaises(LLMError) as raised:
            _parse_json(f"not json {token}")

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))

    def test_parse_json_wraps_and_redacts_malformed_embedded_json(self) -> None:
        token = "ghp_" + ("A" * 36)

        with self.assertRaises(LLMError) as raised:
            _parse_json(f"prefix {{bad: '{token}'}} suffix")

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))
        self.assertIn("model returned malformed JSON", str(raised.exception))

    def test_openai_http_errors_are_redacted(self) -> None:
        token = "sk-" + ("A" * 40)
        error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(f'{{"error":"bad {token}"}}'.encode()),
        )

        with (
            patch("autobot.llm.urllib.request.urlopen", side_effect=error),
            self.assertRaises(LLMError) as raised,
        ):
            _post_json("https://api.openai.com/v1/chat/completions", token, {"model": "m"})

        self.assertNotIn(token, str(raised.exception))
        self.assertIn("[redacted-secret]", str(raised.exception))


def _llm() -> CapturingLLM:
    root = Path(tempfile.mkdtemp())
    return CapturingLLM(Config.from_env(root=root))


def _issue() -> Issue:
    return Issue("owner/repo", 1, "Add filter", "Use a dropdown.", "alice", [])


if __name__ == "__main__":
    unittest.main()
