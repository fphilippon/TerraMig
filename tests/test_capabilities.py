import unittest

from terramig.services.capabilities import (
    CapabilityCatalog,
    dependency_ids,
    discovered_service_coverage,
)
from terramig.domain import Resource


class CapabilityCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = CapabilityCatalog()

    def test_compute_full_resource_name_becomes_provider_import_id(self) -> None:
        result = self.catalog.assess(
            "compute.googleapis.com/Network",
            "//compute.googleapis.com/projects/p/global/networks/vpc",
            {"name": "vpc"},
            configuration_complete=True,
        )
        self.assertEqual(result.status, "supported")
        self.assertEqual(result.terraform_type, "google_compute_network")
        self.assertEqual(result.import_id, "projects/p/global/networks/vpc")

    def test_bucket_uses_name_instead_of_cloud_asset_full_name(self) -> None:
        result = self.catalog.assess(
            "storage.googleapis.com/Bucket",
            "//storage.googleapis.com/acme-artifacts",
            {"name": "acme-artifacts", "location": "EU"},
            configuration_complete=True,
        )
        self.assertEqual(result.import_id, "acme-artifacts")
        self.assertEqual(result.status, "supported")

    def test_user_service_account_uses_email_import_id(self) -> None:
        result = self.catalog.assess(
            "iam.googleapis.com/ServiceAccount",
            "//iam.googleapis.com/projects/p/serviceAccounts/109180998218838574983",
            {
                "name": "projects/p/serviceAccounts/app-runner@p.iam.gserviceaccount.com",
                "email": "app-runner@p.iam.gserviceaccount.com",
                "projectId": "p",
            },
            configuration_complete=True,
        )
        self.assertEqual(result.status, "supported")
        self.assertEqual(
            result.import_id,
            "projects/p/serviceAccounts/app-runner@p.iam.gserviceaccount.com",
        )

    def test_google_managed_default_service_account_is_not_adoptable(self) -> None:
        result = self.catalog.assess(
            "iam.googleapis.com/ServiceAccount",
            "//iam.googleapis.com/projects/p/serviceAccounts/111619143665742097425",
            {
                "email": "336779352031-compute@developer.gserviceaccount.com",
                "projectId": "p",
            },
            configuration_complete=True,
        )
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.import_id, "")
        self.assertIn("Google-managed default service account", result.reason)

    def test_provider_managed_local_subnet_route_is_not_adoptable(self) -> None:
        result = self.catalog.assess(
            "compute.googleapis.com/Route",
            "//compute.googleapis.com/projects/p/global/routes/local-subnet-route",
            {
                "name": "local-subnet-route",
                "network": "projects/p/global/networks/vpc",
                "nextHopNetwork": "projects/p/global/networks/vpc",
            },
            configuration_complete=True,
        )
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.import_id, "")
        self.assertIn("google_compute_subnetwork", result.reason)

    def test_provider_managed_default_internet_route_is_not_adoptable(self) -> None:
        result = self.catalog.assess(
            "compute.googleapis.com/Route",
            "//compute.googleapis.com/projects/p/global/routes/default-route-abcd",
            {
                "name": "default-route-abcd",
                "network": "projects/p/global/networks/vpc",
                "destRange": "0.0.0.0/0",
                "description": "Default route to the Internet.",
                "nextHopGateway": (
                    "https://www.googleapis.com/compute/v1/projects/p/global/gateways/"
                    "default-internet-gateway"
                ),
            },
            configuration_complete=True,
        )
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.import_id, "")
        self.assertIn("google_compute_network", result.reason)

    def test_operator_created_internet_route_remains_adoptable(self) -> None:
        result = self.catalog.assess(
            "compute.googleapis.com/Route",
            "//compute.googleapis.com/projects/p/global/routes/egress",
            {
                "name": "egress",
                "network": "projects/p/global/networks/vpc",
                "destRange": "0.0.0.0/0",
                "description": "Application egress",
                "nextHopGateway": (
                    "https://www.googleapis.com/compute/v1/projects/p/global/gateways/"
                    "default-internet-gateway"
                ),
            },
            configuration_complete=True,
        )
        self.assertEqual(result.status, "supported")

    def test_search_metadata_and_unknown_types_are_never_marked_supported(self) -> None:
        partial = self.catalog.assess(
            "compute.googleapis.com/Network",
            "//compute.googleapis.com/projects/p/global/networks/vpc",
            {"name": "vpc"},
            configuration_complete=False,
        )
        unknown = self.catalog.assess(
            "example.googleapis.com/Thing",
            "//example.googleapis.com/projects/p/things/x",
            {"name": "x"},
            configuration_complete=True,
        )
        self.assertEqual(partial.status, "partial")
        self.assertEqual(unknown.status, "unknown")

    def test_transient_runtime_assets_are_explicitly_unsupported(self) -> None:
        result = self.catalog.assess(
            "run.googleapis.com/Execution",
            "//run.googleapis.com/projects/p/locations/eu/executions/job-1",
            {},
            configuration_complete=True,
        )
        self.assertEqual(result.status, "unsupported")

    def test_nested_resource_references_become_dependency_edges(self) -> None:
        network = "//compute.googleapis.com/projects/p/global/networks/vpc"
        subnet = "//compute.googleapis.com/projects/p/regions/eu/subnetworks/app"
        dependencies = dependency_ids(
            subnet,
            "",
            {"network": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc"},
            [(network, "projects/p/global/networks/vpc"), (subnet, "projects/p/regions/eu/subnetworks/app")],
        )
        self.assertEqual(dependencies, (network,))

    def test_identity_reverse_and_self_references_do_not_create_edges(self) -> None:
        network = "//compute.googleapis.com/projects/p/global/networks/vpc"
        subnet = "//compute.googleapis.com/projects/p/regions/eu/subnetworks/app"
        known = [
            (network, "projects/p/global/networks/vpc"),
            (subnet, "projects/p/regions/eu/subnetworks/app"),
        ]
        dependencies = dependency_ids(
            network,
            network,
            {
                "name": "projects/p/global/networks/vpc",
                "selfLink": "https://www.googleapis.com/compute/v1/projects/p/global/networks/vpc",
                "subnetworks": [
                    "https://www.googleapis.com/compute/v1/projects/p/regions/eu/subnetworks/app"
                ],
            },
            known,
        )
        self.assertEqual(dependencies, ())

    def test_expanded_catalog_covers_common_managed_services(self) -> None:
        expected = {
            "alloydb.googleapis.com/Cluster": "google_alloydb_cluster",
            "cloudscheduler.googleapis.com/Job": "google_cloud_scheduler_job",
            "eventarc.googleapis.com/Trigger": "google_eventarc_trigger",
            "monitoring.googleapis.com/AlertPolicy": "google_monitoring_alert_policy",
            "networkconnectivity.googleapis.com/Hub": "google_network_connectivity_hub",
        }
        for asset_type, terraform_type in expected.items():
            self.assertEqual(
                self.catalog.capabilities[asset_type].terraform_type, terraform_type
            )
        self.assertGreaterEqual(self.catalog.coverage()["total_asset_types"], 85)

    def test_base_compute_storage_and_database_matrix_is_registered(self) -> None:
        expected = {
            "compute.googleapis.com/InstantSnapshot": "google_compute_instant_snapshot",
            "compute.googleapis.com/MachineImage": "google_compute_machine_image",
            "compute.googleapis.com/Network": "google_compute_network",
            "compute.googleapis.com/StoragePool": "google_compute_storage_pool",
            "container.googleapis.com/Cluster": "google_container_cluster",
            "servicenetworking.googleapis.com/Connection":
                "google_service_networking_connection",
            "storage.googleapis.com/Bucket": "google_storage_bucket",
            "file.googleapis.com/Backup": "google_filestore_backup",
            "file.googleapis.com/Instance": "google_filestore_instance",
            "file.googleapis.com/Snapshot": "google_filestore_snapshot",
            "netapp.googleapis.com/StoragePool": "google_netapp_storage_pool",
            "netapp.googleapis.com/Volume": "google_netapp_volume",
            "sqladmin.googleapis.com/Instance": "google_sql_database_instance",
            "alloydb.googleapis.com/Backup": "google_alloydb_backup",
            "alloydb.googleapis.com/Cluster": "google_alloydb_cluster",
            "bigtableadmin.googleapis.com/AppProfile": "google_bigtable_app_profile",
            "redis.googleapis.com/Cluster": "google_redis_cluster",
            "spanner.googleapis.com/InstancePartition": "google_spanner_instance_partition",
        }
        for asset_type, terraform_type in expected.items():
            capability = self.catalog.capabilities[asset_type]
            self.assertTrue(capability.importable, asset_type)
            self.assertEqual(capability.terraform_type, terraform_type)

    def test_base_domain_coverage_is_reported_separately(self) -> None:
        domains = self.catalog.coverage()["domains"]
        self.assertEqual(set(domains), {"compute", "storage", "databases"})
        self.assertGreaterEqual(domains["compute"]["supported"], 35)
        self.assertGreaterEqual(domains["storage"]["supported"], 6)
        self.assertGreaterEqual(domains["databases"]["supported"], 15)
        for counts in domains.values():
            self.assertEqual(
                counts["total"],
                counts["supported"] + counts["partial"] + counts["unsupported"],
            )

    def test_base_resource_import_ids_remain_canonical(self) -> None:
        cases = (
            (
                "compute.googleapis.com/InstantSnapshot",
                "//compute.googleapis.com/projects/p/zones/europe-west1-b/"
                "instantSnapshots/checkpoint",
                {"name": "checkpoint", "sourceDisk": "projects/p/zones/europe-west1-b/disks/data"},
                "projects/p/zones/europe-west1-b/instantSnapshots/checkpoint",
            ),
            (
                "file.googleapis.com/Snapshot",
                "//file.googleapis.com/projects/p/locations/europe-west1/"
                "instances/files/snapshots/checkpoint",
                {"name": "checkpoint"},
                "projects/p/locations/europe-west1/instances/files/snapshots/checkpoint",
            ),
            (
                "alloydb.googleapis.com/Backup",
                "//alloydb.googleapis.com/projects/p/locations/europe-west1/backups/nightly",
                {"name": "nightly", "clusterName": "projects/p/locations/europe-west1/clusters/db"},
                "projects/p/locations/europe-west1/backups/nightly",
            ),
            (
                "redis.googleapis.com/Cluster",
                "//redis.googleapis.com/projects/p/locations/europe-west1/clusters/cache",
                {"name": "cache", "region": "europe-west1", "shardCount": 3},
                "projects/p/locations/europe-west1/clusters/cache",
            ),
            (
                "spanner.googleapis.com/InstancePartition",
                "//spanner.googleapis.com/projects/p/instances/db/instancePartitions/analytics",
                {"name": "analytics", "displayName": "Analytics", "config": "nam8"},
                "projects/p/instances/db/instancePartitions/analytics",
            ),
        )
        for asset_type, asset_name, attributes, expected_import_id in cases:
            result = self.catalog.assess(
                asset_type,
                asset_name,
                attributes,
                configuration_complete=True,
            )
            self.assertEqual(result.status, "supported", asset_type)
            self.assertEqual(result.import_id, expected_import_id, asset_type)

    def test_database_backup_artifacts_are_explicitly_classified(self) -> None:
        for asset_type in (
            "sqladmin.googleapis.com/BackupRun",
            "spanner.googleapis.com/Backup",
            "bigtableadmin.googleapis.com/Backup",
            "firestore.googleapis.com/Backup",
        ):
            result = self.catalog.assess(
                asset_type,
                f"//{asset_type.split('/', 1)[0]}/projects/p/backups/example",
                {"name": "example"},
                configuration_complete=True,
            )
            self.assertEqual(result.status, "unsupported", asset_type)
            self.assertEqual(result.import_id, "")

    def test_service_networking_connection_uses_provider_specific_import_id(self) -> None:
        result = self.catalog.assess(
            "servicenetworking.googleapis.com/Connection",
            "//servicenetworking.googleapis.com/projects/p/global/networks/private-services",
            {
                "network": "projects/p/global/networks/private-services",
                "reservedPeeringRanges": ["alloydb-range"],
            },
            configuration_complete=True,
        )
        self.assertEqual(result.status, "supported")
        self.assertEqual(
            result.import_id,
            "projects/p/global/networks/private-services:"
            "servicenetworking.googleapis.com",
        )

    def test_gke_managed_compute_children_are_not_adopted_independently(self) -> None:
        result = self.catalog.assess(
            "compute.googleapis.com/Instance",
            "//compute.googleapis.com/projects/p/zones/europe-west1-b/instances/gke-node",
            {
                "name": "gke-node",
                "zone": "europe-west1-b",
                "machineType": "e2-standard-4",
                "labels": {
                    "goog-gke-node": "",
                    "goog-k8s-cluster-name": "application",
                },
            },
            configuration_complete=True,
        )
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(result.import_id, "")
        self.assertIn("owned by GKE", result.reason)

    def test_scope_ambiguous_resource_stays_partial(self) -> None:
        result = self.catalog.assess(
            "logging.googleapis.com/LogSink",
            "//logging.googleapis.com/projects/p/sinks/export",
            {"name": "export", "destination": "storage.googleapis.com/bucket"},
            configuration_complete=True,
        )
        self.assertEqual(result.status, "partial")

    def test_agent_engine_and_load_balancer_types_are_mapped(self) -> None:
        expected = {
            "aiplatform.googleapis.com/ReasoningEngine": "google_vertex_ai_reasoning_engine",
            "compute.googleapis.com/BackendBucket": "google_compute_backend_bucket",
            "compute.googleapis.com/GlobalForwardingRule": "google_compute_global_forwarding_rule",
            "compute.googleapis.com/RegionBackendService": "google_compute_region_backend_service",
        }
        for asset_type, terraform_type in expected.items():
            self.assertEqual(
                self.catalog.capabilities[asset_type].terraform_type, terraform_type
            )

    def test_discovered_services_are_derived_from_runtime_inventory(self) -> None:
        resources = [
            Resource(
                id="bucket",
                type="storage.googleapis.com/Bucket",
                name="bucket",
                location="EU",
                attributes={},
                support_status="supported",
            ),
            Resource(
                id="agent",
                type="aiplatform.googleapis.com/ReasoningEngine",
                name="agent",
                location="europe-west1",
                attributes={},
                support_status="partial",
            ),
            Resource(
                id="agent-2",
                type="aiplatform.googleapis.com/ReasoningEngine",
                name="agent-2",
                location="europe-west1",
                attributes={},
                support_status="supported",
            ),
        ]
        coverage = discovered_service_coverage(resources)
        self.assertEqual(coverage["total_services"], 2)
        self.assertEqual(coverage["total_resources"], 3)
        self.assertEqual(
            coverage["services"][0],
            {
                "service": "aiplatform.googleapis.com",
                "resources": 2,
                "asset_types": ["aiplatform.googleapis.com/ReasoningEngine"],
                "by_status": {"partial": 1, "supported": 1},
            },
        )


if __name__ == "__main__":
    unittest.main()
