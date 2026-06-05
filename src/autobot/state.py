from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from autobot.models import IssueRecord, IssueState, utc_now


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists issue_state (
                    repo text not null,
                    issue_number integer not null,
                    state text not null,
                    conversation_json text not null,
                    branch text,
                    plan_json text not null,
                    cost_json text not null,
                    blocked_on text,
                    review_rounds integer not null,
                    files_touched_json text not null,
                    pr_url text,
                    created_at text not null,
                    updated_at text not null,
                    primary key (repo, issue_number)
                )
                """
            )
            _ensure_column(conn, "issue_state", "pr_url", "text")

    def get(self, repo: str, issue_number: int) -> IssueRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from issue_state where repo = ? and issue_number = ?",
                (repo, issue_number),
            ).fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

    def ensure(self, repo: str, issue_number: int) -> IssueRecord:
        record = self.get(repo, issue_number)
        if record is not None:
            return record
        record = IssueRecord(repo=repo, issue_number=issue_number)
        self.upsert(record)
        return record

    def upsert(self, record: IssueRecord) -> None:
        record.updated_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into issue_state (
                    repo, issue_number, state, conversation_json, branch, plan_json,
                    cost_json, blocked_on, review_rounds, files_touched_json, pr_url,
                    created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(repo, issue_number) do update set
                    state = excluded.state,
                    conversation_json = excluded.conversation_json,
                    branch = excluded.branch,
                    plan_json = excluded.plan_json,
                    cost_json = excluded.cost_json,
                    blocked_on = excluded.blocked_on,
                    review_rounds = excluded.review_rounds,
                    files_touched_json = excluded.files_touched_json,
                    pr_url = excluded.pr_url,
                    updated_at = excluded.updated_at
                """,
                (
                    record.repo,
                    record.issue_number,
                    record.state.value,
                    json.dumps(record.conversation, sort_keys=True),
                    record.branch,
                    json.dumps(record.plan, sort_keys=True),
                    json.dumps(record.cost, sort_keys=True),
                    record.blocked_on,
                    record.review_rounds,
                    json.dumps(record.files_touched, sort_keys=True),
                    record.pr_url,
                    record.created_at,
                    record.updated_at,
                ),
            )

    def list_waiting(self) -> list[IssueRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "select * from issue_state where state = ? order by updated_at",
                (IssueState.WAITING.value,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    @staticmethod
    def _loads(value: str, fallback: Any) -> Any:
        if not value:
            return fallback
        return json.loads(value)

    def _record_from_row(self, row: sqlite3.Row) -> IssueRecord:
        conversation = self._loads(row["conversation_json"], {})
        return IssueRecord(
            repo=row["repo"],
            issue_number=int(row["issue_number"]),
            state=IssueState(row["state"]),
            conversation=conversation,
            branch=row["branch"],
            plan=self._loads(row["plan_json"], {}),
            cost=self._loads(row["cost_json"], {}),
            blocked_on=row["blocked_on"],
            review_rounds=int(row["review_rounds"]),
            files_touched=self._loads(row["files_touched_json"], []),
            pr_url=row["pr_url"] or conversation.get("pr_url"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if name not in columns:
        conn.execute(f"alter table {table} add column {name} {definition}")
