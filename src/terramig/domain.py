from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    CREATED = "created"
    DISCOVERED = "discovered"
    MATCHED = "matched"
    GENERATED = "generated"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    IMPORTED = "imported"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Resource:
    id: str
    type: str
    name: str
    location: str
    attributes: dict[str, Any]
    dependency_ids: tuple[str, ...] = ()
    terraform_type: str = ""
    import_id: str = ""
    support_status: str = "unknown"
    support_reason: str = ""
    provider_version: str = ""


@dataclass(frozen=True)
class ModuleVersion:
    id: str
    source: str
    version: str
    resource_types: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...] = ()
    managed_resource_address: str = ""
    description: str = ""
    schema_status: str = "verified"
    registry_kind: str = "private"
    verified_publisher: bool = False
    managed_resource_addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModuleCandidate:
    resource_id: str
    module: ModuleVersion
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ImportOperation:
    address: str
    remote_id: str
    command: str
    resource_id: str = ""


@dataclass
class GenerationBundle:
    terraform: str
    imports: list[ImportOperation]
    decisions: list[str]
    warnings: list[str]
    agent: str = ""
    duration_ms: int = 0
    composition_log: list[str] = field(default_factory=list)
    prompt_sha256: str = ""
    response_sha256: str = ""
    assurance_checks: list[str] = field(default_factory=list)
    redacted_fields: int = 0
    backend_tf: str = ""
    providers_tf: str = ""


@dataclass
class VerificationResult:
    success: bool
    drift_detected: bool
    terraform_version: str
    commands: list[str]
    summary: str
    diagnostics: list[str]
    output: str
    planned_imports: int = 0
    repair_attempts: int = 0
    repaired_by_ai: bool = False


@dataclass
class HCPImportLifecycle:
    status: str = "not-submitted"
    workspace_id: str = ""
    configuration_version_id: str = ""
    run_id: str = ""
    run_url: str = ""
    workspace_url: str = ""
    remote_status: str = ""
    post_import_run_id: str = ""
    post_import_status: str = ""
    imported_resource_count: int = 0
    drift_free: bool | None = None
    checked_at: str = ""
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class GitDelivery:
    model_repository: str
    target_repository: str
    model_ref: str = "main"
    base_branch: str = "main"
    branch: str = ""
    target_path: str = "terraform"
    create_pull_request: bool = True
    allow_overwrite: bool = False
    update_existing_directory: bool = False
    status: str = "pending"
    commit_sha: str = ""
    pull_request_url: str = ""
    changed_files: list[str] = field(default_factory=list)
    skipped_template_files: list[str] = field(default_factory=list)
    output: str = ""
    delivered_at: str = ""


@dataclass
class DurableJob:
    id: str
    kind: str
    action: str
    subject_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    available_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    cancel_requested: bool = False
    last_error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass
class Workflow:
    id: str
    project_id: str
    runtime_origin: str = "legacy"
    stage: Stage = Stage.CREATED
    resources: list[Resource] = field(default_factory=list)
    ordered_resource_ids: list[str] = field(default_factory=list)
    selected_resource_ids: list[str] = field(default_factory=list)
    managed_resources: dict[str, dict[str, str]] = field(default_factory=dict)
    candidates: dict[str, list[ModuleCandidate]] = field(default_factory=dict)
    module_selections: dict[str, str] = field(default_factory=dict)
    public_registry_searched: bool = False
    bundle: GenerationBundle | None = None
    verification: VerificationResult | None = None
    drift_accepted: bool = False
    drift_accepted_at: str = ""
    import_run_id: str | None = None
    target_protection: dict[str, Any] = field(default_factory=dict)
    hcp_import: HCPImportLifecycle = field(default_factory=HCPImportLifecycle)
    git_delivery: GitDelivery | None = None
    events: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["stage"] = self.stage.value
        return value
