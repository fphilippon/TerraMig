from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain import ModuleVersion
from ..ports import ModuleRegistry
from .assurance import canonical_sha256
from .capabilities import CapabilityCatalog


CATALOG_SCHEMA_VERSION = 1
MODULE_SOURCE_PATTERN = re.compile(
    r"[A-Za-z0-9.-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"
)
EXACT_VERSION_PATTERN = re.compile(
    r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
BLOCK_PATTERN = re.compile(
    r'(?m)^\s*(variable|output|resource)\s+"([^"]+)"(?:\s+"([^"]+)")?\s*\{'
)


@dataclass(frozen=True)
class ModuleInspection:
    resource_types: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    managed_resource_address: str
    schema_status: str
    diagnostics: tuple[str, ...] = ()
    managed_resource_addresses: tuple[str, ...] = ()


class TerraformModuleInspector:
    """Download an exact HCP-library module version and inspect its root module."""

    def __init__(
        self,
        hostname: str,
        token: str,
        terraform_binary: str = "terraform",
        timeout_seconds: int = 180,
        capability_catalog: CapabilityCatalog | None = None,
    ) -> None:
        self.hostname = hostname.removeprefix("https://").removeprefix("http://").rstrip("/")
        self.token = token
        self.terraform_binary = terraform_binary
        self.timeout_seconds = timeout_seconds
        self.capabilities = capability_catalog or CapabilityCatalog()

    def inspect(self, module: ModuleVersion) -> ModuleInspection:
        source_hostname = module.source.split("/", 1)[0]
        if not MODULE_SOURCE_PATTERN.fullmatch(module.source) or source_hostname not in {
            self.hostname,
            "registry.terraform.io",
        }:
            raise ValueError("module source has an invalid registry address")
        if not EXACT_VERSION_PATTERN.fullmatch(module.version):
            raise ValueError("private-module version must be an exact semantic version")
        with tempfile.TemporaryDirectory(prefix="terramig-catalog-") as directory:
            root = Path(directory)
            (root / "main.tf").write_text(
                'module "catalog" {\n'
                f'  source  = "{module.source}"\n'
                f'  version = "{module.version}"\n'
                "}\n"
            )
            process = subprocess.run(
                [
                    self.terraform_binary,
                    "init",
                    "-backend=false",
                    "-input=false",
                    "-no-color",
                ],
                cwd=root,
                env={
                    **os.environ,
                    "TF_IN_AUTOMATION": "1",
                    "TF_INPUT": "0",
                    self._token_variable(): self.token,
                },
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            detail = process.stderr.strip() or process.stdout.strip()
            manifest_path = root / ".terraform/modules/modules.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text())
                entry = next(
                    (
                        item
                        for item in manifest.get("Modules", [])
                        if item.get("Key") == "catalog"
                    ),
                    None,
                )
                if not entry or not entry.get("Dir"):
                    raise RuntimeError(
                        f"Terraform did not resolve {module.source}@{module.version}"
                    )
                module_root = (root / entry["Dir"]).resolve()
            else:
                module_root = (root / ".terraform/modules/catalog").resolve()
                if not process.returncode or not module_root.is_dir():
                    raise RuntimeError(
                        f"could not download {module.source}@{module.version}: {detail[-1200:]}"
                    )
            if root.resolve() not in module_root.parents:
                raise RuntimeError("Terraform module path escaped the catalog workspace")
            inspection = self.inspect_directory(module_root)
            if not process.returncode:
                return inspection
            error_summary = next(
                (
                    line.strip().removeprefix("Error:").strip()
                    for line in detail.splitlines()
                    if line.strip().startswith("Error:")
                ),
                "Terraform initialization failed",
            )
            return ModuleInspection(
                resource_types=inspection.resource_types,
                inputs=inspection.inputs,
                outputs=inspection.outputs,
                managed_resource_address="",
                schema_status="incompatible",
                diagnostics=(
                    *inspection.diagnostics,
                    f"Terraform compatibility check failed: {error_summary}",
                ),
                managed_resource_addresses=(
                    inspection.managed_resource_addresses
                ),
            )

    def inspect_directory(self, module_root: Path) -> ModuleInspection:
        variables: set[str] = set()
        outputs: set[str] = set()
        resources: list[tuple[str, str, bool]] = []
        for terraform_file in sorted(module_root.glob("*.tf")):
            content = terraform_file.read_text(errors="replace")
            for match in BLOCK_PATTERN.finditer(content):
                kind, first, second = match.groups()
                if kind == "variable":
                    variables.add(first)
                elif kind == "output":
                    outputs.add(first)
                elif kind == "resource" and second:
                    body = self._block_body(content, match.end() - 1)
                    resources.append(
                        (
                            first,
                            second,
                            bool(
                                re.search(
                                    r"(?m)^\s*(?:count|for_each)\s*=", body
                                )
                            ),
                        )
                    )

        reverse = {
            capability.terraform_type: capability.asset_type
            for capability in self.capabilities.capabilities.values()
            if capability.terraform_type and capability.importable
        }
        asset_types = tuple(
            sorted(
                {
                    reverse[resource_type]
                    for resource_type, _, _ in resources
                    if resource_type in reverse
                }
            )
        )
        diagnostics: list[str] = []
        managed_addresses = tuple(
            sorted(
                f"{resource_type}.{resource_name}"
                for resource_type, resource_name, _ in resources
            )
        )
        if len(resources) == 1 and asset_types and not resources[0][2]:
            managed_address = f"{resources[0][0]}.{resources[0][1]}"
            status = "verified"
        else:
            managed_address = ""
            status = "needs-review"
            if not resources:
                diagnostics.append("No root managed resource block was found.")
            elif len(resources) > 1:
                diagnostics.append(
                    "Multiple root managed resources require an explicit ownership address."
                )
            elif resources[0][2]:
                diagnostics.append(
                    "The root managed resource uses count or for_each and requires an instance-specific ownership address."
                )
            if resources and not asset_types:
                diagnostics.append(
                    "Managed resource types are not present in the TerraMig GCP capability catalog."
                )
        return ModuleInspection(
            resource_types=asset_types,
            inputs=tuple(sorted(variables)),
            outputs=tuple(sorted(outputs)),
            managed_resource_address=managed_address,
            schema_status=status,
            diagnostics=tuple(diagnostics),
            managed_resource_addresses=managed_addresses,
        )

    @staticmethod
    def _block_body(content: str, opening_brace: int) -> str:
        depth = 0
        for index in range(opening_brace, len(content)):
            if content[index] == "{":
                depth += 1
            elif content[index] == "}":
                depth -= 1
                if depth == 0:
                    return content[opening_brace + 1 : index]
        return content[opening_brace + 1 :]

    def _token_variable(self) -> str:
        return f"TF_TOKEN_{re.sub(r'[^A-Za-z0-9]', '_', self.hostname)}"


class ModuleCatalogBuilder:
    def __init__(
        self,
        registry: ModuleRegistry,
        inspector: TerraformModuleInspector,
        organization: str,
        provider_version: str,
    ) -> None:
        self.registry = registry
        self.inspector = inspector
        self.organization = organization
        self.provider_version = provider_version

    def build(self, signing_key: str = "", progress=None) -> dict[str, Any]:
        modules: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for module in self.registry.list_modules():
            if progress:
                progress(f"Inspecting {module.source}@{module.version}")
            if not module.version or module.version == "unavailable":
                failures.append(
                    {
                        "source": module.source,
                        "version": module.version,
                        "error": "No usable module version is available.",
                    }
                )
                continue
            try:
                inspection = self.inspector.inspect(module)
            except Exception as error:
                failures.append(
                    {
                        "source": module.source,
                        "version": module.version,
                        "error": str(error),
                    }
                )
                if progress:
                    progress(f"Inspection failed for {module.source}: {error}")
                continue
            record = {
                "id": module.id,
                "source": module.source,
                "version": module.version,
                "description": module.description,
                **asdict(inspection),
            }
            if module.registry_kind in {"private", "hcp-public"}:
                record["trust_status"] = "trusted"
                if record["schema_status"] != "incompatible":
                    record["schema_status"] = "trusted"
            for field in (
                "resource_types",
                "inputs",
                "outputs",
                "diagnostics",
                "managed_resource_addresses",
            ):
                record[field] = list(record[field])
            modules.append(record)
            if progress:
                progress(
                    f"{module.source}@{module.version}: {record['schema_status']}"
                )

        payload: dict[str, Any] = {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "organization": self.organization,
            "provider": "hashicorp/google",
            "provider_version": self.provider_version,
            "modules": sorted(modules, key=lambda item: (item["source"], item["version"])),
            "failures": sorted(failures, key=lambda item: item["source"]),
        }
        digest = canonical_sha256(payload)
        integrity: dict[str, str] = {"sha256": digest}
        if signing_key:
            integrity["hmac_sha256"] = hmac.new(
                signing_key.encode(), digest.encode(), hashlib.sha256
            ).hexdigest()
        payload["integrity"] = integrity
        return payload


def validate_catalog(
    payload: dict[str, Any],
    *,
    signing_key: str = "",
    require_signature: bool = False,
) -> dict[str, Any]:
    if payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("unsupported TerraMig module catalog schema")
    integrity = payload.get("integrity", {})
    expected = integrity.get("sha256", "")
    unsigned = {key: value for key, value in payload.items() if key != "integrity"}
    actual = canonical_sha256(unsigned)
    if not expected or not hmac.compare_digest(expected, actual):
        raise ValueError("module catalog integrity digest does not match its contents")
    signature = integrity.get("hmac_sha256", "")
    if require_signature and not signing_key:
        raise ValueError("catalog signature verification key is required")
    if signing_key:
        wanted = hmac.new(
            signing_key.encode(), expected.encode(), hashlib.sha256
        ).hexdigest()
        if not signature or not hmac.compare_digest(signature, wanted):
            raise ValueError("module catalog signature is missing or invalid")
    elif require_signature and not signature:
        raise ValueError("a signed module catalog is required")
    modules = payload.get("modules")
    if not isinstance(modules, list):
        raise ValueError("module catalog modules must be a list")
    seen: set[tuple[str, str]] = set()
    for module in modules:
        if not isinstance(module, dict):
            raise ValueError("module catalog entries must be objects")
        required = {
            "source",
            "version",
            "resource_types",
            "inputs",
            "outputs",
            "managed_resource_address",
            "schema_status",
        }
        missing = required - set(module)
        if missing:
            raise ValueError(
                f"module catalog entry is incomplete: {', '.join(sorted(missing))}"
            )
        source = module["source"]
        version = module["version"]
        if (
            not isinstance(source, str)
            or not MODULE_SOURCE_PATTERN.fullmatch(source)
            or not isinstance(version, str)
            or not EXACT_VERSION_PATTERN.fullmatch(version)
        ):
            raise ValueError("module catalog contains an invalid source or version")
        identity = (source, version)
        if identity in seen:
            raise ValueError("module catalog contains a duplicate source and version")
        seen.add(identity)
        if module["schema_status"] not in {
            "verified",
            "trusted",
            "needs-review",
            "incompatible",
        }:
            raise ValueError("module catalog contains an invalid schema status")
        for field in (
            "resource_types",
            "inputs",
            "outputs",
            "managed_resource_addresses",
        ):
            if field == "managed_resource_addresses" and field not in module:
                continue
            if not isinstance(module[field], list) or not all(
                isinstance(item, str) for item in module[field]
            ):
                raise ValueError(f"module catalog {field} must be a list of strings")
    return payload
