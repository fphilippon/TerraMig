from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from typing import Callable
from uuid import uuid4

from ..domain import GitDelivery, HCPImportLifecycle, Stage, Workflow
from ..persistence import Persistence
from ..ports import GenerationAgent, GitDeliveryProvider, InventoryProvider, ModuleRegistry, PublicModuleRegistry, StateImporter, TerraformVerifier
from .dependencies import dependency_order
from .artifacts import finalize_terraform_artifacts
from .matching import match_modules
from .prompting import build_generation_prompt


class InvalidTransition(ValueError):
    pass


DIRECT_RESOURCE = "__resource__"
LATEST_COMPATIBLE_MODULES = "latest-compatible"
ALL_DIRECT_RESOURCES = "direct"


def synchronized(function):
    @wraps(function)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return function(self, *args, **kwargs)

    return wrapper


class WorkflowService:
    MAX_AI_VERIFICATION_REPAIRS = 2

    def __init__(
        self,
        inventory: InventoryProvider,
        registry: ModuleRegistry,
        agent: GenerationAgent,
        importer: StateImporter,
        hcp_target: dict[str, str] | None = None,
        verifier: TerraformVerifier | None = None,
        persistence: Persistence | None = None,
        git_delivery_engine: GitDeliveryProvider | None = None,
        require_git_delivery: bool = False,
        public_registry: PublicModuleRegistry | None = None,
        runtime_origin: str = "test",
        required_runtime_origin: str = "",
    ) -> None:
        self.inventory = inventory
        self.registry = registry
        self.agent = agent
        self.importer = importer
        self.hcp_target = hcp_target or {}
        self.verifier = verifier
        self.persistence = persistence
        self.git_delivery_engine = git_delivery_engine
        self.require_git_delivery = require_git_delivery
        self.public_registry = public_registry
        self.runtime_origin = runtime_origin
        self.required_runtime_origin = required_runtime_origin
        self.lock = RLock()
        restored = persistence.list_workflows() if persistence else []
        self.workflows: dict[str, Workflow] = {
            workflow.id: workflow for workflow in restored
        }

    @synchronized
    def create(self, project_id: str) -> Workflow:
        now = self._now()
        workflow = Workflow(
            id=str(uuid4()),
            project_id=project_id,
            runtime_origin=self.runtime_origin,
            created_at=now,
            updated_at=now,
        )
        workflow.events.append("Workflow created")
        self.workflows[workflow.id] = workflow
        self._save(workflow)
        return workflow

    def get(self, workflow_id: str) -> Workflow:
        if self.persistence:
            persisted = self.persistence.get_workflow(workflow_id)
            if persisted:
                self.workflows[workflow_id] = persisted
        try:
            return self.workflows[workflow_id]
        except KeyError as error:
            raise KeyError(f"unknown workflow {workflow_id}") from error

    @synchronized
    def discover(
        self,
        workflow_id: str,
        progress: Callable[[str], None] | None = None,
    ) -> Workflow:
        workflow = self._at(workflow_id, Stage.CREATED)
        if progress:
            workflow.resources = self.inventory.discover(
                workflow.project_id, progress=progress
            )
        else:
            # Preserve compatibility with external inventory adapters that
            # implement the original single-argument protocol.
            workflow.resources = self.inventory.discover(workflow.project_id)
        if progress:
            progress(f"Resolving dependencies across {len(workflow.resources)} resources")
        ordering = dependency_order(workflow.resources)
        if ordering.cycles:
            cyclic_ids = {
                resource_id
                for component in ordering.cycles
                for resource_id in component
            }
            downgraded_ids = {
                resource.id
                for resource in workflow.resources
                if resource.id in cyclic_ids
                and resource.support_status == "supported"
            }
            workflow.resources = [
                replace(
                    resource,
                    support_status="partial",
                    support_reason=(
                        f"{resource.support_reason} "
                        "A dependency cycle was detected in Cloud Asset metadata; "
                        "manual dependency review is required before adoption."
                    ).strip(),
                )
                if resource.id in cyclic_ids
                and resource.support_status == "supported"
                else resource
                for resource in workflow.resources
            ]
            cycle_summary = (
                f"Collapsed {len(ordering.cycles)} dependency cycle group(s) "
                f"covering {len(cyclic_ids)} resources; "
                + (
                    f"{len(downgraded_ids)} supported resource(s) were changed "
                    "to partial for manual review"
                    if downgraded_ids
                    else "none were supported adoption targets"
                )
            )
            workflow.events.append(cycle_summary)
            if progress:
                progress(cycle_summary)
                for index, component in enumerate(ordering.cycles[:5], start=1):
                    preview = ", ".join(component[:3])
                    suffix = (
                        f", +{len(component) - 3} more"
                        if len(component) > 3
                        else ""
                    )
                    progress(f"Dependency cycle {index}: {preview}{suffix}")
                if len(ordering.cycles) > 5:
                    progress(
                        f"{len(ordering.cycles) - 5} additional dependency cycle "
                        "group(s) detected; live detail is limited to the first 5"
                    )
        workflow.ordered_resource_ids = list(ordering.resource_ids)
        by_id = {resource.id: resource for resource in workflow.resources}
        workflow.managed_resources = self._tracked_managed_resources(workflow)
        workflow.selected_resource_ids = [
            resource_id
            for resource_id in workflow.ordered_resource_ids
            if by_id[resource_id].support_status == "supported"
            and resource_id not in workflow.managed_resources
        ]
        workflow.stage = Stage.DISCOVERED
        workflow.events.append(f"Discovered {len(workflow.resources)} resources")
        if workflow.managed_resources:
            workflow.events.append(
                f"Excluded {len(workflow.managed_resources)} resource(s) previously "
                "imported by a completed, drift-free TerraMig lifecycle"
            )
        coverage: dict[str, int] = {}
        for resource in workflow.resources:
            coverage[resource.support_status] = coverage.get(resource.support_status, 0) + 1
        workflow.events.append(
            "Capability assessment: "
            + ", ".join(f"{count} {status}" for status, count in sorted(coverage.items()))
        )
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    def _tracked_managed_resources(
        self, current_workflow: Workflow
    ) -> dict[str, dict[str, str]]:
        """Return completed TerraMig import evidence for newly discovered resources.

        This is intentionally labelled as lifecycle evidence rather than a live
        HCP state assertion. A future state reconciliation adapter can replace or
        augment these records without changing the workflow/UI contract.
        """
        known_ids = {resource.id for resource in current_workflow.resources}
        ids_by_import_id: dict[str, str] = {}
        duplicate_import_ids: set[str] = set()
        for resource in current_workflow.resources:
            if not resource.import_id:
                continue
            if resource.import_id in ids_by_import_id:
                duplicate_import_ids.add(resource.import_id)
            else:
                ids_by_import_id[resource.import_id] = resource.id
        for import_id in duplicate_import_ids:
            ids_by_import_id.pop(import_id, None)
        workflows = (
            self.persistence.list_workflows()
            if self.persistence
            else list(self.workflows.values())
        )
        tracked: dict[str, dict[str, str]] = {}
        for prior in workflows:
            if (
                prior.id == current_workflow.id
                or prior.project_id != current_workflow.project_id
                or prior.stage != Stage.IMPORTED
                or prior.hcp_import.drift_free is not True
                or not prior.bundle
            ):
                continue
            workspace = str(self.hcp_target.get("workspace", ""))
            organization = str(self.hcp_target.get("organization", ""))
            target = (
                f"{organization}/{workspace}"
                if organization and workspace
                else workspace or "HCP Terraform"
            )
            for operation in prior.bundle.imports:
                resource_id = (
                    operation.resource_id
                    or ids_by_import_id.get(operation.remote_id, "")
                )
                if not resource_id or resource_id not in known_ids:
                    continue
                tracked.setdefault(
                    resource_id,
                    {
                        "status": "tracked-imported",
                        "source": "completed-terramig-lifecycle",
                        "workflow_id": prior.id,
                        "target": target,
                        "address": operation.address,
                        "run_url": prior.hcp_import.run_url,
                        "verified_at": (
                            prior.hcp_import.checked_at or prior.updated_at
                        ),
                    },
                )
        return tracked

    @synchronized
    def match(
        self,
        workflow_id: str,
        selected_resource_ids: list[str] | None = None,
        *,
        include_public: bool = False,
    ) -> Workflow:
        workflow = self.get(workflow_id)
        self._require_current_runtime(workflow)
        if workflow.stage not in {Stage.DISCOVERED, Stage.MATCHED, Stage.GENERATED}:
            raise InvalidTransition(
                f"expected stage discovered, matched, or generated, found {workflow.stage.value}"
            )
        revising_generated = workflow.stage == Stage.GENERATED
        prior_verification = workflow.verification
        prior_selections = dict(workflow.module_selections)
        workflow.selected_resource_ids = self._validated_resource_selection(
            workflow, selected_resource_ids
        )
        selected_resources = self._selected_resources(workflow)
        library_modules = self.registry.list_modules()
        modules = [
            module
            for module in library_modules
            if module.schema_status in {"verified", "trusted"}
        ]
        ignored_library = len(library_modules) - len(modules)
        public_count = 0
        if include_public:
            workflow.public_registry_searched = True
            if self.public_registry:
                public_modules = self.public_registry.search_modules(selected_resources)
                public_modules = [
                    module
                    for module in public_modules
                    if module.schema_status == "registry-documented"
                    and module.registry_kind == "public"
                    and module.verified_publisher
                ]
                public_count = len(public_modules)
                modules.extend(public_modules)
        workflow.candidates = match_modules(selected_resources, modules)
        module_selections: dict[str, str] = {}
        reset_composite = 0
        for resource in selected_resources:
            candidates = workflow.candidates.get(resource.id, [])
            prior = prior_selections.get(resource.id, "")
            prior_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.module.source == prior
                ),
                None,
            )
            if prior == DIRECT_RESOURCE:
                module_selections[resource.id] = DIRECT_RESOURCE
            elif prior_candidate and not (
                revising_generated
                and prior_verification
                and prior_verification.drift_detected
                and not self._is_single_resource_candidate(
                    resource, prior_candidate
                )
            ):
                module_selections[resource.id] = prior
            else:
                safe_candidate = next(
                    (
                        candidate
                        for candidate in candidates
                        if self._is_single_resource_candidate(
                            resource, candidate
                        )
                    ),
                    None,
                )
                module_selections[resource.id] = (
                    safe_candidate.module.source
                    if safe_candidate
                    else DIRECT_RESOURCE
                )
                if prior_candidate and module_selections[resource.id] == DIRECT_RESOURCE:
                    reset_composite += 1
        workflow.module_selections = module_selections
        if revising_generated:
            workflow.bundle = None
            workflow.verification = None
            workflow.drift_accepted = False
            workflow.drift_accepted_at = ""
            workflow.git_delivery = None
            workflow.import_run_id = None
            workflow.target_protection = {}
            workflow.hcp_import = HCPImportLifecycle()
        count = sum(bool(items) for items in workflow.candidates.values())
        selected_modules = sum(
            source != DIRECT_RESOURCE
            for source in workflow.module_selections.values()
        )
        workflow.stage = Stage.MATCHED
        if revising_generated:
            workflow.events.append(
                "Returned to module matching; discarded the generated bundle and validation result"
            )
            if reset_composite:
                workflow.events.append(
                    f"Reset {reset_composite} composite module selection(s) to direct resources after drift"
                )
        workflow.events.append(
            f"Selected {len(selected_resources)} resources for adoption"
        )
        if ignored_library:
            workflow.events.append(
                f"Excluded {ignored_library} HCP library modules that Terraform cannot load"
            )
        if include_public:
            workflow.events.append(
                f"Searched the public Terraform Registry and loaded {public_count} reviewed candidates"
            )
        workflow.events.append(
            f"Found module candidates for {count} resources; "
            f"selected {selected_modules} module representation(s) and "
            f"{len(selected_resources) - selected_modules} direct google resource(s)"
        )
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    @synchronized
    def select_modules(self, workflow_id: str, policy: str) -> Workflow:
        workflow = self._at(workflow_id, Stage.MATCHED)
        if policy not in {LATEST_COMPATIBLE_MODULES, ALL_DIRECT_RESOURCES}:
            raise InvalidTransition(
                "Module selection policy must be latest-compatible or direct"
            )

        selected_resources = self._selected_resources(workflow)
        selections: dict[str, str] = {}
        selected_modules = 0
        composite_fallbacks = 0
        unmatched_fallbacks = 0
        for resource in selected_resources:
            candidates = workflow.candidates.get(resource.id, [])
            safe_candidate = (
                next(
                    (
                        candidate
                        for candidate in candidates
                        if self._is_single_resource_candidate(resource, candidate)
                    ),
                    None,
                )
                if policy == LATEST_COMPATIBLE_MODULES
                else None
            )
            if safe_candidate:
                selections[resource.id] = safe_candidate.module.source
                selected_modules += 1
            else:
                selections[resource.id] = DIRECT_RESOURCE
                if candidates:
                    composite_fallbacks += 1
                else:
                    unmatched_fallbacks += 1

        workflow.module_selections = selections
        if policy == LATEST_COMPATIBLE_MODULES:
            workflow.events.append(
                "Bulk module selection applied: "
                f"{selected_modules} latest compatible module(s), "
                f"{composite_fallbacks + unmatched_fallbacks} direct fallback(s); "
                f"{composite_fallbacks} resource(s) with only composite candidates "
                "remain available for manual review"
            )
        else:
            workflow.events.append(
                f"Bulk module selection applied: {len(selected_resources)} direct "
                "provider resource representation(s)"
            )
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    @synchronized
    def generate(
        self,
        workflow_id: str,
        module_selections: dict[str, str] | None = None,
        progress=None,
    ) -> Workflow:
        workflow = self._at(workflow_id, Stage.MATCHED)
        if module_selections is not None and not isinstance(module_selections, dict):
            raise InvalidTransition("Module selections must be an object keyed by resource ID")
        selected_resources = self._selected_resources(workflow)
        submitted = module_selections or workflow.module_selections
        selected_candidates: dict[str, list] = {}
        normalized_selections: dict[str, str] = {}
        for resource in selected_resources:
            candidates = workflow.candidates.get(resource.id, [])
            source = str(submitted.get(resource.id, "")).strip()
            if source in {"", DIRECT_RESOURCE}:
                selected_candidates[resource.id] = []
                normalized_selections[resource.id] = DIRECT_RESOURCE
            elif candidates:
                chosen = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.module.source == source
                    ),
                    None,
                )
                if not chosen:
                    raise InvalidTransition(
                        f"Select one of the compatible module candidates for {resource.name}"
                    )
                selected_candidates[resource.id] = [chosen]
                normalized_selections[resource.id] = chosen.module.source
            else:
                raise InvalidTransition(
                    f"No compatible module candidate matches {resource.name}; use the direct resource fallback"
                )
        workflow.module_selections = normalized_selections
        workflow.drift_accepted = False
        workflow.drift_accepted_at = ""
        blocked = [
            resource for resource in selected_resources
            if resource.support_status != "supported"
        ]
        if blocked:
            summary = ", ".join(
                f"{resource.name} ({resource.support_status})" for resource in blocked[:5]
            )
            if len(blocked) > 5:
                summary += f", and {len(blocked) - 5} more"
            raise InvalidTransition(
                "AI composition is blocked until every selected resource has a verified "
                f"Terraform mapping, complete configuration, and import ID: {summary}"
            )
        prompt = build_generation_prompt(
            workflow.project_id,
            selected_resources,
            workflow.selected_resource_ids,
            selected_candidates,
            self.hcp_target,
        )

        def record_progress(message: str) -> None:
            if progress:
                progress(message)
            if self._is_audit_progress(message):
                workflow.events.append(message)
                workflow.updated_at = self._now()
                self._save(workflow)

        record_progress(
            f"AI composition queued for {len(selected_resources)} supported resources"
        )
        try:
            workflow.bundle = self.agent.generate(prompt, progress=record_progress)
        except Exception as error:
            record_progress(
                "AI assurance: final proposal rejected · "
                + str(error).replace("\n", " ")[:500]
            )
            record_progress(
                f"AI composition failed before a proposal was accepted ({type(error).__name__})"
            )
            raise
        self._finalize_artifacts(workflow)
        record_progress(
            "Terraform artifacts: generated host-owned backend.tf and providers.tf"
        )
        workflow.stage = Stage.GENERATED
        workflow.events.append(
            f"AI agent generated a reviewable adoption bundle with {len(workflow.bundle.imports)} imports"
        )
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    def _validated_resource_selection(
        self, workflow: Workflow, submitted: list[str] | None
    ) -> list[str]:
        selected = (
            list(submitted)
            if submitted is not None
            else list(workflow.selected_resource_ids or workflow.ordered_resource_ids)
        )
        if submitted is not None and not isinstance(submitted, list):
            raise InvalidTransition("Selected resource IDs must be a list")
        if not selected:
            raise InvalidTransition("Select at least one discovered resource to adopt")
        if not all(isinstance(resource_id, str) for resource_id in selected):
            raise InvalidTransition("Selected resource IDs must be strings")
        by_id = {resource.id: resource for resource in workflow.resources}
        unknown = sorted(set(selected) - set(by_id))
        if unknown:
            raise InvalidTransition(
                "Selected resources are not part of this discovery: "
                + ", ".join(unknown[:5])
            )
        closure = set(selected)
        changed = True
        while changed:
            changed = False
            for resource_id in tuple(closure):
                for dependency_id in by_id[resource_id].dependency_ids:
                    if (
                        dependency_id in by_id
                        and by_id[dependency_id].support_status == "supported"
                        and dependency_id not in workflow.managed_resources
                        and dependency_id not in closure
                    ):
                        closure.add(dependency_id)
                        changed = True
        return [
            resource_id
            for resource_id in workflow.ordered_resource_ids
            if resource_id in closure
        ]

    @staticmethod
    def _selected_resources(workflow: Workflow) -> list:
        by_id = {resource.id: resource for resource in workflow.resources}
        return [
            by_id[resource_id]
            for resource_id in (
                workflow.selected_resource_ids or workflow.ordered_resource_ids
            )
            if resource_id in by_id
        ]

    @synchronized
    def import_state(
        self,
        workflow_id: str,
        *,
        allow_nonempty_workspace: bool = False,
        progress=None,
    ) -> Workflow:
        workflow = self._at(workflow_id, Stage.VALIDATED)
        assert workflow.bundle is not None
        self._finalize_artifacts(workflow)
        assert workflow.verification is not None
        drift_override = (
            workflow.drift_accepted
            and workflow.verification.drift_detected
            and workflow.verification.planned_imports == len(workflow.bundle.imports)
        )
        if not (
            (
                workflow.verification.success
                and not workflow.verification.drift_detected
            )
            or drift_override
        ):
            raise InvalidTransition(
                "HCP import is blocked until the local plan is drift-free or its "
                "verified drift is explicitly accepted"
            )
        if self.require_git_delivery and (
            not workflow.git_delivery
            or workflow.git_delivery.status
            not in {"branch-pushed", "pull-request-open"}
        ):
            raise InvalidTransition(
                "HCP submission is blocked until the generated bundle is delivered "
                "to the configured target Git repository"
            )
        if getattr(self.importer, "completion_stage", None) == Stage.SUBMITTED:
            workflow.import_run_id = self.importer.import_bundle(
                workflow.project_id,
                workflow.bundle,
                workflow_id=workflow.id,
                lifecycle=workflow.hcp_import,
                checkpoint=lambda lifecycle: self._checkpoint_hcp(
                    workflow, lifecycle
                ),
                allow_nonempty_workspace=allow_nonempty_workspace,
                progress=progress,
            )
        else:
            workflow.import_run_id = self.importer.import_bundle(
                workflow.project_id,
                workflow.bundle,
                allow_nonempty_workspace=allow_nonempty_workspace,
                progress=progress,
            )
        workflow.target_protection = getattr(
            self.importer, "last_protection", {}
        )
        workflow.stage = getattr(self.importer, "completion_stage", Stage.IMPORTED)
        if workflow.stage == Stage.SUBMITTED:
            workflow.events.append(
                f"HCP saved plan {workflow.import_run_id} submitted; explicit HCP apply is still required"
            )
            if workflow.target_protection.get("nonempty_workspace_override"):
                workflow.events.append(
                    "Operator explicitly authorized HCP submission to a non-empty "
                    f"workspace managing {workflow.target_protection.get('resource_count', 0)} "
                    "resources; saved-plan review and manual apply remain mandatory"
                )
            if workflow.target_protection.get(
                "preserved_configuration_file_count"
            ):
                workflow.events.append(
                    "Preserved "
                    f"{workflow.target_protection['preserved_configuration_file_count']} "
                    "files from HCP configuration "
                    f"{workflow.target_protection.get('preserved_configuration_version_id', '')} "
                    "before overlaying the new adoption"
                )
            if workflow.target_protection.get("vcs_connected"):
                workflow.events.append(
                    "VCS-driven HCP workspace accepted for provisional plan "
                    "submission; merge the delivered Terraform configuration "
                    "into the authoritative repository before manual apply"
                )
            workflow.events.append(
                "HCP target protection passed: unlocked workspace, no active runs, "
                "supported execution mode, and auto-apply disabled"
            )
        else:
            workflow.events.append(f"Mock import run {workflow.import_run_id} completed")
            workflow.hcp_import.status = "completed"
            workflow.hcp_import.run_id = workflow.import_run_id or ""
            workflow.hcp_import.remote_status = "fixture-completed"
            workflow.hcp_import.imported_resource_count = len(workflow.bundle.imports)
            workflow.hcp_import.drift_free = True
            workflow.hcp_import.diagnostics.append(
                "Fixture post-import plan confirmed zero drift"
            )
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    @synchronized
    def accept_drift(self, workflow_id: str, accepted: bool) -> Workflow:
        workflow = self._at(workflow_id, Stage.GENERATED)
        if not accepted:
            raise InvalidTransition("Explicit drift acceptance is required")
        if workflow.bundle is None or workflow.verification is None:
            raise InvalidTransition(
                "Run local Terraform verification before accepting drift"
            )
        if not workflow.verification.drift_detected:
            raise InvalidTransition("The local Terraform plan did not detect drift")
        if workflow.verification.planned_imports != len(workflow.bundle.imports):
            raise InvalidTransition(
                "Drift cannot be accepted because the Terraform plan does not "
                "contain every expected import"
            )
        now = self._now()
        workflow.drift_accepted = True
        workflow.drift_accepted_at = now
        workflow.stage = Stage.VALIDATED
        workflow.events.append(
            "Operator explicitly accepted the verified Terraform plan drift; "
            "the HCP saved plan still requires manual review and apply"
        )
        workflow.updated_at = now
        self._save(workflow)
        return workflow

    @synchronized
    def reconcile_import(self, workflow_id: str, progress=None) -> Workflow:
        workflow = self._at(workflow_id, Stage.SUBMITTED)
        if not hasattr(self.importer, "reconcile"):
            raise InvalidTransition("the configured importer cannot reconcile HCP runs")
        assert workflow.bundle is not None
        lifecycle, complete = self.importer.reconcile(
            workflow.hcp_import,
            len(workflow.bundle.imports),
            workflow_id=workflow.id,
            checkpoint=lambda value: self._checkpoint_hcp(workflow, value),
            progress=progress,
        )
        workflow.hcp_import = lifecycle
        if complete:
            workflow.stage = Stage.IMPORTED
            workflow.events.append(
                f"HCP import {lifecycle.run_id} applied; workspace manages "
                f"{lifecycle.imported_resource_count} resources"
            )
            workflow.events.append(
                f"Post-import plan {lifecycle.post_import_run_id} confirmed zero drift"
            )
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    @synchronized
    def deliver_git(self, workflow_id: str, payload: dict, progress=None) -> Workflow:
        workflow = self._at(workflow_id, Stage.VALIDATED)
        if self.git_delivery_engine is None:
            raise InvalidTransition("no Git delivery engine is configured")
        assert workflow.bundle is not None
        self._finalize_artifacts(workflow)
        delivery = GitDelivery(
            model_repository=str(payload.get("model_repository", "")).strip(),
            target_repository=str(payload.get("target_repository", "")).strip(),
            model_ref=str(payload.get("model_ref", "main")).strip() or "main",
            base_branch=str(payload.get("base_branch", "main")).strip() or "main",
            branch=str(payload.get("branch", "")).strip(),
            target_path=str(payload.get("target_path", "terraform")).strip()
            or "terraform",
            create_pull_request=bool(payload.get("create_pull_request", True)),
            allow_overwrite=bool(payload.get("allow_overwrite", False)),
            update_existing_directory=bool(
                payload.get("update_existing_directory", False)
            ),
        )
        workflow.git_delivery = delivery
        workflow.updated_at = self._now()
        self._save(workflow)
        try:
            workflow.git_delivery = self.git_delivery_engine.deliver(
                workflow.id, workflow.bundle, delivery, progress=progress
            )
        except Exception as error:
            delivery.status = "failed"
            delivery.output = str(error)[-3000:]
            workflow.updated_at = self._now()
            self._save(workflow)
            raise
        destination = (
            workflow.git_delivery.pull_request_url
            or f"{workflow.git_delivery.target_repository}#{workflow.git_delivery.branch}"
        )
        workflow.events.append(f"Delivered generated Terraform to {destination}")
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    @synchronized
    def verify(self, workflow_id: str, progress=None) -> Workflow:
        workflow = self._at(workflow_id, Stage.GENERATED)
        if self.verifier is None:
            raise InvalidTransition("no Terraform verifier is configured")
        assert workflow.bundle is not None
        self._finalize_artifacts(workflow)
        workflow.verification = self.verifier.verify(
            workflow.project_id, workflow.bundle, progress=progress
        )
        workflow.drift_accepted = False
        workflow.drift_accepted_at = ""
        repairs = 0
        repair = getattr(self.agent, "repair_after_verification", None)
        while (
            callable(repair)
            and repairs < self.MAX_AI_VERIFICATION_REPAIRS
            and self._verification_is_ai_repairable(workflow.verification)
        ):
            repairs += 1
            message = (
                f"AI verification repair {repairs}/{self.MAX_AI_VERIFICATION_REPAIRS}: "
                "Terraform reported a repairable configuration mismatch"
            )
            if progress:
                progress(message)
            workflow.events.append(message)
            workflow.updated_at = self._now()
            self._save(workflow)
            try:
                workflow.bundle = repair(
                    self._generation_prompt_for(workflow),
                    workflow.bundle,
                    workflow.verification,
                    progress=progress,
                )
                self._finalize_artifacts(workflow)
                workflow.updated_at = self._now()
                self._save(workflow)
                workflow.verification = self.verifier.verify(
                    workflow.project_id, workflow.bundle, progress=progress
                )
            except Exception as error:
                detail = str(error).replace("\n", " ")[:360]
                workflow.events.append(
                    f"AI verification repair {repairs} rejected by host assurance: {detail}"
                )
                if progress:
                    progress(workflow.events[-1])
                break
        workflow.verification.repair_attempts = repairs
        workflow.verification.repaired_by_ai = bool(
            repairs and workflow.verification.success
        )
        if workflow.verification.success and not workflow.verification.drift_detected:
            workflow.stage = Stage.VALIDATED
            workflow.events.append(
                f"Local Terraform plan verified {workflow.verification.planned_imports} imports with no drift"
            )
            if workflow.verification.repaired_by_ai:
                workflow.events.append(
                    f"AI-assisted Terraform repair accepted after {repairs} bounded attempt(s)"
                )
        else:
            selected_module_sources = sorted(
                {
                    source
                    for source in workflow.module_selections.values()
                    if source != DIRECT_RESOURCE
                }
            )
            if (
                workflow.verification.drift_detected
                and selected_module_sources
            ):
                workflow.verification.diagnostics.append(
                    "Selected module configuration is not import-only. "
                    "Return to Match and choose a direct provider resource or another module: "
                    + ", ".join(selected_module_sources)
                )
            workflow.events.append("Local Terraform verification blocked the import phase")
        workflow.updated_at = self._now()
        self._save(workflow)
        return workflow

    def _generation_prompt_for(self, workflow: Workflow) -> dict:
        selected = self._selected_resources(workflow)
        selected_candidates: dict[str, list] = {}
        for resource in selected:
            selected_source = workflow.module_selections.get(
                resource.id, DIRECT_RESOURCE
            )
            selected_candidates[resource.id] = [
                candidate
                for candidate in workflow.candidates.get(resource.id, [])
                if candidate.module.source == selected_source
            ][:1]
        return build_generation_prompt(
            workflow.project_id,
            selected,
            workflow.selected_resource_ids,
            selected_candidates,
            self.hcp_target,
        )

    @staticmethod
    def _is_single_resource_candidate(resource, candidate) -> bool:
        module = candidate.module
        addresses = tuple(
            dict.fromkeys(
                address
                for address in (
                    module.managed_resource_addresses
                    or (
                        (module.managed_resource_address,)
                        if module.managed_resource_address
                        else ()
                    )
                )
                if address
            )
        )
        if len(addresses) != 1 or len(module.resource_types) != 1:
            return False
        return addresses[0].split(".", 1)[0] == resource.terraform_type

    @staticmethod
    def _verification_is_ai_repairable(verification) -> bool:
        if verification.success or verification.summary not in {
            "Terraform validation failed",
            "Terraform plan failed",
        }:
            return False
        evidence = "\n".join(
            [verification.summary, *verification.diagnostics, verification.output]
        ).lower()
        operational_failures = (
            "permission denied",
            "permission_denied",
            "forbidden",
            "unauthorized",
            "authentication",
            "credential",
            "quota",
            "billing",
            "connection refused",
            "connection reset",
            "timed out",
            "deadline exceeded",
            "api has not been used",
            "does not exist",
            "not found",
        )
        return not any(marker in evidence for marker in operational_failures)

    @staticmethod
    def _is_audit_progress(message: str) -> bool:
        lowered = message.lower()
        if lowered.startswith(
            (
                "bob stdout:",
                "bob stderr:",
                "copilot stdout:",
                "copilot stderr:",
            )
        ):
            return False
        return " is running · " not in lowered

    def _at(self, workflow_id: str, expected: Stage) -> Workflow:
        workflow = self.get(workflow_id)
        self._require_current_runtime(workflow)
        if workflow.stage != expected:
            raise InvalidTransition(
                f"expected stage {expected.value}, found {workflow.stage.value}"
            )
        return workflow

    def _require_current_runtime(self, workflow: Workflow) -> None:
        if (
            self.required_runtime_origin
            and workflow.runtime_origin != self.required_runtime_origin
        ):
            raise InvalidTransition(
                "This workflow predates the production-only runtime and cannot be "
                "continued safely. Start a new infrastructure adoption run."
            )

    def _finalize_artifacts(self, workflow: Workflow) -> None:
        assert workflow.bundle is not None
        provider_versions = {
            resource.provider_version
            for resource in self._selected_resources(workflow)
            if resource.provider_version
        }
        if len(provider_versions) > 1:
            raise InvalidTransition(
                "Selected resources require inconsistent reviewed Google provider versions"
            )
        finalize_terraform_artifacts(
            workflow.bundle,
            self.hcp_target,
            next(iter(provider_versions), "7.39.0"),
            workflow.project_id,
        )

    def _save(self, workflow: Workflow) -> None:
        if self.persistence:
            self.persistence.save_workflow(workflow)

    def _checkpoint_hcp(self, workflow: Workflow, lifecycle) -> None:
        workflow.hcp_import = lifecycle
        workflow.import_run_id = lifecycle.run_id or workflow.import_run_id
        workflow.updated_at = self._now()
        self._save(workflow)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
