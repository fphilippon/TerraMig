import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

from terramig.adapters.hcp_auth import HCPTokenVerifier


class HCPTokenVerifierTests(unittest.TestCase):
    def test_read_only_account_request_verifies_token(self) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"data": {"id": "user-123", "type": "users"}}
        ).encode()
        response.__enter__.return_value = response
        with patch(
            "terramig.adapters.hcp_auth.urlopen",
            return_value=response,
        ) as request:
            result = HCPTokenVerifier(
                "app.terraform.io", "sensitive-token"
            ).test_connection()

        sent = request.call_args.args[0]
        self.assertEqual(
            sent.full_url,
            "https://app.terraform.io/api/v2/account/details",
        )
        self.assertEqual(
            sent.get_header("Authorization"),
            "Bearer sensitive-token",
        )
        self.assertEqual(result["status"], "verified")
        self.assertNotIn("sensitive-token", str(result))

    def test_http_and_network_errors_do_not_expose_token(self) -> None:
        for error in (
            HTTPError("https://example", 401, "unauthorized", {}, None),
            URLError("unreachable"),
        ):
            with self.subTest(error=type(error).__name__), patch(
                "terramig.adapters.hcp_auth.urlopen",
                side_effect=error,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    HCPTokenVerifier(
                        "app.terraform.io", "sensitive-token"
                    ).test_connection()
                self.assertNotIn("sensitive-token", str(raised.exception))

    def test_incomplete_response_fails_closed(self) -> None:
        response = MagicMock()
        response.read.return_value = b'{"data": []}'
        response.__enter__.return_value = response
        with patch(
            "terramig.adapters.hcp_auth.urlopen",
            return_value=response,
        ), self.assertRaisesRegex(RuntimeError, "incomplete"):
            HCPTokenVerifier(
                "https://hcp.example.test", "sensitive-token"
            ).test_connection()


if __name__ == "__main__":
    unittest.main()
