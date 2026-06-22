from __future__ import annotations

import base64
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from autobot.config import Config
from autobot.github_auth import (
    GitHubAppCredentials,
    GitHubAppTokenSource,
    GitHubAuthError,
    _github_app_jwt,
    missing_github_app_settings,
    resolve_github_token,
)


def decode_segment(value: str) -> dict:
    padded = value + ("=" * (-len(value) % 4))
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.headers = {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class GitHubAuthTests(unittest.TestCase):
    def test_resolve_github_token_prefers_direct_token(self) -> None:
        env = {
            "GITHUB_TOKEN": "direct-token",
            "GITHUB_APP_ID": "123",
            "GITHUB_APP_INSTALLATION_ID": "456",
            "GITHUB_APP_PRIVATE_KEY_PATH": "/missing/key.pem",
        }
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", env, clear=True),
            patch("autobot.github_auth.GitHubAppTokenSource.installation_token") as mint,
        ):
            config = Config.from_env(Path(tmp))

            token = resolve_github_token(config)

        self.assertEqual(token, "direct-token")
        mint.assert_not_called()

    def test_missing_github_app_settings_reports_partial_configuration(self) -> None:
        env = {"GITHUB_APP_ID": "123"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp))

        self.assertEqual(
            missing_github_app_settings(config),
            ["GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH"],
        )
        with self.assertRaises(GitHubAuthError) as raised:
            resolve_github_token(config)
        self.assertIn("GITHUB_APP_INSTALLATION_ID", str(raised.exception))

    def test_partial_github_app_settings_do_not_break_dry_run(self) -> None:
        env = {"GITHUB_APP_ID": "123"}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", env, clear=True):
            config = Config.from_env(Path(tmp), dry_run=True, mock_llm=True)

            token = resolve_github_token(config)

        self.assertIsNone(token)

    def test_github_app_jwt_uses_rs256_claims_and_private_key_path(self) -> None:
        with TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "app.pem"
            key_path.write_text("placeholder", encoding="utf-8")
            credentials = GitHubAppCredentials("123", "456", key_path)
            completed = SimpleNamespace(returncode=0, stdout=b"signature", stderr=b"")

            with (
                patch("autobot.github_auth.time.time", return_value=1000),
                patch("autobot.github_auth.subprocess.run", return_value=completed) as run,
            ):
                token = _github_app_jwt(credentials)

        header, payload, signature = token.split(".")
        self.assertEqual(decode_segment(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(decode_segment(payload), {"exp": 1540, "iat": 940, "iss": "123"})
        self.assertEqual(signature, "c2lnbmF0dXJl")
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command, ["openssl", "dgst", "-sha256", "-sign", str(key_path)])
        self.assertEqual(run.call_args.kwargs["input"], f"{header}.{payload}".encode("ascii"))

    def test_github_app_token_source_exchanges_jwt_for_installation_token(self) -> None:
        with TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "app.pem"
            key_path.write_text("placeholder", encoding="utf-8")
            credentials = GitHubAppCredentials("123", "456", key_path)
            seen = {}

            def fake_urlopen(request, timeout, context=None):
                seen["url"] = request.full_url
                seen["timeout"] = timeout
                seen["authorization"] = request.get_header("Authorization")
                seen["accept"] = request.get_header("Accept")
                seen["version"] = request.get_header("X-github-api-version")
                return FakeResponse({"token": "installation-token"})

            with (
                patch("autobot.github_auth._github_app_jwt", return_value="jwt"),
                patch("autobot.http_transport.urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                token = GitHubAppTokenSource(credentials).installation_token()

        self.assertEqual(token, "installation-token")
        self.assertEqual(
            seen,
            {
                "url": "https://api.github.com/app/installations/456/access_tokens",
                "timeout": 30,
                "authorization": "Bearer jwt",
                "accept": "application/vnd.github+json",
                "version": "2022-11-28",
            },
        )
