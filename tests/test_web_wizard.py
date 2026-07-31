from html.parser import HTMLParser
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WizardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.screens: list[dict[str, str | None]] = []
        self.steps: list[dict[str, str | None]] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.add(element_id)
        classes = (attributes.get("class") or "").split()
        if "wizard-screen" in classes:
            self.screens.append(attributes)
        if tag == "button" and "adoption-step" in classes:
            self.steps.append(attributes)


class WebWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = WizardParser()
        cls.html = (ROOT / "web" / "index.html").read_text()
        cls.parser.feed(cls.html)
        cls.javascript = (ROOT / "web" / "app.js").read_text()

    def test_workflow_is_split_into_seven_navigable_screens(self) -> None:
        self.assertEqual([str(index) for index in range(7)], [screen["data-wizard-screen"] for screen in self.parser.screens])
        self.assertEqual([str(index) for index in range(7)], [step["data-screen"] for step in self.parser.steps])
        self.assertIn("wizard-previous", self.parser.ids)
        self.assertIn("wizard-next", self.parser.ids)

    def test_only_scope_screen_is_visible_before_a_run(self) -> None:
        self.assertNotIn("hidden", (self.parser.screens[0].get("class") or "").split())
        self.assertTrue(all("hidden" in (screen.get("class") or "").split() for screen in self.parser.screens[1:]))

    def test_only_infrastructure_adoption_journey_is_exposed(self) -> None:
        self.assertNotIn("state-migration", self.html)
        self.assertNotIn("stateMigration", self.javascript)

    def test_runtime_is_production_only(self) -> None:
        self.assertNotIn("runtime-mode", self.html)
        self.assertNotIn("settings.mode", self.javascript)
        self.assertNotIn("Sandbox", self.html + self.javascript)

    def test_private_module_governance_remains_visible(self) -> None:
        self.assertIn("Private modules", self.html)
        self.assertIn("Governed module catalog", self.html)
        self.assertIn("automatically trusts every HCP Private Library module", self.html)
        self.assertIn("curated public", self.html)
        self.assertIn("/api/catalog/status", self.javascript)
        self.assertIn("/api/catalog/scan", self.javascript)
        self.assertIn("coverage.domains", self.javascript)
        self.assertIn("Databases", self.javascript)
        self.assertNotIn("Verified module manifest", self.html + self.javascript)

    def test_controller_guards_navigation_with_workflow_state(self) -> None:
        self.assertIn("function maxAccessibleScreen()", self.javascript)
        self.assertIn("index > maxAccessibleScreen()", self.javascript)
        self.assertIn("currentScreen === 6 && workflow.stage === 'validated'", self.javascript)
        self.assertIn("allow_nonempty_workspace:", self.javascript)
        self.assertIn("$('allow-nonempty-workspace').checked", self.javascript)
        self.assertIn("driftAcceptanceEligible()", self.javascript)
        self.assertIn("Remote destroy gate", self.javascript)
        self.assertIn("Checking · do not apply", self.javascript)
        self.assertIn(
            "runAction('accept-drift', {accept: true})", self.javascript
        )

    def test_verify_screen_exposes_explicit_guarded_drift_acceptance(self) -> None:
        for element_id in (
            "drift-acceptance",
            "accept-drift",
            "drift-acceptance-note",
            "import-plan-status",
            "nonempty-workspace-override",
            "allow-nonempty-workspace",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn(
            "result.planned_imports === workflow.bundle.imports.length",
            self.javascript,
        )
        self.assertIn("let driftAcceptanceRenderKey = '';", self.javascript)
        self.assertIn(
            "if (driftAcceptanceRenderKey !== acceptanceKey)",
            self.javascript,
        )
        self.assertIn(
            "Check this confirmation, then click “Accept drift and continue” below.",
            self.javascript,
        )
        self.assertIn("Drift accepted · manual review required", self.javascript)

    def test_git_delivery_and_durable_job_controls_are_exposed(self) -> None:
        for element_id in (
            "git-model-repository",
            "git-target-repository",
            "git-target-path",
            "git-create-pr",
            "git-update-existing-directory",
            "hcp-lifecycle",
            "jobs-body",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("waitForJob", self.javascript)
        self.assertIn("/api/jobs/", self.javascript)
        self.assertIn("settings.readiness.github_api", self.javascript)
        self.assertNotIn('id="git-create-pr" checked', self.html)

    def test_git_delivery_retry_snapshots_current_editable_values(self) -> None:
        snapshot = "const gitDeliveryPayload = currentScreen === 5 ? readGitDeliveryForm() : null;"
        busy_render = "wizardBusy = true;"
        self.assertIn("function readGitDeliveryForm()", self.javascript)
        self.assertIn("function hydrateGitDeliveryForm(delivery)", self.javascript)
        self.assertIn(
            "update_existing_directory: $('git-update-existing-directory').checked",
            self.javascript,
        )
        self.assertLess(self.javascript.index(snapshot), self.javascript.index(busy_render))
        self.assertIn("runAction('deliver', gitDeliveryPayload)", self.javascript)
        self.assertIn("if (gitDeliveryFormWorkflowId === workflowId) return;", self.javascript)

    def test_user_facing_brand_is_terramig(self) -> None:
        html = (ROOT / "web" / "index.html").read_text()
        readme = (ROOT / "README.md").read_text()
        self.assertIn("TerraMig", html)
        self.assertIn("# TerraMig", readme)
        self.assertNotIn("Terra" + "forge", html + readme)

    def test_operational_ui_exposes_evidence_and_observability(self) -> None:
        self.assertIn("view-operations", self.parser.ids)
        self.assertIn("download-workflow-report", self.parser.ids)
        self.assertIn("assurance-checks", self.parser.ids)
        self.assertIn("/api/operations", self.javascript)
        self.assertIn("/report", self.javascript)

    def test_local_authentication_and_durable_run_restore_are_exposed(self) -> None:
        for element_id in (
            "auth-gate",
            "login-form",
            "password-change-form",
            "logout",
            "persisted-workflows",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("X-CSRF-Token", self.javascript)
        self.assertIn("/api/auth/change-password", self.javascript)
        self.assertIn("resumeWorkflow", self.javascript)

    def test_ai_credentials_have_masked_lifecycle_controls(self) -> None:
        for element_id in (
            "ai-provider",
            "ai-credential",
            "ai-credential-save",
            "ai-credential-test",
            "ai-credential-clear",
            "ai-credential-status",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("/api/settings/ai-credential", self.javascript)
        self.assertIn("type=\"password\"", (ROOT / "web" / "index.html").read_text())

    def test_hcp_token_has_masked_lifecycle_controls(self) -> None:
        for element_id in (
            "hcp-credential",
            "hcp-credential-save",
            "hcp-credential-test",
            "hcp-credential-clear",
            "hcp-credential-status",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("/api/settings/hcp-credential", self.javascript)

    def test_long_running_phases_expose_durable_live_terminals(self) -> None:
        for element_id in (
            "discovery-live-log",
            "verification-live-log",
            "git-live-log",
            "hcp-live-log",
            "hcp-submission-enabled",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("captureJobLogs", self.javascript)
        self.assertIn("workspace_url", self.javascript)
        self.assertIn("AI repair accepted", self.javascript)
        self.assertIn("repair_attempts", self.javascript)

    def test_generated_artifact_view_includes_backend_and_provider_files(self) -> None:
        self.assertIn("function terraformArtifactText", self.javascript)
        self.assertIn("['backend.tf', bundle.backend_tf]", self.javascript)
        self.assertIn("['providers.tf', bundle.providers_tf]", self.javascript)

    def test_gcp_connection_has_read_only_verification_controls(self) -> None:
        for element_id in (
            "gcp-project-id",
            "gcp-credential",
            "gcp-credential-file",
            "gcp-credential-save",
            "gcp-credential-clear",
            "gcp-connection-reset",
            "gcp-connection-test",
            "gcp-connection-status",
            "gcp-connection-note",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("/api/settings/gcp-connection/test", self.javascript)
        self.assertIn("/api/settings/gcp-connection/configure", self.javascript)
        self.assertIn("/api/settings/gcp-credential", self.javascript)
        self.assertIn("Save &amp; test connection", self.html)
        self.assertIn("Cloud Asset Inventory", self.javascript)

    def test_inventory_exposes_runtime_gcp_service_coverage(self) -> None:
        self.assertIn("inventory-services", self.parser.ids)
        self.assertIn("data.service_coverage", self.javascript)
        self.assertIn("service.asset_types", self.javascript)

    def test_adoption_exposes_resource_and_module_selection(self) -> None:
        for element_id in (
            "resource-selection-count",
            "resource-visibility",
            "select-supported-resources",
            "clear-resource-selection",
            "search-public-modules",
            "public-registry-status",
            "module-bulk-status",
            "select-latest-compatible-modules",
            "select-direct-resources",
            "return-to-match",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("selected_resource_ids", self.javascript)
        self.assertIn("module_selections", self.javascript)
        self.assertIn("include_public: true", self.javascript)
        self.assertIn("applyModuleSelectionPolicy", self.javascript)
        self.assertIn("select-modules", self.javascript)
        self.assertIn("latest-compatible", self.javascript)
        self.assertIn("__resource__", self.javascript)
        self.assertIn("resourceVisibility === 'all'", self.javascript)
        self.assertIn("const directOption", self.javascript)
        self.assertIn("Composite ·", self.javascript)
        self.assertIn("returnToModuleMatching", self.javascript)

    def test_large_inventories_are_grouped_filterable_and_bulk_actionable(self) -> None:
        for element_id in (
            "resource-search",
            "resource-grouping",
            "resource-management",
            "match-search",
            "match-grouping",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("function resourceDomain(resource)", self.javascript)
        self.assertIn("function groupResources(resources, mode)", self.javascript)
        self.assertIn("resources.slice(0, 40)", self.javascript)
        self.assertIn("resources.slice(0, 30)", self.javascript)
        self.assertIn("data-resource-group-action", self.javascript)
        self.assertIn("data-match-group-policy", self.javascript)
        self.assertIn("Previously imported", self.javascript)

    def test_ai_composition_exposes_async_live_session(self) -> None:
        for element_id in (
            "composition-terminal-panel",
            "composition-run-status",
            "composition-process-label",
            "composition-live-log",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("let activeJob = null", self.javascript)
        self.assertIn("function renderCompositionProgress()", self.javascript)
        self.assertIn("Agent working asynchronously", self.javascript)
        self.assertIn("currentScreen = 3;", self.javascript)

    def test_resource_and_module_layouts_contain_long_content(self) -> None:
        styles = (ROOT / "web" / "styles.css").read_text()
        self.assertIn(".primary, .wizard-screen, .panel { min-width: 0; }", styles)
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto", styles)
        self.assertIn(".module-match-card { display: block; min-width: 0; overflow: hidden", styles)
        self.assertIn(".agent-terminal", styles)

    def test_screen_number_watermarks_are_removed(self) -> None:
        self.assertNotIn("screen-number", self.html)

    def test_git_ssh_key_has_masked_lifecycle_controls(self) -> None:
        for element_id in (
            "git-ssh-credential",
            "git-ssh-test-repository",
            "git-ssh-credential-save",
            "git-ssh-credential-test",
            "git-ssh-credential-clear",
            "git-ssh-credential-status",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("/api/settings/git-ssh-credential", self.javascript)
        self.assertIn("Save key &amp; test access", self.html)
        self.assertIn("JSON.stringify({credential, repository})", self.javascript)
        self.assertIn("-webkit-text-security: disc", (ROOT / "web" / "styles.css").read_text())

    def test_github_api_token_has_masked_lifecycle_controls(self) -> None:
        for element_id in (
            "github-credential",
            "github-credential-save",
            "github-credential-test",
            "github-credential-clear",
            "github-credential-status",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn("/api/settings/github-credential", self.javascript)
        self.assertIn("GitHub API token", self.html)


if __name__ == "__main__":
    unittest.main()
