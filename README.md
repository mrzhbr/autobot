# Autonomous Teammate Prototype

This repository is a local, poll-based prototype of an autonomous coding teammate. It reads GitHub issues, decides whether the issue is ready, asks one batched clarification when it is not, resumes after a human reply, implements in a per-issue sandbox, reviews the diff, and opens a draft pull request.

The implementation is intentionally thin but end-to-end:

- `cli run --repo <owner/name> --issue <n>` processes one issue.
- `cli watch --repo <owner/name>` polls for issues assigned to or mentioning `AGENT_LOGIN`.
- SQLite stores durable per-issue state in `.autobot/state.db`.
- Outward actions are appended to `.autobot/audit.jsonl`.
- Pydantic validates structured LLM output before the pipeline acts on it.
- Live runs use GitHub Issues, GitHub PRs, Docker, and an OpenAI or Anthropic-compatible LLM path.
- `--dry-run --mock-llm` exercises the state machine without comments, pushes, PRs, Docker, or LLM calls.

## Setup

Use Python 3.12 or newer and Docker.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

For a live GitHub run:

```sh
export GITHUB_TOKEN=ghp_...
export AGENT_LOGIN=your-bot-login
export OPENAI_API_KEY=sk-...
export LLM_PROVIDER=openai
export TRIAGE_MODEL=gpt-4.1
export IMPLEMENT_MODEL=gpt-4.1
export REVIEW_MODEL=gpt-4.1
```

Anthropic can be used instead:

```sh
export ANTHROPIC_API_KEY=...
export LLM_PROVIDER=anthropic
export MODEL=claude-sonnet-4-20250514
```

Optional cost pricing is read from env. If unset, token usage is recorded and dollars are reported as `not configured`.

```sh
export TRIAGE_INPUT_PRICE_PER_1K=0.002
export TRIAGE_OUTPUT_PRICE_PER_1K=0.008
export IMPLEMENT_INPUT_PRICE_PER_1K=0.002
export IMPLEMENT_OUTPUT_PRICE_PER_1K=0.008
export REVIEW_INPUT_PRICE_PER_1K=0.002
export REVIEW_OUTPUT_PRICE_PER_1K=0.008
```

## Commands

Process one issue:

```sh
./cli run --repo owner/name --issue 123
```

Poll actionable issues once:

```sh
./cli watch --repo owner/name --once
```

Poll continuously:

```sh
./cli watch --repo owner/name --interval 60
```

Run a local dry-run against a public issue body:

```sh
./cli run --repo octocat/Hello-World --issue 1 --dry-run --mock-llm
```

Dry-run still reads the GitHub issue, but it writes only under `.autobot/work`, uses a generated local git repo, skips Docker, skips outward comments and labels, and returns `dry-run://draft-pr`.

Command output includes the per-issue summary required for review:

- branch
- files touched
- review rounds
- cost ledger with tokens, dollars, and wall-clock seconds
- verification commands run
- current blocked reason, if any

## State Machine

Each issue follows:

```text
seen -> triaged -> needs_spec -> asked -> waiting -> resumed -> spec_ready
     -> implementing -> review_loop -> pr_open
```

If triage returns `ready: false`, the agent posts one comment with up to three questions, stores the comment id, marks the issue `agent-waiting`, and exits. On the next `run` or `watch`, comments with ids greater than the stored question comment and not authored by the bot are folded into the issue record before triage is rerun.

If `MAX_ISSUE_TOKENS` or `MAX_ISSUE_DOLLARS` is reached, the agent records a `budget_pause`, moves the issue to `waiting`, and posts one human-facing notification in live mode. Rerun after increasing the budget or changing the issue state.

If an issue appears to require authentication, cryptography, secrets handling, or database migrations, the agent pauses in `waiting` and asks for human ownership or a narrowed non-sensitive scope.

## Adapters

Implemented defaults:

- GitHub Issues for `IssueTracker`
- GitHub git/API operations for `GitHost`
- issue comments for `ChatChannel`
- OpenAI or Anthropic HTTP calls for `LLM`

Documented stubs are included for Linear, Jira, and Slack in `src/autobot/stubs.py`.

## Safety

The prototype enforces these guardrails:

- Opens draft PRs only.
- Refuses to push default-like branches such as `main` and `master`.
- Does not force-push.
- Runs implementation writes, tests, lint, and type checks through the Docker sandbox in live mode.
- Scans the final diff for common secret-like values before commit and PR creation.
- Caps issue comments per run with `COMMENT_LIMIT_PER_RUN`.
- Labels issues `agent-waiting`, `agent-working`, and `agent-pr-open` as state changes occur.
- Records outward comments, labels, pushes, and draft PRs to the audit log.

## Sandbox

Live mode uses `docker run --rm` with the checked-out repo mounted at `/work`.

Useful env vars:

```sh
export SANDBOX_IMAGE=python:3.12-slim
export SANDBOX_SETUP_COMMAND="python -m pip install -e .[dev]"
export AUTO_TEST_COMMAND="python -m pytest"
```

If `AUTO_TEST_COMMAND` is unset, the prototype detects common project files and chooses test commands such as `pytest`, `npm test`, `go test ./...`, `cargo test`, `unittest`, or `compileall`. It also detects common lint/type checks such as Ruff, mypy, pyright, `npm run lint`, `npm run typecheck`, `go vet ./...`, and `cargo clippy --all-targets`.

## Development

Run local tests after setup:

```sh
python -m unittest discover -s tests
```

Or use the project environment directly with `uv`:

```sh
PYTHONPATH=src UV_CACHE_DIR=.autobot/uv-cache uv run python -m unittest discover -s tests
UV_CACHE_DIR=.autobot/uv-cache uv run ruff check .
UV_CACHE_DIR=.autobot/uv-cache uv run ruff format --check .
```
