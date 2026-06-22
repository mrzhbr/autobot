from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from autobot.models import Issue, IssueComment
from autobot.scanner import redact_secret_like_values

LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"


class LinearError(RuntimeError):
    pass


class LinearIssueTracker:
    """Linear IssueTracker adapter using TEAM-123 identifiers.

    Autobot state still stores ``issue_number`` as an integer. In Linear mode the
    configured team key supplies the string prefix, so issue number 123 maps to
    ``<LINEAR_TEAM_KEY>-123``.
    """

    def __init__(
        self,
        api_key: str | None,
        agent_login: str | None = None,
        team_key: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.agent_login = agent_login
        self.team_key = team_key

    def list_actionable(self, repo_or_team: str) -> list[int]:
        team_key = self._team_key(repo_or_team)
        variables: dict[str, Any] = {
            "teamKey": team_key,
            "label": "agent-ready",
        }
        if self.agent_login:
            query = _LINEAR_ACTIONABLE_WITH_AGENT_QUERY
            variables["agent"] = self.agent_login
        else:
            query = _LINEAR_ACTIONABLE_LABEL_QUERY
        data = self._graphql(query, variables)
        issues = data.get("issues", {}).get("nodes", [])
        if not isinstance(issues, list):
            raise LinearError("Linear actionable query returned an invalid issue list")
        return sorted(
            number
            for item in issues
            if isinstance(item, dict)
            for number in [_issue_number(item.get("identifier"))]
            if number is not None
        )

    def get(self, repo_or_team: str, issue_number: int) -> Issue:
        identifier = self._identifier(repo_or_team, issue_number)
        data = self._graphql(_LINEAR_ISSUE_QUERY, {"identifier": identifier})
        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise LinearError(f"Linear issue {identifier} was not found")
        comments = issue.get("comments", {}).get("nodes", [])
        if not isinstance(comments, list):
            raise LinearError(f"Linear issue {identifier} returned invalid comments")
        return Issue(
            repo=repo_or_team,
            number=_issue_number(issue.get("identifier")) or issue_number,
            title=str(issue.get("title") or ""),
            body=str(issue.get("description") or ""),
            author=_user_name(issue.get("creator")),
            labels=_labels(issue.get("labels")),
            comments=[_comment(item) for item in comments if isinstance(item, dict)],
        )

    def comment(self, repo_or_team: str, issue_number: int, text: str) -> int:
        issue_id = self._issue_id(repo_or_team, issue_number)
        data = self._graphql(
            _LINEAR_COMMENT_CREATE_MUTATION,
            {"issueId": issue_id, "body": redact_secret_like_values(text)},
        )
        comment = data.get("commentCreate", {}).get("comment")
        if not isinstance(comment, dict):
            raise LinearError("Linear commentCreate returned no comment")
        return _comment_id(comment)

    def set_label(self, repo_or_team: str, issue_number: int, label: str) -> None:
        issue = self._issue_for_labels(repo_or_team, issue_number)
        team_id = _required_str(issue.get("team", {}).get("id"), "Linear team id")
        label_id = _find_label_id(issue.get("labels"), label)
        if label_id is None:
            label_id = self._create_label(team_id, label)
        current = [
            str(item["id"])
            for item in issue.get("labels", {}).get("nodes", [])
            if isinstance(item, dict) and item.get("id")
        ]
        if label_id in current:
            return
        self._graphql(
            _LINEAR_ISSUE_UPDATE_LABELS_MUTATION,
            {
                "id": _required_str(issue.get("id"), "Linear issue id"),
                "labelIds": [*current, label_id],
            },
        )

    def _issue_id(self, repo_or_team: str, issue_number: int) -> str:
        issue = self._graphql(
            _LINEAR_ISSUE_ID_QUERY,
            {"identifier": self._identifier(repo_or_team, issue_number)},
        ).get("issue")
        if not isinstance(issue, dict):
            raise LinearError("Linear issue was not found")
        return _required_str(issue.get("id"), "Linear issue id")

    def _issue_for_labels(self, repo_or_team: str, issue_number: int) -> dict[str, Any]:
        issue = self._graphql(
            _LINEAR_LABELS_QUERY,
            {"identifier": self._identifier(repo_or_team, issue_number)},
        ).get("issue")
        if not isinstance(issue, dict):
            raise LinearError("Linear issue was not found")
        return issue

    def _create_label(self, team_id: str, label: str) -> str:
        data = self._graphql(
            _LINEAR_LABEL_CREATE_MUTATION,
            {"teamId": team_id, "name": label},
        )
        created = data.get("issueLabelCreate", {}).get("issueLabel")
        if not isinstance(created, dict):
            raise LinearError("Linear issueLabelCreate returned no label")
        return _required_str(created.get("id"), "Linear label id")

    def _identifier(self, repo_or_team: str, issue_number: int) -> str:
        return f"{self._team_key(repo_or_team)}-{issue_number}"

    def _team_key(self, repo_or_team: str) -> str:
        if self.team_key:
            return self.team_key
        if "/" in repo_or_team:
            raise LinearError("LINEAR_TEAM_KEY is required when --repo is a GitHub repository")
        return repo_or_team

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise LinearError("LINEAR_API_KEY is required")
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(LINEAR_GRAPHQL_URL, data=body, method="POST")
        request.add_header("Authorization", self.api_key)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise LinearError("Linear GraphQL request timed out") from exc
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise LinearError(self._redact(f"Linear GraphQL failed: {exc.code} {payload}")) from exc
        except urllib.error.URLError as exc:
            raise LinearError(self._redact(f"Linear GraphQL failed: {exc.reason}")) from exc
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise LinearError(self._redact(f"Linear GraphQL errors: {errors}"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise LinearError("Linear GraphQL returned no data")
        return data

    def _redact(self, message: str) -> str:
        redacted = redact_secret_like_values(message)
        if self.api_key:
            redacted = redacted.replace(self.api_key, "[redacted-secret]")
        return redacted


_LINEAR_ISSUE_QUERY = """
query AutobotIssue($identifier: String!) {
  issue(id: $identifier) {
    id
    identifier
    title
    description
    creator { name displayName email }
    labels { nodes { id name } }
    comments(first: 100) {
      nodes { id body createdAt user { name displayName email } }
    }
  }
}
"""

_LINEAR_ISSUE_ID_QUERY = """
query AutobotIssueId($identifier: String!) {
  issue(id: $identifier) { id }
}
"""

_LINEAR_LABELS_QUERY = """
query AutobotIssueLabels($identifier: String!) {
  issue(id: $identifier) {
    id
    team { id }
    labels { nodes { id name } }
  }
}
"""

_LINEAR_ACTIONABLE_LABEL_QUERY = """
query AutobotActionable($teamKey: String!, $label: String!) {
  issues(
    first: 50
    filter: {
      team: { key: { eq: $teamKey } }
      state: { type: { nin: ["completed", "canceled"] } }
      labels: { name: { eq: $label } }
    }
  ) {
    nodes { identifier }
  }
}
"""

_LINEAR_ACTIONABLE_WITH_AGENT_QUERY = """
query AutobotActionableForAgent($teamKey: String!, $label: String!, $agent: String!) {
  issues(
    first: 50
    filter: {
      team: { key: { eq: $teamKey } }
      state: { type: { nin: ["completed", "canceled"] } }
      or: [
        { labels: { name: { eq: $label } } }
        { assignee: { name: { eq: $agent } } }
        { assignee: { displayName: { eq: $agent } } }
      ]
    }
  ) {
    nodes { identifier }
  }
}
"""

_LINEAR_COMMENT_CREATE_MUTATION = """
mutation AutobotCreateComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id createdAt }
  }
}
"""

_LINEAR_LABEL_CREATE_MUTATION = """
mutation AutobotCreateLabel($teamId: String!, $name: String!) {
  issueLabelCreate(input: { teamId: $teamId, name: $name }) {
    success
    issueLabel { id name }
  }
}
"""

_LINEAR_ISSUE_UPDATE_LABELS_MUTATION = """
mutation AutobotApplyLabel($id: String!, $labelIds: [String!]) {
  issueUpdate(id: $id, input: { labelIds: $labelIds }) {
    success
  }
}
"""


def _issue_number(identifier: Any) -> int | None:
    if not isinstance(identifier, str) or "-" not in identifier:
        return None
    suffix = identifier.rsplit("-", 1)[-1]
    try:
        return int(suffix)
    except ValueError:
        return None


def _labels(value: Any) -> list[str]:
    nodes = value.get("nodes", []) if isinstance(value, dict) else []
    return [str(item.get("name")) for item in nodes if isinstance(item, dict) and item.get("name")]


def _comment(value: dict[str, Any]) -> IssueComment:
    return IssueComment(
        id=_comment_id(value),
        author=_user_name(value.get("user")),
        body=str(value.get("body") or ""),
        created_at=str(value.get("createdAt") or ""),
    )


def _comment_id(value: dict[str, Any]) -> int:
    created = str(value.get("createdAt") or "")
    parsed = _parse_linear_time(created)
    if parsed is not None:
        return int(parsed.timestamp() * 1_000_000)
    raw_id = str(value.get("id") or "")
    digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:15]
    return int(digest, 16)


def _parse_linear_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _user_name(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    for key in ("displayName", "name", "email", "id"):
        if value.get(key):
            return str(value[key])
    return "unknown"


def _find_label_id(labels: Any, name: str) -> str | None:
    nodes = labels.get("nodes", []) if isinstance(labels, dict) else []
    for item in nodes:
        if isinstance(item, dict) and item.get("name") == name and item.get("id"):
            return str(item["id"])
    return None


def _required_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise LinearError(f"{name} missing from Linear response")
    return value
