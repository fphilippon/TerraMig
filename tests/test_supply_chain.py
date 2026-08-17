import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SupplyChainTests(unittest.TestCase):
    def test_all_github_actions_are_pinned_to_commits(self) -> None:
        workflows = list((ROOT / ".github/workflows").glob("*.yml"))
        self.assertGreaterEqual(len(workflows), 4)
        for workflow in workflows:
            for line in workflow.read_text().splitlines():
                if "uses:" not in line:
                    continue
                reference = line.split("uses:", 1)[1].strip().split()[0]
                self.assertRegex(
                    reference,
                    r"^[^@\s]+@[0-9a-f]{40}$",
                    f"{workflow.name} contains an unpinned action: {reference}",
                )

    def test_supply_chain_has_scan_sbom_and_provenance(self) -> None:
        supply = (ROOT / ".github/workflows/supply-chain.yml").read_text()
        codeql = (ROOT / ".github/workflows/codeql.yml").read_text()
        release = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("trivy-action@", supply)
        self.assertIn("trivyignores: .trivyignore.yaml", supply)
        self.assertIn("pip-audit==2.10.1", supply)
        self.assertIn("sbom-action@", supply)
        self.assertIn("attest-build-provenance@", release)
        self.assertIn("'always' || 'never'", codeql)
        self.assertIn("name: codeql-sarif", codeql)
        self.assertNotRegex(
            "\n".join(
                line for line in supply.splitlines() if "uses:" in line
            ),
            r"@(main|master|v\d+)\b",
        )

    def test_runtime_dependencies_are_exactly_pinned(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text().splitlines()
        dependencies = [
            line for line in requirements if line and not line.startswith("#")
        ]
        self.assertTrue(dependencies)
        self.assertTrue(
            all(
                "==" in line
                and not any(token in line for token in (">", "<", "~="))
                for line in dependencies
            )
        )

    def test_trivy_exceptions_are_scoped_documented_and_expiring(self) -> None:
        ignore = (ROOT / ".trivyignore.yaml").read_text()
        entries = ignore.split("  - id: ")[1:]
        self.assertTrue(entries)
        for entry in entries:
            self.assertTrue(
                "paths:" in entry or "purls:" in entry,
                f"Trivy exception is not scoped: {entry.splitlines()[0]}",
            )
            self.assertIn("statement:", entry)
            self.assertIn("expired_at: 2026-10-29", entry)
        self.assertIn("usr/local/bin/terraform", ignore)
        self.assertIn("usr/lib/google-cloud-sdk/bin/gcloud-crc32c", ignore)


if __name__ == "__main__":
    unittest.main()
