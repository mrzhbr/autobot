from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from autobot.scanner import redact_secret_like_values

DEFAULT_TIMEOUT_SECONDS = 240
DEFAULT_RETRIES = 2


class LLMHTTPError(RuntimeError):
    pass


def post_json(url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    return read_http_json(request, "OpenAI")


def read_http_json(
    request: urllib.request.Request,
    provider: str,
    data: bytes | None = None,
) -> dict[str, Any]:
    timeout = _env_int("LLM_HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, minimum=1)
    retries = _env_int("LLM_HTTP_RETRIES", DEFAULT_RETRIES, minimum=0)
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, data=data, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            if attempt < attempts:
                _sleep_before_retry(attempt)
                continue
            raise LLMHTTPError(_timeout_message(provider, attempts)) from exc
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            message = redact_secret_like_values(f"{provider} request failed: {exc.code} {payload}")
            raise LLMHTTPError(message) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError) and attempt < attempts:
                _sleep_before_retry(attempt)
                continue
            message = redact_secret_like_values(f"{provider} request failed: {exc}")
            raise LLMHTTPError(message) from exc
    raise LLMHTTPError(_timeout_message(provider, attempts))


def _timeout_message(provider: str, attempts: int) -> str:
    return f"{provider} request timed out after {attempts} attempts while reading response"


def _sleep_before_retry(attempt: int) -> None:
    time.sleep(min(attempt, 3))


def _env_int(name: str, default: int, minimum: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise LLMHTTPError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise LLMHTTPError(f"{name} must be at least {minimum}")
    return parsed
