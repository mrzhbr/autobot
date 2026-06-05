from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autobot.context import format_context, gather_context
from autobot.models import ContextFile, Issue, IssueComment


class ContextTests(unittest.TestCase):
    def test_priority_files_are_included_first_and_content_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("r" * 50, encoding="utf-8")
            (root / "feature.py").write_text("dropdown behavior\n", encoding="utf-8")

            files = gather_context(root, _issue("Add dropdown"), max_files=2, max_bytes=10)

        self.assertEqual(files[0].path, "README.md")
        self.assertEqual(files[0].content, "r" * 10)
        self.assertEqual(files[1].path, "feature.py")

    def test_human_comments_feed_keyword_matching_after_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "filters").mkdir()
            (root / "filters" / "dropdown.py").write_text(
                "def dropdown(): pass\n",
                encoding="utf-8",
            )

            issue = _issue(
                "Add filter control",
                comments=[IssueComment(2, "alice", "Use a dropdown.", "2026-06-05T00:01:00Z")],
            )
            files = gather_context(root, issue, max_files=3)

        self.assertIn("filters/dropdown.py", [item.path for item in files])

    def test_ignored_dirs_and_binary_files_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# Demo\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dropdown.py").write_text("ignored\n", encoding="utf-8")
            (root / "dropdown.bin").write_bytes(b"dropdown")

            files = gather_context(root, _issue("Add dropdown"), max_files=5)

        paths = [item.path for item in files]
        self.assertNotIn("node_modules/dropdown.py", paths)
        self.assertNotIn("dropdown.bin", paths)

    def test_formatted_context_redacts_secret_like_paths_and_content(self) -> None:
        token = "ghp_" + ("A" * 36)

        formatted = format_context([ContextFile(f"docs/{token}.md", f"Do not expose {token}\n")])

        self.assertNotIn(token, formatted)
        self.assertEqual(formatted.count("[redacted-secret]"), 2)


def _issue(title: str, comments: list[IssueComment] | None = None) -> Issue:
    return Issue("owner/repo", 1, title, "Please implement it.", "alice", [], comments or [])


if __name__ == "__main__":
    unittest.main()
