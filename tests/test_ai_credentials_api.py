from io import BytesIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from terramig import server
from terramig.persistence import MemoryPersistence
from terramig.services.auth import AuthService
from terramig.services.secrets import SecretVault
from terramig.settings import Settings, SettingsStore


class AiCredentialApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_app = server.APP
        self.directory = tempfile.TemporaryDirectory()
        application = object.__new__(server.Application)
        application.persistence = MemoryPersistence()
        application.store = SettingsStore(
            Path(self.directory.name) / "settings.json",
            persistence=application.persistence,
        )
        application.secrets = SecretVault(
            application.persistence,
            key=Fernet.generate_key().decode(),
        )
        application.settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
        )
        application._rebuild_services()
        application.auth = AuthService(application.persistence)
        server.APP = application

        initial = application.auth.login("admin", "admin")
        authenticated = application.auth.authenticate(initial.token)
        changed = application.auth.change_password(
            authenticated,
            "admin",
            "Secure-Migration-2026!",
        )
        self.headers = {
            "Cookie": (
                f"terramig_session={changed.token}; "
                f"terramig_csrf={changed.csrf_token}"
            ),
            "X-CSRF-Token": changed.csrf_token,
        }

    def tearDown(self) -> None:
        server.APP = self.original_app
        self.directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        body = json.dumps(payload or {}).encode() if payload is not None else b""
        handler = object.__new__(server.TerraMigHandler)
        handler.path = path
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.headers = {
            "Content-Length": str(len(body)),
            **self.headers,
        }
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.close_connection = True
        getattr(handler, f"do_{method}")()
        response = handler.wfile.getvalue()
        header_bytes, response_body = response.split(b"\r\n\r\n", 1)
        status = int(header_bytes.split(b" ", 2)[1])
        return status, json.loads(response_body)

    def test_save_status_test_and_clear_never_return_plaintext(self) -> None:
        plaintext = "github_pat_never-return-this"
        status, saved = self.request(
            "PUT",
            "/api/settings/ai-credential",
            {"provider": "github-copilot", "credential": plaintext},
        )
        self.assertEqual(status, 200)
        self.assertNotIn(plaintext, json.dumps(saved))
        self.assertTrue(saved["settings"]["readiness"]["ai_credential"])
        self.assertFalse(saved["settings"]["readiness"]["ai_authenticated"])
        record = server.APP.persistence.get_secret("ai.github-copilot")
        self.assertNotIn(plaintext, record.ciphertext)

        with patch(
            "terramig.server.CliGenerationAgent.test_connection",
            return_value={
                "provider": "github-copilot",
                "status": "verified",
                "message": "ok",
            },
        ):
            status, tested = self.request(
                "POST",
                "/api/settings/ai-credential/test",
                {"provider": "github-copilot"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(tested["credential"]["verified_at"])
        self.assertNotIn(plaintext, json.dumps(tested))

        status, settings = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertTrue(
            settings["ai_credentials"]["github-copilot"]["configured"]
        )
        self.assertTrue(settings["readiness"]["ai_authenticated"])
        self.assertNotIn(plaintext, json.dumps(settings))

        status, cleared = self.request(
            "DELETE",
            "/api/settings/ai-credential",
            {"provider": "github-copilot"},
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleared["credential"]["configured"])
        self.assertIsNone(
            server.APP.persistence.get_secret("ai.github-copilot")
        )
        self.assertNotIn(
            plaintext,
            json.dumps(server.APP.persistence.list_audit_events()),
        )

    def test_secret_routes_require_csrf(self) -> None:
        self.headers.pop("X-CSRF-Token")
        status, body = self.request(
            "PUT",
            "/api/settings/ai-credential",
            {"provider": "ibm-bob", "credential": "long-enough-secret"},
        )
        self.assertEqual(status, 403)
        self.assertIn("CSRF", body["error"])

    def test_failed_connection_redacts_the_credential(self) -> None:
        plaintext = "bob-key-never-expose"
        self.request(
            "PUT",
            "/api/settings/ai-credential",
            {"provider": "ibm-bob", "credential": plaintext},
        )
        with patch(
            "terramig.server.CliGenerationAgent.test_connection",
            side_effect=RuntimeError(f"provider rejected {plaintext}"),
        ):
            status, body = self.request(
                "POST",
                "/api/settings/ai-credential/test",
                {"provider": "ibm-bob"},
            )
        self.assertEqual(status, 409)
        self.assertNotIn(plaintext, json.dumps(body))
        self.assertIn("[REDACTED]", body["error"])

    def test_saving_bob_credential_activates_the_selected_agent(self) -> None:
        status, saved = self.request(
            "PUT",
            "/api/settings/ai-credential",
            {
                "provider": "ibm-bob",
                "credential": "bob-api-key-value",
                "activate": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["settings"]["ai_provider"], "ibm-bob")
        self.assertEqual(server.APP.settings.ai_provider, "ibm-bob")

    def test_hcp_token_save_test_and_clear_never_return_plaintext(self) -> None:
        plaintext = "hcp-token-never-return-this"
        status, saved = self.request(
            "PUT",
            "/api/settings/hcp-credential",
            {"credential": plaintext},
        )
        self.assertEqual(status, 200)
        self.assertNotIn(plaintext, json.dumps(saved))
        self.assertTrue(saved["settings"]["readiness"]["hcp_token"])
        self.assertFalse(
            saved["settings"]["readiness"]["hcp_authenticated"]
        )
        record = server.APP.persistence.get_secret("hcp.terraform")
        self.assertNotIn(plaintext, record.ciphertext)

        with patch(
            "terramig.server.HCPTokenVerifier.test_connection",
            return_value={
                "status": "verified",
                "message": "ok",
            },
        ):
            status, tested = self.request(
                "POST",
                "/api/settings/hcp-credential/test",
                {},
            )
        self.assertEqual(status, 200)
        self.assertTrue(tested["credential"]["verified_at"])
        self.assertNotIn(plaintext, json.dumps(tested))

        status, settings = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertTrue(settings["hcp_credential"]["configured"])
        self.assertTrue(settings["readiness"]["hcp_authenticated"])
        self.assertNotIn(plaintext, json.dumps(settings))

        status, cleared = self.request(
            "DELETE",
            "/api/settings/hcp-credential",
            {},
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleared["credential"]["configured"])
        self.assertIsNone(
            server.APP.persistence.get_secret("hcp.terraform")
        )
        self.assertNotIn(
            plaintext,
            json.dumps(server.APP.persistence.list_audit_events()),
        )

    def test_git_ssh_key_save_test_and_clear_never_return_plaintext(self) -> None:
        plaintext = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
        status, saved = self.request(
            "PUT",
            "/api/settings/git-ssh-credential",
            {"credential": plaintext},
        )
        self.assertEqual(status, 200)
        self.assertNotIn(plaintext, json.dumps(saved))
        self.assertTrue(saved["settings"]["readiness"]["git_ssh_key"])
        self.assertFalse(
            saved["settings"]["readiness"]["git_ssh_authenticated"]
        )
        record = server.APP.persistence.get_secret("git.ssh-private-key")
        self.assertNotIn(plaintext, record.ciphertext)

        with patch(
            "terramig.server.GitDeliveryEngine.test_ssh_connection",
            return_value={
                "status": "verified",
                "message": "ok",
                "fingerprint": "SHA256:test",
            },
        ):
            status, tested = self.request(
                "POST",
                "/api/settings/git-ssh-credential/test",
                {"repository": "git@github.com:acme/private.git"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(tested["credential"]["verified_at"])
        self.assertNotIn(plaintext, json.dumps(tested))

        status, settings = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertTrue(settings["git_ssh_credential"]["configured"])
        self.assertTrue(settings["readiness"]["git_ssh_authenticated"])
        self.assertNotIn(plaintext, json.dumps(settings))

        status, cleared = self.request(
            "DELETE",
            "/api/settings/git-ssh-credential",
            {},
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleared["credential"]["configured"])
        self.assertIsNone(
            server.APP.persistence.get_secret("git.ssh-private-key")
        )
        self.assertNotIn(
            plaintext,
            json.dumps(server.APP.persistence.list_audit_events()),
        )

    def test_github_token_save_test_and_clear_never_return_plaintext(self) -> None:
        plaintext = "github_pat_never-return-this-token"
        status, saved = self.request(
            "PUT",
            "/api/settings/github-credential",
            {"credential": plaintext},
        )
        self.assertEqual(status, 200)
        self.assertNotIn(plaintext, json.dumps(saved))
        self.assertTrue(saved["settings"]["readiness"]["github_token"])
        self.assertFalse(saved["settings"]["readiness"]["github_api"])
        record = server.APP.persistence.get_secret("git.github-api-token")
        self.assertNotIn(plaintext, record.ciphertext)

        with patch(
            "terramig.server.GitDeliveryEngine.test_github_connection",
            return_value={
                "status": "verified",
                "message": "ok",
                "account": "octocat",
            },
        ):
            status, tested = self.request(
                "POST",
                "/api/settings/github-credential/test",
                {},
            )
        self.assertEqual(status, 200)
        self.assertEqual(tested["account"], "octocat")
        self.assertTrue(tested["credential"]["verified_at"])
        self.assertTrue(tested["settings"]["readiness"]["github_api"])
        self.assertNotIn(plaintext, json.dumps(tested))

        status, cleared = self.request(
            "DELETE",
            "/api/settings/github-credential",
            {},
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleared["credential"]["configured"])
        self.assertIsNone(
            server.APP.persistence.get_secret("git.github-api-token")
        )
        self.assertNotIn(
            plaintext,
            json.dumps(server.APP.persistence.list_audit_events()),
        )

    def test_gcp_connection_verifies_saved_project_and_is_invalidated_on_change(self) -> None:
        status, _ = self.request(
            "PUT",
            "/api/settings",
            {
                "hcp_hostname": "app.terraform.io",
                "hcp_organization": "acme",
                "hcp_workspace": "adoption",
                "gcp_project_id": "acme-prod-123",
                "ai_provider": "github-copilot",
            },
        )
        self.assertEqual(status, 200)
        with patch(
            "terramig.server.GCPConnectionVerifier.test_connection",
            return_value={
                "status": "verified",
                "message": "ok",
                "project_id": "acme-prod-123",
                "project_number": "123456789",
                "project_name": "Acme production",
                "account": "reader@example.com",
            },
        ):
            status, tested = self.request(
                "POST",
                "/api/settings/gcp-connection/test",
                {"project_id": "acme-prod-123"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(tested["account"], "reader@example.com")
        self.assertTrue(tested["settings"]["readiness"]["gcp_authenticated"])
        self.assertTrue(tested["settings"]["gcp_verified_at"])
        events = server.APP.persistence.list_audit_events()
        self.assertEqual(events[0]["name"], "gcp.connection.verified")

        status, changed = self.request(
            "PUT",
            "/api/settings",
            {"gcp_project_id": "acme-stage-123"},
        )
        self.assertEqual(status, 200)
        self.assertFalse(changed["readiness"]["gcp_authenticated"])
        self.assertEqual(changed["gcp_verified_at"], "")

    def test_git_key_can_be_saved_and_verified_in_one_request(self) -> None:
        plaintext = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()
        with patch(
            "terramig.server.GitDeliveryEngine.test_ssh_connection",
            return_value={
                "status": "verified",
                "message": "ok",
                "fingerprint": "SHA256:test",
            },
        ):
            status, saved = self.request(
                "PUT",
                "/api/settings/git-ssh-credential",
                {
                    "credential": plaintext,
                    "repository": "git@github.com:acme/private.git",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(saved["connection"]["fingerprint"], "SHA256:test")
        self.assertTrue(
            saved["settings"]["readiness"]["git_ssh_authenticated"]
        )

    def test_gcp_credential_save_test_and_clear_never_return_plaintext(self) -> None:
        plaintext = json.dumps(
            {
                "type": "authorized_user",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token-never-return",
            }
        )
        status, saved = self.request(
            "PUT",
            "/api/settings/gcp-credential",
            {"credential": plaintext},
        )
        self.assertEqual(status, 200)
        self.assertTrue(saved["settings"]["gcp_credential"]["configured"])
        self.assertNotIn("refresh-token-never-return", json.dumps(saved))

        self.request(
            "PUT",
            "/api/settings",
            {"gcp_project_id": "acme-prod-123"},
        )
        with patch(
            "terramig.server.GCPConnectionVerifier.test_connection",
            return_value={
                "status": "verified",
                "message": "ok",
                "project_id": "acme-prod-123",
                "project_number": "123",
                "project_name": "Acme",
                "account": "reader@example.com",
            },
        ):
            status, tested = self.request(
                "POST",
                "/api/settings/gcp-connection/test",
                {"project_id": "acme-prod-123"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(
            tested["settings"]["gcp_credential"]["verified_at"]
        )

        status, cleared = self.request(
            "DELETE", "/api/settings/gcp-credential", {}
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleared["credential"]["configured"])
        self.assertFalse(
            cleared["settings"]["readiness"]["gcp_authenticated"]
        )

    def test_gcp_project_and_credential_can_be_saved_and_tested_atomically(self) -> None:
        plaintext = json.dumps(
            {
                "type": "authorized_user",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token-never-return",
                "quota_project_id": "acme-prod-123",
            }
        )
        with patch(
            "terramig.server.GCPConnectionVerifier.test_connection",
            return_value={
                "status": "verified",
                "message": "ok",
                "project_id": "acme-prod-123",
                "project_number": "123",
                "project_name": "Acme",
                "account": "reader@example.com",
            },
        ):
            status, configured = self.request(
                "POST",
                "/api/settings/gcp-connection/configure",
                {
                    "project_id": "acme-prod-123",
                    "credential": plaintext,
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(configured["settings"]["gcp_project_id"], "acme-prod-123")
        self.assertTrue(configured["settings"]["readiness"]["gcp_authenticated"])
        self.assertNotIn("refresh-token-never-return", json.dumps(configured))

    def test_gcp_connection_reset_clears_project_credential_and_verification(self) -> None:
        server.APP.settings.gcp_project_id = "acme-prod-123"
        server.APP.settings.gcp_verified_project_id = "acme-prod-123"
        server.APP.settings.gcp_verified_account = "reader@example.com"
        server.APP.settings.gcp_verified_at = "2026-07-21T12:00:00+00:00"
        server.APP.store.save(server.APP.settings)
        server.APP.secrets.save_gcp_credential(
            json.dumps(
                {
                    "type": "authorized_user",
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "refresh_token": "refresh-token",
                }
            )
        )

        status, reset = self.request(
            "DELETE",
            "/api/settings/gcp-credential",
            {"clear_project": True},
        )

        self.assertEqual(status, 200)
        self.assertEqual(reset["settings"]["gcp_project_id"], "")
        self.assertEqual(reset["settings"]["gcp_verified_project_id"], "")
        self.assertFalse(reset["credential"]["configured"])

    def test_gcp_connection_requires_the_saved_project(self) -> None:
        status, body = self.request(
            "POST",
            "/api/settings/gcp-connection/test",
            {"project_id": "acme-prod-123"},
        )
        self.assertEqual(status, 409)
        self.assertIn("Save the GCP project ID", body["error"])


if __name__ == "__main__":
    unittest.main()
