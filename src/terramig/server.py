from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from .adapters.cli_ai import CliGenerationAgent
from .adapters.hcp_auth import HCPTokenVerifier
from .adapters.gcp_auth import GCPConnectionVerifier, PROJECT_ID
from .adapters.mcp import TerraformMCPContextProvider
from .adapters.real import GCloudInventory, HCPRegistry, HCPRunSubmitter, ReadOnlyStateImporter, TerraformPublicRegistry
from .adapters.git_delivery import (
    GitDeliveryEngine,
    validate_delivery_payload,
)
from .adapters.terraform import LocalTerraformVerifier
from .settings import Settings, SettingsStore
from .persistence import Persistence, build_persistence
from .services.auth import AuthenticatedSession, AuthenticationError, AuthService
from .services.workflow import InvalidTransition, WorkflowService
from .services.assurance import canonical_sha256, sanitize_untrusted
from .services.capabilities import CapabilityCatalog, discovered_service_coverage
from .services.catalog import validate_catalog
from .services.observability import Observability
from .services.jobs import DurableWorker, JobOutcome, JobQueue, NonRetryableJobError
from .services.secrets import SecretVault


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "web"
DEFAULT_MODULE_CATALOG = ROOT / ".terramig" / "private-modules.json"
OBSERVABILITY = Observability()


def module_catalog_path() -> Path:
    configured = os.getenv("TERRAMIG_MODULE_MANIFEST", "").strip()
    return Path(configured) if configured else DEFAULT_MODULE_CATALOG


def build_service(
    settings: Settings,
    persistence: Persistence | None = None,
    *,
    ai_credential: str = "",
    hcp_token: str = "",
    github_token: str = "",
    git_ssh_private_key: str = "",
    gcp_credential: str = "",
    catalog_path: Path | None = None,
) -> WorkflowService:
    inventory = GCloudInventory(
        settings.gcp_project_id,
        gcp_credential,
        timeout_seconds=float(
            os.getenv("TERRAMIG_GCP_DISCOVERY_TIMEOUT_SECONDS", "600")
        ),
    )
    registry = HCPRegistry(
        settings.hcp_hostname,
        settings.hcp_organization,
        hcp_token,
        schema_catalog=catalog_path or module_catalog_path(),
        auto_catalog=True,
    )
    public_registry = TerraformPublicRegistry()
    if settings.hcp_submission_enabled:
        importer = HCPRunSubmitter(
            settings.hcp_hostname,
            settings.hcp_organization,
            settings.hcp_workspace,
            hcp_token,
        )
    else:
        importer = ReadOnlyStateImporter(
            settings.hcp_organization, settings.hcp_workspace
        )
    verifier = LocalTerraformVerifier(
        hcp_hostname=settings.hcp_hostname,
        hcp_token=hcp_token,
        gcp_credential=gcp_credential,
    )
    git_delivery_engine = GitDeliveryEngine(
        github_token=github_token,
        ssh_private_key=git_ssh_private_key,
    )
    agent = CliGenerationAgent(
        settings.ai_provider,
        ROOT,
        ROOT / "skills" / "adopt-gcp-infrastructure",
        credential=ai_credential,
        context_provider=TerraformMCPContextProvider(
            settings.hcp_hostname,
            settings.hcp_organization,
            hcp_token,
        ),
    )
    return WorkflowService(
        inventory,
        registry,
        agent,
        importer,
        hcp_target={
            "hostname": settings.hcp_hostname,
            "organization": settings.hcp_organization,
            "workspace": settings.hcp_workspace,
        },
        verifier=verifier,
        persistence=persistence,
        git_delivery_engine=git_delivery_engine,
        require_git_delivery=True,
        public_registry=public_registry,
        runtime_origin="production",
        required_runtime_origin="production",
    )


class Application:
    def __init__(self) -> None:
        self.persistence = build_persistence()
        self.store = SettingsStore(
            ROOT / ".terramig" / "settings.json", self.persistence
        )
        self.secrets = SecretVault(
            self.persistence,
            key_file=ROOT / ".terramig" / "secret.key",
        )
        self.settings = self.store.load()
        self._rebuild_services()
        self.auth = AuthService(self.persistence)
        self.jobs = JobQueue(self.persistence)
        self.worker = DurableWorker(self.persistence, self._job_handlers())
        OBSERVABILITY.attach(self.persistence)

    def _rebuild_services(self) -> None:
        hcp_token = self.secrets.resolve_hcp_credential()
        github_status = self.secrets.github_status()
        github_token = (
            self.secrets.resolve_github_credential()
            if github_status["verified_at"]
            else ""
        )
        self.service = build_service(
            self.settings,
            self.persistence,
            ai_credential=self.secrets.resolve_ai_credential(
                self.settings.ai_provider
            ),
            hcp_token=hcp_token,
            github_token=github_token,
            git_ssh_private_key=self.secrets.resolve_git_ssh_credential(),
            gcp_credential=self.secrets.resolve_gcp_credential(),
            catalog_path=module_catalog_path(),
        )

    def readiness(self) -> dict[str, bool | str]:
        credential = self.secrets.ai_status(self.settings.ai_provider)
        hcp_credential = self.secrets.hcp_status()
        github_credential = self.secrets.github_status()
        git_ssh_credential = self.secrets.git_ssh_status()
        gcp_credential = self.secrets.gcp_status()
        return self.settings.readiness(
            ai_credential_configured=bool(credential["configured"]),
            ai_authenticated=bool(credential["verified_at"]),
            hcp_token_configured=bool(hcp_credential["configured"]),
            hcp_authenticated=bool(hcp_credential["verified_at"]),
            github_token_configured=bool(github_credential["configured"]),
            github_authenticated=bool(github_credential["verified_at"]),
            git_ssh_key_configured=bool(git_ssh_credential["configured"]),
            git_ssh_authenticated=bool(git_ssh_credential["verified_at"]),
            gcp_credential_configured=bool(gcp_credential["configured"]),
            gcp_authenticated=self._gcp_authenticated(),
        )

    def public_settings(self) -> dict:
        credential = self.secrets.ai_status(self.settings.ai_provider)
        hcp_credential = self.secrets.hcp_status()
        github_credential = self.secrets.github_status()
        git_ssh_credential = self.secrets.git_ssh_status()
        gcp_credential = self.secrets.gcp_status()
        value = self.settings.public_dict(
            ai_credential_configured=bool(credential["configured"]),
            ai_authenticated=bool(credential["verified_at"]),
            hcp_token_configured=bool(hcp_credential["configured"]),
            hcp_authenticated=bool(hcp_credential["verified_at"]),
            github_token_configured=bool(github_credential["configured"]),
            github_authenticated=bool(github_credential["verified_at"]),
            git_ssh_key_configured=bool(git_ssh_credential["configured"]),
            git_ssh_authenticated=bool(git_ssh_credential["verified_at"]),
            gcp_credential_configured=bool(gcp_credential["configured"]),
            gcp_authenticated=self._gcp_authenticated(),
        )
        value["ai_credentials"] = self.secrets.ai_statuses()
        value["hcp_credential"] = hcp_credential
        value["github_credential"] = github_credential
        value["git_ssh_credential"] = git_ssh_credential
        value["gcp_credential"] = gcp_credential
        return value

    def _gcp_authenticated(self) -> bool:
        return bool(
            self.settings.gcp_project_id
            and self.settings.gcp_verified_project_id
            == self.settings.gcp_project_id
            and self.settings.gcp_verified_at
        )

    def start_worker(self) -> None:
        for workflow in self.persistence.list_workflows():
            if (
                workflow.stage.value == "submitted"
                and workflow.runtime_origin == "production"
            ):
                self.jobs.enqueue(
                    "adoption",
                    "reconcile",
                    workflow.id,
                    max_attempts=1000,
                )
        self.worker.start()

    def _job_handlers(self):
        return {
            "adoption.discover": lambda job: self._adoption_job(job, "discover"),
            "adoption.match": lambda job: self._adoption_job(job, "match"),
            "adoption.select-modules": lambda job: self._adoption_job(
                job, "select-modules"
            ),
            "adoption.generate": lambda job: self._adoption_job(job, "generate"),
            "adoption.validate": lambda job: self._adoption_job(job, "validate"),
            "adoption.accept-drift": lambda job: self._adoption_job(
                job, "accept-drift"
            ),
            "adoption.deliver": lambda job: self._adoption_job(job, "deliver"),
            "adoption.import": lambda job: self._adoption_job(job, "import"),
            "adoption.reconcile": lambda job: self._adoption_job(job, "reconcile"),
            "catalog.scan": self._catalog_job,
        }

    def _catalog_job(self, job) -> JobOutcome:
        progress = lambda message: self.jobs.log(job.id, message)
        refresh = getattr(self.service.registry, "refresh_catalog", None)
        if not refresh:
            raise NonRetryableJobError(
                "the configured HCP private library does not support catalog scanning"
            )
        started = time.monotonic()
        progress("Private-module registry scan started")
        modules = refresh(force=bool(job.payload.get("force", True)), progress=progress)
        status = self.catalog_status()
        OBSERVABILITY.record_operation(
            "catalog.scan",
            "succeeded",
            round((time.monotonic() - started) * 1000),
            subject_id=job.subject_id,
        )
        progress("Private-module registry scan completed")
        return JobOutcome(
            {
                "modules": len(modules),
                "verified_modules": status.get("verified_modules", 0),
                "trusted_modules": status.get("trusted_modules", 0),
                "incompatible_modules": status.get(
                    "incompatible_modules", 0
                ),
                "failures": status.get("failures", 0),
                "catalog": status,
            }
        )

    def _adoption_job(self, job, action: str) -> JobOutcome:
        started = time.monotonic()
        progress = lambda message: self.jobs.log(job.id, message)
        phase = action.replace("-", " ").capitalize()
        progress(f"{phase} phase started")
        try:
            if action == "discover":
                workflow = self.service.discover(job.subject_id, progress=progress)
            elif action == "match":
                workflow = self.service.match(
                    job.subject_id,
                    job.payload.get("selected_resource_ids"),
                    include_public=bool(job.payload.get("include_public", False)),
                )
            elif action == "select-modules":
                workflow = self.service.select_modules(
                    job.subject_id,
                    str(job.payload.get("policy", "")),
                )
            elif action == "generate":
                workflow = self.service.generate(
                    job.subject_id,
                    job.payload.get("module_selections"),
                    progress=progress,
                )
            elif action == "validate":
                workflow = self.service.verify(job.subject_id, progress=progress)
            elif action == "accept-drift":
                workflow = self.service.accept_drift(
                    job.subject_id, bool(job.payload.get("accept", False))
                )
            elif action == "deliver":
                workflow = self.service.deliver_git(
                    job.subject_id, job.payload, progress=progress
                )
            elif action == "import":
                workflow = self.service.import_state(
                    job.subject_id,
                    allow_nonempty_workspace=(
                        job.payload.get("allow_nonempty_workspace") is True
                    ),
                    progress=progress,
                )
                if workflow.stage.value == "submitted":
                    self.jobs.enqueue(
                        "adoption",
                        "reconcile",
                        workflow.id,
                        max_attempts=1000,
                        delay_seconds=float(
                            os.getenv("TERRAMIG_HCP_POLL_SECONDS", "10")
                        ),
                    )
            elif action == "reconcile":
                workflow = self.service.reconcile_import(
                    job.subject_id, progress=progress
                )
                if workflow.stage.value == "submitted":
                    return JobOutcome(
                        {
                            "subject_id": workflow.id,
                            "stage": workflow.stage.value,
                            "hcp_status": workflow.hcp_import.status,
                        },
                        retry_after_seconds=float(
                            os.getenv("TERRAMIG_HCP_POLL_SECONDS", "10")
                        ),
                    )
            else:
                raise NonRetryableJobError(f"unknown adoption action {action}")
        except TimeoutError as error:
            progress(f"{phase} phase timed out: {error}")
            raise NonRetryableJobError(str(error)) from error
        except (InvalidTransition, ValueError) as error:
            progress(f"{phase} phase blocked: {error}")
            raise NonRetryableJobError(str(error)) from error
        except Exception as error:
            progress(f"{phase} phase failed: {error}")
            raise
        OBSERVABILITY.record_operation(
            f"adoption.{action}",
            "succeeded",
            round((time.monotonic() - started) * 1000),
            subject_id=job.subject_id,
        )
        progress(f"{phase} phase completed")
        return JobOutcome(
            {"subject_id": workflow.id, "stage": workflow.stage.value}
        )

    def update_settings(self, payload: dict) -> dict:
        project_id = payload.get(
            "gcp_project_id", self.settings.gcp_project_id
        ).strip()
        preserve_gcp_verification = project_id == self.settings.gcp_project_id
        hcp_submission_enabled = payload.get(
            "hcp_submission_enabled", self.settings.hcp_submission_enabled
        )
        if not isinstance(hcp_submission_enabled, bool):
            raise ValueError("HCP saved-plan submission setting must be boolean")
        settings = Settings(
            hcp_hostname=payload.get("hcp_hostname", self.settings.hcp_hostname).strip(),
            hcp_organization=payload.get("hcp_organization", self.settings.hcp_organization).strip(),
            hcp_workspace=payload.get("hcp_workspace", self.settings.hcp_workspace).strip(),
            gcp_project_id=project_id,
            ai_provider=payload.get("ai_provider", self.settings.ai_provider).strip(),
            gcp_verified_project_id=(
                self.settings.gcp_verified_project_id
                if preserve_gcp_verification
                else ""
            ),
            gcp_verified_account=(
                self.settings.gcp_verified_account
                if preserve_gcp_verification
                else ""
            ),
            gcp_verified_at=(
                self.settings.gcp_verified_at if preserve_gcp_verification else ""
            ),
            hcp_submission_enabled=hcp_submission_enabled,
        )
        settings.validate()
        self.store.save(settings)
        self.settings = settings
        self._rebuild_services()
        return self.public_settings()

    def test_gcp_connection(self, project_id: str) -> dict:
        project_id = project_id.strip()
        if project_id != self.settings.gcp_project_id:
            raise ValueError(
                "Save the GCP project ID in Configuration before testing it"
            )
        credential = self.secrets.resolve_gcp_credential()
        verifier = GCPConnectionVerifier(credential=credential)
        try:
            result = verifier.test_connection(project_id)
        except RuntimeError as error:
            self.settings.gcp_verified_project_id = ""
            self.settings.gcp_verified_account = ""
            self.settings.gcp_verified_at = ""
            self.store.save(self.settings)
            self.persistence.record_audit_event(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "kind": "security",
                    "name": "gcp.connection.verified",
                    "status": "failed",
                    "project_id": project_id,
                }
            )
            raise ValueError(str(error)) from error
        verified_at = datetime.now(timezone.utc).isoformat()
        self.settings.gcp_verified_project_id = project_id
        self.settings.gcp_verified_account = result["account"]
        self.settings.gcp_verified_at = verified_at
        self.store.save(self.settings)
        if credential:
            self.secrets.mark_gcp_verified()
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": verified_at,
                "kind": "security",
                "name": "gcp.connection.verified",
                "status": "succeeded",
                "project_id": project_id,
            }
        )
        return {**result, "verified_at": verified_at, "settings": self.public_settings()}

    def configure_gcp_connection(self, payload: dict) -> dict:
        project_id = str(payload.get("project_id", "")).strip()
        credential = str(payload.get("credential", ""))
        if not PROJECT_ID.fullmatch(project_id):
            raise ValueError("GCP project ID must be a valid 6-30 character project ID")
        if credential.strip():
            self.save_gcp_credential({"credential": credential})
        elif not self.secrets.gcp_status()["configured"]:
            raise ValueError(
                "Choose an Application Default Credential JSON file or paste its contents before saving"
            )
        self.update_settings({"gcp_project_id": project_id})
        return self.test_gcp_connection(project_id)

    def save_ai_credential(self, payload: dict) -> dict:
        provider = str(payload.get("provider", "")).strip()
        credential = str(payload.get("credential", ""))
        status = self.secrets.save_ai_credential(provider, credential)
        if bool(payload.get("activate", True)) and provider != self.settings.ai_provider:
            self.settings.ai_provider = provider
            self.settings.validate()
            self.store.save(self.settings)
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "ai.credential.saved",
                "status": "succeeded",
                "provider": provider,
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def clear_ai_credential(self, provider: str) -> dict:
        status = self.secrets.clear_ai_credential(provider)
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "ai.credential.cleared",
                "status": "succeeded",
                "provider": provider,
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def test_ai_credential(self, provider: str) -> dict:
        provider = provider.strip()
        credential = self.secrets.resolve_ai_credential(provider)
        if not credential:
            raise ValueError("Save an AI credential before testing the connection")
        agent = CliGenerationAgent(
            provider,
            ROOT,
            ROOT / "skills" / "adopt-gcp-infrastructure",
            credential=credential,
        )
        try:
            result = agent.test_connection()
        except RuntimeError as error:
            self.persistence.record_audit_event(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "kind": "security",
                    "name": "ai.credential.verified",
                    "status": "failed",
                    "provider": provider,
                }
            )
            raise ValueError(
                str(error).replace(credential, "[REDACTED]")
            ) from error
        status = self.secrets.mark_ai_verified(provider)
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "ai.credential.verified",
                "status": "succeeded",
                "provider": provider,
            }
        )
        return {**result, "credential": status}

    def save_hcp_credential(self, payload: dict) -> dict:
        credential = str(payload.get("credential", ""))
        status = self.secrets.save_hcp_credential(credential)
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "hcp.credential.saved",
                "status": "succeeded",
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def clear_hcp_credential(self) -> dict:
        status = self.secrets.clear_hcp_credential()
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "hcp.credential.cleared",
                "status": "succeeded",
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def test_hcp_credential(self) -> dict:
        credential = self.secrets.resolve_hcp_credential()
        if not credential:
            raise ValueError(
                "Save an HCP Terraform token before testing the connection"
            )
        verifier = HCPTokenVerifier(
            self.settings.hcp_hostname,
            credential,
        )
        try:
            result = verifier.test_connection()
        except RuntimeError as error:
            self.persistence.record_audit_event(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "kind": "security",
                    "name": "hcp.credential.verified",
                    "status": "failed",
                }
            )
            raise ValueError(
                str(error).replace(credential, "[REDACTED]")
            ) from error
        status = self.secrets.mark_hcp_verified()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "hcp.credential.verified",
                "status": "succeeded",
            }
        )
        return {**result, "credential": status}

    def save_github_credential(self, payload: dict) -> dict:
        credential = str(payload.get("credential", ""))
        status = self.secrets.save_github_credential(credential)
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "git.github-credential.saved",
                "status": "succeeded",
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def clear_github_credential(self) -> dict:
        status = self.secrets.clear_github_credential()
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "git.github-credential.cleared",
                "status": "succeeded",
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def test_github_credential(self) -> dict:
        credential = self.secrets.resolve_github_credential()
        if not credential:
            raise ValueError(
                "Save a GitHub API token before testing the connection"
            )
        engine = GitDeliveryEngine(github_token=credential)
        try:
            result = engine.test_github_connection()
        except RuntimeError as error:
            self.persistence.record_audit_event(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "kind": "security",
                    "name": "git.github-credential.verified",
                    "status": "failed",
                }
            )
            raise ValueError(str(error).replace(credential, "[REDACTED]")) from error
        status = self.secrets.mark_github_verified()
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "git.github-credential.verified",
                "status": "succeeded",
            }
        )
        return {**result, "credential": status, "settings": self.public_settings()}

    def save_git_ssh_credential(self, payload: dict) -> dict:
        credential = str(payload.get("credential", ""))
        status = self.secrets.save_git_ssh_credential(credential)
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "git.ssh-credential.saved",
                "status": "succeeded",
            }
        )
        repository = str(payload.get("repository", "")).strip()
        if repository:
            connection = self.test_git_ssh_credential(repository)
            return {
                "credential": connection["credential"],
                "connection": connection,
                "settings": self.public_settings(),
            }
        return {"credential": status, "settings": self.public_settings()}

    def save_gcp_credential(self, payload: dict) -> dict:
        status = self.secrets.save_gcp_credential(str(payload.get("credential", "")))
        self.settings.gcp_verified_project_id = ""
        self.settings.gcp_verified_account = ""
        self.settings.gcp_verified_at = ""
        self.store.save(self.settings)
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "gcp.credential.saved",
                "status": "succeeded",
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def clear_gcp_credential(self, *, clear_project: bool = False) -> dict:
        status = self.secrets.clear_gcp_credential()
        if clear_project:
            self.settings.gcp_project_id = ""
        self.settings.gcp_verified_project_id = ""
        self.settings.gcp_verified_account = ""
        self.settings.gcp_verified_at = ""
        self.store.save(self.settings)
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "gcp.credential.cleared",
                "status": "succeeded",
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def clear_git_ssh_credential(self) -> dict:
        status = self.secrets.clear_git_ssh_credential()
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "git.ssh-credential.cleared",
                "status": "succeeded",
            }
        )
        return {"credential": status, "settings": self.public_settings()}

    def test_git_ssh_credential(self, repository: str) -> dict:
        credential = self.secrets.resolve_git_ssh_credential()
        if not credential:
            raise ValueError(
                "Save a Git SSH private key before testing the connection"
            )
        engine = GitDeliveryEngine(ssh_private_key=credential)
        try:
            result = engine.test_ssh_connection(repository.strip())
        except RuntimeError as error:
            self.persistence.record_audit_event(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "kind": "security",
                    "name": "git.ssh-credential.verified",
                    "status": "failed",
                }
            )
            raise ValueError(str(error).replace(credential, "[REDACTED]")) from error
        status = self.secrets.mark_git_ssh_verified()
        self._rebuild_services()
        self.persistence.record_audit_event(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": "security",
                "name": "git.ssh-credential.verified",
                "status": "succeeded",
            }
        )
        return {**result, "credential": status}

    def database_status(self) -> dict[str, object]:
        return {
            "kind": self.persistence.kind,
            "healthy": self.persistence.healthy(),
            "durable": self.persistence.kind == "postgresql",
        }

    def workflows(self) -> list[dict]:
        persisted = self.persistence.list_workflows()
        self.service.workflows.update({item.id: item for item in persisted})
        return [
            {
                "id": workflow.id,
                "project_id": workflow.project_id,
                "stage": workflow.stage.value,
                "runtime_origin": workflow.runtime_origin,
                "created_at": workflow.created_at,
                "updated_at": workflow.updated_at,
            }
            for workflow in sorted(
                self.service.workflows.values(),
                key=lambda item: item.updated_at,
                reverse=True,
            )
        ]

    def operations(self) -> dict:
        self.workflows()
        snapshot = OBSERVABILITY.snapshot(list(self.service.workflows.values()))
        snapshot["jobs"] = self.job_summary()
        return snapshot

    def capability_coverage(self) -> dict:
        return CapabilityCatalog().coverage()

    def job_summary(self) -> dict:
        jobs = self.jobs.list(500)
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status.value] = counts.get(job.status.value, 0) + 1
        return {"total": len(jobs), "by_status": counts}

    def catalog_status(self) -> dict:
        path = module_catalog_path()
        if not path.is_file():
            return {
                "configured": False,
                "status": "not-scanned",
                "path": str(path),
                "message": "Scan the HCP private library to build the catalog.",
            }
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            return {
                "configured": True,
                "status": "invalid",
                "path": str(path),
                "message": f"Catalog cannot be read: {error}",
            }
        if "integrity" not in payload:
            return {
                "configured": True,
                "status": "legacy",
                "path": str(path),
                "modules": len(payload.get("modules", [])),
                "message": "Catalog has no integrity envelope; scan the registry to rebuild it.",
            }
        try:
            validate_catalog(
                payload,
                signing_key=os.getenv("TERRAMIG_CATALOG_SIGNING_KEY", ""),
                require_signature=os.getenv(
                    "TERRAMIG_REQUIRE_SIGNED_CATALOG", ""
                ).lower()
                in {"1", "true", "yes"},
            )
        except ValueError as error:
            return {
                "configured": True,
                "status": "invalid",
                "path": str(path),
                "message": str(error),
            }
        return {
            "configured": True,
            "status": "verified",
            "path": str(path),
            "generated_at": payload.get("generated_at", ""),
            "modules": len(payload.get("modules", [])),
            "verified_modules": sum(
                item.get("schema_status") == "verified"
                for item in payload.get("modules", [])
            ),
            "trusted_modules": sum(
                item.get("schema_status") == "trusted"
                for item in payload.get("modules", [])
            ),
            "incompatible_modules": sum(
                item.get("schema_status") == "incompatible"
                for item in payload.get("modules", [])
            ),
            "needs_review_modules": sum(
                item.get("schema_status") == "needs-review"
                for item in payload.get("modules", [])
            ),
            "failures": len(payload.get("failures", [])),
            "sha256": payload["integrity"]["sha256"],
            "signed": bool(payload["integrity"].get("hmac_sha256")),
        }

    def workflow_report(self, workflow_id: str) -> dict:
        workflow = self.service.get(workflow_id)
        sanitized, redactions = sanitize_untrusted(workflow.to_dict())
        report = {
            "schema_version": 1,
            "kind": "terramig-adoption-report",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "target": {
                "hostname": self.settings.hcp_hostname,
                "organization": self.settings.hcp_organization,
                "workspace": self.settings.hcp_workspace,
            },
            "redacted_fields": redactions,
            "workflow": sanitized,
        }
        report["sha256"] = canonical_sha256(report)
        return report

APP = Application()


class TerraMigHandler(BaseHTTPRequestHandler):
    server_version = "TerraMig/0.1"

    def do_GET(self) -> None:
        self._start_request()
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/api/health", "/api/health/live"}:
                self._json({"status": "ok"})
                return
            if path == "/api/health/ready":
                readiness = APP.readiness()
                database = APP.database_status()
                ready = bool(database["healthy"] and readiness["ready"])
                self._json(
                    {
                        "status": "ready" if ready else "not-ready",
                        "database": database,
                        **readiness,
                    },
                    HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            if path == "/api/auth/session":
                authenticated = self._require_auth(allow_password_change=True)
                self._json(
                    {
                        "user": APP.auth.public_user(authenticated.user),
                        "expires_at": authenticated.session.expires_at,
                        "persistence": APP.database_status(),
                    }
                )
                return
            if path.startswith("/api/"):
                self._require_auth()
            if path == "/api/operations":
                self._json(APP.operations())
            elif path == "/api/metrics":
                self._text(
                    OBSERVABILITY.prometheus(list(APP.service.workflows.values())),
                    "text/plain; version=0.0.4",
                )
            elif path == "/api/capabilities":
                self._json(APP.capability_coverage())
            elif path == "/api/catalog/status":
                self._json(APP.catalog_status())
            elif path == "/api/settings":
                self._json(
                    {
                        **APP.public_settings(),
                        "persistence": APP.database_status(),
                    }
                )
            elif path == "/api/workflows":
                self._json({"workflows": APP.workflows()})
            elif path == "/api/jobs":
                self._json(
                    {"jobs": [job.to_dict() for job in APP.jobs.list(200)]}
                )
            elif path == "/api/projects":
                self._require_capabilities("gcloud", "gcp_authenticated")
                self._json({"projects": APP.service.inventory.list_projects()})
            elif path == "/api/inventory":
                self._require_capabilities("gcloud", "gcp_authenticated")
                project_id = parse_qs(parsed.query).get("project_id", [""])[0]
                if not project_id:
                    raise ValueError("project_id is required")
                resources = APP.service.inventory.discover(project_id)
                self._json(
                    {
                        "resources": [asdict(resource) for resource in resources],
                        "service_coverage": discovered_service_coverage(resources),
                    }
                )
            elif path == "/api/modules":
                self._require_capabilities("hcp_authenticated")
                modules = APP.service.registry.list_modules()
                self._json({"modules": [asdict(module) for module in modules]})
            elif path.startswith("/api/workflows/") and path.endswith("/report"):
                workflow_id = path.removeprefix("/api/workflows/").removesuffix("/report").strip("/")
                self._json(APP.workflow_report(workflow_id))
            elif path.startswith("/api/workflows/"):
                workflow_id = path.removeprefix("/api/workflows/").strip("/")
                self._json(APP.service.get(workflow_id).to_dict())
            elif path.startswith("/api/jobs/"):
                job_id = path.removeprefix("/api/jobs/").strip("/")
                self._json(APP.jobs.get(job_id).to_dict())
            else:
                self._static(path)
        except KeyError as error:
            self._error(HTTPStatus.NOT_FOUND, str(error))
        except AuthenticationError as error:
            self._error(HTTPStatus(error.status), str(error))
        except ValueError as error:
            self._error(HTTPStatus.CONFLICT, str(error))
        except Exception as error:
            self._internal_error(error)

    def do_POST(self) -> None:
        self._start_request()
        path = urlparse(self.path).path
        try:
            if path == "/api/auth/login":
                payload = self._body()
                issued = APP.auth.login(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                )
                self._set_auth_cookies(
                    issued.token, issued.csrf_token, APP.auth.session_hours * 3600
                )
                self._json(
                    {
                        "user": APP.auth.public_user(issued.user),
                        "csrf_token": issued.csrf_token,
                        "expires_at": issued.expires_at,
                    }
                )
                return
            authenticated = self._require_auth(allow_password_change=True)
            self._require_csrf(authenticated)
            if path == "/api/auth/logout":
                APP.auth.logout(self._session_cookie())
                self._clear_auth_cookies()
                self._json({"status": "signed-out"})
                return
            if path == "/api/auth/change-password":
                payload = self._body()
                issued = APP.auth.change_password(
                    authenticated,
                    str(payload.get("current_password", "")),
                    str(payload.get("new_password", "")),
                )
                self._set_auth_cookies(
                    issued.token, issued.csrf_token, APP.auth.session_hours * 3600
                )
                self._json(
                    {
                        "user": APP.auth.public_user(issued.user),
                        "csrf_token": issued.csrf_token,
                        "expires_at": issued.expires_at,
                    }
                )
                return
            if authenticated.user.must_change_password:
                raise AuthenticationError(
                    "Change the default password before using TerraMig", status=403
                )
            if path == "/api/settings/ai-credential/test":
                payload = self._body()
                self._json(
                    APP.test_ai_credential(
                        str(payload.get("provider", APP.settings.ai_provider))
                    )
                )
                return
            if path == "/api/settings/hcp-credential/test":
                self._json(APP.test_hcp_credential())
                return
            if path == "/api/settings/github-credential/test":
                self._json(APP.test_github_credential())
                return
            if path == "/api/settings/git-ssh-credential/test":
                payload = self._body()
                self._json(
                    APP.test_git_ssh_credential(
                        str(payload.get("repository", ""))
                    )
                )
                return
            if path == "/api/settings/gcp-connection/test":
                payload = self._body()
                self._json(
                    APP.test_gcp_connection(str(payload.get("project_id", "")))
                )
                return
            if path == "/api/settings/gcp-connection/configure":
                self._json(APP.configure_gcp_connection(self._body()))
                return
            if path == "/api/workflows":
                self._require_capabilities(
                    "gcloud",
                    "gcp_authenticated",
                    "hcp_authenticated",
                    "ai",
                    "adoption_target",
                )
                payload = self._body()
                project_id = payload.get("project_id", "").strip()
                if not project_id:
                    self._error(HTTPStatus.BAD_REQUEST, "project_id is required")
                    return
                workflow = self._operation(
                    "adoption.create", "", lambda: APP.service.create(project_id)
                )
                self._json(workflow.to_dict(), HTTPStatus.CREATED)
                return

            if path == "/api/catalog/scan":
                self._require_capabilities("hcp_authenticated")
                payload = self._body()
                job = APP.jobs.enqueue(
                    "catalog",
                    "scan",
                    APP.settings.hcp_organization,
                    {"force": bool(payload.get("force", True))},
                    max_attempts=1,
                )
                self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                return

            job_prefix = "/api/jobs/"
            if path.startswith(job_prefix):
                job_id, separator, action = path.removeprefix(job_prefix).partition("/")
                if not separator:
                    self._error(HTTPStatus.BAD_REQUEST, "job action is required")
                    return
                if action == "cancel":
                    job = APP.jobs.cancel(job_id)
                elif action == "retry":
                    job = APP.jobs.retry(job_id)
                else:
                    self._error(HTTPStatus.NOT_FOUND, f"unknown job action {action}")
                    return
                self._json(job.to_dict(), HTTPStatus.ACCEPTED)
                return

            prefix = "/api/workflows/"
            if not path.startswith(prefix):
                self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
                return
            workflow_id, separator, action = path.removeprefix(prefix).partition("/")
            if not separator:
                self._error(HTTPStatus.BAD_REQUEST, "workflow action is required")
                return
            actions = {
                "discover",
                "match",
                "select-modules",
                "generate",
                "validate",
                "accept-drift",
                "deliver",
                "import",
                "reconcile",
            }
            if action not in actions:
                self._error(HTTPStatus.NOT_FOUND, f"unknown action {action}")
                return
            payload = self._body()
            if action == "deliver":
                self._require_capabilities("git", "git_auth")
                validate_delivery_payload(payload)
            if action == "import":
                if not bool(payload.get("confirm")):
                    raise InvalidTransition("Explicit HCP saved-plan submission approval is required")
            if action == "accept-drift" and not bool(payload.get("accept")):
                raise InvalidTransition("Explicit drift acceptance is required")
            job = APP.jobs.enqueue(
                "adoption",
                action,
                workflow_id,
                payload
                if action
                in {
                    "match",
                    "select-modules",
                    "generate",
                    "accept-drift",
                    "deliver",
                    "import",
                }
                else {},
                max_attempts=(
                    1000
                    if action == "reconcile"
                    else 1
                    if action == "accept-drift"
                    else 3
                ),
            )
            self._json(job.to_dict(), HTTPStatus.ACCEPTED)
        except AuthenticationError as error:
            self._error(HTTPStatus(error.status), str(error))
        except (InvalidTransition, ValueError) as error:
            self._error(HTTPStatus.CONFLICT, str(error))
        except KeyError as error:
            self._error(HTTPStatus.NOT_FOUND, str(error))
        except Exception as error:
            self._internal_error(error)

    def do_PUT(self) -> None:
        self._start_request()
        try:
            authenticated = self._require_auth()
            self._require_csrf(authenticated)
            path = urlparse(self.path).path
            if path == "/api/settings/ai-credential":
                self._json(APP.save_ai_credential(self._body()))
                return
            if path == "/api/settings/hcp-credential":
                self._json(APP.save_hcp_credential(self._body()))
                return
            if path == "/api/settings/github-credential":
                self._json(APP.save_github_credential(self._body()))
                return
            if path == "/api/settings/git-ssh-credential":
                self._json(APP.save_git_ssh_credential(self._body()))
                return
            if path == "/api/settings/gcp-credential":
                self._json(APP.save_gcp_credential(self._body()))
                return
            if path != "/api/settings":
                self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
                return
            self._json(APP.update_settings(self._body()))
        except AuthenticationError as error:
            self._error(HTTPStatus(error.status), str(error))
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self._internal_error(error)

    def do_DELETE(self) -> None:
        self._start_request()
        try:
            authenticated = self._require_auth()
            self._require_csrf(authenticated)
            path = urlparse(self.path).path
            if path == "/api/settings/hcp-credential":
                self._json(APP.clear_hcp_credential())
                return
            if path == "/api/settings/github-credential":
                self._json(APP.clear_github_credential())
                return
            if path == "/api/settings/git-ssh-credential":
                self._json(APP.clear_git_ssh_credential())
                return
            if path == "/api/settings/gcp-credential":
                payload = self._body()
                self._json(
                    APP.clear_gcp_credential(
                        clear_project=bool(payload.get("clear_project"))
                    )
                )
                return
            if path != "/api/settings/ai-credential":
                self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
                return
            payload = self._body()
            self._json(
                APP.clear_ai_credential(
                    str(payload.get("provider", APP.settings.ai_provider))
                )
            )
        except AuthenticationError as error:
            self._error(HTTPStatus(error.status), str(error))
        except ValueError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self._internal_error(error)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        if length > 1_048_576:
            raise ValueError("request body exceeds 1 MB")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self._request_id)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)
        self._record_response(int(status))

    def _text(
        self, body: str, content_type: str, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", self._request_id)
        self._security_headers()
        self.end_headers()
        self.wfile.write(encoded)
        self._record_response(int(status))

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json({"error": message}, status)

    def _internal_error(self, error: Exception) -> None:
        print(
            OBSERVABILITY.structured_log(
                {
                    "event": "server_error",
                    "error_type": type(error).__name__,
                    "request_id": self._request_id,
                }
            )
        )
        self._error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"Internal server error (request {self._request_id})",
        )

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self._error(HTTPStatus.FORBIDDEN, "invalid path")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self._request_id)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)
        self._record_response(HTTPStatus.OK)

    def _start_request(self) -> None:
        self._request_started = time.monotonic()
        supplied = self.headers.get("X-Request-ID", "")
        self._request_id = (
            supplied
            if supplied and len(supplied) <= 128
            else str(uuid4())
        )
        self._response_recorded = False
        self._pending_headers: list[tuple[str, str]] = []

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        for name, value in self._pending_headers:
            self.send_header(name, value)

    def _require_auth(
        self, *, allow_password_change: bool = False
    ) -> AuthenticatedSession:
        authenticated = APP.auth.authenticate(self._session_cookie())
        if authenticated.user.must_change_password and not allow_password_change:
            raise AuthenticationError(
                "Change the default password before using TerraMig", status=403
            )
        return authenticated

    def _require_csrf(self, authenticated: AuthenticatedSession) -> None:
        APP.auth.verify_csrf(
            authenticated,
            self._cookie("terramig_csrf"),
            self.headers.get("X-CSRF-Token", ""),
        )

    def _session_cookie(self) -> str:
        return self._cookie("terramig_session")

    def _cookie(self, name: str) -> str:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        return cookie[name].value if name in cookie else ""

    def _set_auth_cookies(
        self, session_token: str, csrf_token: str, max_age: int
    ) -> None:
        secure = os.getenv("TERRAMIG_SECURE_COOKIES", "").lower() in {
            "1",
            "true",
            "yes",
        }
        attributes = f"Path=/; Max-Age={max_age}; SameSite=Strict"
        if secure:
            attributes += "; Secure"
        self._pending_headers.extend(
            [
                (
                    "Set-Cookie",
                    f"terramig_session={session_token}; {attributes}; HttpOnly",
                ),
                ("Set-Cookie", f"terramig_csrf={csrf_token}; {attributes}"),
            ]
        )

    def _clear_auth_cookies(self) -> None:
        attributes = "Path=/; Max-Age=0; SameSite=Strict"
        if os.getenv("TERRAMIG_SECURE_COOKIES", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            attributes += "; Secure"
        self._pending_headers.extend(
            [
                (
                    "Set-Cookie",
                    f"terramig_session=deleted; {attributes}; HttpOnly",
                ),
                ("Set-Cookie", f"terramig_csrf=deleted; {attributes}"),
            ]
        )

    def _record_response(self, status: int) -> None:
        if self._response_recorded:
            return
        self._response_recorded = True
        duration_ms = round((time.monotonic() - self._request_started) * 1000)
        OBSERVABILITY.record_request(
            self.command, self.path, int(status), duration_ms, self._request_id
        )
        print(
            OBSERVABILITY.structured_log(
                {
                    "event": "http_request",
                    "method": self.command,
                    "path": urlparse(self.path).path,
                    "status": int(status),
                    "duration_ms": duration_ms,
                    "request_id": self._request_id,
                }
            )
        )

    @staticmethod
    def _operation(name: str, subject_id: str, function):
        started = time.monotonic()
        try:
            result = function()
        except Exception:
            OBSERVABILITY.record_operation(
                name,
                "failed",
                round((time.monotonic() - started) * 1000),
                subject_id=subject_id,
            )
            raise
        OBSERVABILITY.record_operation(
            name,
            "succeeded",
            round((time.monotonic() - started) * 1000),
            subject_id=subject_id or getattr(result, "id", ""),
        )
        return result

    @staticmethod
    def _require_capabilities(*capabilities: str) -> None:
        readiness = APP.readiness()
        missing = [name for name in capabilities if not readiness[name]]
        if missing:
            raise ValueError(
                f"production integrations are not ready; missing: {', '.join(missing)}"
            )


def main() -> None:
    host = os.getenv("TERRAMIG_HOST", "127.0.0.1")
    port = int(os.getenv("TERRAMIG_PORT", "8080"))
    if os.getenv("TERRAMIG_WORKER_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
    }:
        APP.start_worker()
    print(f"TerraMig listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), TerraMigHandler).serve_forever()


if __name__ == "__main__":
    main()
