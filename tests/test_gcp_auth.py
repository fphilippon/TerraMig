import json
import os
import subprocess
import unittest
from unittest.mock import patch

from terramig.adapters.gcp_auth import GCPConnectionVerifier


class GCPConnectionVerifierTests(unittest.TestCase):
    def test_connection_checks_identity_project_and_cloud_asset_access(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                [], 0, json.dumps([{"account": "reader@example.com"}]), ""
            ),
            subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "projectId": "acme-prod-123",
                        "projectNumber": "123456789",
                        "name": "Acme production",
                    }
                ),
                "",
            ),
            subprocess.CompletedProcess([], 0, "[]", ""),
        ]
        with patch(
            "terramig.adapters.gcp_auth.subprocess.run", side_effect=responses
        ) as run:
            result = GCPConnectionVerifier().test_connection("acme-prod-123")

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["account"], "reader@example.com")
        self.assertEqual(result["project_number"], "123456789")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:3], ["gcloud", "auth", "list"])
        self.assertEqual(
            commands[1][:4],
            ["gcloud", "projects", "describe", "acme-prod-123"],
        )
        self.assertIn("asset", commands[2])
        self.assertIn("--content-type=resource", commands[2])
        self.assertIn("--limit=1", commands[2])

    def test_connection_rejects_invalid_project_before_launching_gcloud(self) -> None:
        with patch("terramig.adapters.gcp_auth.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "project ID"):
                GCPConnectionVerifier().test_connection("--impersonate=x")
        run.assert_not_called()

    def test_connection_requires_an_active_account(self) -> None:
        response = subprocess.CompletedProcess([], 0, "[]", "")
        with patch(
            "terramig.adapters.gcp_auth.subprocess.run", return_value=response
        ):
            with self.assertRaisesRegex(RuntimeError, "no active account"):
                GCPConnectionVerifier().test_connection("acme-prod-123")

    def test_missing_gcloud_is_reported_as_a_connection_error(self) -> None:
        with patch(
            "terramig.adapters.gcp_auth.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                GCPConnectionVerifier().test_connection("acme-prod-123")

    def test_saved_credential_is_passed_through_private_override_file(self) -> None:
        credential = json.dumps(
            {
                "type": "service_account",
                "project_id": "acme-prod-123",
                "client_email": "reader@acme-prod-123.iam.gserviceaccount.com",
            }
        )
        paths = []

        def run(command, **options):
            environment = options["env"]
            path = environment["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"]
            paths.append(path)
            self.assertEqual(path, environment["GOOGLE_APPLICATION_CREDENTIALS"])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path) as stream:
                self.assertEqual(
                    json.load(stream)["client_email"],
                    "reader@acme-prod-123.iam.gserviceaccount.com",
                )
            payload = (
                {"projectId": "acme-prod-123", "projectNumber": "123"}
                if "describe" in command
                else []
            )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        with patch("terramig.adapters.gcp_auth.subprocess.run", side_effect=run) as invoked:
            result = GCPConnectionVerifier(credential=credential).test_connection(
                "acme-prod-123"
            )

        self.assertEqual(invoked.call_count, 2)
        self.assertEqual(result["account"], "reader@acme-prod-123.iam.gserviceaccount.com")
        self.assertTrue(all(not os.path.exists(path) for path in paths))


if __name__ == "__main__":
    unittest.main()
