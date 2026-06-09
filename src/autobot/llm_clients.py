from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

from autobot.config import OPENROUTER_MODEL_PREFIX
from autobot.llm_http import LLMHTTPError, post_json, read_http_json


def openai_json(model: str, prompt: str) -> dict[str, Any]:
    token = os.getenv("OPENAI_API_KEY")
    if not token:
        raise LLMHTTPError("OPENAI_API_KEY is required unless AUTOBOT_MOCK_LLM=1")
    body = _json_body(model, prompt)
    return post_json("https://api.openai.com/v1/chat/completions", token, body)


def openrouter_json(model: str, prompt: str) -> dict[str, Any]:
    token = os.getenv("OPENROUTER_API_KEY")
    if not token:
        raise LLMHTTPError("OPENROUTER_API_KEY is required unless AUTOBOT_MOCK_LLM=1")
    url = _openrouter_base_url().rstrip("/") + "/chat/completions"
    body = _json_body(_openrouter_model(model), prompt)
    try:
        return _openrouter_post(url, token, body)
    except LLMHTTPError as exc:
        if not _can_retry_without_response_format(str(exc)):
            raise
        fallback = dict(body)
        fallback.pop("response_format", None)
        return _openrouter_post(url, token, fallback)


def anthropic_json(model: str, prompt: str) -> dict[str, Any]:
    token = os.getenv("ANTHROPIC_API_KEY")
    if not token:
        raise LLMHTTPError("ANTHROPIC_API_KEY is required unless AUTOBOT_MOCK_LLM=1")
    request = urllib.request.Request("https://api.anthropic.com/v1/messages", method="POST")
    request.add_header("x-api-key", token)
    request.add_header("anthropic-version", "2023-06-01")
    request.add_header("content-type", "application/json")
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 4096,
            "system": "Return only valid JSON.",
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    return read_http_json(request, "Anthropic", body)


def _json_body(model: str, prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }


def _openrouter_model(model: str) -> str:
    normalized = model.strip()
    if normalized.lower().startswith(OPENROUTER_MODEL_PREFIX):
        return normalized[len(OPENROUTER_MODEL_PREFIX) :]
    return normalized


def _openrouter_base_url() -> str:
    return os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")


def _openrouter_post(url: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    if referer := os.getenv("OPENROUTER_HTTP_REFERER"):
        request.add_header("HTTP-Referer", referer)
    if title := os.getenv("OPENROUTER_APP_TITLE"):
        request.add_header("X-OpenRouter-Title", title)
    return read_http_json(request, "OpenRouter")


def _can_retry_without_response_format(message: str) -> bool:
    lowered = message.lower()
    return "response_format" in lowered and any(
        marker in lowered for marker in ("unsupported", "not support", "invalid", "unknown")
    )
