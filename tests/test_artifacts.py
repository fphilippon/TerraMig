import unittest

from terramig.domain import GenerationBundle, ImportOperation
from terramig.services.artifacts import (
    finalize_terraform_artifacts,
    terraform_declaration_addresses,
    terraform_files,
    terraform_resource_files,
)


class TerraformArtifactTests(unittest.TestCase):
    def test_host_owns_backend_and_provider_files(self) -> None:
        bundle = GenerationBundle(
            '''terraform {
  cloud {
    organization = "wrong"
  }
}
provider "google" { project = "wrong" }
variable "project_id" { type = string }
resource "google_storage_bucket" "archive" {
  name = "archive"
}
''',
            [],
            [],
            [],
        )
        finalize_terraform_artifacts(
            bundle,
            {
                "hostname": "app.terraform.io",
                "organization": "acme",
                "workspace": "gcp-adoption",
            },
            "7.39.0",
            "example-project",
        )

        files = terraform_files(bundle)
        self.assertEqual(set(files), {"backend.tf", "providers.tf", "main.tf"})
        self.assertIn('organization = "acme"', files["backend.tf"])
        self.assertIn('name = "gcp-adoption"', files["backend.tf"])
        self.assertIn('version = "= 7.39.0"', files["providers.tf"])
        self.assertIn('provider "google"', files["providers.tf"])
        self.assertIn('default     = "example-project"', files["providers.tf"])
        self.assertIn('resource "google_storage_bucket"', files["main.tf"])
        self.assertNotIn("terraform {", files["main.tf"])
        self.assertNotIn('provider "google"', files["main.tf"])

    def test_backend_is_omitted_without_an_hcp_target(self) -> None:
        bundle = GenerationBundle('resource "x" "y" {}', [], [], [])
        finalize_terraform_artifacts(bundle, {}, "7.39.0")
        self.assertNotIn("backend.tf", terraform_files(bundle))

    def test_heredoc_braces_do_not_hide_host_owned_blocks(self) -> None:
        bundle = GenerationBundle(
            '''resource "x" "y" {
  script = <<-EOT
  if true; then { echo ok; }
  EOT
}
provider "google" {
  project = "wrong"
}
''',
            [],
            [],
            [],
        )
        finalize_terraform_artifacts(bundle, {}, "7.39.0", "project-id")
        self.assertIn("if true; then { echo ok; }", bundle.terraform)
        self.assertNotIn('project = "wrong"', bundle.terraform)
        self.assertIn('default     = "project-id"', bundle.providers_tf)

    def test_existing_directory_layout_uses_stable_resource_files(self) -> None:
        bundle = GenerationBundle(
            '''# first adoption batch
resource "google_storage_bucket" "archive" {
  name = "archive"
}

module "network" {
  source = "app.terraform.io/acme/network/google"
}

locals {
  environment = "prod"
}
''',
            [
                ImportOperation(
                    "google_storage_bucket.archive",
                    "archive",
                    "terraform import google_storage_bucket.archive archive",
                ),
                ImportOperation(
                    "module.network.google_compute_network.this",
                    "projects/p/global/networks/default",
                    "terraform import module.network.google_compute_network.this projects/p/global/networks/default",
                ),
            ],
            [],
            [],
        )

        files = terraform_resource_files(bundle)

        self.assertEqual(
            set(files),
            {
                "google_storage_bucket_archive.tf",
                "module_network.tf",
                "locals.tf",
            },
        )
        self.assertIn(
            'resource "google_storage_bucket" "archive"',
            files["google_storage_bucket_archive.tf"],
        )
        self.assertIn(
            "to = google_storage_bucket.archive",
            files["google_storage_bucket_archive.tf"],
        )
        self.assertIn(
            "to = module.network.google_compute_network.this",
            files["module_network.tf"],
        )
        self.assertEqual(
            terraform_declaration_addresses(bundle.terraform),
            {
                "google_storage_bucket.archive",
                "module.network",
            },
        )


if __name__ == "__main__":
    unittest.main()
