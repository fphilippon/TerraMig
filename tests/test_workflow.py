import unittest
from pathlib import Path

from terramig.adapters.fixtures import DeterministicGenerationAgent, FixtureInventory, FixtureRegistry, MockStateImporter, MockTerraformVerifier
from terramig.adapters.git_delivery import MockGitDeliveryEngine
from terramig.domain import ModuleVersion, Resource, Stage, VerificationResult, Workflow
from terramig.persistence import MemoryPersistence
from terramig.services.workflow import DIRECT_RESOURCE, InvalidTransition, WorkflowService


ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.importer = MockStateImporter()
        self.service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            self.importer,
            verifier=MockTerraformVerifier(),
        )
        self.workflow = self.service.create("acme-clickops-prod")

    def test_legacy_workflow_cannot_enter_production_runtime(self) -> None:
        persistence = MemoryPersistence()
        persistence.save_workflow(
            Workflow(id="legacy-run", project_id="legacy-project")
        )
        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
            persistence=persistence,
            runtime_origin="production",
            required_runtime_origin="production",
        )

        with self.assertRaisesRegex(InvalidTransition, "predates"):
            service.discover("legacy-run")

    def test_complete_mock_adoption(self) -> None:
        workflow = self.service.discover(self.workflow.id)
        self.assertEqual(workflow.stage, Stage.DISCOVERED)
        self.assertEqual(workflow.ordered_resource_ids[0], "acme-clickops-prod-artifacts")
        workflow = self.service.match(workflow.id)
        self.assertTrue(all(workflow.candidates.values()))
        workflow = self.service.generate(workflow.id)
        self.assertEqual(len(workflow.bundle.imports), len(workflow.resources))
        self.assertTrue(
            any("AI composition: normalized" in event for event in workflow.events)
        )
        self.assertTrue(
            any("reviewable adoption bundle" in event for event in workflow.events)
        )
        self.assertIn("app.terraform.io/acme/network/google", workflow.bundle.terraform)
        self.assertIn("network = module.app_vpc.network_self_link", workflow.bundle.terraform)
        self.assertIn("subnetwork = module.app_subnet.subnetwork_self_link", workflow.bundle.terraform)
        self.assertIn("machine_type = \"e2-small\"", workflow.bundle.terraform)
        workflow = self.service.verify(workflow.id)
        self.assertEqual(workflow.stage, Stage.VALIDATED)
        self.assertFalse(workflow.verification.drift_detected)
        workflow = self.service.import_state(workflow.id)
        self.assertEqual(workflow.stage, Stage.IMPORTED)
        self.assertEqual(len(self.importer.runs), 1)

    def test_only_operator_selected_resources_are_matched_and_generated(self) -> None:
        workflow = self.service.discover(self.workflow.id)
        bucket_id = "acme-clickops-prod-artifacts"
        workflow = self.service.match(workflow.id, [bucket_id])
        self.assertEqual(workflow.selected_resource_ids, [bucket_id])
        self.assertEqual(list(workflow.candidates), [bucket_id])
        workflow = self.service.generate(
            workflow.id,
            {bucket_id: "app.terraform.io/acme/storage-bucket/google"},
        )
        self.assertEqual(len(workflow.bundle.imports), 1)
        self.assertEqual(workflow.bundle.imports[0].remote_id, bucket_id)

    def test_discovery_selects_only_verified_adoption_targets_by_default(self) -> None:
        class MixedInventory:
            def discover(self, project_id):
                return [
                    Resource(
                        "managed",
                        "compute.googleapis.com/Route",
                        "managed",
                        "global",
                        {},
                        support_status="unsupported",
                    ),
                    Resource(
                        "bucket",
                        "storage.googleapis.com/Bucket",
                        "bucket",
                        "EU",
                        {},
                        terraform_type="google_storage_bucket",
                        import_id="bucket",
                        support_status="supported",
                    ),
                ]

        service = WorkflowService(
            MixedInventory(),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
        )
        workflow = service.discover(service.create("p").id)
        self.assertEqual(workflow.selected_resource_ids, ["bucket"])

    def test_discovery_tracks_completed_imports_and_does_not_reselect_them(self) -> None:
        persistence = MemoryPersistence()
        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
            persistence=persistence,
            hcp_target={"organization": "acme", "workspace": "adoption"},
        )
        bucket_id = "acme-clickops-prod-artifacts"
        first = service.create("acme-clickops-prod")
        service.discover(first.id)
        service.match(first.id, [bucket_id])
        service.generate(first.id)
        service.verify(first.id)
        service.import_state(first.id)

        second = service.create("acme-clickops-prod")
        second = service.discover(second.id)

        self.assertNotIn(bucket_id, second.selected_resource_ids)
        self.assertEqual(
            second.managed_resources[bucket_id]["status"], "tracked-imported"
        )
        self.assertEqual(
            second.managed_resources[bucket_id]["target"], "acme/adoption"
        )
        self.assertTrue(
            any("previously imported" in event for event in second.events)
        )

    def test_discovery_survives_cycles_and_review_gates_supported_members(self) -> None:
        class CyclicInventory:
            def discover(self, project_id, progress=None):
                return [
                    Resource(
                        "backup-a",
                        "backupdr.googleapis.com/Backup",
                        "backup-a",
                        "global",
                        {},
                        ("backup-b",),
                        support_status="unknown",
                    ),
                    Resource(
                        "backup-b",
                        "backupdr.googleapis.com/Backup",
                        "backup-b",
                        "global",
                        {},
                        ("backup-a",),
                        support_status="unknown",
                    ),
                    Resource(
                        "supported-a",
                        "compute.googleapis.com/Instance",
                        "supported-a",
                        "global",
                        {},
                        ("supported-b",),
                        terraform_type="google_compute_instance",
                        import_id="projects/p/zones/z/instances/a",
                        support_status="supported",
                        support_reason="Mapped deterministically.",
                    ),
                    Resource(
                        "supported-b",
                        "compute.googleapis.com/Instance",
                        "supported-b",
                        "global",
                        {},
                        ("supported-a",),
                        terraform_type="google_compute_instance",
                        import_id="projects/p/zones/z/instances/b",
                        support_status="supported",
                        support_reason="Mapped deterministically.",
                    ),
                    Resource(
                        "independent",
                        "storage.googleapis.com/Bucket",
                        "independent",
                        "global",
                        {},
                        terraform_type="google_storage_bucket",
                        import_id="independent",
                        support_status="supported",
                    ),
                ]

        service = WorkflowService(
            CyclicInventory(),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
        )
        messages: list[str] = []
        workflow = service.discover(service.create("p").id, progress=messages.append)

        self.assertEqual(workflow.stage, Stage.DISCOVERED)
        self.assertEqual(workflow.selected_resource_ids, ["independent"])
        statuses = {
            resource.id: resource.support_status
            for resource in workflow.resources
        }
        self.assertEqual(statuses["backup-a"], "unknown")
        self.assertEqual(statuses["supported-a"], "partial")
        self.assertTrue(any("Collapsed 2 dependency cycle" in item for item in messages))
        self.assertTrue(any("2 supported resource(s)" in item for item in messages))
        self.assertTrue(
            any("Collapsed 2 dependency cycle" in item for item in workflow.events)
        )

    def test_resource_selection_includes_discovered_dependency_closure(self) -> None:
        workflow = self.service.discover(self.workflow.id)
        instance_id = "projects/acme-clickops-prod/zones/europe-west1-b/instances/web-01"
        workflow = self.service.match(workflow.id, [instance_id])
        self.assertEqual(
            workflow.selected_resource_ids,
            [
                "projects/acme-clickops-prod/global/networks/app-vpc",
                "projects/acme-clickops-prod/regions/europe-west1/subnetworks/app-subnet",
                instance_id,
            ],
        )

    def test_dependency_closure_does_not_reimport_tracked_resources(self) -> None:
        workflow = self.service.discover(self.workflow.id)
        network_id = "projects/acme-clickops-prod/global/networks/app-vpc"
        instance_id = "projects/acme-clickops-prod/zones/europe-west1-b/instances/web-01"
        workflow.managed_resources[network_id] = {
            "status": "tracked-imported",
            "source": "completed-terramig-lifecycle",
            "workflow_id": "prior",
            "target": "acme/network",
            "address": "google_compute_network.app_vpc",
        }

        workflow = self.service.match(workflow.id, [instance_id])

        self.assertNotIn(network_id, workflow.selected_resource_ids)
        self.assertIn(
            "projects/acme-clickops-prod/regions/europe-west1/subnetworks/app-subnet",
            workflow.selected_resource_ids,
        )

    def test_resource_selection_does_not_auto_select_unsupported_dependencies(self) -> None:
        class MixedDependencyInventory:
            def discover(self, project_id):
                return [
                    Resource(
                        "managed-route",
                        "compute.googleapis.com/Route",
                        "managed-route",
                        "global",
                        {},
                        support_status="unsupported",
                    ),
                    Resource(
                        "bucket",
                        "storage.googleapis.com/Bucket",
                        "bucket",
                        "EU",
                        {},
                        ("managed-route",),
                        terraform_type="google_storage_bucket",
                        import_id="bucket",
                        support_status="supported",
                    ),
                ]

        service = WorkflowService(
            MixedDependencyInventory(),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
        )
        workflow = service.discover(service.create("p").id)
        workflow = service.match(workflow.id, ["bucket"])
        self.assertEqual(workflow.selected_resource_ids, ["bucket"])

    def test_operator_can_select_a_public_registry_candidate(self) -> None:
        class PublicRegistry:
            def search_modules(self, resources):
                return [
                    ModuleVersion(
                        "public-bucket",
                        "example-modules/single-bucket/google",
                        "1.2.3",
                        ("storage.googleapis.com/Bucket",),
                        ("name",),
                        ("url",),
                        "google_storage_bucket.this",
                        "Verified public single-bucket module",
                        "registry-documented",
                        "public",
                        True,
                    )
                ]

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
            public_registry=PublicRegistry(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        bucket_id = "acme-clickops-prod-artifacts"
        workflow = service.match(workflow.id, [bucket_id], include_public=True)
        sources = [item.module.source for item in workflow.candidates[bucket_id]]
        self.assertIn("example-modules/single-bucket/google", sources)
        workflow = service.generate(
            workflow.id,
            {bucket_id: "example-modules/single-bucket/google"},
        )
        self.assertIn(
            'source  = "example-modules/single-bucket/google"',
            workflow.bundle.terraform,
        )

    def test_trusted_composite_hcp_module_is_available_but_not_auto_selected(self) -> None:
        class TrustedRegistry:
            def list_modules(self):
                return [
                    ModuleVersion(
                        id="trusted-stack",
                        source="app.terraform.io/acme/storage-stack/google",
                        version="1.2.3",
                        resource_types=("storage.googleapis.com/Bucket",),
                        inputs=("name",),
                        managed_resource_address="",
                        schema_status="trusted",
                        registry_kind="private",
                        managed_resource_addresses=(
                            "google_storage_bucket.this",
                            "google_storage_bucket_iam_member.members",
                        ),
                    ),
                    ModuleVersion(
                        id="legacy-stack",
                        source="app.terraform.io/acme/legacy/google",
                        version="0.1.0",
                        resource_types=("storage.googleapis.com/Bucket",),
                        inputs=(),
                        schema_status="incompatible",
                        registry_kind="private",
                    ),
                ]

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            TrustedRegistry(),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        bucket_id = "acme-clickops-prod-artifacts"
        workflow = service.match(workflow.id, [bucket_id])
        sources = [item.module.source for item in workflow.candidates[bucket_id]]
        self.assertEqual(
            sources, ["app.terraform.io/acme/storage-stack/google"]
        )
        self.assertEqual(
            workflow.module_selections[bucket_id],
            DIRECT_RESOURCE,
        )
        workflow = service.select_modules(workflow.id, "latest-compatible")
        self.assertEqual(workflow.module_selections[bucket_id], DIRECT_RESOURCE)
        workflow = service.generate(
            workflow.id,
            {bucket_id: "app.terraform.io/acme/storage-stack/google"},
        )
        self.assertIn(
            'source  = "app.terraform.io/acme/storage-stack/google"',
            workflow.bundle.terraform,
        )

    def test_direct_resource_can_be_selected_when_module_candidates_exist(self) -> None:
        workflow = self.service.discover(self.workflow.id)
        bucket_id = "acme-clickops-prod-artifacts"
        workflow = self.service.match(workflow.id, [bucket_id])
        self.assertTrue(workflow.candidates[bucket_id])
        workflow = self.service.generate(
            workflow.id,
            {bucket_id: DIRECT_RESOURCE},
        )
        self.assertEqual(workflow.module_selections[bucket_id], DIRECT_RESOURCE)
        self.assertIn(
            'resource "google_storage_bucket"',
            workflow.bundle.terraform,
        )

    def test_bulk_module_selection_uses_compatible_and_direct_policies(self) -> None:
        workflow = self.service.discover(self.workflow.id)
        bucket_id = "acme-clickops-prod-artifacts"
        workflow = self.service.match(workflow.id, [bucket_id])

        workflow = self.service.select_modules(workflow.id, "direct")
        self.assertEqual(workflow.module_selections[bucket_id], DIRECT_RESOURCE)

        workflow = self.service.select_modules(workflow.id, "latest-compatible")
        self.assertEqual(
            workflow.module_selections[bucket_id],
            "app.terraform.io/acme/storage-bucket/google",
        )
        self.assertTrue(
            any(
                "Bulk module selection applied: 1 latest compatible module"
                in event
                for event in workflow.events
            )
        )

    def test_bulk_module_selection_rejects_unknown_policy(self) -> None:
        workflow = self.service.discover(self.workflow.id)
        workflow = self.service.match(workflow.id)
        with self.assertRaisesRegex(InvalidTransition, "selection policy"):
            self.service.select_modules(workflow.id, "newest-anything")

    def test_no_module_match_falls_back_to_verified_direct_resource(self) -> None:
        class EmptyRegistry:
            def list_modules(self):
                return []

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            EmptyRegistry(),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        bucket_id = "acme-clickops-prod-artifacts"
        workflow = service.match(workflow.id, [bucket_id])
        self.assertEqual(workflow.module_selections[bucket_id], DIRECT_RESOURCE)
        workflow = service.select_modules(workflow.id, "latest-compatible")
        self.assertEqual(workflow.module_selections[bucket_id], DIRECT_RESOURCE)
        workflow = service.generate(workflow.id, {bucket_id: DIRECT_RESOURCE})
        self.assertIn('resource "google_storage_bucket"', workflow.bundle.terraform)
        self.assertIn("raw resource fallback requires review", workflow.bundle.warnings[0])

    def test_cannot_skip_review_stages(self) -> None:
        with self.assertRaises(InvalidTransition):
            self.service.import_state(self.workflow.id)

    def test_hcp_target_is_rendered_in_cloud_block(self) -> None:
        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter("acme", "gcp-adoption-prod"),
            {"hostname": "app.terraform.io", "organization": "acme", "workspace": "gcp-adoption-prod"},
            MockTerraformVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        workflow = service.generate(workflow.id)
        self.assertIn('organization = "acme"', workflow.bundle.backend_tf)
        self.assertIn('name = "gcp-adoption-prod"', workflow.bundle.backend_tf)
        self.assertIn('source  = "hashicorp/google"', workflow.bundle.providers_tf)
        self.assertIn(
            'default     = "acme-clickops-prod"', workflow.bundle.providers_tf
        )
        self.assertNotIn('provider "google"', workflow.bundle.terraform)

    def test_drift_blocks_import(self) -> None:
        class DriftVerifier:
            def verify(self, project_id, bundle, progress=None):
                return VerificationResult(False, True, "1.14", [], "Drift detected", ["update: module.vm"], "plan", 1)

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=DriftVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        workflow = service.verify(workflow.id)
        self.assertEqual(workflow.stage, Stage.GENERATED)
        with self.assertRaises(InvalidTransition):
            service.import_state(workflow.id)

    def test_operator_can_accept_verified_drift_with_complete_import_coverage(self) -> None:
        class DriftVerifier:
            def verify(self, project_id, bundle, progress=None):
                return VerificationResult(
                    False,
                    True,
                    "1.14",
                    [],
                    "Drift detected",
                    ["update: module.vm"],
                    "plan",
                    len(bundle.imports),
                )

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=DriftVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        workflow = service.verify(workflow.id)

        self.assertEqual(workflow.stage, Stage.GENERATED)
        with self.assertRaises(InvalidTransition):
            service.import_state(workflow.id)

        workflow = service.accept_drift(workflow.id, True)

        self.assertEqual(workflow.stage, Stage.VALIDATED)
        self.assertTrue(workflow.drift_accepted)
        self.assertTrue(workflow.drift_accepted_at)
        self.assertTrue(
            any("explicitly accepted" in event for event in workflow.events)
        )
        self.assertEqual(service.import_state(workflow.id).stage, Stage.IMPORTED)

    def test_drift_acceptance_cannot_override_incomplete_import_coverage(self) -> None:
        class IncompleteDriftVerifier:
            def verify(self, project_id, bundle, progress=None):
                return VerificationResult(
                    False,
                    True,
                    "1.14",
                    [],
                    "Drift and incomplete imports",
                    ["missing import"],
                    "plan",
                    max(0, len(bundle.imports) - 1),
                )

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=IncompleteDriftVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        service.verify(workflow.id)

        with self.assertRaisesRegex(InvalidTransition, "every expected import"):
            service.accept_drift(workflow.id, True)

    def test_drift_acceptance_cannot_override_validation_failure(self) -> None:
        class InvalidVerifier:
            def verify(self, project_id, bundle, progress=None):
                return VerificationResult(
                    False,
                    False,
                    "1.14",
                    [],
                    "Terraform validation failed",
                    ["invalid configuration"],
                    "validate",
                    0,
                )

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=InvalidVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        service.verify(workflow.id)

        with self.assertRaisesRegex(InvalidTransition, "did not detect drift"):
            service.accept_drift(workflow.id, True)

    def test_failed_drift_plan_can_return_to_match_and_discard_artifacts(self) -> None:
        class CompositeRegistry:
            def list_modules(self):
                return [
                    ModuleVersion(
                        id="composite-storage",
                        source="app.terraform.io/acme/storage-stack/google",
                        version="1.2.3",
                        resource_types=("storage.googleapis.com/Bucket",),
                        inputs=("name",),
                        managed_resource_addresses=(
                            "google_project_service.storage_api",
                            "google_storage_bucket.this",
                        ),
                        schema_status="trusted",
                        registry_kind="private",
                    )
                ]

        class DriftVerifier:
            def verify(self, project_id, bundle, progress=None):
                return VerificationResult(
                    False,
                    True,
                    "1.15.8",
                    [],
                    "Local plan is not import-only; HCP import is blocked.",
                    ["Drift-producing actions: module.bucket.google_project_service.storage_api"],
                    "Plan: 1 to import, 1 to add, 0 to change, 0 to destroy.",
                    1,
                )

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            CompositeRegistry(),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=DriftVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        bucket_id = "acme-clickops-prod-artifacts"
        service.match(workflow.id, [bucket_id])
        service.generate(
            workflow.id,
            {bucket_id: "app.terraform.io/acme/storage-stack/google"},
        )
        workflow = service.verify(workflow.id)
        self.assertEqual(workflow.stage, Stage.GENERATED)
        self.assertIn(
            "Return to Match",
            workflow.verification.diagnostics[-1],
        )

        workflow = service.match(workflow.id, [bucket_id])

        self.assertEqual(workflow.stage, Stage.MATCHED)
        self.assertIsNone(workflow.bundle)
        self.assertIsNone(workflow.verification)
        self.assertEqual(workflow.module_selections[bucket_id], DIRECT_RESOURCE)
        self.assertTrue(
            any(
                "discarded the generated bundle" in event
                for event in workflow.events
            )
        )

    def test_repairable_validation_failure_runs_bounded_ai_repair(self) -> None:
        class RepairingAgent(DeterministicGenerationAgent):
            def __init__(self):
                self.repairs = 0

            def repair_after_verification(
                self, task, bundle, verification, progress=None
            ):
                self.repairs += 1
                if progress:
                    progress("AI verification repair: corrected stale module cardinality")
                return bundle

        class RepairingVerifier:
            def __init__(self):
                self.calls = 0

            def verify(self, project_id, bundle, progress=None):
                self.calls += 1
                if self.calls == 1:
                    return VerificationResult(
                        False,
                        False,
                        "1.15.8",
                        [],
                        "Terraform validation failed",
                        ["error: Invalid import 'to' expression"],
                        "The target resource does not use count.",
                    )
                return VerificationResult(
                    True,
                    False,
                    "1.15.8",
                    [],
                    "Import-only plan verified",
                    [],
                    "plan accepted",
                    len(bundle.imports),
                )

        agent = RepairingAgent()
        verifier = RepairingVerifier()
        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            agent,
            MockStateImporter(),
            verifier=verifier,
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        workflow = service.verify(workflow.id)
        self.assertEqual(workflow.stage, Stage.VALIDATED)
        self.assertEqual(agent.repairs, 1)
        self.assertEqual(verifier.calls, 2)
        self.assertEqual(workflow.verification.repair_attempts, 1)
        self.assertTrue(workflow.verification.repaired_by_ai)

    def test_operational_validation_failure_is_never_sent_to_ai(self) -> None:
        class ObservedAgent(DeterministicGenerationAgent):
            repairs = 0

            def repair_after_verification(self, *args, **kwargs):
                self.repairs += 1
                raise AssertionError("operational failures must not reach AI")

        class AuthFailureVerifier:
            def verify(self, project_id, bundle, progress=None):
                return VerificationResult(
                    False,
                    False,
                    "1.15.8",
                    [],
                    "Terraform plan failed",
                    ["error: permission denied"],
                    "Google credentials were rejected",
                )

        agent = ObservedAgent()
        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            agent,
            MockStateImporter(),
            verifier=AuthFailureVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        workflow = service.verify(workflow.id)
        self.assertEqual(workflow.stage, Stage.GENERATED)
        self.assertEqual(agent.repairs, 0)

    def test_unknown_capability_blocks_ai_composition(self) -> None:
        class UnknownInventory:
            def list_projects(self):
                return [{"id": "p", "name": "p"}]

            def discover(self, project_id):
                return [Resource("x", "example.googleapis.com/Thing", "x", "global", {})]

        service = WorkflowService(
            UnknownInventory(),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
        )
        workflow = service.create("p")
        service.discover(workflow.id)
        service.match(workflow.id)
        with self.assertRaisesRegex(InvalidTransition, "verified Terraform mapping"):
            service.generate(workflow.id)

    def test_required_git_delivery_blocks_import_until_target_branch_exists(self) -> None:
        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
            git_delivery_engine=MockGitDeliveryEngine(),
            require_git_delivery=True,
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        service.verify(workflow.id)
        with self.assertRaisesRegex(InvalidTransition, "target Git repository"):
            service.import_state(workflow.id)
        workflow = service.deliver_git(
            workflow.id,
            {
                "model_repository": "git@github.com:acme/model.git",
                "target_repository": "git@github.com:acme/application.git",
                "target_path": "environments/prod",
                "update_existing_directory": True,
            },
        )
        self.assertEqual(workflow.git_delivery.status, "pull-request-open")
        self.assertTrue(workflow.git_delivery.update_existing_directory)
        workflow = service.import_state(workflow.id)
        self.assertEqual(workflow.stage, Stage.IMPORTED)

    def test_hcp_reconciliation_is_required_before_imported_stage(self) -> None:
        class SubmittedImporter:
            completion_stage = Stage.SUBMITTED
            last_protection = {"checks": ["safe target"]}

            def import_bundle(
                self,
                project_id,
                bundle,
                *,
                workflow_id,
                lifecycle,
                checkpoint,
                allow_nonempty_workspace=False,
                progress=None,
            ):
                lifecycle.workspace_id = "ws-1"
                lifecycle.configuration_version_id = "cv-1"
                lifecycle.run_id = "run-1"
                lifecycle.status = "awaiting-hcp-apply"
                checkpoint(lifecycle)
                return lifecycle.run_id

            def reconcile(
                self, lifecycle, expected_imports, *, workflow_id, checkpoint,
                progress=None,
            ):
                lifecycle.status = "completed"
                lifecycle.remote_status = "applied"
                lifecycle.post_import_run_id = "run-post"
                lifecycle.imported_resource_count = expected_imports
                lifecycle.drift_free = True
                checkpoint(lifecycle)
                return lifecycle, True

        service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            SubmittedImporter(),
            verifier=MockTerraformVerifier(),
        )
        workflow = service.create("acme-clickops-prod")
        service.discover(workflow.id)
        service.match(workflow.id)
        service.generate(workflow.id)
        service.verify(workflow.id)
        workflow = service.import_state(workflow.id)
        self.assertEqual(workflow.stage, Stage.SUBMITTED)
        workflow = service.reconcile_import(workflow.id)
        self.assertEqual(workflow.stage, Stage.IMPORTED)
        self.assertTrue(workflow.hcp_import.drift_free)
        self.assertIn("zero drift", workflow.events[-1])


if __name__ == "__main__":
    unittest.main()
