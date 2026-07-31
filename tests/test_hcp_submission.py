import io
import tarfile
import unittest
from unittest.mock import patch

from terramig.adapters.real import HCPRunSubmitter, HCPWorkspaceProtector
from terramig.domain import GenerationBundle, HCPImportLifecycle, ImportOperation, Stage
from terramig.services.artifacts import finalize_terraform_artifacts


class HCPRunSubmitterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = GenerationBundle(
            'terraform {}\nresource "google_compute_network" "vpc" { name = "vpc" }\n',
            [
                ImportOperation(
                    "google_compute_network.vpc",
                    "projects/p/global/networks/vpc",
                    "terraform import google_compute_network.vpc projects/p/global/networks/vpc",
                )
            ],
            [],
            [],
        )
        finalize_terraform_artifacts(
            self.bundle,
            {
                "hostname": "app.terraform.io",
                "organization": "acme",
                "workspace": "adoption",
            },
            "7.39.0",
        )
        self.submitter = HCPRunSubmitter(
            "app.terraform.io", "acme", "adoption", "token"
        )

    def test_workspace_protector_accepts_vcs_driven_workspace(self) -> None:
        workspace = {
            "data": {
                "id": "ws-1",
                "attributes": {
                    "locked": False,
                    "resource-count": 0,
                    "execution-mode": "remote",
                    "auto-apply": False,
                    "vcs-repo": {
                        "identifier": "acme/application-infrastructure"
                    },
                },
            }
        }
        protector = HCPWorkspaceProtector(
            lambda path: (
                workspace
                if path.startswith("/api/v2/organizations/")
                else {"data": []}
            )
        )

        evidence = protector.assert_safe("acme", "adoption")

        self.assertTrue(evidence["vcs_connected"])
        self.assertIn(
            "workspace is VCS-driven; TerraMig uses an isolated provisional "
            "configuration and leaves VCS settings unchanged",
            evidence["checks"],
        )

    def test_vcs_workspace_does_not_bypass_auto_apply_protection(self) -> None:
        workspace = {
            "data": {
                "id": "ws-1",
                "attributes": {
                    "locked": False,
                    "resource-count": 0,
                    "execution-mode": "remote",
                    "auto-apply": True,
                    "vcs-repo": {
                        "identifier": "acme/application-infrastructure"
                    },
                },
            }
        }
        protector = HCPWorkspaceProtector(lambda _path: workspace)

        with self.assertRaisesRegex(ValueError, "auto-apply disabled"):
            protector.assert_safe("acme", "adoption")

    def test_archive_contains_configuration_and_declarative_imports(self) -> None:
        content = self.submitter.bundle_archive(self.bundle)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            self.assertEqual(
                sorted(archive.getnames()),
                ["backend.tf", "imports.tf", "main.tf", "providers.tf"],
            )
            imports = archive.extractfile("imports.tf").read().decode()
            backend = archive.extractfile("backend.tf").read().decode()
            providers = archive.extractfile("providers.tf").read().decode()
        self.assertIn("to = google_compute_network.vpc", imports)
        self.assertIn('id = "projects/p/global/networks/vpc"', imports)
        self.assertIn('organization = "acme"', backend)
        self.assertIn('name = "adoption"', backend)
        self.assertIn('source  = "hashicorp/google"', providers)

    def test_archive_preserves_existing_configuration_and_adds_overlay(self) -> None:
        preserved = {
            "backend.tf": b'terraform { cloud {} }\n',
            "providers.tf": b'provider "google" {}\n',
            "main.tf": b'resource "google_storage_bucket" "existing" {}\n',
            "imports.tf": b'import { to = google_storage_bucket.existing }\n',
        }

        content = self.submitter.bundle_archive(
            self.bundle,
            preserved_files=preserved,
            workflow_id="workflow-1",
        )

        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            names = sorted(archive.getnames())
            existing = archive.extractfile("main.tf").read()
            overlay = archive.extractfile(
                "terramig_workflow_1_main.tf"
            ).read().decode()
            imports = archive.extractfile(
                "terramig_workflow_1_imports.tf"
            ).read().decode()
        self.assertEqual(
            names,
            [
                "backend.tf",
                "imports.tf",
                "main.tf",
                "providers.tf",
                "terramig_workflow_1_imports.tf",
                "terramig_workflow_1_main.tf",
            ],
        )
        self.assertEqual(existing, preserved["main.tf"])
        self.assertIn('resource "google_compute_network" "vpc"', overlay)
        self.assertIn("to = google_compute_network.vpc", imports)

    def test_configuration_archive_rejects_parent_path(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            value = b"secret"
            info = tarfile.TarInfo("../secret.tf")
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))

        with self.assertRaisesRegex(RuntimeError, "unsafe path"):
            self.submitter._extract_configuration_archive(buffer.getvalue())

    def test_configuration_archive_rejects_symbolic_links(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            info = tarfile.TarInfo("main.tf")
            info.type = tarfile.SYMTYPE
            info.linkname = "../secret.tf"
            archive.addfile(info)

        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            self.submitter._extract_configuration_archive(buffer.getvalue())

    def test_current_configuration_resolves_state_run_and_source_archive(
        self,
    ) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            value = b'resource "google_storage_bucket" "existing" {}\n'
            info = tarfile.TarInfo("main.tf")
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
        state_version = {
            "data": {
                "relationships": {
                    "run": {"data": {"type": "runs", "id": "run-current"}}
                }
            }
        }
        run = {
            "data": {
                "relationships": {
                    "configuration-version": {
                        "data": {
                            "type": "configuration-versions",
                            "id": "cv-current",
                        }
                    }
                }
            }
        }
        with patch.object(
            self.submitter,
            "_get_json",
            side_effect=[state_version, run],
        ) as get, patch.object(
            self.submitter,
            "_download_configuration_archive",
            return_value=buffer.getvalue(),
        ) as download:
            files, evidence = self.submitter._current_configuration("ws-1")

        self.assertEqual(files["main.tf"], value)
        self.assertEqual(
            evidence["preserved_configuration_version_id"], "cv-current"
        )
        self.assertEqual(
            evidence["preserved_configuration_file_count"], 1
        )
        self.assertEqual(
            get.call_args_list[0].args[0],
            "/api/v2/workspaces/ws-1/current-state-version",
        )
        download.assert_called_once_with("cv-current")

    def test_submission_queues_saved_plan_without_auto_apply(self) -> None:
        workspace = {
            "data": {
                "id": "ws-1",
                "attributes": {
                    "locked": False,
                    "resource-count": 0,
                    "execution-mode": "remote",
                    "auto-apply": False,
                    "vcs-repo": {
                        "identifier": "acme/application-infrastructure"
                    },
                },
            }
        }
        no_active_runs = {"data": []}
        configuration = {
            "data": {
                "id": "cv-1",
                "attributes": {"upload-url": "https://archivist.example/upload"},
            }
        }
        run = {"data": {"id": "run-1"}}
        with patch.object(
            self.submitter,
            "_get_json",
            side_effect=[
                workspace,
                no_active_runs,
                workspace,
                no_active_runs,
                {"data": []},
            ],
        ), patch.object(
            self.submitter, "_post_json", side_effect=[configuration, run]
        ) as post, patch.object(
            self.submitter, "_put_bytes"
        ) as upload, patch.object(
            self.submitter, "_wait_uploaded"
        ) as wait:
            run_id = self.submitter.import_bundle("p", self.bundle)

        self.assertEqual(run_id, "run-1")
        self.assertEqual(self.submitter.completion_stage, Stage.SUBMITTED)
        configuration_payload = post.call_args_list[0].args[1]
        self.assertFalse(configuration_payload["data"]["attributes"]["auto-queue-runs"])
        self.assertTrue(configuration_payload["data"]["attributes"]["provisional"])
        run_payload = post.call_args_list[1].args[1]
        self.assertTrue(run_payload["data"]["attributes"]["save-plan"])
        upload.assert_called_once()
        wait.assert_called_once_with("cv-1")
        self.assertEqual(self.submitter.last_protection["status"], "protected")
        self.assertTrue(self.submitter.last_protection["vcs_connected"])

    def test_submission_rejects_nonempty_target_before_upload(self) -> None:
        workspace = {
            "data": {
                "id": "ws-1",
                "attributes": {
                    "locked": False,
                    "resource-count": 2,
                    "execution-mode": "remote",
                },
            }
        }
        with patch.object(self.submitter, "_get_json", return_value=workspace), patch.object(
            self.submitter, "_put_bytes"
        ) as upload:
            with self.assertRaisesRegex(ValueError, "already manages 2 resources"):
                self.submitter.import_bundle("p", self.bundle)
        upload.assert_not_called()

    def test_submission_allows_explicit_nonempty_target_exception(self) -> None:
        workspace = {
            "data": {
                "id": "ws-1",
                "attributes": {
                    "locked": False,
                    "resource-count": 2,
                    "execution-mode": "remote",
                    "auto-apply": False,
                    "vcs-repo": None,
                },
            }
        }
        configuration = {
            "data": {
                "id": "cv-1",
                "attributes": {"upload-url": "https://archivist.example/upload"},
            }
        }
        with patch.object(
            self.submitter,
            "_get_json",
            side_effect=[
                workspace,
                {"data": []},
                workspace,
                {"data": []},
                {"data": []},
            ],
        ), patch.object(
            self.submitter,
            "_post_json",
            side_effect=[configuration, {"data": {"id": "run-1"}}],
        ), patch.object(
            self.submitter,
            "_current_configuration",
            return_value=(
                {"main.tf": b'resource "google_storage_bucket" "existing" {}\n'},
                {
                    "preserved_configuration_version_id": "cv-existing",
                    "preserved_configuration_file_count": 1,
                },
            ),
        ) as preserve, patch.object(
            self.submitter,
            "_current_configuration_version_id",
            return_value="cv-existing",
        ), patch.object(
            self.submitter, "_put_bytes"
        ) as upload, patch.object(
            self.submitter, "_wait_uploaded"
        ):
            run_id = self.submitter.import_bundle(
                "p",
                self.bundle,
                allow_nonempty_workspace=True,
            )

        self.assertEqual(run_id, "run-1")
        self.assertEqual(self.submitter.last_protection["resource_count"], 2)
        self.assertTrue(
            self.submitter.last_protection["nonempty_workspace_override"]
        )
        self.assertIn(
            "operator authorized the non-empty workspace exception",
            self.submitter.last_protection["checks"][2],
        )
        self.assertEqual(
            self.submitter.last_protection[
                "preserved_configuration_version_id"
            ],
            "cv-existing",
        )
        preserve.assert_called_once_with("ws-1")
        uploaded_archive = upload.call_args.args[1]
        with tarfile.open(
            fileobj=io.BytesIO(uploaded_archive), mode="r:gz"
        ) as archive:
            self.assertIn("main.tf", archive.getnames())
            self.assertIn(
                "terramig_p_main.tf",
                archive.getnames(),
            )

    def test_nonempty_exception_does_not_bypass_other_protections(self) -> None:
        workspace = {
            "data": {
                "id": "ws-1",
                "attributes": {
                    "locked": True,
                    "resource-count": 2,
                    "execution-mode": "remote",
                },
            }
        }
        with patch.object(self.submitter, "_get_json", return_value=workspace):
            with self.assertRaisesRegex(ValueError, "workspace is locked"):
                self.submitter.import_bundle(
                    "p",
                    self.bundle,
                    allow_nonempty_workspace=True,
                )

    def test_reconciliation_waits_for_apply_then_proves_zero_drift(self) -> None:
        lifecycle = HCPImportLifecycle(
            status="awaiting-hcp-apply",
            workspace_id="ws-1",
            configuration_version_id="cv-1",
            run_id="run-1",
        )
        with patch.object(
            self.submitter,
            "_get_json",
            return_value={"data": {"attributes": {"status": "planned_and_saved"}}},
        ):
            lifecycle, complete = self.submitter.reconcile(
                lifecycle, 1, workflow_id="workflow-1"
            )
        self.assertFalse(complete)
        self.assertEqual(lifecycle.status, "awaiting-hcp-apply")

        with patch.object(
            self.submitter,
            "_get_json",
            side_effect=[
                {"data": {"attributes": {"status": "applied"}}},
                {"data": {"attributes": {"resource-count": 1}}},
                {"data": []},
            ],
        ), patch.object(
            self.submitter,
            "_post_json",
            return_value={"data": {"id": "run-post"}},
        ):
            lifecycle, complete = self.submitter.reconcile(
                lifecycle, 1, workflow_id="workflow-1"
            )
        self.assertFalse(complete)
        self.assertEqual(lifecycle.post_import_run_id, "run-post")

        with patch.object(
            self.submitter,
            "_get_json",
            side_effect=[
                {"data": {"attributes": {"status": "applied"}}},
                {"data": {"attributes": {"resource-count": 1}}},
                {
                    "data": {
                        "attributes": {"status": "planned_and_finished"},
                        "relationships": {
                            "plan": {"data": {"type": "plans", "id": "plan-post"}}
                        },
                    }
                },
                {
                    "data": {
                        "attributes": {
                            "resource-additions": 0,
                            "resource-changes": 0,
                            "resource-destructions": 0,
                        }
                    }
                },
            ],
        ):
            lifecycle, complete = self.submitter.reconcile(
                lifecycle, 1, workflow_id="workflow-1"
            )
        self.assertTrue(complete)
        self.assertTrue(lifecycle.drift_free)
        self.assertEqual(lifecycle.status, "completed")

    def test_reconciliation_discards_remote_plan_with_destruction(self) -> None:
        lifecycle = HCPImportLifecycle(
            status="awaiting-hcp-apply",
            workspace_id="ws-1",
            configuration_version_id="cv-1",
            run_id="run-1",
        )
        run = {
            "data": {
                "attributes": {"status": "planned_and_saved"},
                "relationships": {
                    "plan": {"data": {"type": "plans", "id": "plan-1"}}
                },
            }
        }
        plan = {
            "data": {
                "attributes": {
                    "resource-additions": 0,
                    "resource-changes": 1,
                    "resource-destructions": 1,
                }
            }
        }
        output = {
            "resource_changes": [
                {
                    "address": "module.existing.google_storage_bucket.bucket",
                    "change": {"actions": ["delete"]},
                }
            ]
        }
        with patch.object(
            self.submitter,
            "_get_json",
            side_effect=[run, plan, output],
        ), patch.object(
            self.submitter, "_post_empty"
        ) as discard:
            with self.assertRaisesRegex(
                ValueError, "HCP destructive plan blocked"
            ):
                self.submitter.reconcile(
                    lifecycle, 1, workflow_id="workflow-1"
                )

        discard.assert_called_once_with(
            "/api/v2/runs/run-1/actions/discard"
        )
        self.assertEqual(lifecycle.status, "destructive-plan-discarded")
        self.assertEqual(lifecycle.remote_status, "discarded")
        self.assertIn(
            "module.existing.google_storage_bucket.bucket",
            lifecycle.diagnostics[-1],
        )

    def test_submission_recovers_existing_run_after_worker_crash(self) -> None:
        lifecycle = HCPImportLifecycle(
            status="configuration-uploaded",
            workspace_id="ws-1",
            configuration_version_id="cv-1",
        )
        with patch.object(
            self.submitter,
            "_get_json",
            return_value={
                "data": [
                    {
                        "id": "run-existing",
                        "attributes": {
                            "message": "TerraMig adoption workflow-1"
                        },
                    }
                ]
            },
        ), patch.object(self.submitter, "_post_json") as post:
            run_id = self.submitter.import_bundle(
                "project",
                self.bundle,
                workflow_id="workflow-1",
                lifecycle=lifecycle,
            )
        self.assertEqual(run_id, "run-existing")
        self.assertEqual(lifecycle.status, "remote-plan-check")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
