from io import BytesIO
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from terramig import server
from terramig.adapters.fixtures import (
    DeterministicGenerationAgent,
    FixtureInventory,
    FixtureRegistry,
    MockStateImporter,
    MockTerraformVerifier,
)
from terramig.domain import VerificationResult
from terramig.persistence import MemoryPersistence
from terramig.settings import Settings
from terramig.services.auth import AuthService
from terramig.services.jobs import DurableWorker, JobQueue
from terramig.services.workflow import WorkflowService


ROOT = Path(__file__).resolve().parents[1]


class JobApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            "settings": server.APP.settings,
            "service": server.APP.service,
            "auth": server.APP.auth,
            "persistence": server.APP.persistence,
            "jobs": server.APP.jobs,
            "worker": server.APP.worker,
        }
        persistence = MemoryPersistence()
        settings = Settings(
            hcp_organization="acme", hcp_workspace="adoption"
        )
        server.APP.settings = settings
        server.APP.persistence = persistence
        server.APP.service = WorkflowService(
            FixtureInventory(ROOT / "fixtures" / "gcp_inventory.json"),
            FixtureRegistry(ROOT / "fixtures" / "private_registry.json"),
            DeterministicGenerationAgent(),
            MockStateImporter(),
            verifier=MockTerraformVerifier(),
            persistence=persistence,
        )
        self.readiness = patch.object(
            server.Application,
            "readiness",
            return_value={
                "gcloud": True,
                "gcp_authenticated": True,
                "hcp_authenticated": True,
                "ai": True,
                "adoption_target": True,
            },
        )
        self.readiness.start()
        server.APP.jobs = JobQueue(persistence)
        server.APP.worker = DurableWorker(
            persistence, server.APP._job_handlers(), worker_id="api-test"
        )
        server.APP.auth = AuthService(persistence)
        issued = server.APP.auth.login("admin", "admin")
        issued = server.APP.auth.change_password(
            server.APP.auth.authenticate(issued.token),
            "admin",
            "Durable-API-Test-2026!",
        )
        self.headers = {
            "Cookie": (
                f"terramig_session={issued.token}; "
                f"terramig_csrf={issued.csrf_token}"
            ),
            "X-CSRF-Token": issued.csrf_token,
        }

    def tearDown(self) -> None:
        self.readiness.stop()
        for name, value in self.original.items():
            setattr(server.APP, name, value)

    def request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict]:
        body = json.dumps(payload or {}).encode() if payload is not None else b""
        handler = object.__new__(server.TerraMigHandler)
        handler.path = path
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            **self.headers,
        }
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.close_connection = True
        getattr(handler, f"do_{method}")()
        response = handler.wfile.getvalue()
        headers, response_body = response.split(b"\r\n\r\n", 1)
        return int(headers.split(b" ", 2)[1]), json.loads(response_body)

    def test_workflow_action_returns_durable_job_and_subject_updates_after_worker(self) -> None:
        status, workflow = self.request(
            "POST", "/api/workflows", {"project_id": "acme-clickops-prod"}
        )
        self.assertEqual(status, 201)
        status, job = self.request(
            "POST", f"/api/workflows/{workflow['id']}/discover", {}
        )
        self.assertEqual(status, 202)
        self.assertEqual(job["status"], "queued")

        self.assertTrue(server.APP.worker.execute_once())
        status, completed = self.request("GET", f"/api/jobs/{job['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "succeeded")
        status, updated = self.request(
            "GET", f"/api/workflows/{workflow['id']}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["stage"], "discovered")

    def test_job_cancel_endpoint_is_persistent(self) -> None:
        workflow = server.APP.service.create("acme-clickops-prod")
        queued = server.APP.jobs.enqueue(
            "adoption", "discover", workflow.id
        )
        status, cancelled = self.request(
            "POST", f"/api/jobs/{queued.id}/cancel", {}
        )
        self.assertEqual(status, 202)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(server.APP.jobs.get(queued.id).status.value, "cancelled")

    def test_bulk_module_selection_runs_as_a_durable_job(self) -> None:
        workflow = server.APP.service.create("acme-clickops-prod")
        server.APP.service.discover(workflow.id)
        workflow = server.APP.service.match(workflow.id)

        status, job = self.request(
            "POST",
            f"/api/workflows/{workflow.id}/select-modules",
            {"policy": "direct"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(job["action"], "select-modules")
        self.assertTrue(server.APP.worker.execute_once())

        updated = server.APP.service.get(workflow.id)
        self.assertTrue(updated.module_selections)
        self.assertEqual(
            set(updated.module_selections.values()),
            {"__resource__"},
        )

    def test_drift_acceptance_runs_as_a_durable_audited_job(self) -> None:
        class DriftVerifier:
            def verify(self, project_id, bundle, progress=None):
                return VerificationResult(
                    False,
                    True,
                    "1.14",
                    [],
                    "Drift detected",
                    ["update: google_storage_bucket.bucket"],
                    "plan",
                    len(bundle.imports),
                )

        server.APP.service.verifier = DriftVerifier()
        workflow = server.APP.service.create("acme-clickops-prod")
        server.APP.service.discover(workflow.id)
        server.APP.service.match(workflow.id)
        server.APP.service.generate(workflow.id)
        server.APP.service.verify(workflow.id)

        status, job = self.request(
            "POST",
            f"/api/workflows/{workflow.id}/accept-drift",
            {"accept": True},
        )
        self.assertEqual(status, 202)
        self.assertEqual(job["action"], "accept-drift")
        self.assertTrue(server.APP.worker.execute_once())
        completed = server.APP.jobs.get(job["id"])
        self.assertEqual(completed.status.value, "succeeded", completed.last_error)

        updated = server.APP.service.get(workflow.id)
        self.assertEqual(updated.stage.value, "validated")
        self.assertTrue(updated.drift_accepted)
        self.assertTrue(
            any("explicitly accepted" in event for event in updated.events)
        )

    def test_drift_acceptance_requires_explicit_confirmation(self) -> None:
        workflow = server.APP.service.create("acme-clickops-prod")

        status, response = self.request(
            "POST",
            f"/api/workflows/{workflow.id}/accept-drift",
            {"accept": False},
        )

        self.assertEqual(status, 409)
        self.assertIn("Explicit drift acceptance", response["error"])

    def test_import_job_preserves_nonempty_workspace_authorization(self) -> None:
        workflow = server.APP.service.create("acme-clickops-prod")
        server.APP.service.discover(workflow.id)
        server.APP.service.match(workflow.id)
        server.APP.service.generate(workflow.id)
        server.APP.service.verify(workflow.id)

        status, job = self.request(
            "POST",
            f"/api/workflows/{workflow.id}/import",
            {
                "confirm": True,
                "allow_nonempty_workspace": True,
            },
        )

        self.assertEqual(status, 202)
        self.assertTrue(job["payload"]["allow_nonempty_workspace"])
        self.assertTrue(server.APP.worker.execute_once())
        completed = server.APP.jobs.get(job["id"])
        self.assertEqual(completed.status.value, "succeeded", completed.last_error)

    def test_private_registry_scan_runs_as_a_durable_job(self) -> None:
        server.APP.service.registry.refresh_catalog = (
            lambda *, force, progress: progress("verified module scan") or []
        )
        with patch.object(
            server.APP,
            "catalog_status",
            return_value={
                "status": "verified",
                "verified_modules": 0,
                "failures": 0,
            },
        ):
            status, job = self.request(
                "POST", "/api/catalog/scan", {"force": True}
            )
            self.assertEqual(status, 202)
            self.assertEqual(job["kind"], "catalog")
            self.assertTrue(server.APP.worker.execute_once())
            completed = server.APP.jobs.get(job["id"])
        self.assertEqual(completed.status.value, "succeeded")
        self.assertIn("verified module scan", completed.result["logs"])


if __name__ == "__main__":
    unittest.main()
