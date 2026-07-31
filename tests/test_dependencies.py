import unittest

from terramig.domain import Resource
from terramig.services.dependencies import (
    DependencyError,
    dependency_order,
    topological_order,
)


def resource(identifier: str, dependencies: tuple[str, ...] = ()) -> Resource:
    return Resource(identifier, "test/Resource", identifier, "global", {}, dependencies)


class DependencyTests(unittest.TestCase):
    def test_dependencies_are_ordered_before_consumers(self) -> None:
        resources = [resource("vm", ("subnet",)), resource("network"), resource("subnet", ("network",))]
        self.assertEqual(topological_order(resources), ["network", "subnet", "vm"])

    def test_missing_dependency_is_rejected(self) -> None:
        with self.assertRaisesRegex(DependencyError, "missing dependency"):
            topological_order([resource("vm", ("subnet",))])

    def test_cycle_is_rejected(self) -> None:
        with self.assertRaisesRegex(DependencyError, "cycle"):
            topological_order([resource("a", ("b",)), resource("b", ("a",))])

    def test_component_order_collapses_cycles_without_mislabeling_downstream_nodes(self) -> None:
        result = dependency_order(
            [
                resource("consumer", ("cycle-a",)),
                resource("cycle-a", ("cycle-b",)),
                resource("cycle-b", ("cycle-a",)),
                resource("independent"),
            ]
        )
        self.assertEqual(result.cycles, (("cycle-a", "cycle-b"),))
        self.assertLess(
            result.resource_ids.index("cycle-a"),
            result.resource_ids.index("consumer"),
        )
        self.assertLess(
            result.resource_ids.index("cycle-b"),
            result.resource_ids.index("consumer"),
        )

    def test_large_inventory_ordering_does_not_depend_on_python_recursion(self) -> None:
        resources = [
            resource(
                f"resource-{index:04d}",
                (f"resource-{index - 1:04d}",) if index else (),
            )
            for index in range(1500)
        ]
        result = dependency_order(list(reversed(resources)))
        self.assertFalse(result.cycles)
        self.assertEqual(result.resource_ids[0], "resource-0000")
        self.assertEqual(result.resource_ids[-1], "resource-1499")


if __name__ == "__main__":
    unittest.main()
