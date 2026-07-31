import unittest
from unittest.mock import patch

from terramig.server import build_service
from terramig.settings import Settings


class HCPTokenWiringTests(unittest.TestCase):
    def test_application_managed_token_reaches_every_hcp_consumer(self) -> None:
        settings = Settings(
            hcp_hostname="app.terraform.io",
            hcp_organization="acme",
            hcp_workspace="adoption",
        )
        token = "application-managed-hcp-token"
        with patch.dict(
            "os.environ",
            {
                "TERRAMIG_HCP_TOKEN": "",
                "TERRAMIG_ENABLE_HCP_SUBMISSION": "true",
            },
        ):
            service = build_service(settings, hcp_token=token)

        self.assertEqual(service.registry.token, token)
        self.assertEqual(service.importer.token, token)
        self.assertEqual(
            service.agent.context_provider.token,
            token,
        )
        self.assertEqual(
            service.verifier.environment["TF_TOKEN_app_terraform_io"],
            token,
        )
    def test_application_managed_ssh_key_reaches_git_delivery_only(self) -> None:
        settings = Settings(
            hcp_hostname="app.terraform.io",
            hcp_organization="acme",
            hcp_workspace="adoption",
        )
        private_key = "private-key-material"
        service = build_service(
            settings,
            git_ssh_private_key=private_key,
        )
        self.assertEqual(
            service.git_delivery_engine.ssh_private_key,
            private_key,
        )
        self.assertNotIn("TERRAMIG_GIT_SSH_PRIVATE_KEY", service.verifier.environment)

    def test_application_managed_github_token_reaches_git_delivery_only(self) -> None:
        settings = Settings(
            hcp_hostname="app.terraform.io",
            hcp_organization="acme",
            hcp_workspace="adoption",
        )
        token = "github_pat_delivery-token"
        service = build_service(settings, github_token=token)

        self.assertEqual(service.git_delivery_engine.github_token, token)
        self.assertNotIn(
            "TERRAMIG_GITHUB_TOKEN", service.agent._agent_environment()
        )

    def test_application_managed_gcp_credential_reaches_read_only_consumers(self) -> None:
        settings = Settings(
            hcp_organization="acme",
            hcp_workspace="adoption",
            gcp_project_id="acme-prod-123",
        )
        credential = '{"type":"authorized_user"}'
        service = build_service(settings, gcp_credential=credential)

        self.assertEqual(service.inventory.credential, credential)
        self.assertEqual(service.verifier.gcp_credential, credential)
        self.assertNotIn(
            "GOOGLE_APPLICATION_CREDENTIALS", service.verifier.environment
        )
        self.assertNotIn(
            "TERRAMIG_GCP_CREDENTIALS_JSON",
            service.agent._agent_environment(),
        )


if __name__ == "__main__":
    unittest.main()
