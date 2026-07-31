import unittest
from pathlib import Path

from terramig import server
from terramig.adapters.fixtures import (
    DeterministicGenerationAgent,
    FixtureInventory,
    FixtureRegistry,
    MockStateImporter,
    MockTerraformVerifier,
)
from terramig.domain import Resource
from terramig.settings import Settings
from terramig.services.workflow import WorkflowService


ROOT = Path(__file__).resolve().parents[1]


class OperationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_settings = server.APP.settings
        self.original_service = server.APP.service
        server.APP.settings = Settings(
            hcp_organization="acme", hcp_workspace="adoption"
        )
        server.APP.service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
        )

    def tearDown(self) -> None:
        server.APP.settings = self.original_settings
        server.APP.service = self.original_service

    def test_operational_snapshot_and_capability_coverage(self) -> None:
        workflow = server.APP.service.create("p")
        snapshot = server.APP.operations()
        self.assertEqual(snapshot["workflows"]["total"], 1)
        self.assertEqual(snapshot["workflows"]["by_stage"]["created"], 1)
        coverage = server.APP.capability_coverage()
        self.assertGreaterEqual(coverage["total_asset_types"], 95)
        self.assertGreater(coverage["supported"], coverage["partial"])
        self.assertGreaterEqual(coverage["domains"]["compute"]["supported"], 35)
        self.assertGreaterEqual(coverage["domains"]["storage"]["supported"], 6)
        self.assertGreaterEqual(coverage["domains"]["databases"]["supported"], 15)
        self.assertTrue(workflow.created_at)

    def test_downloadable_report_redacts_secret_shaped_attributes(self) -> None:
        workflow = server.APP.service.create("p")
        workflow.resources = [
            Resource(
                id="secret",
                type="secretmanager.googleapis.com/Secret",
                name="secret",
                location="global",
                attributes={"name": "secret", "clientSecret": "do-not-export"},
            )
        ]
        report = server.APP.workflow_report(workflow.id)
        attributes = report["workflow"]["resources"][0]["attributes"]
        self.assertEqual(attributes["clientSecret"], "[REDACTED]")
        self.assertGreaterEqual(report["redacted_fields"], 1)
        self.assertEqual(len(report["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
