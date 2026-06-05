from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autobot.config import Config
from autobot.llm import HttpLLM
from autobot.models import ContextFile, Issue, Usage


class CapturingLLM(HttpLLM):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.calls: list[tuple[str, str, str]] = []

    def _json_call(self, role: str, model: str, prompt: str):
        self.calls.append((role, model, prompt))
        return (
            {
                "plan": ["Do the work."],
                "changes": [{"path": "README.md", "content": "# Demo\n"}],
                "test_commands": ["python -m pytest"],
            },
            Usage(role, model, 1, 1, 0.001),
        )


class LLMTests(unittest.TestCase):
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


def _llm() -> CapturingLLM:
    root = Path(tempfile.mkdtemp())
    return CapturingLLM(Config.from_env(root=root))


def _issue() -> Issue:
    return Issue("owner/repo", 1, "Add filter", "Use a dropdown.", "alice", [])


if __name__ == "__main__":
    unittest.main()
