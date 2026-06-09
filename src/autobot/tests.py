from __future__ import annotations

import json
from pathlib import Path


def detect_test_commands(repo_dir: Path, configured: str | None) -> list[str]:
    return detect_verification_commands(repo_dir, configured).tests


def merge_verification_commands(
    authored_tests: list[str],
    implementation_tests: list[str],
    detected: VerificationCommands,
) -> list[str]:
    tests = [*authored_tests, *implementation_tests, *detected.tests]
    tests = list(dict.fromkeys(tests))
    return [*tests, *detected.lint, *detected.types]


class VerificationCommands:
    def __init__(
        self,
        tests: list[str],
        lint: list[str] | None = None,
        types: list[str] | None = None,
    ) -> None:
        self.tests = tests
        self.lint = lint or []
        self.types = types or []

    def all(self) -> list[str]:
        return [*self.tests, *self.lint, *self.types]


def detect_verification_commands(repo_dir: Path, configured: str | None) -> VerificationCommands:
    if configured:
        return VerificationCommands(tests=[configured])
    if (repo_dir / "pyproject.toml").exists() or (repo_dir / "setup.py").exists():
        return _python_commands(repo_dir)
    if (repo_dir / "package.json").exists():
        return _node_commands(repo_dir)
    if (repo_dir / "go.mod").exists():
        return VerificationCommands(tests=["go test ./..."], lint=["go vet ./..."])
    if (repo_dir / "Cargo.toml").exists():
        return VerificationCommands(tests=["cargo test"], lint=["cargo clippy --all-targets"])
    if (repo_dir / "tests").exists():
        return VerificationCommands(tests=["python -m unittest discover -s tests"])
    return VerificationCommands(tests=["python -m compileall ."])


def _python_commands(repo_dir: Path) -> VerificationCommands:
    lint = []
    types = []
    pyproject = _read_text(repo_dir / "pyproject.toml")
    if "[tool.ruff" in pyproject:
        lint.extend(
            [
                _python_tool(repo_dir, "ruff check ."),
                _python_tool(repo_dir, "ruff format --check ."),
            ]
        )
    if "[tool.mypy" in pyproject or (repo_dir / "mypy.ini").exists():
        types.append(_python_tool(repo_dir, "mypy ."))
    if "[tool.pyright" in pyproject or (repo_dir / "pyrightconfig.json").exists():
        types.append(_python_tool(repo_dir, "pyright"))
    return VerificationCommands(tests=[_python_module(repo_dir, "pytest")], lint=lint, types=types)


def _python_module(repo_dir: Path, module: str) -> str:
    if (repo_dir / "pyproject.toml").exists():
        return f"UV_PROJECT_ENVIRONMENT=.venv .autobot-bootstrap/bin/uv run python -m {module}"
    return f".venv/bin/python -m {module}"


def _python_tool(repo_dir: Path, command: str) -> str:
    if (repo_dir / "pyproject.toml").exists():
        return f"UV_PROJECT_ENVIRONMENT=.venv .autobot-bootstrap/bin/uv run {command}"
    return f".venv/bin/python -m {command}"


def _node_commands(repo_dir: Path) -> VerificationCommands:
    scripts = _package_scripts(repo_dir / "package.json")
    tests = ["npm test"]
    lint = []
    types = []
    if "lint" in scripts:
        lint.append("npm run lint")
    if "typecheck" in scripts:
        types.append("npm run typecheck")
    elif "type-check" in scripts:
        types.append("npm run type-check")
    return VerificationCommands(tests=tests, lint=lint, types=types)


def _package_scripts(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
