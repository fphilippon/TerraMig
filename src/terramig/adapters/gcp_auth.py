from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")


@contextmanager
def gcloud_credential_environment(credential: str):
    """Expose one JSON credential to gcloud through a short-lived private file."""
    if not credential:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="terramig-gcp-") as directory:
        path = Path(directory) / "application-default-credentials.json"
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w") as stream:
            stream.write(credential)
        environment = os.environ.copy()
        environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
        environment["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] = str(path)
        yield environment


class GCPConnectionVerifier:
    """Verify the active gcloud identity and read-only project/asset access."""

    def __init__(self, command_timeout: int = 60, credential: str = "") -> None:
        self.command_timeout = command_timeout
        self.credential = credential

    def test_connection(self, project_id: str) -> dict[str, str]:
        project_id = project_id.strip()
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("GCP project ID must be a valid 6-30 character project ID")

        if self.credential:
            credential = json.loads(self.credential)
            account = str(
                credential.get("client_email")
                or credential.get("quota_project_id")
                or credential.get("type", "external-account")
            )
        else:
            accounts = self._run(
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=json",
            )
            if not isinstance(accounts, list) or not accounts:
                raise RuntimeError(
                    "gcloud has no active account; save GCP credentials or authenticate the TerraMig runtime"
                )
            account = str(accounts[0].get("account", "")).strip()
            if not account:
                raise RuntimeError("gcloud active account response did not include an account")

        project = self._run(
            "projects",
            "describe",
            project_id,
            "--format=json",
        )
        if not isinstance(project, dict) or project.get("projectId") != project_id:
            raise RuntimeError("gcloud project response did not match the requested project")

        self._run(
            "asset",
            "list",
            f"--project={project_id}",
            "--content-type=resource",
            "--limit=1",
            "--format=json",
        )
        return {
            "status": "verified",
            "message": "GCP identity, project, and Cloud Asset Inventory access verified",
            "project_id": project_id,
            "project_number": str(project.get("projectNumber", "")),
            "project_name": str(project.get("name", project_id)),
            "account": account,
        }

    def _run(self, *arguments: str):
        try:
            with gcloud_credential_environment(self.credential) as environment:
                process = subprocess.run(
                    ["gcloud", *arguments],
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout,
                    check=False,
                    env=environment,
                )
        except FileNotFoundError as error:
            raise RuntimeError("gcloud is not installed in the TerraMig runtime") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("gcloud connection test timed out") from error
        if process.returncode:
            detail = (process.stderr or process.stdout).strip()
            if self.credential:
                detail = detail.replace(self.credential, "[REDACTED]")
            raise RuntimeError(detail[-3000:] or "gcloud command failed")
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("gcloud returned an invalid JSON response") from error
