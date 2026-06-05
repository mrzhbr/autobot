from __future__ import annotations

from dataclasses import asdict

from autobot.models import Usage, utc_now


class CostLedger:
    def __init__(self, data: dict | None = None) -> None:
        data = data or {}
        self.calls: list[dict] = list(data.get("calls", []))
        self.started_at = data.get("started_at") or utc_now()
        self.finished_at = data.get("finished_at")

    def add(self, usage: Usage | None) -> None:
        if usage is None:
            return
        row = asdict(usage)
        row["at"] = utc_now()
        self.calls.append(row)

    @property
    def input_tokens(self) -> int:
        return sum(int(call.get("input_tokens") or 0) for call in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(int(call.get("output_tokens") or 0) for call in self.calls)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def dollars(self) -> float | None:
        values = [call.get("dollars") for call in self.calls]
        if not values or any(value is None for value in values):
            return None
        return round(sum(float(value) for value in values), 6)

    def hit_budget(self, max_tokens: int | None, max_dollars: float | None) -> bool:
        if max_tokens is not None and self.total_tokens >= max_tokens:
            return True
        return max_dollars is not None and self.dollars is not None and self.dollars >= max_dollars

    def finish(self) -> None:
        self.finished_at = utc_now()

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "dollars": self.dollars,
        }
