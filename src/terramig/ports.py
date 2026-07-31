from __future__ import annotations

from typing import Callable, Protocol

from .domain import GenerationBundle, GitDelivery, ModuleVersion, Resource, VerificationResult


class InventoryProvider(Protocol):
    def list_projects(self) -> list[dict[str, str]]: ...
    def discover(
        self, project_id: str, progress: Callable[[str], None] | None = None
    ) -> list[Resource]: ...


class ModuleRegistry(Protocol):
    def list_modules(self) -> list[ModuleVersion]: ...


class PublicModuleRegistry(Protocol):
    def search_modules(self, resources: list[Resource]) -> list[ModuleVersion]: ...


class GenerationAgent(Protocol):
    def generate(
        self, prompt: dict, progress: Callable[[str], None] | None = None
    ) -> GenerationBundle: ...


class TerraformRepairAgent(Protocol):
    def repair_after_verification(
        self,
        task: dict,
        bundle: GenerationBundle,
        verification: VerificationResult,
        progress: Callable[[str], None] | None = None,
    ) -> GenerationBundle: ...


class GenerationContextProvider(Protocol):
    def enrich(self, prompt: dict) -> dict: ...


class StateImporter(Protocol):
    def import_bundle(
        self,
        project_id: str,
        bundle: GenerationBundle,
        *,
        allow_nonempty_workspace: bool = False,
        progress=None,
    ) -> str: ...


class TerraformVerifier(Protocol):
    def verify(
        self, project_id: str, bundle: GenerationBundle, progress=None
    ) -> VerificationResult: ...


class GitDeliveryProvider(Protocol):
    def deliver(
        self,
        workflow_id: str,
        bundle: GenerationBundle,
        delivery: GitDelivery,
        progress=None,
    ) -> GitDelivery: ...
