from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autobot.config import Config
from autobot.http_transport import urlopen
from autobot.scanner import redact_secret_like_values

GITHUB_APP_ENV_VARS = (
    "GITHUB_APP_ID",
    "GITHUB_APP_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
)


class GitHubAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubAppCredentials:
    app_id: str
    installation_id: str
    private_key_path: Path


def missing_github_app_settings(config: Config) -> list[str]:
    if config.github_token:
        return []
    values = {
        "GITHUB_APP_ID": config.github_app_id,
        "GITHUB_APP_INSTALLATION_ID": config.github_app_installation_id,
        "GITHUB_APP_PRIVATE_KEY_PATH": str(config.github_app_private_key_path or ""),
    }
    if not any(values.values()):
        return []
    return [name for name, value in values.items() if not value]


def github_app_credentials(config: Config) -> GitHubAppCredentials | None:
    missing = missing_github_app_settings(config)
    if missing:
        return None
    if (
        not config.github_app_id
        or not config.github_app_installation_id
        or not config.github_app_private_key_path
    ):
        return None
    return GitHubAppCredentials(
        app_id=config.github_app_id,
        installation_id=config.github_app_installation_id,
        private_key_path=config.github_app_private_key_path,
    )


def has_github_auth(config: Config) -> bool:
    return bool(config.github_token or github_app_credentials(config))


def github_auth_requirement_message(config: Config) -> str:
    missing = missing_github_app_settings(config)
    if missing:
        return "GitHub App auth is missing: " + ", ".join(missing)
    return "GITHUB_TOKEN or GitHub App credentials are required for live runs"


def resolve_github_token(config: Config) -> str | None:
    if config.github_token:
        return config.github_token
    if config.dry_run and missing_github_app_settings(config):
        return None
    credentials = github_app_credentials(config)
    if not credentials:
        missing = missing_github_app_settings(config)
        if missing:
            raise GitHubAuthError("GitHub App auth is missing: " + ", ".join(missing))
        return None
    return GitHubAppTokenSource(credentials).installation_token()


class GitHubAppTokenSource:
    def __init__(self, credentials: GitHubAppCredentials) -> None:
        self.credentials = credentials

    def installation_token(self) -> str:
        jwt = _github_app_jwt(self.credentials)
        path = f"/app/installations/{self.credentials.installation_id}/access_tokens"
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=b"{}",
            method="POST",
        )
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {jwt}")
        request.add_header("Content-Type", "application/json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise GitHubAuthError("GitHub App installation token request timed out") from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = redact_secret_like_values(
                f"GitHub App installation token request failed: {exc.code} {body}"
            )
            raise GitHubAuthError(message) from exc
        except urllib.error.URLError as exc:
            message = redact_secret_like_values(
                f"GitHub App installation token request failed: {exc.reason}"
            )
            raise GitHubAuthError(message) from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubAuthError("GitHub App installation token response did not include token")
        return token


def _github_app_jwt(credentials: GitHubAppCredentials) -> str:
    if not credentials.private_key_path.is_file():
        raise GitHubAuthError(
            f"GITHUB_APP_PRIVATE_KEY_PATH does not exist: {credentials.private_key_path}"
        )
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": credentials.app_id,
    }
    signing_input = (_base64url_json(header) + "." + _base64url_json(payload)).encode("ascii")
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(credentials.private_key_path)],
        input=signing_input,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace")
        raise GitHubAuthError(
            redact_secret_like_values("failed to sign GitHub App JWT with openssl: " + output)
        )
    return signing_input.decode("ascii") + "." + _base64url_bytes(result.stdout)


def _base64url_json(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _base64url_bytes(encoded)


def _base64url_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
