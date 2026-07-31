from __future__ import annotations

import json
import io
import os
import re
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import threading
import time
from typing import Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from ..domain import GenerationBundle, HCPImportLifecycle, ModuleVersion, Resource, Stage
from .terraform import LocalTerraformVerifier
from ..services.artifacts import terraform_files
from ..services.capabilities import CapabilityCatalog, dependency_ids
from ..services.catalog import (
    ModuleCatalogBuilder,
    TerraformModuleInspector,
    validate_catalog,
)


ADOPTION_METADATA_ASSET_TYPES = frozenset(
    {
        "cloudbilling.googleapis.com/ProjectBillingInfo",
        "cloudresourcemanager.googleapis.com/Project",
        "compute.googleapis.com/Project",
        "serviceusage.googleapis.com/Service",
    }
)


class HCPWorkspaceProtector:
    """Fail-closed checks for a workspace that is about to receive imported state."""

    def __init__(self, get_json) -> None:
        self.get_json = get_json

    def assert_safe(
        self,
        organization: str,
        workspace: str,
        *,
        allow_missing: bool = False,
        allow_nonempty_workspace: bool = False,
    ) -> dict:
        try:
            payload = self.get_json(
                f"/api/v2/organizations/{quote(organization)}/workspaces/{quote(workspace)}"
            )
        except Exception as error:
            if allow_missing and self._is_not_found(error):
                listing = self.get_json(
                    f"/api/v2/organizations/{quote(organization)}/workspaces"
                    f"?search%5Bname%5D={quote(workspace)}&page%5Bsize%5D=1"
                ).get("data")
                if not isinstance(listing, list):
                    raise RuntimeError(
                        "HCP workspace protection could not verify organization-level workspace access"
                    ) from error
                if listing:
                    raise RuntimeError(
                        "HCP workspace lookup was inconsistent; refusing to assume the destination is absent"
                    ) from error
                return {
                    "workspace": workspace,
                    "status": "available-for-creation",
                    "checks": [
                        "organization workspace access verified",
                        "workspace does not exist",
                        "destination name is available",
                    ],
                }
            raise
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("HCP workspace protection could not read the target workspace")
        attributes = data.get("attributes")
        if not isinstance(attributes, dict):
            raise RuntimeError("HCP workspace protection response is missing attributes")
        required = {"locked", "resource-count", "execution-mode"}
        missing = sorted(required - set(attributes))
        if missing:
            raise RuntimeError(
                "HCP workspace protection response is missing: " + ", ".join(missing)
            )
        if attributes["locked"]:
            raise ValueError("HCP target workspace is locked")
        resource_count = int(attributes["resource-count"] or 0)
        if resource_count and not allow_nonempty_workspace:
            raise ValueError(
                f"HCP target workspace already manages {resource_count} resources; "
                "explicitly authorize the non-empty workspace exception to continue"
            )
        execution_mode = attributes["execution-mode"]
        if execution_mode not in {"remote", "agent"}:
            raise ValueError(
                f"HCP target workspace execution mode must be remote or agent, found {execution_mode}"
            )
        if attributes.get("auto-apply"):
            raise ValueError("HCP target workspace must have auto-apply disabled")
        vcs_connected = bool(attributes.get("vcs-repo"))
        workspace_id = data.get("id")
        if not workspace_id:
            raise RuntimeError("HCP workspace protection response is missing the workspace ID")
        runs = self.get_json(
            f"/api/v2/workspaces/{workspace_id}/runs"
            "?filter%5Bstatus_group%5D=non_final&page%5Bsize%5D=1"
        ).get("data")
        if not isinstance(runs, list):
            raise RuntimeError("HCP workspace protection could not inspect active runs")
        if runs:
            status = runs[0].get("attributes", {}).get("status", "unknown")
            raise ValueError(
                f"HCP target workspace has a non-final run ({status}); wait for it to finish"
            )
        nonempty_override_applied = bool(
            resource_count and allow_nonempty_workspace
        )
        state_check = (
            f"operator authorized the non-empty workspace exception "
            f"for {resource_count} managed resources"
            if nonempty_override_applied
            else "workspace state is empty"
        )
        return {
            "workspace_id": workspace_id,
            "workspace": workspace,
            "status": "protected",
            "resource_count": resource_count,
            "nonempty_workspace_override": nonempty_override_applied,
            "execution_mode": execution_mode,
            "auto_apply": False,
            "vcs_connected": vcs_connected,
            "active_runs": 0,
            "checks": [
                "workspace exists and is accessible",
                "workspace is unlocked",
                state_check,
                "no queued or active runs",
                "execution mode supports HCP plans",
                "auto-apply is disabled",
                (
                    "workspace is VCS-driven; TerraMig uses an isolated "
                    "provisional configuration and leaves VCS settings unchanged"
                    if vcs_connected
                    else "workspace is API-driven"
                ),
            ],
        }

    @staticmethod
    def _is_not_found(error: Exception) -> bool:
        return getattr(error, "code", None) == 404


class GCloudInventory:
    """Read-only GCP Cloud Asset Inventory adapter using the authenticated gcloud CLI."""

    def __init__(
        self,
        configured_project: str = "",
        credential: str = "",
        *,
        timeout_seconds: float = 600,
        heartbeat_seconds: float = 10,
    ) -> None:
        self.configured_project = configured_project
        self.credential = credential
        self.timeout_seconds = max(120.0, float(timeout_seconds))
        self.heartbeat_seconds = max(1.0, float(heartbeat_seconds))
        self.capabilities = CapabilityCatalog()

    def list_projects(self) -> list[dict[str, str]]:
        if self.configured_project:
            return [{"id": self.configured_project, "name": self.configured_project}]
        data = self._run("projects", "list", "--filter=lifecycleState:ACTIVE", "--format=json")
        return [{"id": item["projectId"], "name": item.get("name", item["projectId"])} for item in data]

    def discover(
        self,
        project_id: str,
        progress: Callable[[str], None] | None = None,
    ) -> list[Resource]:
        emit = progress or (lambda _message: None)
        emit(
            f"Cloud Asset discovery started for {project_id} "
            f"(command timeout {int(self.timeout_seconds)}s)"
        )
        data, configuration_complete = self._discover_assets(project_id, progress=progress)
        discovered_count = len(data)
        data = [
            item
            for item in data
            if item.get("assetType") not in ADOPTION_METADATA_ASSET_TYPES
        ]
        metadata_count = discovered_count - len(data)
        if metadata_count:
            emit(
                f"Excluded {metadata_count} project, billing, and enabled-API metadata assets"
            )
        data = self._coalesce_service_accounts(data, project_id)
        emit(f"Normalizing and assessing {len(data)} inventory resource assets")
        prepared: list[tuple[dict, dict, object]] = []
        for item in data:
            resource_envelope = item.get("resource") or {}
            attributes = dict(resource_envelope.get("data") or item.get("additionalAttributes") or {})
            if item.get("labels") and "labels" not in attributes:
                attributes["labels"] = item["labels"]
            assessment = self.capabilities.assess(
                item["assetType"],
                item["name"],
                attributes,
                configuration_complete=configuration_complete and bool(resource_envelope.get("data")),
            )
            prepared.append((item, attributes, assessment))

        known_resources = [
            (item["name"], assessment.import_id)
            for item, _, assessment in prepared
        ]
        normalized: list[Resource] = []
        for item, attributes, assessment in prepared:
            resource_envelope = item.get("resource") or {}
            parent = item.get("parentFullResourceName") or resource_envelope.get("parent", "")
            normalized.append(
                Resource(
                    id=item["name"],
                    type=item["assetType"],
                    name=(
                        item.get("displayName")
                        or attributes.get("name")
                        or item["name"].rsplit("/", 1)[-1]
                    ),
                    location=item.get("location") or resource_envelope.get("location", "global"),
                    attributes=attributes,
                    dependency_ids=dependency_ids(
                        item["name"], parent, attributes, known_resources
                    ),
                    terraform_type=assessment.terraform_type,
                    import_id=assessment.import_id,
                    support_status=assessment.status,
                    support_reason=assessment.reason,
                    provider_version=assessment.provider_version,
                )
            )
        coverage: dict[str, int] = {}
        for resource in normalized:
            coverage[resource.support_status] = coverage.get(resource.support_status, 0) + 1
        coverage_summary = ", ".join(
            f"{count} {status}" for status, count in sorted(coverage.items())
        )
        emit(
            f"Discovery normalized {len(normalized)} resources"
            + (f" ({coverage_summary})" if coverage_summary else "")
        )
        return normalized

    @staticmethod
    def _coalesce_service_accounts(data: list[dict], project_id: str) -> list[dict]:
        """Collapse Cloud Asset's numeric-ID and email views of one IAM account."""

        merged: dict[str, dict] = {}
        for raw in data:
            item = dict(raw)
            key = str(item.get("name", ""))
            if item.get("assetType") == "iam.googleapis.com/ServiceAccount":
                envelope = item.get("resource") or {}
                attributes = envelope.get("data") or item.get("additionalAttributes") or {}
                email = str(attributes.get("email") or "")
                explicit_name = str(attributes.get("name") or "")
                if not email and "/serviceAccounts/" in explicit_name:
                    email = explicit_name.rsplit("/serviceAccounts/", 1)[-1]
                if not email and "/serviceAccounts/" in key:
                    candidate = key.rsplit("/serviceAccounts/", 1)[-1]
                    if "@" in candidate:
                        email = candidate
                account_project = str(attributes.get("projectId") or project_id)
                if email and account_project:
                    key = (
                        f"//iam.googleapis.com/projects/{account_project}"
                        f"/serviceAccounts/{email}"
                    )
                    item["name"] = key
            existing = merged.get(key)
            if existing is None:
                merged[key] = item
                continue
            existing_complete = bool((existing.get("resource") or {}).get("data"))
            item_complete = bool((item.get("resource") or {}).get("data"))
            preferred, metadata = (
                (item, existing) if item_complete and not existing_complete else (existing, item)
            )
            combined = dict(metadata)
            combined.update(preferred)
            combined["name"] = key
            merged[key] = combined
        return list(merged.values())

    def _discover_assets(
        self,
        project_id: str,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[list[dict], bool]:
        emit = progress or (lambda _message: None)
        emit(
            "Querying Cloud Asset RESOURCE content; large projects can take several minutes"
        )
        try:
            listed = self._run(
                "asset",
                "list",
                f"--project={project_id}",
                "--content-type=resource",
                "--format=json",
                progress=progress,
                operation="Cloud Asset RESOURCE query",
            )
        except RuntimeError:
            emit(
                "RESOURCE content query was unavailable; falling back to the Cloud Asset search index"
            )
            return (
                self._run(
                    "asset",
                    "search-all-resources",
                    f"--scope=projects/{project_id}",
                    "--read-mask=*",
                    "--format=json",
                    progress=progress,
                    operation="Cloud Asset search query",
                ),
                False,
            )
        emit(f"Cloud Asset RESOURCE query returned {len(listed)} assets")
        emit("Querying the Cloud Asset search index for omitted and global resources")
        try:
            searched = self._run(
                "asset",
                "search-all-resources",
                f"--scope=projects/{project_id}",
                "--read-mask=*",
                "--format=json",
                progress=progress,
                operation="Cloud Asset search query",
            )
        except RuntimeError:
            emit(
                "Cloud Asset search was unavailable; continuing with configuration-complete RESOURCE results"
            )
            return listed, True
        emit(f"Cloud Asset search query returned {len(searched)} assets")

        # RESOURCE content is authoritative for composition. Search contributes
        # assets omitted by list/export APIs (notably some global Compute types)
        # and fills display metadata without replacing configuration-complete data.
        merged = {item["name"]: dict(item) for item in searched}
        for item in listed:
            search_metadata = merged.get(item["name"], {})
            combined = dict(search_metadata)
            combined.update(item)
            merged[item["name"]] = combined
        emit(
            f"Merged and deduplicated Cloud Asset results into {len(merged)} unique assets"
        )
        return list(merged.values()), True

    def _run(
        self,
        *arguments: str,
        progress: Callable[[str], None] | None = None,
        operation: str = "gcloud query",
    ) -> list[dict]:
        from .gcp_auth import gcloud_credential_environment

        started = time.monotonic()
        with gcloud_credential_environment(self.credential) as environment:
            process = subprocess.Popen(
                ["gcloud", *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            while True:
                elapsed = time.monotonic() - started
                remaining = self.timeout_seconds - elapsed
                if remaining <= 0:
                    process.kill()
                    _stdout, stderr = process.communicate()
                    detail = stderr.strip() or "no diagnostic output"
                    if self.credential:
                        detail = detail.replace(self.credential, "[REDACTED]")
                    raise TimeoutError(
                        f"{operation} exceeded {int(self.timeout_seconds)}s; "
                        f"gcloud output: {detail}"
                    )
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(self.heartbeat_seconds, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    if progress:
                        progress(
                            f"{operation} still running "
                            f"({int(time.monotonic() - started)}s elapsed)"
                        )
        if process.returncode:
            detail = stderr.strip() or "gcloud command failed"
            if self.credential:
                detail = detail.replace(self.credential, "[REDACTED]")
            raise RuntimeError(detail)
        if progress:
            progress(f"{operation} completed in {time.monotonic() - started:.1f}s")
        payload = json.loads(stdout)
        if not isinstance(payload, list):
            raise RuntimeError(f"{operation} returned an unexpected JSON response")
        return payload


class _ModuleSnapshot:
    def __init__(self, modules: list[ModuleVersion]) -> None:
        self.modules = modules

    def list_modules(self) -> list[ModuleVersion]:
        return list(self.modules)


class HCPRegistry:
    """Read-only HCP Terraform V2 API private-library catalog adapter."""

    def __init__(
        self,
        hostname: str,
        organization: str,
        token: str,
        schema_catalog: Path | None = None,
        *,
        auto_catalog: bool = False,
    ) -> None:
        self.hostname = hostname.rstrip("/")
        self.organization = organization
        self.token = token
        configured = os.getenv("TERRAMIG_MODULE_MANIFEST", "")
        self.schema_catalog = schema_catalog or (Path(configured) if configured else None)
        self.auto_catalog = auto_catalog
        self._catalog_lock = threading.Lock()

    def list_modules(self) -> list[ModuleVersion]:
        modules = self._metadata_modules()
        if self.auto_catalog:
            self._ensure_catalog(modules)
        return self._enrich(modules)

    def refresh_catalog(self, *, force: bool = True, progress=None) -> list[ModuleVersion]:
        modules = self._metadata_modules()
        self._ensure_catalog(modules, force=force, progress=progress)
        return self._enrich(modules)

    def _metadata_modules(self) -> list[ModuleVersion]:
        path = f"/api/v2/organizations/{quote(self.organization)}/registry-modules?page%5Bsize%5D=100"
        items: list[dict] = []
        while path:
            envelope = self._get(path)
            items.extend(envelope.get("data", []))
            path = envelope.get("links", {}).get("next")
        modules: list[ModuleVersion] = []
        for item in items:
            attributes = item.get("attributes", {})
            provider = str(attributes.get("provider", "")).lower()
            if provider not in {"google", "gcp"}:
                continue
            registry_name = str(
                attributes.get("registry-name", "private")
            ).lower()
            if registry_name not in {"private", "public"}:
                continue
            version = self._latest_version(item, registry_name)
            namespace = attributes.get("namespace", self.organization)
            if registry_name == "public":
                source = (
                    f"registry.terraform.io/{namespace}/"
                    f"{attributes['name']}/{provider}"
                )
            else:
                source = (
                    f"{self.hostname}/{namespace}/{attributes['name']}/{provider}"
                )
            modules.append(
                ModuleVersion(
                    id=item["id"],
                    source=source,
                    version=version,
                    resource_types=(),
                    inputs=(),
                    description=str(attributes.get("description", "")),
                    schema_status="metadata-only",
                    registry_kind=(
                        "hcp-public" if registry_name == "public" else "private"
                    ),
                )
            )
        return modules

    def _latest_version(self, item: dict, registry_name: str) -> str:
        attributes = item.get("attributes", {})
        if registry_name == "private":
            usable = [
                entry
                for entry in attributes.get("version-statuses", [])
                if entry.get("status") in {"ok", "uploaded"}
            ]
            versions = [str(entry.get("version", "")) for entry in usable]
        else:
            versions = self._public_versions(item)
        exact = [version for version in versions if version]
        return max(exact, key=self._version_key) if exact else "unavailable"

    def _public_versions(self, item: dict) -> list[str]:
        self_link = str(item.get("links", {}).get("self", "")).rstrip("/")
        if not self_link:
            attributes = item.get("attributes", {})
            self_link = (
                f"/api/v2/organizations/{quote(self.organization)}/"
                f"registry-modules/public/{quote(str(attributes['namespace']))}/"
                f"{quote(str(attributes['name']))}/"
                f"{quote(str(attributes['provider']))}"
            )
        path = f"{self_link}/versions?page%5Bsize%5D=100"
        versions: list[str] = []
        while path:
            envelope = self._get(path)
            versions.extend(
                str(entry.get("attributes", {}).get("version", ""))
                for entry in envelope.get("data", [])
            )
            path = envelope.get("links", {}).get("next")
        return versions

    def _enrich(self, modules: list[ModuleVersion]) -> list[ModuleVersion]:
        schemas = self._schemas()
        enriched: list[ModuleVersion] = []
        for module in modules:
            schema = schemas.get(f"{module.source}@{module.version}") or {}
            enriched.append(
                ModuleVersion(
                    id=module.id,
                    source=module.source,
                    version=module.version,
                    resource_types=tuple(schema.get("resource_types", [])),
                    inputs=tuple(schema.get("inputs", [])),
                    outputs=tuple(schema.get("outputs", [])),
                    managed_resource_address=schema.get("managed_resource_address", ""),
                    description=(
                        schema.get("description")
                        or module.description
                        or "Registry metadata loaded; scan to verify its ownership schema."
                    ),
                    schema_status=(
                        schema.get("schema_status", "verified")
                        if schema
                        else "metadata-only"
                    ),
                    registry_kind=module.registry_kind,
                    verified_publisher=module.verified_publisher,
                    managed_resource_addresses=tuple(
                        schema.get("managed_resource_addresses", [])
                    ),
                )
            )
        return enriched

    def _ensure_catalog(
        self,
        modules: list[ModuleVersion],
        *,
        force: bool = False,
        progress=None,
    ) -> None:
        if not self.schema_catalog:
            raise RuntimeError("automatic private-module catalog storage is not configured")
        with self._catalog_lock:
            if not force and self._catalog_is_current(modules):
                if progress:
                    progress("Private-module catalog is current")
                return
            if progress:
                progress(f"Scanning {len(modules)} HCP library Google modules")
            capabilities = CapabilityCatalog()
            inspector = TerraformModuleInspector(
                self.hostname,
                self.token,
                capability_catalog=capabilities,
            )
            payload = ModuleCatalogBuilder(
                _ModuleSnapshot(modules),
                inspector,
                self.organization,
                capabilities.provider_version,
            ).build(
                os.getenv("TERRAMIG_CATALOG_SIGNING_KEY", ""),
                progress=progress,
            )
            self.schema_catalog.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.schema_catalog.with_suffix(
                f"{self.schema_catalog.suffix}.tmp"
            )
            temporary.write_text(json.dumps(payload, indent=2) + "\n")
            temporary.chmod(0o600)
            temporary.replace(self.schema_catalog)
            if progress:
                trusted = sum(
                    item.get("schema_status") == "trusted"
                    for item in payload["modules"]
                )
                incompatible = sum(
                    item.get("schema_status") == "incompatible"
                    for item in payload["modules"]
                )
                progress(
                    f"Catalog stored: {trusted} trusted, "
                    f"{incompatible} incompatible, "
                    f"{len(payload['failures'])} failed inspections"
                )

    def _catalog_is_current(self, modules: list[ModuleVersion]) -> bool:
        if not self.schema_catalog or not self.schema_catalog.is_file():
            return False
        try:
            payload = validate_catalog(
                json.loads(self.schema_catalog.read_text()),
                signing_key=os.getenv("TERRAMIG_CATALOG_SIGNING_KEY", ""),
                require_signature=os.getenv(
                    "TERRAMIG_REQUIRE_SIGNED_CATALOG", ""
                ).lower()
                in {"1", "true", "yes"},
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if payload.get("organization") != self.organization:
            return False
        expected = {(module.source, module.version) for module in modules}
        recorded = {
            (item.get("source", ""), item.get("version", ""))
            for item in payload.get("modules", []) + payload.get("failures", [])
        }
        return expected == recorded

    def _schemas(self) -> dict[str, dict]:
        if not self.schema_catalog or not self.schema_catalog.is_file():
            return {}
        payload = json.loads(self.schema_catalog.read_text())
        if "integrity" in payload or "schema_version" in payload:
            payload = validate_catalog(
                payload,
                signing_key=os.getenv("TERRAMIG_CATALOG_SIGNING_KEY", ""),
                require_signature=os.getenv(
                    "TERRAMIG_REQUIRE_SIGNED_CATALOG", ""
                ).lower()
                in {"1", "true", "yes"},
            )
        schemas: dict[str, dict] = {}
        for item in payload.get("modules", []):
            if item.get("version"):
                schemas[f"{item['source']}@{item['version']}"] = item
        return schemas

    def _get(self, path: str) -> dict:
        url = path if path.startswith("http") else f"https://{self.hostname}{path}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/vnd.api+json",
            },
        )
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    @staticmethod
    def _version_key(version: str) -> tuple:
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", version)
        if not match:
            return (0, 0, 0, 0, version)
        major, minor, patch, prerelease = match.groups()
        return (
            int(major),
            int(minor),
            int(patch),
            1 if not prerelease else 0,
            prerelease or "",
        )


class TerraformPublicRegistry:
    """Search verified publishers in the official public Terraform Registry."""

    def __init__(
        self,
        base_url: str = "https://registry.terraform.io/v1/modules",
        max_results_per_query: int = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_results_per_query = max(1, min(max_results_per_query, 20))
        self.capabilities = CapabilityCatalog()

    def search_modules(self, resources: list[Resource]) -> list[ModuleVersion]:
        reverse = {
            capability.terraform_type: capability.asset_type
            for capability in self.capabilities.capabilities.values()
            if capability.terraform_type and capability.importable
        }
        modules: dict[tuple[str, str], ModuleVersion] = {}
        queries = sorted({self._query(resource) for resource in resources if resource.terraform_type})
        for query in queries:
            search = self._get(
                "/search?"
                + urlencode(
                    {
                        "q": query,
                        "provider": "google",
                        "verified": "true",
                        "limit": self.max_results_per_query,
                    }
                )
            )
            for result in search.get("modules", []):
                if result.get("provider") != "google" or not result.get("verified"):
                    continue
                namespace = str(result.get("namespace", ""))
                name = str(result.get("name", ""))
                version = str(result.get("version", ""))
                if not all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (namespace, name)):
                    continue
                if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
                    continue
                detail = self._get(
                    f"/{quote(namespace)}/{quote(name)}/google/{quote(version)}"
                )
                base_source = f"{namespace}/{name}/google"
                units = [detail.get("root", {})] + list(detail.get("submodules", []))
                for unit in units:
                    module = self._module_from_unit(
                        result, unit, base_source, version, reverse
                    )
                    if module:
                        modules[(module.source, module.version)] = module
        return sorted(modules.values(), key=lambda module: module.source)

    @staticmethod
    def _module_from_unit(
        result: dict,
        unit: dict,
        base_source: str,
        version: str,
        reverse: dict[str, str],
    ) -> ModuleVersion | None:
        resources = unit.get("resources", [])
        if not isinstance(resources, list) or len(resources) != 1:
            return None
        managed = resources[0]
        terraform_type = str(managed.get("type", ""))
        resource_name = str(managed.get("name", ""))
        asset_type = reverse.get(terraform_type)
        if not asset_type or not re.fullmatch(r"[A-Za-z0-9_-]+", resource_name):
            return None
        path = str(unit.get("path", "")).strip("/")
        if path:
            if (
                not path.startswith("modules/")
                or ".." in path.split("/")
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            ):
                return None
        source = f"{base_source}//{path}" if path else base_source
        inputs = tuple(
            sorted(
                str(item["name"])
                for item in unit.get("inputs", [])
                if isinstance(item, dict) and item.get("name")
            )
        )
        outputs = tuple(
            sorted(
                str(item["name"])
                for item in unit.get("outputs", [])
                if isinstance(item, dict) and item.get("name")
            )
        )
        return ModuleVersion(
            id=f"public:{source}@{version}",
            source=source,
            version=version,
            resource_types=(asset_type,),
            inputs=inputs,
            outputs=outputs,
            managed_resource_address=f"{terraform_type}.{resource_name}",
            description=str(result.get("description", "Public Registry module")),
            schema_status="registry-documented",
            registry_kind="public",
            verified_publisher=True,
        )

    @staticmethod
    def _query(resource: Resource) -> str:
        asset_name = resource.type.rsplit("/", 1)[-1]
        words = re.sub(r"(?<!^)(?=[A-Z])", " ", asset_name).lower().strip()
        return words or resource.terraform_type.removeprefix("google_").replace("_", " ")

    def _get(self, path: str) -> dict:
        request = Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json", "User-Agent": "TerraMig/0.1"},
        )
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())


class ReadOnlyStateImporter:
    def __init__(self, organization: str, workspace: str) -> None:
        self.organization = organization
        self.workspace = workspace

    def import_bundle(
        self,
        project_id: str,
        bundle: GenerationBundle,
        *,
        allow_nonempty_workspace: bool = False,
        progress=None,
    ) -> str:
        raise ValueError(
            f"HCP import execution for {self.organization}/{self.workspace} is intentionally disabled. "
            "Enable protected HCP saved-plan submission in Configuration to use the "
            "provisional configuration and saved-plan path."
        )


class HCPRunSubmitter:
    """Upload a provisional configuration and queue a saved, explicitly applied HCP plan."""

    completion_stage = Stage.SUBMITTED
    MAX_CONFIGURATION_ARCHIVE_BYTES = 50 * 1024 * 1024
    MAX_CONFIGURATION_FILES = 1000

    def __init__(
        self,
        hostname: str,
        organization: str,
        workspace: str,
        token: str,
        timeout_seconds: int = 45,
    ) -> None:
        self.hostname = hostname.rstrip("/")
        self.organization = organization
        self.workspace = workspace
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.last_protection: dict = {}

    def import_bundle(
        self,
        project_id: str,
        bundle: GenerationBundle,
        *,
        workflow_id: str = "",
        lifecycle: HCPImportLifecycle | None = None,
        checkpoint=None,
        allow_nonempty_workspace: bool = False,
        progress=None,
    ) -> str:
        emit = progress or (lambda _message: None)
        lifecycle = lifecycle or HCPImportLifecycle()
        lifecycle.workspace_url = (
            f"https://{self.hostname}/app/{quote(self.organization)}"
            f"/workspaces/{quote(self.workspace)}"
        )
        emit(f"HCP workspace: {lifecycle.workspace_url}")
        marker = workflow_id or project_id
        message = f"TerraMig adoption {marker}"
        if lifecycle.run_id:
            emit(f"HCP saved plan already submitted: {lifecycle.run_url or lifecycle.run_id}")
            lifecycle.status = "remote-plan-check"
            self._checkpoint(lifecycle, checkpoint)
            return lifecycle.run_id
        if lifecycle.workspace_id and lifecycle.configuration_version_id:
            recovered_run_id = self._find_run(lifecycle.workspace_id, message)
            if recovered_run_id:
                lifecycle.run_id = recovered_run_id
                lifecycle.run_url = (
                    f"https://{self.hostname}/app/{quote(self.organization)}"
                    f"/workspaces/{quote(self.workspace)}/runs/{recovered_run_id}"
                )
                lifecycle.status = "remote-plan-check"
                emit(f"Recovered HCP run: {lifecycle.run_url}")
                self._checkpoint(lifecycle, checkpoint)
                return recovered_run_id
        protector = HCPWorkspaceProtector(self._get_json)
        emit("Running HCP target protection checks")
        self.last_protection = protector.assert_safe(
            self.organization,
            self.workspace,
            allow_nonempty_workspace=allow_nonempty_workspace,
        )
        workspace_id = self.last_protection["workspace_id"]
        emit(f"HCP target protection passed for workspace {workspace_id}")
        if self.last_protection.get("nonempty_workspace_override"):
            emit(
                "HCP non-empty workspace exception accepted for "
                f"{self.last_protection['resource_count']} existing managed resources"
            )
        preserved_files: dict[str, bytes] = {}
        preservation: dict = {}
        if self.last_protection.get("nonempty_workspace_override"):
            emit("Retrieving the current HCP configuration for incremental adoption")
            preserved_files, preservation = self._current_configuration(
                workspace_id
            )
            self._record_preservation(preservation)
            emit(
                "Preserving "
                f"{preservation['preserved_configuration_file_count']} files from "
                f"HCP configuration {preservation['preserved_configuration_version_id']}"
            )
        lifecycle.workspace_id = workspace_id
        if not lifecycle.configuration_version_id:
            lifecycle.status = "preflight-passed"
        self._checkpoint(lifecycle, checkpoint)
        configuration_id = lifecycle.configuration_version_id
        upload_url = ""
        if not configuration_id:
            emit("Creating provisional HCP configuration version")
            configuration = self._post_json(
                f"/api/v2/workspaces/{workspace_id}/configuration-versions",
                {
                    "data": {
                        "type": "configuration-versions",
                        "attributes": {"auto-queue-runs": False, "provisional": True},
                    }
                },
            )
            configuration_id = configuration["data"]["id"]
            lifecycle.configuration_version_id = configuration_id
            lifecycle.status = "configuration-created"
            emit(f"HCP configuration version created: {configuration_id}")
            self._checkpoint(lifecycle, checkpoint)
            upload_url = configuration["data"]["attributes"]["upload-url"]
        if lifecycle.status != "configuration-uploaded":
            if not upload_url:
                configuration = self._get_json(
                    f"/api/v2/configuration-versions/{configuration_id}"
                )
                configuration_attributes = configuration["data"]["attributes"]
                if configuration_attributes.get("status") == "uploaded":
                    lifecycle.status = "configuration-uploaded"
                    self._checkpoint(lifecycle, checkpoint)
                else:
                    upload_url = configuration_attributes.get("upload-url", "")
                    if not upload_url:
                        raise RuntimeError(
                            "HCP configuration version cannot be resumed because its upload URL is unavailable"
                        )
            if upload_url:
                emit("Uploading merged Terraform configuration to HCP Terraform")
                self._put_bytes(
                    upload_url,
                    self.bundle_archive(
                        bundle,
                        preserved_files=preserved_files,
                        workflow_id=marker,
                    ),
                )
                self._wait_uploaded(configuration_id)
                lifecycle.status = "configuration-uploaded"
                emit("HCP configuration upload completed")
                self._checkpoint(lifecycle, checkpoint)
        # Recheck immediately before queuing the saved plan so a concurrent run or
        # state change that happened during upload fails closed.
        emit("Re-running HCP target protection immediately before submission")
        self.last_protection = protector.assert_safe(
            self.organization,
            self.workspace,
            allow_nonempty_workspace=allow_nonempty_workspace,
        )
        if preservation:
            current_configuration_id = self._current_configuration_version_id(
                workspace_id
            )
            preserved_configuration_id = preservation[
                "preserved_configuration_version_id"
            ]
            if current_configuration_id != preserved_configuration_id:
                raise ValueError(
                    "HCP target configuration changed during submission; "
                    "restart the adoption so TerraMig can preserve the latest files"
                )
            self._record_preservation(preservation)
        emit("Final HCP target protection passed")
        if self.last_protection.get("nonempty_workspace_override"):
            emit(
                "HCP non-empty workspace exception reconfirmed immediately before submission"
            )
        run_id = lifecycle.run_id or self._find_run(
            workspace_id, message
        )
        if not run_id:
            emit("Queueing provisional saved plan in HCP Terraform")
            run = self._post_json(
                "/api/v2/runs",
                {
                    "data": {
                        "type": "runs",
                        "attributes": {
                            "message": message,
                            "save-plan": True,
                        },
                        "relationships": {
                            "workspace": {
                                "data": {"type": "workspaces", "id": workspace_id}
                            },
                            "configuration-version": {
                                "data": {
                                    "type": "configuration-versions",
                                    "id": configuration_id,
                                }
                            },
                        },
                    }
                },
            )
            run_id = run["data"]["id"]
        lifecycle.run_id = run_id
        lifecycle.run_url = f"https://{self.hostname}/app/{quote(self.organization)}/workspaces/{quote(self.workspace)}/runs/{run_id}"
        lifecycle.status = "remote-plan-check"
        emit(f"HCP saved plan submitted: {lifecycle.run_url}")
        emit(
            "Waiting for the HCP plan destruction gate; do not apply the run yet"
        )
        self._checkpoint(lifecycle, checkpoint)
        return run_id

    def reconcile(
        self,
        lifecycle: HCPImportLifecycle,
        expected_imports: int,
        *,
        workflow_id: str,
        checkpoint=None,
        progress=None,
    ) -> tuple[HCPImportLifecycle, bool]:
        emit = progress or (lambda _message: None)
        if not lifecycle.run_id or not lifecycle.workspace_id:
            raise ValueError("HCP lifecycle is missing its submitted run or workspace")
        run = self._get_json(f"/api/v2/runs/{lifecycle.run_id}").get("data", {})
        status = run.get("attributes", {}).get("status", "unknown")
        emit(f"HCP import run {lifecycle.run_id} status: {status}")
        if lifecycle.run_url:
            emit(f"HCP run: {lifecycle.run_url}")
        lifecycle.remote_status = status
        lifecycle.checked_at = self._timestamp()
        failed = {"errored", "canceled", "force_canceled", "discarded"}
        if status in failed:
            lifecycle.status = "failed"
            lifecycle.diagnostics.append(f"HCP import run finished with status {status}")
            self._checkpoint(lifecycle, checkpoint)
            raise ValueError(f"HCP import run {lifecycle.run_id} finished with status {status}")
        final_plan_statuses = {
            "planned",
            "planned_and_finished",
            "planned_and_saved",
            "policy_checked",
        }
        destructive_plan = self._destructive_plan(run)
        if destructive_plan:
            count = destructive_plan["destroy"]
            addresses = destructive_plan["addresses"]
            address_text = (
                ": " + ", ".join(addresses)
                if addresses
                else ""
            )
            diagnostic = (
                f"HCP destructive plan blocked: {count} destroy action"
                f"{'' if count == 1 else 's'}{address_text}"
            )
            lifecycle.diagnostics.append(diagnostic)
            emit(diagnostic)
            if status != "applied":
                self._post_empty(
                    f"/api/v2/runs/{lifecycle.run_id}/actions/discard"
                )
                lifecycle.status = "destructive-plan-discarded"
                lifecycle.remote_status = "discarded"
                emit(
                    "TerraMig automatically discarded the HCP run before apply"
                )
            else:
                lifecycle.status = "destructive-plan-applied"
            self._checkpoint(lifecycle, checkpoint)
            raise ValueError(diagnostic)
        if status in final_plan_statuses:
            lifecycle.status = "awaiting-hcp-apply"
            safety_message = (
                "Remote HCP plan passed the destruction gate: zero destroy actions"
            )
            if safety_message not in lifecycle.diagnostics:
                lifecycle.diagnostics.append(safety_message)
            emit(safety_message)
        if status != "applied":
            if status not in final_plan_statuses:
                lifecycle.status = "remote-plan-check"
            self._checkpoint(lifecycle, checkpoint)
            return lifecycle, False

        workspace = self._get_json(
            f"/api/v2/workspaces/{lifecycle.workspace_id}"
        ).get("data", {})
        resource_count = int(
            workspace.get("attributes", {}).get("resource-count") or 0
        )
        lifecycle.imported_resource_count = resource_count
        emit(f"HCP apply completed; workspace now manages {resource_count} resources")
        if resource_count < expected_imports:
            lifecycle.status = "post-import-blocked"
            lifecycle.diagnostics.append(
                f"Workspace manages {resource_count} resources; expected at least {expected_imports}"
            )
            self._checkpoint(lifecycle, checkpoint)
            raise ValueError(
                f"HCP workspace contains {resource_count} resources after apply; "
                f"expected at least {expected_imports}"
            )

        if not lifecycle.post_import_run_id:
            emit("Queueing HCP post-import no-drift plan")
            message = f"TerraMig post-import verification {workflow_id}"
            post_run_id = self._find_run(lifecycle.workspace_id, message)
            if not post_run_id:
                post_run = self._post_json(
                    "/api/v2/runs",
                    {
                        "data": {
                            "type": "runs",
                            "attributes": {
                                "message": message,
                                "plan-only": True,
                            },
                            "relationships": {
                                "workspace": {
                                    "data": {
                                        "type": "workspaces",
                                        "id": lifecycle.workspace_id,
                                    }
                                },
                                "configuration-version": {
                                    "data": {
                                        "type": "configuration-versions",
                                        "id": lifecycle.configuration_version_id,
                                    }
                                },
                            },
                        }
                    },
                )
                post_run_id = post_run["data"]["id"]
            lifecycle.post_import_run_id = post_run_id
            lifecycle.status = "post-import-plan-queued"
            emit(f"HCP post-import plan queued: {post_run_id}")
            self._checkpoint(lifecycle, checkpoint)
            return lifecycle, False

        post_run = self._get_json(
            f"/api/v2/runs/{lifecycle.post_import_run_id}"
        ).get("data", {})
        post_status = post_run.get("attributes", {}).get("status", "unknown")
        emit(f"HCP post-import plan {lifecycle.post_import_run_id} status: {post_status}")
        lifecycle.post_import_status = post_status
        if post_status in failed:
            lifecycle.status = "post-import-blocked"
            lifecycle.diagnostics.append(
                f"Post-import plan finished with status {post_status}"
            )
            self._checkpoint(lifecycle, checkpoint)
            raise ValueError(
                f"HCP post-import verification run finished with status {post_status}"
            )
        completed_plan_statuses = {
            "planned",
            "planned_and_finished",
            "planned_and_saved",
            "policy_checked",
        }
        if post_status not in completed_plan_statuses:
            lifecycle.status = "post-import-planning"
            self._checkpoint(lifecycle, checkpoint)
            return lifecycle, False
        plan_id = (
            post_run.get("relationships", {})
            .get("plan", {})
            .get("data", {})
            .get("id", "")
        )
        if not plan_id:
            lifecycle.status = "post-import-planning"
            self._checkpoint(lifecycle, checkpoint)
            return lifecycle, False
        plan = self._get_json(f"/api/v2/plans/{plan_id}").get("data", {})
        attributes = plan.get("attributes", {})
        counts = {
            "add": int(attributes.get("resource-additions") or 0),
            "change": int(attributes.get("resource-changes") or 0),
            "destroy": int(attributes.get("resource-destructions") or 0),
        }
        lifecycle.drift_free = not any(counts.values())
        lifecycle.diagnostics.append(
            "Post-import HCP plan: "
            f"{counts['add']} add, {counts['change']} change, {counts['destroy']} destroy"
        )
        lifecycle.status = "completed" if lifecycle.drift_free else "drift-detected"
        emit(
            "HCP post-import result: "
            f"{counts['add']} add, {counts['change']} change, {counts['destroy']} destroy"
        )
        self._checkpoint(lifecycle, checkpoint)
        if not lifecycle.drift_free:
            raise ValueError("post-import HCP plan detected configuration drift")
        return lifecycle, True

    @classmethod
    def bundle_archive(
        cls,
        bundle: GenerationBundle,
        *,
        preserved_files: dict[str, bytes] | None = None,
        workflow_id: str = "",
    ) -> bytes:
        files = dict(preserved_files or {})
        generated = {
            name: content.encode() for name, content in terraform_files(bundle).items()
        }
        generated["imports.tf"] = LocalTerraformVerifier.import_blocks(bundle).encode()
        existing_hcl = "\n".join(
            content.decode(errors="ignore")
            for name, content in files.items()
            if name.endswith((".tf", ".tf.json"))
        )
        if re.search(r"\b(?:cloud\s*\{|backend\s+\")", existing_hcl):
            generated.pop("backend.tf", None)
        if re.search(r'\bprovider\s+"google"\s*\{', existing_hcl):
            generated.pop("providers.tf", None)
        marker = re.sub(r"[^a-z0-9_]", "_", workflow_id.lower())[:40].strip("_")
        marker = marker or "adoption"
        for name, content in generated.items():
            target = name
            if target in files:
                target = f"terramig_{marker}_{PurePosixPath(name).name}"
                suffix = 2
                while target in files:
                    target = (
                        f"terramig_{marker}_{suffix}_{PurePosixPath(name).name}"
                    )
                    suffix += 1
            files[target] = content
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name in sorted(files):
                content = files[name]
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mtime = 0
                archive.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    def _current_configuration(
        self, workspace_id: str
    ) -> tuple[dict[str, bytes], dict]:
        configuration_id = self._current_configuration_version_id(workspace_id)
        archive = self._download_configuration_archive(configuration_id)
        files = self._extract_configuration_archive(archive)
        if not any(name.endswith((".tf", ".tf.json")) for name in files):
            raise RuntimeError(
                "HCP current configuration contains no Terraform files; "
                "refusing a non-empty workspace submission"
            )
        return files, {
            "preserved_configuration_version_id": configuration_id,
            "preserved_configuration_file_count": len(files),
        }

    def _current_configuration_version_id(self, workspace_id: str) -> str:
        state_version = self._get_json(
            f"/api/v2/workspaces/{workspace_id}/current-state-version"
        ).get("data", {})
        run_id = (
            state_version.get("relationships", {})
            .get("run", {})
            .get("data", {})
            .get("id", "")
        )
        if not run_id:
            raise RuntimeError(
                "HCP current state is not linked to a run; TerraMig cannot "
                "preserve the configuration for a non-empty workspace"
            )
        run = self._get_json(f"/api/v2/runs/{run_id}").get("data", {})
        configuration_id = (
            run.get("relationships", {})
            .get("configuration-version", {})
            .get("data", {})
            .get("id", "")
        )
        if not configuration_id:
            raise RuntimeError(
                "HCP current run is not linked to a configuration version; "
                "refusing a non-empty workspace submission"
            )
        return str(configuration_id)

    def _download_configuration_archive(self, configuration_id: str) -> bytes:
        request = Request(
            f"https://{self.hostname}/api/v2/configuration-versions/"
            f"{quote(configuration_id)}/download",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urlopen(request, timeout=60) as response:
            content = response.read(self.MAX_CONFIGURATION_ARCHIVE_BYTES + 1)
        if len(content) > self.MAX_CONFIGURATION_ARCHIVE_BYTES:
            raise RuntimeError(
                "HCP current configuration archive exceeds the TerraMig safety limit"
            )
        return content

    @classmethod
    def _extract_configuration_archive(cls, content: bytes) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        total_size = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
                members = archive.getmembers()
                if len(members) > cls.MAX_CONFIGURATION_FILES:
                    raise RuntimeError(
                        "HCP current configuration contains too many files"
                    )
                for member in members:
                    if member.issym() or member.islnk():
                        raise RuntimeError(
                            "HCP current configuration contains a symbolic link"
                        )
                    if not member.isfile():
                        continue
                    path = PurePosixPath(member.name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or not path.parts
                    ):
                        raise RuntimeError(
                            "HCP current configuration contains an unsafe path"
                        )
                    normalized = str(path)
                    if path.parts[0] in {".git", ".terraform"}:
                        continue
                    if normalized.endswith((".tfstate", ".tfplan")):
                        continue
                    if normalized in files:
                        raise RuntimeError(
                            "HCP current configuration contains a duplicate path"
                        )
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    value = extracted.read(
                        cls.MAX_CONFIGURATION_ARCHIVE_BYTES + 1
                    )
                    total_size += len(value)
                    if (
                        len(value) > cls.MAX_CONFIGURATION_ARCHIVE_BYTES
                        or total_size > cls.MAX_CONFIGURATION_ARCHIVE_BYTES
                    ):
                        raise RuntimeError(
                            "HCP current configuration expands beyond the "
                            "TerraMig safety limit"
                        )
                    files[normalized] = value
        except tarfile.TarError as error:
            raise RuntimeError(
                "HCP current configuration archive is invalid"
            ) from error
        return files

    def _record_preservation(self, preservation: dict) -> None:
        self.last_protection.update(preservation)
        check = (
            "preserved "
            f"{preservation['preserved_configuration_file_count']} files from "
            "the current HCP configuration"
        )
        if check not in self.last_protection["checks"]:
            self.last_protection["checks"].append(check)

    def _destructive_plan(self, run: dict) -> dict | None:
        plan_id = (
            run.get("relationships", {})
            .get("plan", {})
            .get("data", {})
            .get("id", "")
        )
        if not plan_id:
            return None
        plan = self._get_json(f"/api/v2/plans/{plan_id}").get("data", {})
        count = int(
            plan.get("attributes", {}).get("resource-destructions") or 0
        )
        if not count:
            return None
        addresses: list[str] = []
        try:
            output = self._get_json(f"/api/v2/plans/{plan_id}/json-output")
            addresses = [
                str(change.get("address", ""))
                for change in output.get("resource_changes", [])
                if "delete" in change.get("change", {}).get("actions", [])
                and change.get("address")
            ][:20]
        except Exception:
            # The aggregate count is authoritative even when the token cannot
            # access JSON plan output.
            pass
        return {"plan_id": plan_id, "destroy": count, "addresses": addresses}

    def _wait_uploaded(self, configuration_id: str) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            payload = self._get_json(f"/api/v2/configuration-versions/{configuration_id}")
            status = payload["data"]["attributes"]["status"]
            if status == "uploaded":
                return
            if status == "errored":
                raise RuntimeError("HCP Terraform rejected the uploaded configuration version")
            time.sleep(1)
        raise TimeoutError("HCP Terraform did not process the configuration version in time")

    def _find_run(self, workspace_id: str, message: str) -> str:
        payload = self._get_json(
            f"/api/v2/workspaces/{workspace_id}/runs?page%5Bsize%5D=100"
        )
        for item in payload.get("data", []):
            if item.get("attributes", {}).get("message") == message:
                return str(item.get("id", ""))
        return ""

    @staticmethod
    def _checkpoint(lifecycle: HCPImportLifecycle, checkpoint) -> None:
        if checkpoint:
            checkpoint(lifecycle)

    @staticmethod
    def _timestamp() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    def _get_json(self, path: str) -> dict:
        return self._request(path, "GET")

    def _post_json(self, path: str, payload: dict) -> dict:
        return self._request(path, "POST", json.dumps(payload).encode())

    def _post_empty(self, path: str) -> dict:
        return self._request(path, "POST", b"")

    def _request(self, path: str, method: str, data: bytes | None = None) -> dict:
        url = path if path.startswith("http") else f"https://{self.hostname}{path}"
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/vnd.api+json",
            },
        )
        with urlopen(request, timeout=30) as response:
            body = response.read()
        return json.loads(body) if body else {}

    @staticmethod
    def _put_bytes(url: str, content: bytes) -> None:
        request = Request(
            url,
            data=content,
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urlopen(request, timeout=60) as response:
            response.read()
