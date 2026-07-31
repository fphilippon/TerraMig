from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..domain import Resource


class DependencyError(ValueError):
    pass


@dataclass(frozen=True)
class DependencyOrder:
    resource_ids: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...] = ()


def dependency_order(resources: list[Resource]) -> DependencyOrder:
    """Order the component graph while retaining exact strongly connected groups."""

    by_id = {resource.id: resource for resource in resources}
    if len(by_id) != len(resources):
        raise DependencyError("inventory contains duplicate resource ids")

    dependencies: dict[str, tuple[str, ...]] = {}
    for resource in resources:
        for dependency_id in resource.dependency_ids:
            if dependency_id not in by_id:
                raise DependencyError(
                    f"{resource.id} references missing dependency {dependency_id}"
                )
        dependencies[resource.id] = tuple(sorted(set(resource.dependency_ids)))

    components = _strongly_connected_components(dependencies)
    component_by_resource = {
        resource_id: index
        for index, component in enumerate(components)
        for resource_id in component
    }
    cyclic_components = tuple(
        component
        for component in components
        if len(component) > 1
        or component[0] in dependencies.get(component[0], ())
    )

    indegree = {index: 0 for index in range(len(components))}
    consumers: dict[int, set[int]] = defaultdict(set)
    for resource_id, resource_dependencies in dependencies.items():
        consumer_component = component_by_resource[resource_id]
        for dependency_id in resource_dependencies:
            dependency_component = component_by_resource[dependency_id]
            if dependency_component == consumer_component:
                continue
            if consumer_component not in consumers[dependency_component]:
                consumers[dependency_component].add(consumer_component)
                indegree[consumer_component] += 1

    ready = sorted(
        (index for index, degree in indegree.items() if degree == 0),
        key=lambda index: components[index],
    )
    ordered: list[str] = []
    while ready:
        current_component = ready.pop(0)
        ordered.extend(components[current_component])
        for consumer in sorted(
            consumers[current_component], key=lambda index: components[index]
        ):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                ready.append(consumer)
                ready.sort(key=lambda index: components[index])

    if len(ordered) != len(resources):
        raise DependencyError("dependency component graph could not be ordered")
    return DependencyOrder(tuple(ordered), cyclic_components)


def topological_order(resources: list[Resource]) -> list[str]:
    """Return dependencies before consumers and reject exact cyclic groups."""

    result = dependency_order(resources)
    if result.cycles:
        cyclic = sorted(
            resource_id
            for component in result.cycles
            for resource_id in component
        )
        raise DependencyError(f"dependency cycle detected: {', '.join(cyclic)}")
    return list(result.resource_ids)


def _strongly_connected_components(
    dependencies: dict[str, tuple[str, ...]],
) -> list[tuple[str, ...]]:
    """Iterative Kosaraju SCCs, safe for inventories larger than Python's stack."""

    visited: set[str] = set()
    finish_order: list[str] = []
    for start in sorted(dependencies):
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            resource_id, expanded = stack.pop()
            if expanded:
                finish_order.append(resource_id)
                continue
            if resource_id in visited:
                continue
            visited.add(resource_id)
            stack.append((resource_id, True))
            for dependency_id in reversed(dependencies[resource_id]):
                if dependency_id not in visited:
                    stack.append((dependency_id, False))

    reverse_graph: dict[str, list[str]] = {
        resource_id: [] for resource_id in dependencies
    }
    for resource_id, resource_dependencies in dependencies.items():
        for dependency_id in resource_dependencies:
            reverse_graph[dependency_id].append(resource_id)
    for consumers in reverse_graph.values():
        consumers.sort()

    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component: list[str] = []
        stack = [(start, False)]
        while stack:
            resource_id, _expanded = stack.pop()
            if resource_id in assigned:
                continue
            assigned.add(resource_id)
            component.append(resource_id)
            for consumer_id in reversed(reverse_graph[resource_id]):
                if consumer_id not in assigned:
                    stack.append((consumer_id, False))
        components.append(tuple(sorted(component)))
    return components
