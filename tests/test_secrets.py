import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from terramig.persistence import MemoryPersistence
from terramig.services.secrets import SecretVault


class SecretVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.persistence = MemoryPersistence()
        self.key = Fernet.generate_key().decode()
        self.vault = SecretVault(self.persistence, key=self.key)

    def test_ai_credential_is_encrypted_and_never_returned_in_status(self) -> None:
        value = "github_pat_sensitive-value"
        status = self.vault.save_ai_credential("github-copilot", value)
        record = self.persistence.get_secret("ai.github-copilot")

        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "encrypted-store")
        self.assertNotIn(value, record.ciphertext)
        self.assertNotIn(value, str(status))
        self.assertEqual(self.vault.resolve_ai_credential("github-copilot"), value)

    def test_replacing_credential_clears_previous_verification(self) -> None:
        self.vault.save_ai_credential("ibm-bob", "first-secret")
        self.vault.mark_ai_verified("ibm-bob")
        self.assertTrue(self.vault.ai_status("ibm-bob")["verified_at"])

        self.vault.save_ai_credential("ibm-bob", "second-secret")
        self.assertFalse(self.vault.ai_status("ibm-bob")["verified_at"])
        self.assertEqual(self.vault.resolve_ai_credential("ibm-bob"), "second-secret")

    def test_clear_removes_only_application_managed_credential(self) -> None:
        self.vault.save_ai_credential("ibm-bob", "stored-secret")
        with patch.dict(os.environ, {"BOBSHELL_API_KEY": "environment-secret"}):
            status = self.vault.clear_ai_credential("ibm-bob")
            self.assertEqual(status["source"], "environment")
            self.assertFalse(status["managed"])
            self.assertEqual(
                self.vault.resolve_ai_credential("ibm-bob"),
                "environment-secret",
            )
            self.assertFalse(status["verified_at"])
            verified = self.vault.mark_ai_verified("ibm-bob")
            self.assertTrue(verified["verified_at"])

    def test_provider_credentials_are_isolated(self) -> None:
        self.vault.save_ai_credential("github-copilot", "copilot-secret")
        self.vault.save_ai_credential("ibm-bob", "bob-secret-value")
        self.assertEqual(
            self.vault.resolve_ai_credential("github-copilot"),
            "copilot-secret",
        )
        self.assertEqual(
            self.vault.resolve_ai_credential("ibm-bob"),
            "bob-secret-value",
        )

    def test_hcp_credential_has_the_same_encrypted_lifecycle(self) -> None:
        value = "hcp-sensitive-token"
        status = self.vault.save_hcp_credential(value)
        record = self.persistence.get_secret("hcp.terraform")

        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "encrypted-store")
        self.assertNotIn(value, record.ciphertext)
        self.assertNotIn(value, str(status))
        self.assertEqual(self.vault.resolve_hcp_credential(), value)
        self.assertFalse(status["verified_at"])

        verified = self.vault.mark_hcp_verified()
        self.assertTrue(verified["verified_at"])

        self.vault.save_hcp_credential("replacement-hcp-token")
        self.assertFalse(self.vault.hcp_status()["verified_at"])
        cleared = self.vault.clear_hcp_credential()
        self.assertFalse(cleared["configured"])

    def test_hcp_environment_token_remains_deployment_managed(self) -> None:
        with patch.dict(
            os.environ, {"TERRAMIG_HCP_TOKEN": "environment-hcp-token"}
        ):
            self.assertEqual(
                self.vault.resolve_hcp_credential(),
                "environment-hcp-token",
            )
            status = self.vault.hcp_status()
            self.assertEqual(status["source"], "environment")
            self.assertFalse(status["managed"])
            self.assertTrue(self.vault.mark_hcp_verified()["verified_at"])

    def test_github_api_token_has_an_encrypted_verified_lifecycle(self) -> None:
        value = "github_pat_sensitive-delivery-token"
        status = self.vault.save_github_credential(value)
        record = self.persistence.get_secret("git.github-api-token")

        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "encrypted-store")
        self.assertNotIn(value, record.ciphertext)
        self.assertNotIn(value, str(status))
        self.assertEqual(self.vault.resolve_github_credential(), value)
        self.assertFalse(status["verified_at"])

        self.assertTrue(self.vault.mark_github_verified()["verified_at"])
        self.vault.save_github_credential("github_pat_replacement-token")
        self.assertFalse(self.vault.github_status()["verified_at"])
        self.assertFalse(self.vault.clear_github_credential()["configured"])

    def test_git_ssh_private_key_has_encrypted_verified_lifecycle(self) -> None:
        value = self._private_key()
        status = self.vault.save_git_ssh_credential(value)
        record = self.persistence.get_secret("git.ssh-private-key")

        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "encrypted-store")
        self.assertNotIn(value, record.ciphertext)
        self.assertNotIn(value, str(status))
        self.assertEqual(
            self.vault.resolve_git_ssh_credential().strip(), value.strip()
        )
        self.assertFalse(status["verified_at"])

        self.assertTrue(
            self.vault.mark_git_ssh_verified()["verified_at"]
        )
        self.vault.save_git_ssh_credential(self._private_key())
        self.assertFalse(self.vault.git_ssh_status()["verified_at"])
        self.assertFalse(
            self.vault.clear_git_ssh_credential()["configured"]
        )

    def test_git_ssh_key_rejects_invalid_and_passphrase_protected_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "not valid"):
            self.vault.save_git_ssh_credential("not-a-private-key")
        encrypted = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase"),
        ).decode()
        with self.assertRaisesRegex(ValueError, "Passphrase-protected"):
            self.vault.save_git_ssh_credential(encrypted)

    def test_gcp_credential_has_an_encrypted_validated_lifecycle(self) -> None:
        value = json.dumps(
            {
                "type": "authorized_user",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-token",
                "quota_project_id": "acme-prod-123",
            }
        )
        status = self.vault.save_gcp_credential(value)
        record = self.persistence.get_secret("gcp.application-default-credentials")

        self.assertTrue(status["configured"])
        self.assertNotIn("refresh-token", record.ciphertext)
        self.assertEqual(
            json.loads(self.vault.resolve_gcp_credential())["refresh_token"],
            "refresh-token",
        )
        self.assertTrue(self.vault.mark_gcp_verified()["verified_at"])
        self.assertFalse(self.vault.clear_gcp_credential()["configured"])

    def test_gcp_credential_rejects_invalid_or_incomplete_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            self.vault.save_gcp_credential("not-json")
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            self.vault.save_gcp_credential('{"type":"service_account"}')

    @staticmethod
    def _private_key() -> str:
        return ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()

    def test_generated_key_file_is_private_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.key"
            first = SecretVault(self.persistence, key_file=path)
            first.save_ai_credential("ibm-bob", "persistent-secret")
            second = SecretVault(self.persistence, key_file=path)
            self.assertEqual(
                second.resolve_ai_credential("ibm-bob"),
                "persistent-secret",
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unsupported_provider_and_short_secret_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.vault.ai_status("other")
        with self.assertRaisesRegex(ValueError, "at least 8"):
            self.vault.save_ai_credential("ibm-bob", "short")
        with self.assertRaisesRegex(ValueError, "whitespace"):
            self.vault.save_ai_credential("ibm-bob", "invalid secret")
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            self.vault.save_ai_credential("ibm-bob", "x" * 16_385)


if __name__ == "__main__":
    unittest.main()
