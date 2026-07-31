import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terramig.settings import Settings, SettingsStore
from terramig.persistence import MemoryPersistence


class SettingsTests(unittest.TestCase):
    def test_production_settings_require_hcp_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "organization"):
            Settings().validate()

    def test_settings_round_trip_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            expected = Settings(
                hcp_hostname="app.terraform.io",
                hcp_organization="acme",
                hcp_workspace="adoption",
                gcp_project_id="gcp-prod",
                ai_provider="ibm-bob",
            )
            store.save(expected)
            self.assertEqual(store.load(), expected)
            self.assertNotIn("token", (Path(directory) / "settings.json").read_text().lower())

    def test_settings_round_trip_through_durable_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persistence = MemoryPersistence()
            store = SettingsStore(
                Path(directory) / "settings.json", persistence=persistence
            )
            expected = Settings(
                hcp_hostname="app.terraform.io",
                hcp_organization="acme",
                hcp_workspace="adoption",
                gcp_project_id="gcp-prod",
                ai_provider="github-copilot",
            )
            store.save(expected)
            self.assertEqual(store.load(), expected)
            self.assertFalse((Path(directory) / "settings.json").exists())

    def test_production_readiness_uses_environment_and_gcloud(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
            gcp_project_id="gcp-prod",
        )
        with patch.dict(
            os.environ,
            {
                "TERRAMIG_HCP_TOKEN": "x",
                "TERRAMIG_GITHUB_TOKEN": "x",
                "COPILOT_GITHUB_TOKEN": "x",
            },
        ), patch("terramig.settings.shutil.which", return_value="/bin/tool"):
            readiness = settings.readiness(gcp_authenticated=True)
        self.assertTrue(readiness["ready"])
        self.assertNotIn("module_manifest", readiness)

    def test_adoption_requires_verified_gcp_project(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
            gcp_project_id="gcp-prod",
        )
        with patch("terramig.settings.shutil.which", return_value="/bin/tool"):
            readiness = settings.readiness(
                ai_credential_configured=True,
                ai_authenticated=True,
                hcp_token_configured=True,
                hcp_authenticated=True,
            )
        self.assertTrue(readiness["gcloud"])
        self.assertFalse(readiness["gcp_authenticated"])
        self.assertFalse(readiness["ready"])

    def test_selected_agent_controls_binary_readiness(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
            ai_provider="ibm-bob",
        )
        with patch("terramig.settings.shutil.which", side_effect=lambda name: "/bin/bob" if name == "bob" else None):
            readiness = settings.readiness()
        self.assertEqual(readiness["ai_binary"], "bob")
        self.assertTrue(readiness["ai_installed"])
        self.assertFalse(readiness["ai_authenticated"])
        self.assertFalse(readiness["ai"])

    def test_hcp_token_must_be_verified_for_workflows(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
        )
        with patch(
            "terramig.settings.shutil.which",
            return_value="/bin/tool",
        ):
            readiness = settings.readiness(
                ai_credential_configured=True,
                ai_authenticated=True,
                hcp_token_configured=True,
                hcp_authenticated=False,
            )
        self.assertTrue(readiness["hcp_token"])
        self.assertFalse(readiness["hcp_authenticated"])
        self.assertFalse(readiness["ready"])

    def test_stored_git_ssh_key_requires_repository_verification(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
        )
        with patch.dict(
            os.environ,
            {
                "TERRAMIG_GITHUB_TOKEN": "",
                "SSH_AUTH_SOCK": "",
                "GIT_SSH_COMMAND": "",
            },
        ), patch("terramig.settings.shutil.which", return_value="/bin/tool"):
            readiness = settings.readiness(
                git_ssh_key_configured=True,
                git_ssh_authenticated=False,
            )
        self.assertTrue(readiness["git_ssh_key"])
        self.assertFalse(readiness["git_ssh_authenticated"])
        self.assertFalse(readiness["git_auth"])
        self.assertFalse(readiness["github_api"])

    def test_selected_agent_requires_its_own_credential(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
            ai_provider="ibm-bob",
        )
        with patch.dict(
            os.environ,
            {
                "COPILOT_GITHUB_TOKEN": "copilot-only",
                "BOBSHELL_API_KEY": "",
            },
        ), patch(
            "terramig.settings.shutil.which",
            side_effect=lambda name: f"/bin/{name}",
        ):
            readiness = settings.readiness()
        self.assertTrue(readiness["ai_installed"])
        self.assertFalse(readiness["ai_authenticated"])

    def test_production_is_not_ready_without_ai_authentication(self) -> None:
        settings = Settings(hcp_organization="acme", hcp_workspace="adoption")
        with patch.dict(
            os.environ,
            {
                "COPILOT_GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GITHUB_TOKEN": "",
            },
        ), patch(
            "terramig.settings.shutil.which",
            return_value="/bin/copilot",
        ):
            readiness = settings.readiness()
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["ai_credential"])
        self.assertFalse(readiness["ai_authenticated"])

    def test_legacy_runtime_mode_is_removed_from_persisted_settings(self) -> None:
        persistence = MemoryPersistence()
        persistence.save_settings(
            {
                "mode": "sandbox",
                "hcp_organization": "acme",
                "hcp_workspace": "adoption",
            }
        )
        store = SettingsStore(Path("unused.json"), persistence=persistence)

        settings = store.load()

        self.assertEqual(settings.hcp_organization, "acme")
        self.assertNotIn("mode", persistence.load_settings())

    def test_github_api_readiness_requires_a_verified_token(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
        )
        configured = settings.readiness(
            github_token_configured=True,
            github_authenticated=False,
        )
        verified = settings.readiness(
            github_token_configured=True,
            github_authenticated=True,
        )

        self.assertTrue(configured["github_token"])
        self.assertFalse(configured["github_api"])
        self.assertTrue(verified["github_api"])
        self.assertTrue(verified["git_auth"])


if __name__ == "__main__":
    unittest.main()
