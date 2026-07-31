from __future__ import annotations

from typing import Callable

import json
from pathlib import Path
from uuid import uuid4

from ..domain import GenerationBundle, ImportOperation, ModuleVersion, Resource, VerificationResult
from ..services.capabilities import CapabilityCatalog


class FixtureInventory:
    def __init__(self, path: Path) -> None:
        self.data = json.loads(path.read_text())
        self.capabilities = CapabilityCatalog()

    def list_projects(self) -> list[dict[str, str]]:
        return [
            {"id": project["id"], "name": project["name"]}
            for project in self.data["projects"]
        ]

    def discover(
        self,
        project_id: str,
        progress: Callable[[str], None] | None = None,
    ) -> list[Resource]:
        if progress:
            progress(f"Loading fixture inventory for {project_id}")
        project = next(
            (item for item in self.data["projects"] if item["id"] == project_id),
            None,
        )
        if project is None:
            raise ValueError(f"fixture project {project_id} does not exist")
        resources: list[Resource] = []
        for item in project["resources"]:
            assessment = self.capabilities.assess(
                item["type"],
                item["id"],
                item["attributes"],
                configuration_complete=True,
            )
            resources.append(
                Resource(
                    id=item["id"],
                    type=item["type"],
                    name=item["name"],
                    location=item["location"],
                    attributes=item["attributes"],
                    dependency_ids=tuple(item.get("dependency_ids", [])),
                    terraform_type=assessment.terraform_type,
                    import_id=assessment.import_id,
                    support_status=assessment.status,
                    support_reason=assessment.reason,
                    provider_version=assessment.provider_version,
                )
            )
        if progress:
            progress(f"Fixture inventory returned {len(resources)} resources")
        return resources


class FixtureRegistry:
    def __init__(self, path: Path) -> None:
        self.data = json.loads(path.read_text())

    def list_modules(self) -> list[ModuleVersion]:
        return [
            ModuleVersion(
                id=item["id"],
                source=item["source"],
                version=item["version"],
                resource_types=tuple(item["resource_types"]),
                inputs=tuple(item["inputs"]),
                outputs=tuple(item.get("outputs", [])),
                managed_resource_address=item.get("managed_resource_address", ""),
                description=item.get("description", ""),
            )
            for item in self.data["modules"]
        ]


class DeterministicGenerationAgent:
    """Offline stand-in that obeys the same structured contract as a real AI agent."""

    def generate(
        self, prompt: dict, progress: Callable[[str], None] | None = None
    ) -> GenerationBundle:
        progress_log: list[str] = []

        def emit(message: str) -> None:
            progress_log.append(message)
            if progress:
                progress(message)

        emit(
            f"AI composition: normalized {len(prompt['resources'])} resources in dependency order"
        )
        terraform_lines = ['terraform {', '  required_version = ">= 1.7.0"']
        target = prompt.get("hcp_target", {})
        if target.get("organization") and target.get("workspace"):
            terraform_lines.extend(
                [
                    "  cloud {",
                    f'    hostname     = "{target.get("hostname", "app.terraform.io")}"',
                    f'    organization = "{target["organization"]}"',
                    "    workspaces {",
                    f'      name = "{target["workspace"]}"',
                    "    }",
                    "  }",
                ]
            )
        terraform_lines.append("}")
        blocks: list[str] = [
            "\n".join(terraform_lines),
            'provider "google" {\n  project = var.project_id\n}',
            'variable "project_id" {\n  type = string\n}',
        ]
        imports: list[ImportOperation] = []
        decisions: list[str] = []
        warnings: list[str] = []

        candidate_count = sum(
            len(item["module_candidates"]) for item in prompt["resources"]
        )
        emit(
            f"AI composition: evaluating {candidate_count} operator-selected module candidates"
        )

        references: dict[str, dict] = {}
        for resource in prompt["resources"]:
            safe_name = resource["name"].replace("-", "_")
            candidate = resource["module_candidates"][0] if resource["module_candidates"] else None
            if candidate:
                module_address = f'module.{safe_name}'
                inputs = self._render_inputs(resource, candidate, references)
                blocks.append(
                    f'module "{safe_name}" {{\n'
                    f'  source  = "{candidate["source"]}"\n'
                    f'  version = "{candidate["version"]}"\n{inputs}\n}}'
                )
                decisions.append(
                    f'{resource["name"]}: selected {candidate["source"]} '
                    f'(shortlist score {candidate["score"]})'
                )
                managed_address = candidate.get("managed_resource_address") or next(
                    (
                        address
                        for address in candidate.get(
                            "managed_resource_addresses", []
                        )
                        if address.startswith(
                            f'{resource["terraform_type"]}.'
                        )
                    ),
                    "",
                )
                if not managed_address:
                    raise ValueError(
                        f'{candidate["source"]}@{candidate["version"]} does not declare a managed resource address'
                    )
                address = f"{module_address}.{managed_address}"
                references[resource["id"]] = {
                    "module": module_address,
                    "outputs": candidate.get("outputs", []),
                }
            else:
                terraform_type = resource["terraform_type"]
                if not terraform_type:
                    raise ValueError(
                        f'{resource["type"]} has no verified Terraform resource mapping'
                    )
                address = f"{terraform_type}.{safe_name}"
                blocks.append(
                    f'# No approved registry module matched {resource["type"]}.\n'
                    f'resource "{terraform_type}" "{safe_name}" {{\n'
                    f'  name = "{resource["name"]}"\n'
                    f'}}'
                )
                warnings.append(f'{resource["name"]}: raw resource fallback requires review')
            remote_id = resource["import_id"]
            imports.append(
                ImportOperation(
                    address=address,
                    remote_id=remote_id,
                    command=f"terraform import '{address}' '{remote_id}'",
                )
            )
        emit(
            f"AI composition: selected modules for {len(decisions)} resources and direct resources for {len(warnings)}"
        )
        emit(
            f"AI assurance: produced {len(imports)} deterministic import address mappings"
        )
        return GenerationBundle(
            "\n\n".join(blocks) + "\n",
            imports,
            decisions,
            warnings,
            agent="fixture-agent",
            duration_ms=42,
            composition_log=progress_log,
        )

    @staticmethod
    def _render_inputs(resource: dict, candidate: dict, references: dict[str, dict]) -> str:
        attributes = dict(resource["attributes"])
        for dependency_id in resource["dependencies"]:
            dependency = references.get(dependency_id)
            if not dependency:
                continue
            for input_name, preferred_output in (
                ("network", "network_self_link"),
                ("subnetwork", "subnetwork_self_link"),
                ("topic", "topic_id"),
                ("service_account", "email"),
                ("kms_key_name", "crypto_key_id"),
            ):
                if input_name in attributes and preferred_output in dependency["outputs"]:
                    attributes[input_name] = {
                        "reference": f'{dependency["module"]}.{preferred_output}'
                    }
        lines: list[str] = []
        for input_name in candidate["inputs"]:
            if input_name not in attributes:
                continue
            value = attributes[input_name]
            rendered = value["reference"] if isinstance(value, dict) and "reference" in value else json.dumps(value)
            lines.append(f"  {input_name} = {rendered}")
        return "\n".join(lines)


class MockStateImporter:
    def __init__(self, organization: str = "", workspace: str = "") -> None:
        self.runs: list[dict] = []
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
        if progress:
            progress(f"Fixture HCP import prepared for {len(bundle.imports)} resources")
        if not bundle.imports:
            raise ValueError("refusing an import bundle with no operations")
        run_id = f"run-mock-{uuid4().hex[:8]}"
        self.runs.append(
            {
                "id": run_id,
                "project_id": project_id,
                "organization": self.organization,
                "workspace": self.workspace,
                "operations": len(bundle.imports),
            }
        )
        if progress:
            progress(f"Fixture HCP import completed as {run_id}")
        return run_id


class MockTerraformVerifier:
    def verify(self, project_id: str, bundle: GenerationBundle, progress=None) -> VerificationResult:
        commands = [
            "terraform init -backend=false -input=false",
            "terraform validate -json",
            "terraform plan -input=false -refresh=true -detailed-exitcode -out=adoption.tfplan",
            "terraform show -json adoption.tfplan",
        ]
        if progress:
            for command in commands:
                progress(f"$ {command}")
            progress(
                f"Fixture Terraform plan: {len(bundle.imports)} imports, 0 drift-producing actions"
            )
        return VerificationResult(
            success=bool(bundle.terraform.strip() and bundle.imports),
            drift_detected=False,
            terraform_version="fixture-1.14",
            commands=commands,
            summary=f"Import-only plan verified for {len(bundle.imports)} resources; no configuration drift detected.",
            diagnostics=[
                "Configuration syntax and module input contract accepted",
                "All remote objects map to unique Terraform addresses",
                "Plan contains imports only; no create, update, or delete actions",
            ],
            output=(
                "Fixture terraform validate: success\n"
                f"Fixture local plan: {len(bundle.imports)} to import, 0 to add, 0 to change, 0 to destroy"
            ),
            planned_imports=len(bundle.imports),
        )
