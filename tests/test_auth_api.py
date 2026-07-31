from io import BytesIO
import json
import unittest

from terramig import server
from terramig.persistence import MemoryPersistence
from terramig.services.auth import AuthService


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_auth = server.APP.auth
        server.APP.auth = AuthService(MemoryPersistence())

    def tearDown(self) -> None:
        server.APP.auth = self.original_auth

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, bytes]:
        body = json.dumps(payload or {}).encode() if payload is not None else b""
        handler = object.__new__(server.TerraMigHandler)
        handler.path = path
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.headers = {
            "Content-Length": str(len(body)),
            **(headers or {}),
        }
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.close_connection = True
        getattr(handler, f"do_{method}")()
        response = handler.wfile.getvalue()
        header_bytes, response_body = response.split(b"\r\n\r\n", 1)
        status = int(header_bytes.split(b" ", 2)[1])
        return status, header_bytes, response_body

    def test_protected_api_requires_local_session(self) -> None:
        status, headers, body = self.request("GET", "/api/settings")
        self.assertEqual(status, 401)
        self.assertIn(b"Content-Security-Policy", headers)
        self.assertIn("Authentication required", json.loads(body)["error"])

    def test_login_forces_password_change_then_unlocks_api(self) -> None:
        status, headers, body = self.request(
            "POST",
            "/api/auth/login",
            {"username": "admin", "password": "admin"},
        )
        self.assertEqual(status, 200)
        login = json.loads(body)
        self.assertTrue(login["user"]["must_change_password"])
        cookies = self._cookies(headers)
        request_headers = {
            "Cookie": (
                f"terramig_session={cookies['terramig_session']}; "
                f"terramig_csrf={cookies['terramig_csrf']}"
            ),
            "X-CSRF-Token": login["csrf_token"],
        }
        status, _, _ = self.request(
            "GET", "/api/settings", headers=request_headers
        )
        self.assertEqual(status, 403)

        status, changed_headers, changed_body = self.request(
            "POST",
            "/api/auth/change-password",
            {
                "current_password": "admin",
                "new_password": "Secure-Migration-2026!",
            },
            request_headers,
        )
        self.assertEqual(status, 200)
        changed = json.loads(changed_body)
        self.assertFalse(changed["user"]["must_change_password"])
        new_cookies = self._cookies(changed_headers)
        status, _, body = self.request(
            "GET",
            "/api/settings",
            headers={
                "Cookie": (
                    f"terramig_session={new_cookies['terramig_session']}; "
                    f"terramig_csrf={new_cookies['terramig_csrf']}"
                )
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("persistence", json.loads(body))

    @staticmethod
    def _cookies(headers: bytes) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in headers.decode().split("\r\n"):
            if not line.lower().startswith("set-cookie:"):
                continue
            name, value = line.split(":", 1)[1].strip().split(";", 1)[0].split("=", 1)
            values[name] = value
        return values


if __name__ == "__main__":
    unittest.main()
