from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable


CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "google_provider_7_39_0_capabilities.json"
)

BASE_GCP_DOMAIN_SERVICES = {
    "compute": frozenset(
        {
            "compute.googleapis.com",
            "container.googleapis.com",
            "servicenetworking.googleapis.com",
        }
    ),
    "storage": frozenset(
        {
            "file.googleapis.com",
            "netapp.googleapis.com",
            "storage.googleapis.com",
        }
    ),
    "databases": frozenset(
        {
            "alloydb.googleapis.com",
            "bigtableadmin.googleapis.com",
            "firestore.googleapis.com",
            "memcache.googleapis.com",
            "memorystore.googleapis.com",
            "redis.googleapis.com",
            "spanner.googleapis.com",
            "sqladmin.googleapis.com",
        }
    ),
}


@dataclass(frozen=True)
class ResourceCapability:
    asset_type: str
    terraform_type: str = ""
    import_strategy: str = "canonical"
    required_attributes: tuple[str, ...] = ()
    importable: bool = True
    reason: str = ""
    partial_reason: str = ""


@dataclass(frozen=True)
class CapabilityAssessment:
    terraform_type: str
    import_id: str
    status: str
    reason: str
    provider_version: str


class CapabilityCatalog:
    """Versioned, deterministic GCP asset-to-Terraform import knowledge."""

    def __init__(self, path: Path = CATALOG_PATH) -> None:
        payload = json.loads(path.read_text())
        self.provider = payload["provider"]
        self.provider_version = payload["provider_version"]
        self.capabilities = {
            item["asset_type"]: ResourceCapability(
                asset_type=item["asset_type"],
                terraform_type=item.get("terraform_type", ""),
                import_strategy=item.get("import_strategy", "canonical"),
                required_attributes=tuple(item.get("required_attributes", [])),
                importable=item.get("importable", True),
                reason=item.get("reason", ""),
                partial_reason=item.get("partial_reason", ""),
            )
            for item in payload["capabilities"]
        }

    def assess(
        self,
        asset_type: str,
        asset_name: str,
        attributes: dict[str, Any],
        *,
        configuration_complete: bool,
    ) -> CapabilityAssessment:
        capability = self.capabilities.get(asset_type)
        if capability is None:
            return CapabilityAssessment(
                "",
                "",
                "unknown",
                f"No {self.provider}@{self.provider_version} capability mapping is registered.",
                self.provider_version,
            )
        if not capability.importable:
            return CapabilityAssessment(
                capability.terraform_type,
                "",
                "unsupported",
                capability.reason or "The Terraform provider does not support adoption of this asset.",
                self.provider_version,
            )
        limitation = self._adoption_limitation(asset_type, asset_name, attributes)
        if limitation:
            return CapabilityAssessment(
                capability.terraform_type,
                "",
                "unsupported",
                limitation,
                self.provider_version,
            )
        import_id = self.import_id(capability, asset_name, attributes)
        missing = [
            name for name in capability.required_attributes
            if self._lookup(attributes, name) is None
        ]
        if capability.partial_reason:
            reason = capability.partial_reason
            status = "partial"
        elif not configuration_complete:
            reason = "Cloud Asset search metadata is not configuration-complete; fetch RESOURCE content before generation."
            status = "partial"
        elif missing:
            reason = "Missing configuration fields required for safe composition: " + ", ".join(missing)
            status = "partial"
        elif not import_id:
            reason = "A provider-specific import ID could not be derived."
            status = "partial"
        else:
            reason = f"Mapped deterministically to {capability.terraform_type}."
            status = "supported"
        return CapabilityAssessment(
            capability.terraform_type,
            import_id,
            status,
            reason,
            self.provider_version,
        )

    def coverage(self) -> dict[str, Any]:
        totals = self._coverage_counts(self.capabilities.values())
        domains = {}
        for domain, services in BASE_GCP_DOMAIN_SERVICES.items():
            capabilities = [
                capability
                for capability in self.capabilities.values()
                if capability.asset_type.split("/", 1)[0] in services
            ]
            domains[domain] = self._coverage_counts(capabilities)
        return {
            "provider": self.provider,
            "provider_version": self.provider_version,
            "total_asset_types": totals["total"],
            "supported": totals["supported"],
            "partial": totals["partial"],
            "unsupported": totals["unsupported"],
            "domains": domains,
        }

    @staticmethod
    def _coverage_counts(
        capabilities: Iterable[ResourceCapability],
    ) -> dict[str, int]:
        items = tuple(capabilities)
        supported = sum(
            capability.importable and not capability.partial_reason
            for capability in items
        )
        partial = sum(
            capability.importable and bool(capability.partial_reason)
            for capability in items
        )
        unsupported = sum(not capability.importable for capability in items)
        return {
            "total": len(items),
            "supported": supported,
            "partial": partial,
            "unsupported": unsupported,
        }

    @staticmethod
    def import_id(
        capability: ResourceCapability, asset_name: str, attributes: dict[str, Any]
    ) -> str:
        canonical = CapabilityCatalog.canonical_name(asset_name)
        if capability.import_strategy == "bucket":
            explicit = CapabilityCatalog._lookup(attributes, "name")
            return str(explicit or canonical.rsplit("/", 1)[-1])
        if capability.import_strategy == "project":
            explicit = CapabilityCatalog._lookup(attributes, "projectId")
            return str(explicit or canonical.rsplit("/", 1)[-1])
        if capability.import_strategy == "service_networking":
            service = str(
                CapabilityCatalog._lookup(attributes, "service")
                or "servicenetworking.googleapis.com"
            )
            return f"{canonical}:{service}" if canonical else ""
        if capability.import_strategy == "service_account":
            explicit = str(CapabilityCatalog._lookup(attributes, "name") or "")
            if "/serviceAccounts/" in explicit:
                return CapabilityCatalog.canonical_name(explicit)
            email = str(CapabilityCatalog._lookup(attributes, "email") or "")
            project_id = str(CapabilityCatalog._lookup(attributes, "projectId") or "")
            if not project_id:
                match = re.search(r"(?:^|/)projects/([^/]+)(?:/|$)", canonical)
                project_id = match.group(1) if match else ""
            if email and project_id:
                return f"projects/{project_id}/serviceAccounts/{email}"
        return canonical

    @staticmethod
    def _adoption_limitation(
        asset_type: str, asset_name: str, attributes: dict[str, Any]
    ) -> str:
        if asset_type.startswith("compute.googleapis.com/"):
            labels = CapabilityCatalog._lookup(attributes, "labels")
            if isinstance(labels, dict) and any(
                str(key).lower().startswith(("goog-gke-", "goog-k8s-"))
                or str(key).lower().startswith("k8s-io-cluster-")
                for key in labels
            ):
                return (
                    "This Compute resource is owned by GKE. Adopt the "
                    "google_container_cluster or google_container_node_pool that manages it "
                    "instead of importing the generated Compute child independently."
                )
        if asset_type == "compute.googleapis.com/Route":
            if CapabilityCatalog._lookup(attributes, "nextHopNetwork"):
                return (
                    "This is a provider-managed local subnet route. next_hop_network is "
                    "computed by google_compute_subnetwork and cannot be configured or imported "
                    "as an independent google_compute_route."
                )
            route_name = str(CapabilityCatalog._lookup(attributes, "name") or "")
            destination = str(
                CapabilityCatalog._lookup(attributes, "destRange") or ""
            )
            description = str(
                CapabilityCatalog._lookup(attributes, "description") or ""
            )
            gateway = CapabilityCatalog.canonical_name(
                str(CapabilityCatalog._lookup(attributes, "nextHopGateway") or "")
            )
            if (
                route_name.startswith("default-route-")
                and destination == "0.0.0.0/0"
                and description == "Default route to the Internet."
                and gateway.endswith("/global/gateways/default-internet-gateway")
            ):
                return (
                    "This is the provider-managed default internet route created with the "
                    "VPC network. Preserve it through google_compute_network with "
                    "delete_default_routes_on_create disabled instead of importing it as an "
                    "independent google_compute_route."
                )
        if asset_type != "iam.googleapis.com/ServiceAccount":
            return ""
        email = str(CapabilityCatalog._lookup(attributes, "email") or "")
        if not email and "/serviceAccounts/" in asset_name:
            email = asset_name.rsplit("/serviceAccounts/", 1)[-1]
        if not email or "@" not in email:
            return ""
        account_id, domain = email.split("@", 1)
        project_id = str(CapabilityCatalog._lookup(attributes, "projectId") or "")
        if not project_id:
            canonical = CapabilityCatalog.canonical_name(asset_name)
            match = re.search(r"(?:^|/)projects/([^/]+)(?:/|$)", canonical)
            project_id = match.group(1) if match else ""
        expected_domain = f"{project_id}.iam.gserviceaccount.com" if project_id else ""
        valid_account_id = (
            6 <= len(account_id) <= 30
            and bool(re.fullmatch(r"[a-z](?:[-a-z0-9]*[a-z0-9])?", account_id))
        )
        if not valid_account_id or (expected_domain and domain != expected_domain):
            return (
                "This is a Google-managed default service account or service agent. "
                "google_service_account only manages user-created service accounts; "
                "the platform-managed identity must remain implicit."
            )
        return ""

    @staticmethod
    def canonical_name(value: str) -> str:
        if value.startswith("//"):
            return value[2:].split("/", 1)[1] if "/" in value[2:] else ""
        for prefix in (
            "https://www.googleapis.com/compute/v1/",
            "https://compute.googleapis.com/compute/v1/",
        ):
            if value.startswith(prefix):
                return value.removeprefix(prefix)
        return value

    @staticmethod
    def _lookup(attributes: dict[str, Any], name: str) -> Any:
        aliases = {
            name,
            name[:1].lower() + name[1:],
            "".join(("_" + char.lower()) if char.isupper() else char for char in name),
        }
        for alias in aliases:
            if alias in attributes and attributes[alias] not in (None, "", [], {}):
                return attributes[alias]
        return None


def discovered_service_coverage(resources: Iterable[Any]) -> dict[str, Any]:
    """Summarize the GCP services actually present in a discovered inventory."""

    services: dict[str, dict[str, Any]] = {}
    total_resources = 0
    for resource in resources:
        asset_type = str(getattr(resource, "type", ""))
        if not asset_type:
            continue
        total_resources += 1
        service = asset_type.split("/", 1)[0]
        status = str(getattr(resource, "support_status", "unknown") or "unknown")
        entry = services.setdefault(
            service,
            {
                "service": service,
                "resources": 0,
                "asset_types": set(),
                "by_status": {},
            },
        )
        entry["resources"] += 1
        entry["asset_types"].add(asset_type)
        entry["by_status"][status] = entry["by_status"].get(status, 0) + 1

    normalized = []
    for entry in services.values():
        normalized.append(
            {
                **entry,
                "asset_types": sorted(entry["asset_types"]),
                "by_status": dict(sorted(entry["by_status"].items())),
            }
        )
    return {
        "total_services": len(normalized),
        "total_resources": total_resources,
        "services": sorted(normalized, key=lambda item: item["service"]),
    }


def dependency_ids(
    resource_id: str,
    parent_id: str,
    attributes: dict[str, Any],
    known_resources: Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    """Resolve exact resource references from nested API payloads and parent edges."""

    aliases: dict[str, str] = {}
    for known_id, import_id in known_resources:
        aliases[known_id] = known_id
        canonical = CapabilityCatalog.canonical_name(known_id)
        if canonical:
            aliases[canonical] = known_id
            aliases[f"https://www.googleapis.com/compute/v1/{canonical}"] = known_id
        if import_id:
            aliases[import_id] = known_id

    found: set[str] = set()
    if parent_id in aliases:
        candidate = aliases[parent_id]
        if candidate != resource_id:
            found.add(candidate)
    elif CapabilityCatalog.canonical_name(parent_id) in aliases:
        candidate = aliases[CapabilityCatalog.canonical_name(parent_id)]
        if candidate != resource_id:
            found.add(candidate)

    for value in _dependency_strings(attributes):
        candidate = aliases.get(value) or aliases.get(CapabilityCatalog.canonical_name(value))
        if candidate and candidate != resource_id:
            found.add(candidate)
    return tuple(sorted(found))


_NON_DEPENDENCY_KEYS = frozenset(
    {
        # Resource identity and display metadata describe the current object.
        "creationtimestamp",
        "description",
        "displayname",
        "email",
        "fingerprint",
        "id",
        "kind",
        "labelfingerprint",
        "labels",
        "metadata",
        "name",
        "project",
        "projectid",
        "selflink",
        "status",
        "uniqueid",
        # Compute Network reports its children here. Those children depend on
        # the network; treating this reverse collection as an edge creates a cycle.
        "subnetworks",
    }
)


def _dependency_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            normalized_key = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized_key in _NON_DEPENDENCY_KEYS:
                continue
            yield from _dependency_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _dependency_strings(item)
