import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from terramig.adapters.git_delivery import GitDeliveryEngine
from terramig.domain import GenerationBundle, GitDelivery, ImportOperation
from terramig.services.artifacts import finalize_terraform_artifacts


def git(*arguments: str, cwd: Path | None = None) -> str:
    process = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise RuntimeError(process.stderr)
    return process.stdout.strip()


def create_repository(path: Path, files: dict[str, str]) -> None:
    path.mkdir()
    git("init", "--initial-branch=main", cwd=path)
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git("add", "--all", cwd=path)
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "initial",
        cwd=path,
    )


class GitDeliveryTests(unittest.TestCase):
    def test_model_is_merged_without_overwriting_target_and_bundle_is_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            target_source = root / "target-source"
            target_bare = root / "target.git"
            checkout = root / "checkout"
            create_repository(
                model,
                {
                    "README.md": "model readme\n",
                    ".github/workflows/terraform.yml": "name: terraform\n",
                    "catalog/model.txt": "model-owned\n",
                },
            )
            create_repository(
                target_source,
                {
                    "README.md": "application-owned readme\n",
                    "app.txt": "application\n",
                },
            )
            git("clone", "--bare", str(target_source), str(target_bare))

            bundle = GenerationBundle(
                'terraform {}\nmodule "bucket" { source = "app.terraform.io/acme/bucket/google" }\n',
                [
                    ImportOperation(
                        "module.bucket.google_storage_bucket.this",
                        "existing-bucket",
                        "terraform import module.bucket.google_storage_bucket.this existing-bucket",
                    )
                ],
                ["selected private module"],
                [],
                agent="test-agent",
            )
            finalize_terraform_artifacts(
                bundle,
                {
                    "hostname": "app.terraform.io",
                    "organization": "acme",
                    "workspace": "adoption",
                },
                "7.39.0",
            )
            delivery = GitDelivery(
                model_repository=model.as_uri(),
                target_repository=target_bare.as_uri(),
                target_path="environments/prod",
                branch="terramig/adoption-1",
                create_pull_request=False,
            )
            with patch.dict(os.environ, {"TERRAMIG_ALLOW_LOCAL_GIT": "true"}):
                result = GitDeliveryEngine().deliver(
                    "workflow-12345678", bundle, delivery
                )

            self.assertEqual(result.status, "branch-pushed")
            self.assertEqual(len(result.commit_sha), 40)
            self.assertIn("README.md", result.skipped_template_files)
            self.assertIn(
                "environments/prod/main.tf", result.changed_files
            )
            self.assertIn("environments/prod/backend.tf", result.changed_files)
            self.assertIn("environments/prod/providers.tf", result.changed_files)
            git(
                "clone",
                "--branch",
                "terramig/adoption-1",
                str(target_bare),
                str(checkout),
            )
            self.assertEqual(
                (checkout / "README.md").read_text(),
                "application-owned readme\n",
            )
            self.assertTrue(
                (checkout / ".github/workflows/terraform.yml").is_file()
            )
            self.assertIn(
                "app.terraform.io/acme/bucket/google",
                (checkout / "environments/prod/main.tf").read_text(),
            )
            self.assertIn(
                "to = module.bucket.google_storage_bucket.this",
                (checkout / "environments/prod/imports.tf").read_text(),
            )
            self.assertIn(
                'organization = "acme"',
                (checkout / "environments/prod/backend.tf").read_text(),
            )
            self.assertIn(
                'source  = "hashicorp/google"',
                (checkout / "environments/prod/providers.tf").read_text(),
            )

    def test_selected_branch_and_replacement_policy_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            target_source = root / "target-source"
            target_bare = root / "target.git"
            checkout = root / "checkout"
            create_repository(model, {"README.md": "model\n"})
            create_repository(
                target_source,
                {
                    "terraform/main.tf": "# old generated configuration\n",
                    "terraform/imports.tf": "# old imports\n",
                },
            )
            git("clone", "--bare", str(target_source), str(target_bare))
            bundle = GenerationBundle(
                'resource "google_storage_bucket" "selected" { name = "existing" }\n',
                [
                    ImportOperation(
                        "google_storage_bucket.selected",
                        "existing",
                        "terraform import google_storage_bucket.selected existing",
                    )
                ],
                [],
                [],
                agent="test-agent",
            )
            finalize_terraform_artifacts(
                bundle,
                {
                    "hostname": "app.terraform.io",
                    "organization": "acme",
                    "workspace": "adoption",
                },
                "7.39.0",
            )
            logs: list[str] = []
            delivery = GitDelivery(
                model_repository=model.as_uri(),
                target_repository=target_bare.as_uri(),
                branch="terramig/replacement-v2",
                target_path="terraform",
                create_pull_request=False,
                allow_overwrite=True,
            )

            with patch.dict(os.environ, {"TERRAMIG_ALLOW_LOCAL_GIT": "true"}):
                result = GitDeliveryEngine().deliver(
                    "workflow-replacement", bundle, delivery, progress=logs.append
                )

            self.assertEqual(result.branch, "terramig/replacement-v2")
            self.assertIn(
                "Git delivery request accepted: branch=terramig/replacement-v2, base=main, "
                "destination=terraform, layout=consolidated, replacement=enabled",
                logs,
            )
            git(
                "clone",
                "--branch",
                "terramig/replacement-v2",
                str(target_bare),
                str(checkout),
            )
            self.assertIn(
                'resource "google_storage_bucket" "selected"',
                (checkout / "terraform/main.tf").read_text(),
            )
            self.assertIn(
                "to = google_storage_bucket.selected",
                (checkout / "terraform/imports.tf").read_text(),
            )

    def test_existing_directory_is_updated_with_per_resource_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model"
            target_source = root / "target-source"
            target_bare = root / "target.git"
            checkout = root / "checkout"
            create_repository(model, {"README.md": "model\n"})
            create_repository(
                target_source,
                {
                    "terraform/main.tf": '# Existing application configuration\nlocals { owner = "team" }\n',
                    "terraform/providers.tf": '# Customer-owned provider wiring\nprovider "google" {}\n',
                },
            )
            git("clone", "--bare", str(target_source), str(target_bare))
            bundle = GenerationBundle(
                'resource "google_storage_bucket" "archive" { name = "archive" }\n',
                [
                    ImportOperation(
                        "google_storage_bucket.archive",
                        "archive",
                        "terraform import google_storage_bucket.archive archive",
                    )
                ],
                [],
                [],
                agent="test-agent",
            )
            finalize_terraform_artifacts(
                bundle,
                {
                    "hostname": "app.terraform.io",
                    "organization": "acme",
                    "workspace": "adoption",
                },
                "7.39.0",
            )
            delivery = GitDelivery(
                model_repository=model.as_uri(),
                target_repository=target_bare.as_uri(),
                branch="terramig/additive",
                target_path="terraform",
                create_pull_request=False,
                update_existing_directory=True,
            )

            with patch.dict(os.environ, {"TERRAMIG_ALLOW_LOCAL_GIT": "true"}):
                result = GitDeliveryEngine().deliver(
                    "workflow-additive", bundle, delivery
                )

            git(
                "clone",
                "--branch",
                "terramig/additive",
                str(target_bare),
                str(checkout),
            )
            destination = checkout / "terraform"
            self.assertIn(
                "Existing application configuration",
                (destination / "main.tf").read_text(),
            )
            self.assertIn(
                "Customer-owned provider wiring",
                (destination / "providers.tf").read_text(),
            )
            resource_file = destination / "google_storage_bucket_archive.tf"
            self.assertIn(
                'resource "google_storage_bucket" "archive"',
                resource_file.read_text(),
            )
            self.assertIn(
                "to = google_storage_bucket.archive",
                resource_file.read_text(),
            )
            self.assertFalse((destination / "imports.tf").exists())
            metadata = json.loads((destination / "terramig.json").read_text())
            self.assertEqual(metadata["layout"], "per-resource")
            self.assertIn(
                "google_storage_bucket_archive.tf",
                metadata["managed_files"],
            )
            self.assertNotIn("providers.tf", metadata["managed_files"])
            self.assertIn(
                "terraform/providers.tf", metadata["preserved_files"]
            )
            self.assertIn(
                "terraform/providers.tf", result.skipped_template_files
            )

    def test_existing_directory_rejects_duplicate_resource_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "terraform"
            destination.mkdir()
            (destination / "main.tf").write_text(
                'resource "google_storage_bucket" "archive" { name = "archive" }\n'
            )
            bundle = GenerationBundle(
                'resource "google_storage_bucket" "archive" { name = "archive" }\n',
                [
                    ImportOperation(
                        "google_storage_bucket.archive",
                        "archive",
                        "terraform import google_storage_bucket.archive archive",
                    )
                ],
                [],
                [],
            )
            delivery = GitDelivery(
                "https://github.com/acme/model.git",
                "https://github.com/acme/target.git",
                target_path="terraform",
                create_pull_request=False,
                update_existing_directory=True,
            )

            with self.assertRaisesRegex(
                ValueError,
                "already declares selected addresses.*google_storage_bucket.archive",
            ):
                GitDeliveryEngine._write_bundle(
                    root, "workflow", bundle, delivery
                )

    def test_existing_consolidated_delivery_migrates_to_resource_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "terraform"
            destination.mkdir()
            (destination / "main.tf").write_text(
                'resource "google_storage_bucket" "archive" { name = "archive" }\n'
            )
            (destination / "imports.tf").write_text("# old imports\n")
            (destination / "terramig.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "workflow_id": "workflow",
                    }
                )
            )
            bundle = GenerationBundle(
                'resource "google_storage_bucket" "archive" { name = "archive" }\n',
                [
                    ImportOperation(
                        "google_storage_bucket.archive",
                        "archive",
                        "terraform import google_storage_bucket.archive archive",
                    )
                ],
                [],
                [],
            )
            delivery = GitDelivery(
                "https://github.com/acme/model.git",
                "https://github.com/acme/target.git",
                target_path="terraform",
                create_pull_request=False,
                update_existing_directory=True,
            )

            changed, _preserved = GitDeliveryEngine._write_bundle(
                root, "workflow", bundle, delivery
            )

            self.assertFalse((destination / "main.tf").exists())
            self.assertFalse((destination / "imports.tf").exists())
            self.assertTrue(
                (destination / "google_storage_bucket_archive.tf").is_file()
            )
            self.assertIn("terraform/main.tf", changed)
            self.assertIn("terraform/imports.tf", changed)

    def test_local_paths_and_unsafe_target_paths_are_rejected(self) -> None:
        engine = GitDeliveryEngine()
        bundle = GenerationBundle("terraform {}", [], [], [])
        with self.assertRaisesRegex(ValueError, "repository URL"):
            engine.deliver(
                "workflow",
                bundle,
                GitDelivery("/tmp/model", "https://github.com/acme/target.git"),
            )
        with self.assertRaisesRegex(ValueError, "target_path"):
            engine.deliver(
                "workflow",
                bundle,
                GitDelivery(
                    "https://github.com/acme/model.git",
                    "https://github.com/acme/target.git",
                    target_path="../escape",
                    create_pull_request=False,
                ),
            )
        with self.assertRaisesRegex(ValueError, "embed credentials"):
            engine.deliver(
                "workflow",
                bundle,
                GitDelivery(
                    "https://token@github.com/acme/model.git",
                    "https://github.com/acme/target.git",
                    create_pull_request=False,
                ),
            )

    def test_private_key_uses_strict_isolated_ssh_environment(self) -> None:
        private_key = self._private_key()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = GitDeliveryEngine(
                ssh_private_key=private_key
            )._git_environment(root)

            command = shlex.split(environment["GIT_SSH_COMMAND"])
            key_path = Path(command[command.index("-i") + 1])
            known_hosts_option = next(
                item for item in command if item.startswith("UserKnownHostsFile=")
            )
            known_hosts = Path(known_hosts_option.split("=", 1)[1])
            self.assertEqual(key_path.read_text().strip(), private_key.strip())
            self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(known_hosts.stat().st_mode & 0o777, 0o600)
            self.assertIn("github.com ssh-ed25519", known_hosts.read_text())
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("BatchMode=yes", command)
            self.assertNotIn("SSH_AUTH_SOCK", environment)

    def test_connection_probe_is_read_only_and_returns_fingerprint(self) -> None:
        engine = GitDeliveryEngine(ssh_private_key=self._private_key())
        with patch.object(engine, "_run", return_value="") as run:
            result = engine.test_ssh_connection(
                "git@github.com:acme/private.git"
            )
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["git", "ls-remote", "--heads"])
        self.assertEqual(command[-1], "git@github.com:acme/private.git")
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["fingerprint"].startswith("SHA256:"))

    def test_connection_probe_converts_https_to_ssh_for_saved_key(self) -> None:
        engine = GitDeliveryEngine(ssh_private_key=self._private_key())
        with patch.object(engine, "_run", return_value="") as run:
            engine.test_ssh_connection("https://github.com/acme/private.git")
        self.assertEqual(
            run.call_args.args[0][-1], "git@github.com:acme/private.git"
        )

    def test_https_repository_uses_saved_ssh_transport_without_token(self) -> None:
        engine = GitDeliveryEngine(ssh_private_key=self._private_key())
        self.assertEqual(
            engine._repository_for_git("https://github.com/acme/private.git"),
            "git@github.com:acme/private.git",
        )

    def test_github_token_keeps_https_transport(self) -> None:
        repository = "https://github.com/acme/private.git"
        engine = GitDeliveryEngine(
            github_token="token", ssh_private_key=self._private_key()
        )
        self.assertEqual(engine._repository_for_git(repository), repository)

    def test_github_token_converts_ssh_url_to_authenticated_https(self) -> None:
        engine = GitDeliveryEngine(
            github_token="token", ssh_private_key=self._private_key()
        )
        self.assertEqual(
            engine._repository_for_git(
                "git@github.com:example-org/example-project.git"
            ),
            "https://github.com/example-org/example-project.git",
        )

    def test_ssh_url_stays_on_ssh_without_github_token(self) -> None:
        repository = "git@github.com:acme/private.git"
        engine = GitDeliveryEngine(ssh_private_key=self._private_key())
        self.assertEqual(engine._repository_for_git(repository), repository)

    def test_github_api_probe_returns_authenticated_account(self) -> None:
        engine = GitDeliveryEngine(github_token="github_pat_test-token")
        with patch.object(engine, "_github_request", return_value={"login": "octocat"}) as request:
            result = engine.test_github_connection()

        request.assert_called_once_with("/user", method="GET")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["account"], "octocat")

    def test_pull_request_without_api_token_fails_before_git_mutation(self) -> None:
        engine = GitDeliveryEngine(ssh_private_key=self._private_key())
        delivery = GitDelivery(
            "https://github.com/acme/model.git",
            "https://github.com/acme/target.git",
            create_pull_request=True,
        )
        with patch.object(engine, "_run") as run, self.assertRaisesRegex(
            ValueError, "requires a verified GitHub API token"
        ):
            engine.deliver("workflow", GenerationBundle("terraform {}", [], [], []), delivery)
        run.assert_not_called()

    @staticmethod
    def _private_key() -> str:
        return ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ).decode()


if __name__ == "__main__":
    unittest.main()
