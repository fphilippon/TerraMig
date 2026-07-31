import unittest
import json
from pathlib import Path
from unittest.mock import patch

from terramig.adapters.terraform import LocalTerraformVerifier
from terramig.domain import GenerationBundle, ImportOperation


class TerraformVerifierTests(unittest.TestCase):
    def test_import_blocks_preserve_addresses_and_quote_ids(self) -> None:
        bundle = GenerationBundle(
            "resource x y {}",
            [ImportOperation("module.vpc.google_compute_network.this", 'projects/p/networks/\"quoted\"', "command")],
            [],
            [],
        )
        imports = LocalTerraformVerifier.import_blocks(bundle)
        self.assertIn("to = module.vpc.google_compute_network.this", imports)
        self.assertIn('id = "projects/p/networks/\\\"quoted\\\""', imports)

    def test_local_verification_removes_only_cloud_block(self) -> None:
        configuration = '''terraform {
  required_version = ">= 1.7"
  cloud {
    organization = "acme"
    workspaces { name = "adoption" }
  }
}
resource "x" "y" {}
'''
        local = LocalTerraformVerifier.without_cloud_block(configuration)
        self.assertNotIn("cloud", local)
        self.assertIn('required_version = ">= 1.7"', local)
        self.assertIn('resource "x" "y" {}', local)

    def test_project_variable_is_only_detected_when_declared(self) -> None:
        self.assertTrue(
            LocalTerraformVerifier.declares_project_variable(
                'variable "project_id" { type = string }'
            )
        )
        self.assertFalse(
            LocalTerraformVerifier.declares_project_variable(
                'resource "google_storage_bucket" "x" { name = "x" }'
            )
        )

    def test_plan_uses_environment_project_without_undeclared_variable(self) -> None:
        bundle = GenerationBundle(
            'resource "google_storage_bucket" "x" { name = "bucket" location = "EU" }',
            [ImportOperation("google_storage_bucket.x", "bucket", "command")],
            [],
            [],
        )
        verifier = LocalTerraformVerifier()
        responses = [
            _process(0, '{"terraform_version":"1.15.8"}'),
            _process(0, ""),
            _process(0, '{"valid":true,"diagnostics":[]}'),
            _process(1, "plan stopped"),
        ]
        with patch.object(verifier, "_run", side_effect=responses) as run:
            verifier.verify("project-id", bundle)
        plan_call = run.call_args_list[3]
        self.assertNotIn("-var=project_id=project-id", plan_call.args[0])
        self.assertEqual(plan_call.kwargs["environment"]["GOOGLE_PROJECT"], "project-id")

    def test_plan_supplies_project_variable_when_configuration_declares_it(self) -> None:
        bundle = GenerationBundle(
            'variable "project_id" { type = string }\nresource "x" "y" {}',
            [ImportOperation("x.y", "y", "command")],
            [],
            [],
        )
        verifier = LocalTerraformVerifier()
        responses = [
            _process(0, '{"terraform_version":"1.15.8"}'),
            _process(0, ""),
            _process(0, '{"valid":true,"diagnostics":[]}'),
            _process(1, "plan stopped"),
        ]
        with patch.object(verifier, "_run", side_effect=responses) as run:
            verifier.verify("project-id", bundle)
        self.assertIn("-var=project_id=project-id", run.call_args_list[3].args[0])

    def test_saved_gcp_credential_is_available_only_during_verification(self) -> None:
        credential = json.dumps(
            {
                "type": "service_account",
                "project_id": "project-id",
                "private_key_id": "test-key",
            }
        )
        bundle = GenerationBundle(
            'resource "google_storage_bucket" "x" { name = "bucket" location = "EU" }',
            [ImportOperation("google_storage_bucket.x", "bucket", "command")],
            [],
            [],
        )
        verifier = LocalTerraformVerifier(gcp_credential=credential)
        observed_paths = []

        def run(command, cwd, **kwargs):
            path = Path(kwargs["environment"]["GOOGLE_APPLICATION_CREDENTIALS"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(), credential)
            observed_paths.append(path)
            if "version" in command:
                return _process(0, '{"terraform_version":"1.15.8"}')
            if "validate" in command:
                return _process(0, '{"valid":true,"diagnostics":[]}')
            return _process(1, "plan stopped")

        with patch.object(verifier, "_run", side_effect=run):
            verifier.verify("project-id", bundle)
        self.assertTrue(observed_paths)
        self.assertFalse(observed_paths[0].exists())


def _process(returncode: int, stdout: str):
    class Process:
        stderr = ""

        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout

    return Process()


if __name__ == "__main__":
    unittest.main()
