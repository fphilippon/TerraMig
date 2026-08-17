from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = (ROOT / "Dockerfile").read_text()

    def test_image_installs_all_production_cli_binaries(self) -> None:
        self.assertIn("@github/copilot@${COPILOT_VERSION}", self.dockerfile)
        self.assertIn("bobshell-${BOB_VERSION}.tgz", self.dockerfile)
        self.assertIn("google-cloud-cli", self.dockerfile)
        self.assertIn("releases.hashicorp.com/terraform", self.dockerfile)
        self.assertIn("hashicorp/terraform-mcp-server:1.1.0", self.dockerfile)

    def test_bob_install_is_pinned_verified_and_node_is_supported(self) -> None:
        self.assertIn("node:22-bookworm-slim", self.dockerfile)
        self.assertIn("ARG COPILOT_VERSION=1.0.71", self.dockerfile)
        self.assertIn("ARG BOB_VERSION=1.0.6", self.dockerfile)
        self.assertIn("sha256sum --check --strict", self.dockerfile)

    def test_runtime_does_not_copy_npm_package_manager_tree(self) -> None:
        self.assertNotIn(
            "COPY --from=ai-cli-tools /usr/local/lib/node_modules "
            "/usr/local/lib/node_modules",
            self.dockerfile,
        )
        self.assertIn(
            "/usr/local/lib/node_modules/@github "
            "/usr/local/lib/node_modules/@github",
            self.dockerfile,
        )
        self.assertIn(
            "/usr/local/lib/node_modules/bobshell "
            "/usr/local/lib/node_modules/bobshell",
            self.dockerfile,
        )

    def test_final_image_checks_binary_paths(self) -> None:
        for binary in (
            "gcloud",
            "copilot",
            "bob",
            "terraform",
            "terraform-mcp-server",
        ):
            self.assertIn(f"command -v {binary}", self.dockerfile)

    def test_runtime_dependencies_are_pinned_and_installed(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text()
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("psycopg[binary]==3.3.4", requirements)
        self.assertIn("--requirement requirements.txt", self.dockerfile)
        self.assertIn('"setuptools>=78.1.1"', self.dockerfile)
        self.assertIn('"msgpack==1.2.1"', self.dockerfile)
        self.assertIn("gcloud-patches", self.dockerfile)
        self.assertIn("postgres:17.10-bookworm", compose)
        self.assertIn("TERRAMIG_REQUIRE_POSTGRES", compose)
        self.assertRegex(
            requirements,
            r"(?m)^cryptography==[0-9]+(?:\.[0-9]+){2}(?:[.-][0-9A-Za-z.-]+)?\s*$",
        )
        self.assertIn("TERRAMIG_SECRET_ENCRYPTION_KEY", compose)
        self.assertIn("COPILOT_GITHUB_TOKEN", compose)
        self.assertIn("BOBSHELL_API_KEY", compose)

    def test_capability_catalogs_are_packaged_with_the_application(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertIn(
            "data/*",
            project["tool"]["setuptools"]["package-data"]["terramig"],
        )

    def test_production_image_and_smoke_test_do_not_depend_on_fixtures(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertNotIn("COPY --chown=terramig:terramig fixtures", self.dockerfile)
        self.assertIn("readiness_status=", workflow)
        self.assertIn('test "$readiness_status" = 503', workflow)
        self.assertIn("TERRAMIG_ADMIN_USERNAME: ci-admin", workflow)
        self.assertIn("login_status=", workflow)
        self.assertIn("/api/auth/session", workflow)
        self.assertNotIn("/api/auth/change-password", workflow)
        self.assertNotIn("/discover\" > /tmp/job.json", workflow)


if __name__ == "__main__":
    unittest.main()
