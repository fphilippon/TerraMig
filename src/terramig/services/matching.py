from __future__ import annotations

from ..domain import ModuleCandidate, ModuleVersion, Resource


def match_modules(
    resources: list[Resource], modules: list[ModuleVersion], limit: int = 3
) -> dict[str, list[ModuleCandidate]]:
    """Deterministically shortlist modules; the agent makes the final composition choice."""
    matches: dict[str, list[ModuleCandidate]] = {}
    for resource in resources:
        candidates: list[ModuleCandidate] = []
        attribute_names = set(resource.attributes)
        for module in modules:
            if resource.type not in module.resource_types:
                continue
            managed_addresses = tuple(
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
            required = set(module.inputs)
            coverage = len(required & attribute_names) / max(len(required), 1)
            exact_bonus = 0.35 if len(module.resource_types) == 1 else 0.15
            governed = module.registry_kind in {"private", "hcp-public"}
            governance_bonus = 0.05 if governed else 0
            score = round(
                min(1.0, 0.55 * coverage + exact_bonus + governance_bonus), 2
            )
            reasons = [
                f"supports {resource.type}",
                f"input coverage {coverage:.0%}",
                (
                    "HCP private library"
                    if governed
                    else "verified public publisher"
                ),
                (
                    "single-resource ownership"
                    if len(managed_addresses) == 1
                    and len(module.resource_types) == 1
                    else f"composite ownership ({len(managed_addresses)} managed resources)"
                ),
            ]
            candidates.append(
                ModuleCandidate(resource.id, module, score, tuple(reasons))
            )
        matches[resource.id] = sorted(
            candidates,
            key=lambda candidate: (
                candidate.module.registry_kind not in {"private", "hcp-public"},
                -candidate.score,
                candidate.module.source,
            ),
        )[:limit]
    return matches
