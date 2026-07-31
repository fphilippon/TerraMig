import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from terramig.adapters.real import GCloudInventory, HCPRegistry, TerraformPublicRegistry
from terramig.domain import Resource
from terramig.services.catalog import ModuleInspection


class RealAdapterTests(unittest.TestCase):
    def test_discovery_excludes_project_billing_and_enabled_api_metadata(self) -> None:
        assets = [
            {
                "name": "//cloudresourcemanager.googleapis.com/projects/123",
                "assetType": "cloudresourcemanager.googleapis.com/Project",
                "resource": {"data": {"projectId": "p"}},
            },
            {
                "name": "//serviceusage.googleapis.com/projects/123/services/bigquery.googleapis.com",
                "assetType": "serviceusage.googleapis.com/Service",
                "resource": {"data": {"name": "bigquery.googleapis.com"}},
            },
            {
                "name": "//cloudbilling.googleapis.com/projects/p/billingInfo",
                "assetType": "cloudbilling.googleapis.com/ProjectBillingInfo",
                "resource": {"data": {"name": "projects/p/billingInfo"}},
            },
            {
                "name": "//compute.googleapis.com/projects/p",
                "assetType": "compute.googleapis.com/Project",
                "resource": {"data": {"name": "p"}},
            },
            {
                "name": "//storage.googleapis.com/example-bucket",
                "assetType": "storage.googleapis.com/Bucket",
                "resource": {"data": {"name": "example-bucket", "location": "EU"}},
            },
        ]
        with patch.object(GCloudInventory, "_run", side_effect=[assets, assets]):
            resources = GCloudInventory("p").discover("p")
        self.assertEqual([resource.type for resource in resources], ["storage.googleapis.com/Bucket"])

    def test_discovery_reports_query_counts_and_normalization_progress(self) -> None:
        assets = [
            {
                "name": "//storage.googleapis.com/example-bucket",
                "assetType": "storage.googleapis.com/Bucket",
                "resource": {"data": {"name": "example-bucket", "location": "EU"}},
            }
        ]
        messages: list[str] = []
        with patch.object(GCloudInventory, "_run", side_effect=[assets, assets]):
            resources = GCloudInventory("p").discover("p", progress=messages.append)
        self.assertEqual(len(resources), 1)
        self.assertTrue(any("RESOURCE query returned 1 assets" in item for item in messages))
        self.assertTrue(any("search query returned 1 assets" in item for item in messages))
        self.assertTrue(any("Discovery normalized 1 resources" in item for item in messages))

    def test_gcloud_query_emits_elapsed_heartbeat(self) -> None:
        process = Mock()
        process.returncode = 0
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["gcloud", "asset", "list"], 1),
            ("[]", ""),
        ]
        messages: list[str] = []
        with patch("terramig.adapters.real.subprocess.Popen", return_value=process):
            result = GCloudInventory("p", heartbeat_seconds=1)._run(
                "asset",
                "list",
                progress=messages.append,
                operation="Cloud Asset RESOURCE query",
            )
        self.assertEqual(result, [])
        self.assertTrue(any("still running" in item for item in messages))
        self.assertTrue(any("completed in" in item for item in messages))

    def test_gcloud_query_timeout_terminates_the_process(self) -> None:
        process = Mock()
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["gcloud", "asset", "list"], 1),
            ("", "deadline diagnostic"),
        ]
        with (
            patch("terramig.adapters.real.subprocess.Popen", return_value=process),
            patch(
                "terramig.adapters.real.time.monotonic",
                side_effect=[0, 0, 121],
            ),
        ):
            with self.assertRaisesRegex(TimeoutError, "exceeded 120s"):
                GCloudInventory(
                    "p", timeout_seconds=120, heartbeat_seconds=1
                )._run("asset", "list", operation="Cloud Asset RESOURCE query")
        process.kill.assert_called_once_with()

    def test_reverse_network_children_do_not_cycle_with_subnet_dependencies(self) -> None:
        network_name = "//compute.googleapis.com/projects/p/global/networks/vpc"
        subnet_name = "//compute.googleapis.com/projects/p/regions/eu/subnetworks/app"
        assets = [
            {
                "name": "//compute.googleapis.com/projects/p",
                "assetType": "compute.googleapis.com/Project",
                "resource": {"data": {"name": "p"}},
            },
            {
                "name": network_name,
                "assetType": "compute.googleapis.com/Network",
                "resource": {
                    "data": {
                        "name": "vpc",
                        "autoCreateSubnetworks": False,
                        "subnetworks": [
                            "https://www.googleapis.com/compute/v1/projects/p/regions/eu/subnetworks/app"
                        ],
                    }
                },
            },
            {
                "name": subnet_name,
                "assetType": "compute.googleapis.com/Subnetwork",
                "resource": {
                    "data": {
                        "name": "app",
                        "region": "eu",
                        "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc",
                    }
                },
            },
        ]
        with patch.object(GCloudInventory, "_run", return_value=assets):
            resources = GCloudInventory("p").discover("p")
        self.assertEqual([resource.id for resource in resources], [network_name, subnet_name])
        self.assertEqual(resources[0].dependency_ids, ())
        self.assertEqual(resources[1].dependency_ids, (network_name,))

    def test_public_registry_returns_verified_single_resource_submodules(self) -> None:
        resource = Resource(
            "network",
            "compute.googleapis.com/Network",
            "network",
            "global",
            {"name": "network"},
            terraform_type="google_compute_network",
            support_status="supported",
        )
        search = {
            "modules": [
                {
                    "namespace": "terraform-google-modules",
                    "name": "network",
                    "provider": "google",
                    "version": "18.1.2",
                    "description": "GCP network module",
                    "verified": True,
                }
            ]
        }
        detail = {
            "root": {"path": "", "resources": [{"type": "google_compute_network", "name": "one"}, {"type": "google_compute_subnetwork", "name": "many"}]},
            "submodules": [
                {
                    "path": "modules/vpc",
                    "inputs": [{"name": "network_name"}],
                    "outputs": [{"name": "network_self_link"}],
                    "resources": [{"type": "google_compute_network", "name": "network"}],
                },
                {
                    "path": "examples/not-selectable",
                    "resources": [{"type": "google_compute_network", "name": "example"}],
                },
            ],
        }
        registry = TerraformPublicRegistry()
        with patch.object(registry, "_get", side_effect=[search, detail]) as get:
            modules = registry.search_modules([resource])
        self.assertEqual(len(modules), 1)
        self.assertEqual(
            modules[0].source,
            "terraform-google-modules/network/google//modules/vpc",
        )
        self.assertEqual(modules[0].managed_resource_address, "google_compute_network.network")
        self.assertEqual(modules[0].registry_kind, "public")
        self.assertTrue(modules[0].verified_publisher)
        self.assertIn("verified=true", get.call_args_list[0].args[0])

    def test_cloud_asset_parent_becomes_dependency(self) -> None:
        network = {"name": "//compute.googleapis.com/projects/p/global/networks/vpc", "assetType": "compute.googleapis.com/Network", "displayName": "vpc", "location": "global"}
        subnet = {"name": "//compute.googleapis.com/projects/p/regions/eu/subnetworks/subnet", "assetType": "compute.googleapis.com/Subnetwork", "displayName": "subnet", "location": "eu", "parentFullResourceName": network["name"]}
        with patch.object(GCloudInventory, "_run", return_value=[network, subnet]):
            resources = GCloudInventory("p").discover("p")
        self.assertEqual(resources[1].dependency_ids, (network["name"],))

    def test_hcp_registry_filters_google_and_selects_usable_version(self) -> None:
        envelope = {"data": [{"id": "mod-1", "attributes": {"name": "network", "namespace": "acme", "provider": "google", "version-statuses": [{"version": "1.2.0", "status": "ok"}]}}], "links": {"next": None}}
        registry = HCPRegistry("app.terraform.io", "acme", "token")
        with patch.object(registry, "_get", return_value=envelope):
            modules = registry.list_modules()
        self.assertEqual(modules[0].source, "app.terraform.io/acme/network/google")
        self.assertEqual(modules[0].version, "1.2.0")
        self.assertEqual(modules[0].schema_status, "metadata-only")

    def test_hcp_registry_includes_curated_public_google_modules(self) -> None:
        envelope = {
            "data": [
                {
                    "id": "private",
                    "attributes": {
                        "name": "network",
                        "namespace": "acme",
                        "provider": "google",
                        "registry-name": "private",
                        "version-statuses": [
                            {"version": "10.0.0", "status": "ok"},
                            {"version": "2.9.0", "status": "ok"},
                        ],
                    },
                },
                {
                    "id": "public",
                    "attributes": {
                        "name": "public-network",
                        "namespace": "community",
                        "provider": "google",
                        "registry-name": "public",
                        "version-statuses": [{"version": "99.0.0", "status": "ok"}],
                    },
                    "links": {
                        "self": "/api/v2/organizations/acme/registry-modules/public/community/public-network/google"
                    },
                },
            ],
            "links": {"next": None},
        }
        public_versions = {
            "data": [
                {"attributes": {"version": "1.9.0", "status": ""}},
                {"attributes": {"version": "2.0.0", "status": ""}},
            ],
            "links": {"next": None},
        }
        registry = HCPRegistry("app.terraform.io", "acme", "token")
        with patch.object(
            registry, "_get", side_effect=[envelope, public_versions]
        ) as get:
            modules = registry.list_modules()
        self.assertEqual(len(modules), 2)
        private, public = modules
        self.assertEqual(private.version, "10.0.0")
        self.assertEqual(private.registry_kind, "private")
        self.assertEqual(
            public.source,
            "registry.terraform.io/community/public-network/google",
        )
        self.assertEqual(public.version, "2.0.0")
        self.assertEqual(public.registry_kind, "hcp-public")
        self.assertIn(
            "/versions?page%5Bsize%5D=100",
            get.call_args_list[1].args[0],
        )

    def test_cloud_asset_resource_content_produces_supported_mapping_and_nested_edge(self) -> None:
        network_name = "//compute.googleapis.com/projects/p/global/networks/vpc"
        assets = [
            {
                "name": network_name,
                "assetType": "compute.googleapis.com/Network",
                "resource": {"data": {"name": "vpc", "autoCreateSubnetworks": False}},
            },
            {
                "name": "//compute.googleapis.com/projects/p/regions/eu/subnetworks/app",
                "assetType": "compute.googleapis.com/Subnetwork",
                "resource": {
                    "location": "eu",
                    "data": {
                        "name": "app",
                        "region": "eu",
                        "network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc",
                    },
                },
            },
        ]
        with patch.object(GCloudInventory, "_run", return_value=assets):
            resources = GCloudInventory("p").discover("p")
        self.assertEqual(resources[0].support_status, "supported")
        self.assertEqual(resources[0].terraform_type, "google_compute_network")
        self.assertEqual(resources[0].import_id, "projects/p/global/networks/vpc")
        self.assertEqual(resources[1].dependency_ids, (network_name,))

    def test_search_fallback_is_marked_partial(self) -> None:
        item = {
            "name": "//compute.googleapis.com/projects/p/global/networks/vpc",
            "assetType": "compute.googleapis.com/Network",
            "displayName": "vpc",
            "additionalAttributes": {"name": "vpc"},
        }
        with patch.object(GCloudInventory, "_run", side_effect=[RuntimeError("no list"), [item]]) as run:
            resource = GCloudInventory("p").discover("p")[0]
        self.assertEqual(run.call_count, 2)
        self.assertEqual(resource.support_status, "partial")

    def test_list_and_search_are_merged_without_downgrading_resource_content(self) -> None:
        listed = {
            "name": "//compute.googleapis.com/projects/p/global/networks/vpc",
            "assetType": "compute.googleapis.com/Network",
            "resource": {"data": {"name": "vpc"}},
        }
        search_only = {
            "name": "//compute.googleapis.com/projects/p/global/forwardingRules/web",
            "assetType": "compute.googleapis.com/ForwardingRule",
            "displayName": "web",
            "additionalAttributes": {"name": "web"},
        }
        inventory = GCloudInventory("p")
        with patch.object(inventory, "_run", side_effect=[[listed], [listed, search_only]]) as run:
            resources = inventory.discover("p")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(resources), 2)
        by_name = {resource.name: resource for resource in resources}
        self.assertEqual(by_name["vpc"].support_status, "supported")
        self.assertEqual(by_name["web"].support_status, "partial")

    def test_service_account_numeric_and_email_views_are_coalesced(self) -> None:
        listed = {
            "name": "//iam.googleapis.com/projects/p/serviceAccounts/109180998218838574983",
            "assetType": "iam.googleapis.com/ServiceAccount",
            "resource": {
                "data": {
                    "name": "projects/p/serviceAccounts/app-runner@p.iam.gserviceaccount.com",
                    "email": "app-runner@p.iam.gserviceaccount.com",
                    "projectId": "p",
                    "displayName": "Application runner",
                }
            },
        }
        searched = {
            "name": "//iam.googleapis.com/projects/p/serviceAccounts/app-runner@p.iam.gserviceaccount.com",
            "assetType": "iam.googleapis.com/ServiceAccount",
            "displayName": "Application runner",
            "additionalAttributes": {
                "email": "app-runner@p.iam.gserviceaccount.com"
            },
        }
        inventory = GCloudInventory("p")
        with patch.object(inventory, "_run", side_effect=[[listed], [searched]]):
            resources = inventory.discover("p")
        self.assertEqual(len(resources), 1)
        self.assertEqual(
            resources[0].id,
            "//iam.googleapis.com/projects/p/serviceAccounts/app-runner@p.iam.gserviceaccount.com",
        )
        self.assertEqual(
            resources[0].import_id,
            "projects/p/serviceAccounts/app-runner@p.iam.gserviceaccount.com",
        )
        self.assertEqual(resources[0].support_status, "supported")

    def test_hcp_registry_enriches_exact_version_from_verified_manifest(self) -> None:
        envelope = {"data": [{"id": "mod-1", "attributes": {"name": "network", "namespace": "acme", "provider": "google", "version-statuses": [{"version": "1.2.0", "status": "ok"}]}}], "links": {"next": None}}
        manifest = {
            "modules": [{
                "source": "app.terraform.io/acme/network/google",
                "version": "1.2.0",
                "resource_types": ["compute.googleapis.com/Network"],
                "inputs": ["name"],
                "outputs": ["network_id"],
                "managed_resource_address": "google_compute_network.this",
            }]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "modules.json"
            path.write_text(json.dumps(manifest))
            registry = HCPRegistry("app.terraform.io", "acme", "token", path)
            with patch.object(registry, "_get", return_value=envelope):
                module = registry.list_modules()[0]
        self.assertEqual(module.schema_status, "verified")
        self.assertEqual(module.resource_types, ("compute.googleapis.com/Network",))
        self.assertEqual(module.managed_resource_address, "google_compute_network.this")

    def test_hcp_registry_automatically_builds_and_reuses_catalog(self) -> None:
        envelope = {"data": [{"id": "mod-1", "attributes": {"name": "network", "namespace": "acme", "provider": "google", "version-statuses": [{"version": "1.2.0", "status": "ok"}]}}], "links": {"next": None}}
        inspection = ModuleInspection(
            ("compute.googleapis.com/Network",),
            ("name",),
            ("network_id",),
            "google_compute_network.this",
            "verified",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-modules.json"
            registry = HCPRegistry(
                "app.terraform.io",
                "acme",
                "token",
                path,
                auto_catalog=True,
            )
            with patch.object(registry, "_get", return_value=envelope), patch(
                "terramig.adapters.real.TerraformModuleInspector.inspect",
                return_value=inspection,
            ) as inspect:
                first = registry.list_modules()
                second = registry.list_modules()
                payload = json.loads(path.read_text())
        self.assertEqual(first[0].schema_status, "trusted")
        self.assertEqual(second[0].managed_resource_address, "google_compute_network.this")
        self.assertEqual(payload["organization"], "acme")
        self.assertIn("sha256", payload["integrity"])
        self.assertEqual(inspect.call_count, 1)

    def test_hcp_registry_preserves_customer_gcp_provider_segment(self) -> None:
        envelope = {
            "data": [
                {
                    "id": "mod-1",
                    "attributes": {
                        "name": "terraform-google-gcsbucket",
                        "namespace": "customer-org",
                        "provider": "gcp",
                        "version-statuses": [{"version": "2.0.0", "status": "ok"}],
                    },
                }
            ],
            "links": {"next": None},
        }
        registry = HCPRegistry("app.terraform.io", "customer-org", "token")
        with patch.object(registry, "_get", return_value=envelope):
            module = registry.list_modules()[0]
        self.assertEqual(
            module.source,
            "app.terraform.io/customer-org/terraform-google-gcsbucket/gcp",
        )


if __name__ == "__main__":
    unittest.main()
