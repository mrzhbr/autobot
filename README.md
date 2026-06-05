# Autonomous Teammate Prototype

This repository is a local, poll-based prototype of an autonomous coding teammate. It reads GitHub issues, decides whether the issue is ready, asks one batched clarification when it is not, resumes after a human reply, implements in a per-issue sandbox, reviews the diff, and opens a draft pull request.

The implementation is intentionally thin but end-to-end:

- `cli run --repo <owner/name> --issue <n>` processes one issue.
- `cli watch --repo <owner/name>` polls for issues assigned to or mentioning `AGENT_LOGIN`.
- SQLite stores durable per-issue state, including PR URLs, in `.autobot/state.db`.
- Outward actions are appended to `.autobot/audit.jsonl`.
- Pydantic validates structured LLM output before the pipeline acts on it.
- Live runs use GitHub Issues, GitHub PRs, Docker, and an OpenAI or Anthropic-compatible LLM path.
- `--dry-run --mock-llm` exercises the state machine without comments, pushes, PRs, Docker, or LLM calls.

## Setup

Use Python 3.12 or newer. Docker is required for live runs, but not for dry-run.

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
export REVIEW_MODELS=gpt-4.1,claude-sonnet-4-20250514
```

`LLM_PROVIDER` may be unset, `openai`, or `anthropic`; any other value fails preflight before processing an issue.

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
export TEST_INPUT_PRICE_PER_1K=0.002
export TEST_OUTPUT_PRICE_PER_1K=0.008
export REVIEW_INPUT_PRICE_PER_1K=0.002
export REVIEW_OUTPUT_PRICE_PER_1K=0.008
```

If `TEST_*` prices are unset, test-authoring uses the configured `IMPLEMENT_*` prices.

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

Within a poll, `watch` isolates per-issue failures: it prints a redacted JSON error row
for the failed issue and continues with the remaining actionable issues. `--once`
returns nonzero if any issue in that poll failed.

Check live-run prerequisites without posting comments, pushing branches, or opening PRs:

```sh
./cli doctor --repo owner/name --issue 123
```

Live doctor checks Git, git author identity, Docker, GitHub credentials, LLM credentials, model names, sandbox image/network/setup settings, and optional issue readability.

Live `run` and `watch` also fail fast when required GitHub or LLM credentials are missing, before cloning or processing an issue. Use `doctor` for the fuller read-only preflight.

Run a local dry-run against a public issue body:

```sh
./cli run --repo octocat/Hello-World --issue 1 --dry-run --mock-llm
```

Dry-run still reads the GitHub issue, but it writes only under `.autobot/work`, uses a generated local git repo, skips Docker, skips outward comments and labels, and returns `dry-run://draft-pr`.

Set `GITHUB_TOKEN` for dry-run reads when the public GitHub API rate limit is exhausted.

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

If a human reply still leaves the issue underspecified, the agent records that result,
keeps the processed replies in the issue record, advances the resume marker past
the latest reply, and waits for a new human comment instead of reprocessing the
same answer.

Dry-run waiting uses the current newest issue comment id as its resume marker, so historical comments are not treated as replies to a question that dry-run did not post.

If `MAX_ISSUE_TOKENS` or `MAX_ISSUE_DOLLARS` is reached, the agent records a `budget_pause`, moves the issue to `waiting`, and posts one human-facing notification in live mode. Human comments do not clear a budget pause; rerun after increasing the budget or changing the issue state.

If an issue title or body appears to require authentication, cryptography, secrets handling, or database migrations, or if the issue text or comments contain raw secret-like values, the agent pauses in `waiting` and asks for human ownership or a narrowed non-sensitive scope.

If processing fails unexpectedly before the agent reaches a human-waiting or PR state, the issue is marked `abandoned`; reruns return the stored blocked reason until the state record is cleared for an intentional retry.

If a draft PR is already recorded in state, reruns return the stored PR URL before cloning the repository again.

If GitHub reports that the pushed branch already has an open PR before the state row
has a stored URL, the adapter reuses that PR only when GitHub still marks it as a
draft.

## Adapters

Implemented defaults:

- GitHub Issues for `IssueTracker`
- GitHub git/API operations for `GitHost`
- issue comments for `ChatChannel`
- OpenAI or Anthropic HTTP calls for `LLM`

CI evidence combines legacy commit statuses and GitHub check runs for the pushed branch.

Issue reads include the newest comment page from GitHub pagination links, so clarification replies after the first 100 comments are still available to the resume loop without walking every page.

Documented stubs are included for Linear, Jira, and Slack in `src/autobot/stubs.py`.

Set `REVIEW_MODELS` to a comma-separated list to rotate reviewer lenses across more than one model. If unset, all reviewers use `REVIEW_MODEL`.

`MAX_REVIEW_ROUNDS` defaults to `3` and must stay between `1` and `3`, preserving
the prototype stopping rule that a draft PR cannot open without review and that
the review loop stops after at most three rounds.

Each review round stores its structured reviewer reports and blocking findings in
the issue record so the durable state shows what the reviewers accepted or asked
the implementer to fix.

## Safety

The prototype enforces these guardrails:

- Opens draft PRs only.
- Fails fast before live processing when required GitHub or LLM credentials are missing.
- Refuses to push default-like branches such as `main` and `master`.
- Does not force-push.
- Resets reused live clones to the remote default branch and removes ignored files before creating the issue branch.
- Stops before committing if Git cannot reliably inspect the staged diff.
- Runs implementation writes, tests, lint, and type checks through the Docker sandbox in live mode.
- Mirrors Docker file writes and directory deletes in dry-run mode.
- Rejects secret-like values in configured sandbox setup commands before Docker execution.
- Asks the LLM to author acceptance-test changes before implementation, records their baseline result, then runs authored, implementation-requested, and detected repo verification commands.
- Scans issue text before triage, proposed changes before disk writes, verification commands before execution, and generated diffs before review, commit, and PR creation for common secret-like values, including raw provider tokens.
- Includes untracked generated files in review and secret-scan diffs before committing.
- Redacts token-like values from CLI, GitHub command, GitHub HTTP/network, GitHub write payloads, LLM provider, doctor issue-read, SQLite state, abandoned-state, sandbox failure output, verification-output, PR body, audit, and issue-comment messages.
- Caps issue comments per processed issue with `COMMENT_LIMIT_PER_RUN`.
- Persists posted comment ids before audit and label metadata, preventing duplicate clarification comments after a local metadata failure.
- Records comment-audit failures as state warnings without undoing posted clarification, guardrail, or budget comments.
- Labels issues `agent-waiting`, `agent-working`, and `agent-pr-open` as state changes occur.
- Creates those GitHub labels on first use if the target repo does not already have them, tolerating concurrent creation.
- Records label and label-audit failures in state without undoing an already-posted comment or opened draft PR.
- Records push and draft-PR audit failures as state warnings without abandoning successful outward actions.
- Records outward comments, labels, pushes, and draft PRs to the audit log.

## Sandbox

Live mode starts one detached `docker run --rm` container per issue with the
checked-out repo mounted at `/work`, runs setup, writes, tests, lint, and type
checks through `docker exec`, then stops the container after sandbox work completes.

Useful env vars:

```sh
export SANDBOX_IMAGE=python:3.12-slim
export SANDBOX_NETWORK=none
export SANDBOX_SETUP_COMMAND="python -m pip install -e .[dev]"
export AUTO_TEST_COMMAND="python -m pytest"
```

`SANDBOX_NETWORK` defaults to `none`. Set it to `bridge` only when setup or verification commands must reach a package registry or another explicitly needed service.

If `SANDBOX_SETUP_COMMAND` is unset, live mode detects common setup profiles: Python installs requirements and the editable project, using `.[dev]` only when a `dev` extra is declared; Node uses the active lockfile manager, Go runs `go mod download`, and Rust runs `cargo fetch`.

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
