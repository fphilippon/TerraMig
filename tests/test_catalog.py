import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from terramig.domain import ModuleVersion
from terramig.services.catalog import (
    ModuleCatalogBuilder,
    ModuleInspection,
    TerraformModuleInspector,
    validate_catalog,
)


class ModuleCatalogTests(unittest.TestCase):
    def test_inspector_extracts_single_owned_resource_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text(
                'variable "name" { type = string }\n'
                'resource "google_storage_bucket" "this" { name = var.name }\n'
                'output "bucket_name" { value = google_storage_bucket.this.name }\n'
            )
            inspection = TerraformModuleInspector(
                "app.terraform.io", "token"
            ).inspect_directory(root)
        self.assertEqual(inspection.schema_status, "verified")
        self.assertEqual(
            inspection.resource_types, ("storage.googleapis.com/Bucket",)
        )
        self.assertEqual(
            inspection.managed_resource_address, "google_storage_bucket.this"
        )
        self.assertEqual(inspection.inputs, ("name",))
        self.assertEqual(inspection.outputs, ("bucket_name",))

    def test_multiple_managed_resources_require_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text(
                'resource "google_storage_bucket" "one" {}\n'
                'resource "google_storage_bucket" "two" {}\n'
            )
            inspection = TerraformModuleInspector(
                "app.terraform.io", "token"
            ).inspect_directory(root)
        self.assertEqual(inspection.schema_status, "needs-review")
        self.assertEqual(inspection.managed_resource_address, "")
        self.assertEqual(
            inspection.managed_resource_addresses,
            ("google_storage_bucket.one", "google_storage_bucket.two"),
        )

    def test_repeated_resource_requires_instance_specific_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text(
                'resource "google_storage_bucket" "this" {\n'
                '  for_each = var.buckets\n'
                '  name = each.key\n'
                '}\n'
            )
            inspection = TerraformModuleInspector(
                "app.terraform.io", "token"
            ).inspect_directory(root)
        self.assertEqual(inspection.schema_status, "needs-review")
        self.assertIn("count or for_each", inspection.diagnostics[0])

    def test_catalog_digest_and_signature_detect_tampering(self) -> None:
        module = ModuleVersion(
            "m1",
            "app.terraform.io/acme/bucket/gcp",
            "1.2.3",
            (),
            (),
        )

        class Registry:
            def list_modules(self):
                return [module]

        class Inspector:
            def inspect(self, value):
                return ModuleInspection(
                    ("storage.googleapis.com/Bucket",),
                    ("name",),
                    ("bucket_name",),
                    "google_storage_bucket.this",
                    "verified",
                )

        payload = ModuleCatalogBuilder(
            Registry(), Inspector(), "acme", "7.39.0"
        ).build("signing-key")
        validate_catalog(payload, signing_key="signing-key", require_signature=True)
        tampered = json.loads(json.dumps(payload))
        tampered["modules"][0]["version"] = "9.9.9"
        with self.assertRaisesRegex(ValueError, "integrity"):
            validate_catalog(
                tampered, signing_key="signing-key", require_signature=True
            )

    def test_inspector_rejects_hcl_injection_in_module_metadata(self) -> None:
        inspector = TerraformModuleInspector("app.terraform.io", "token")
        malicious = ModuleVersion(
            "m1",
            'app.terraform.io/acme/bucket/gcp"\n}\nresource "null_resource" "x',
            "1.2.3",
            (),
            (),
        )
        with self.assertRaisesRegex(ValueError, "invalid registry address"):
            inspector.inspect(malicious)

    def test_inspector_accepts_hcp_curated_public_registry_source(self) -> None:
        inspector = TerraformModuleInspector("app.terraform.io", "token")
        module = ModuleVersion(
            "m1",
            "registry.terraform.io/example-publisher/storage-bucket/google",
            "2.7.2",
            (),
            (),
            registry_kind="hcp-public",
        )
        failed_download = SimpleNamespace(
            returncode=1,
            stderr="public registry unavailable",
            stdout="",
        )
        with patch(
            "terramig.services.catalog.subprocess.run",
            return_value=failed_download,
        ):
            with self.assertRaisesRegex(RuntimeError, "could not download"):
                inspector.inspect(module)

    def test_downloaded_legacy_module_is_visible_but_incompatible(self) -> None:
        inspector = TerraformModuleInspector("app.terraform.io", "token")
        module = ModuleVersion(
            "m1",
            "registry.terraform.io/example/legacy/google",
            "0.1.0",
            (),
            (),
            registry_kind="hcp-public",
        )

        def downloaded_with_compatibility_error(*_args, **kwargs):
            root = Path(kwargs["cwd"])
            module_root = root / ".terraform/modules/catalog"
            module_root.mkdir(parents=True)
            (module_root / "main.tf").write_text(
                'resource "google_compute_network" "this" {}\n'
            )
            return SimpleNamespace(
                returncode=1,
                stderr="Error: Invalid quoted type constraints",
                stdout="",
            )

        with patch(
            "terramig.services.catalog.subprocess.run",
            side_effect=downloaded_with_compatibility_error,
        ):
            inspection = inspector.inspect(module)
        self.assertEqual(inspection.schema_status, "incompatible")
        self.assertEqual(
            inspection.resource_types,
            ("compute.googleapis.com/Network",),
        )
        self.assertEqual(inspection.managed_resource_address, "")
        self.assertIn("compatibility check failed", inspection.diagnostics[-1])

    def test_hcp_library_provenance_trusts_multi_resource_module(self) -> None:
        module = ModuleVersion(
            "m1",
            "registry.terraform.io/acme/network-stack/google",
            "1.2.3",
            (),
            (),
            registry_kind="hcp-public",
        )

        class Registry:
            def list_modules(self):
                return [module]

        class Inspector:
            def inspect(self, value):
                return ModuleInspection(
                    (
                        "compute.googleapis.com/Network",
                        "compute.googleapis.com/Subnetwork",
                    ),
                    ("project_id",),
                    ("network_id",),
                    "",
                    "needs-review",
                    ("Multiple root managed resources.",),
                    (
                        "google_compute_network.this",
                        "google_compute_subnetwork.this",
                    ),
                )

        payload = ModuleCatalogBuilder(
            Registry(), Inspector(), "acme", "7.39.0"
        ).build()
        catalog_module = payload["modules"][0]
        self.assertEqual(catalog_module["schema_status"], "trusted")
        self.assertEqual(catalog_module["trust_status"], "trusted")
        self.assertEqual(
            catalog_module["managed_resource_addresses"],
            [
                "google_compute_network.this",
                "google_compute_subnetwork.this",
            ],
        )
        validate_catalog(payload)


if __name__ == "__main__":
    unittest.main()
