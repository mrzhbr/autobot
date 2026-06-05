from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autobot.diffing import render_untracked_diff


class DiffingTests(unittest.TestCase):
    def test_render_untracked_diff_renders_new_file_hunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "tests").mkdir()
            (repo_dir / "tests" / "test_new.py").write_text(
                "def test_new():\n    pass\n", encoding="utf-8"
            )

            diff = render_untracked_diff(repo_dir, ["tests/test_new.py"])

        self.assertIn("diff --git a/tests/test_new.py b/tests/test_new.py", diff)
        self.assertIn("new file mode 100644", diff)
        self.assertIn("+def test_new():", diff)

    def test_render_untracked_diff_skips_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "build").mkdir()

            diff = render_untracked_diff(repo_dir, ["build"])

        self.assertEqual(diff, "")


if __name__ == "__main__":
    unittest.main()
