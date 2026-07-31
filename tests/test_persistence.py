import unittest
from pathlib import Path

from terramig.adapters.fixtures import (
    DeterministicGenerationAgent,
    FixtureInventory,
    FixtureRegistry,
    MockStateImporter,
    MockTerraformVerifier,
)
from terramig.domain import Workflow
from terramig.persistence import MemoryPersistence, SCHEMA_MIGRATIONS
from terramig.services.workflow import WorkflowService


ROOT = Path(__file__).resolve().parents[1]


def service(persistence: MemoryPersistence) -> WorkflowService:
    return WorkflowService(
        FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
        FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
        DeterministicGenerationAgent(),
        MockStateImporter(),
        verifier=MockTerraformVerifier(),
        persistence=persistence,
    )


class PersistenceTests(unittest.TestCase):
    def test_workflow_round_trip_preserves_managed_resource_evidence(self) -> None:
        persistence = MemoryPersistence()
        workflow = Workflow(
            id="managed-evidence",
            project_id="project",
            managed_resources={
                "projects/project/buckets/example": {
                    "status": "tracked-imported",
                    "source": "completed-terramig-lifecycle",
                    "workflow_id": "prior",
                    "target": "org/workspace",
                    "address": "google_storage_bucket.example",
                }
            },
        )

        persistence.save_workflow(workflow)
        restored = persistence.get_workflow(workflow.id)

        self.assertEqual(restored.managed_resources, workflow.managed_resources)

    def test_workflow_round_trip_restores_nested_domain_types(self) -> None:
        persistence = MemoryPersistence()
        original_service = service(persistence)
        workflow = original_service.create("acme-clickops-prod")
        original_service.discover(workflow.id)
        original_service.match(workflow.id)
        original_service.generate(workflow.id)
        original_service.verify(workflow.id)
        saved = original_service.get(workflow.id)
        saved.drift_accepted = True
        saved.drift_accepted_at = "2026-07-30T12:00:00+00:00"
        persistence.save_workflow(saved)

        restored = service(persistence).get(workflow.id)
        self.assertEqual(restored.stage.value, "validated")
        self.assertTrue(restored.bundle.terraform)
        self.assertIsInstance(restored.resources[0].dependency_ids, tuple)
        self.assertIsInstance(
            restored.candidates[restored.resources[0].id][0].module.inputs, tuple
        )
        self.assertEqual(restored.selected_resource_ids, saved.selected_resource_ids)
        self.assertEqual(restored.module_selections, saved.module_selections)
        self.assertTrue(restored.drift_accepted)
        self.assertEqual(
            restored.drift_accepted_at, "2026-07-30T12:00:00+00:00"
        )

    def test_settings_and_audit_events_survive_repository_reuse(self) -> None:
        persistence = MemoryPersistence()
        persistence.save_settings({"hcp_hostname": "app.terraform.io"})
        persistence.record_audit_event(
            {
                "timestamp": "2026-07-20T10:00:00+00:00",
                "kind": "operation",
                "name": "adoption.create",
                "status": "succeeded",
                "duration_ms": 1,
            }
        )
        self.assertEqual(
            persistence.load_settings(), {"hcp_hostname": "app.terraform.io"}
        )
        self.assertEqual(
            persistence.list_audit_events()[0]["name"], "adoption.create"
        )

    def test_postgresql_schema_covers_all_durable_application_state(self) -> None:
        schema = "\n".join(statements for _, statements in SCHEMA_MIGRATIONS)
        for table in (
            "terramig_settings",
            "terramig_workflows",
            "terramig_users",
            "terramig_sessions",
            "terramig_audit_events",
            "terramig_jobs",
            "terramig_secrets",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", schema)


if __name__ == "__main__":
    unittest.main()
