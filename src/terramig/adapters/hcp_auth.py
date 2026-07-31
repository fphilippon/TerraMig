from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HCPTokenVerifier:
    """Verify an HCP Terraform token without persisting API response data."""

    def __init__(self, hostname: str, token: str, timeout_seconds: int = 20) -> None:
        self.hostname = hostname.strip().rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def test_connection(self) -> dict[str, str]:
        base_url = (
            self.hostname
            if self.hostname.startswith(("http://", "https://"))
            else f"https://{self.hostname}"
        )
        request = Request(
            f"{base_url}/api/v2/account/details",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/vnd.api+json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            raise RuntimeError(
                f"HCP Terraform rejected the token (HTTP {error.code})"
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "HCP Terraform authentication could not be verified"
            ) from error
        if not isinstance(payload.get("data"), dict):
            raise RuntimeError(
                "HCP Terraform authentication response was incomplete"
            )
        return {
            "status": "verified",
            "message": "HCP Terraform authentication verified",
        }
