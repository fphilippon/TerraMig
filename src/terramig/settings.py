from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

@dataclass
class Settings:
    hcp_hostname: str = "app.terraform.io"
    hcp_organization: str = ""
    hcp_workspace: str = ""
    gcp_project_id: str = ""
    ai_provider: str = "github-copilot"
    gcp_verified_project_id: str = ""
    gcp_verified_account: str = ""
    gcp_verified_at: str = ""
    hcp_submission_enabled: bool = True

    def validate(self) -> None:
        if self.ai_provider not in {"github-copilot", "ibm-bob"}:
            raise ValueError("AI agent must be github-copilot or ibm-bob")
        if not self.hcp_hostname.strip():
            raise ValueError("HCP Terraform hostname is required")
        if not isinstance(self.hcp_submission_enabled, bool):
            raise ValueError("HCP saved-plan submission setting must be boolean")
        if not self.hcp_organization.strip():
            raise ValueError("HCP Terraform organization is required")

    def readiness(
        self,
        *,
        ai_credential_configured: bool | None = None,
        ai_authenticated: bool | None = None,
        hcp_token_configured: bool | None = None,
        hcp_authenticated: bool | None = None,
        github_token_configured: bool | None = None,
        github_authenticated: bool | None = None,
        git_ssh_key_configured: bool | None = None,
        git_ssh_authenticated: bool | None = None,
        gcp_credential_configured: bool | None = None,
        gcp_authenticated: bool | None = None,
    ) -> dict[str, bool | str]:
        binary = "copilot" if self.ai_provider == "github-copilot" else "bob"
        ai_installed = shutil.which(binary) is not None
        if ai_credential_configured is None:
            variables = (
                ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
                if self.ai_provider == "github-copilot"
                else ("BOBSHELL_API_KEY",)
            )
            ai_credential_configured = any(
                os.getenv(name, "").strip() for name in variables
            )
        if ai_authenticated is None:
            ai_authenticated = ai_credential_configured
        if hcp_token_configured is None:
            hcp_token_configured = bool(
                os.getenv("TERRAMIG_HCP_TOKEN", "").strip()
            )
        if hcp_authenticated is None:
            hcp_authenticated = hcp_token_configured
        if github_token_configured is None:
            github_token_configured = bool(
                os.getenv("TERRAMIG_GITHUB_TOKEN", "").strip()
            )
        if github_authenticated is None:
            github_authenticated = github_token_configured
        if git_ssh_key_configured is None:
            git_ssh_key_configured = bool(
                os.getenv("TERRAMIG_GIT_SSH_PRIVATE_KEY", "").strip()
            )
        if git_ssh_authenticated is None:
            git_ssh_authenticated = git_ssh_key_configured
        deployment_git_auth = bool(
            os.getenv("SSH_AUTH_SOCK")
            or os.getenv("GIT_SSH_COMMAND")
        )
        if gcp_authenticated is None:
            gcp_authenticated = bool(
                self.gcp_project_id
                and self.gcp_verified_project_id == self.gcp_project_id
                and self.gcp_verified_at
            )
        if gcp_credential_configured is None:
            gcp_credential_configured = bool(
                os.getenv("TERRAMIG_GCP_CREDENTIALS_JSON", "").strip()
                or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
                or os.getenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "").strip()
            )
        checks: dict[str, bool | str] = {
            "hcp_token": bool(hcp_token_configured),
            "hcp_authenticated": bool(hcp_authenticated),
            "gcloud": shutil.which("gcloud") is not None,
            "gcp_credential": bool(gcp_credential_configured),
            "gcp_authenticated": bool(gcp_authenticated),
            "ai": bool(ai_installed and ai_authenticated),
            "ai_installed": ai_installed,
            "ai_credential": bool(ai_credential_configured),
            "ai_authenticated": bool(ai_authenticated),
            "ai_binary": binary,
            "terraform": shutil.which("terraform") is not None,
            "terraform_mcp": shutil.which("terraform-mcp-server") is not None,
            "adoption_target": bool(self.hcp_organization and self.hcp_workspace),
            "hcp_submission": self.hcp_submission_enabled,
            "git": shutil.which("git") is not None,
            "git_auth": bool(
                github_authenticated
                or deployment_git_auth
                or git_ssh_authenticated
            ),
            "github_api": bool(github_authenticated),
            "github_token": bool(github_token_configured),
            "git_ssh_key": bool(git_ssh_key_configured),
            "git_ssh_authenticated": bool(git_ssh_authenticated),
        }
        checks["ready"] = bool(
            checks["hcp_authenticated"]
            and checks["gcloud"]
            and checks["gcp_authenticated"]
            and checks["ai"]
            and checks["terraform"]
            and checks["terraform_mcp"]
            and checks["adoption_target"]
            and checks["git"]
            and checks["git_auth"]
        )
        return checks

    def public_dict(
        self,
        *,
        ai_credential_configured: bool | None = None,
        ai_authenticated: bool | None = None,
        hcp_token_configured: bool | None = None,
        hcp_authenticated: bool | None = None,
        github_token_configured: bool | None = None,
        github_authenticated: bool | None = None,
        git_ssh_key_configured: bool | None = None,
        git_ssh_authenticated: bool | None = None,
        gcp_credential_configured: bool | None = None,
        gcp_authenticated: bool | None = None,
    ) -> dict:
        value = asdict(self)
        value["readiness"] = self.readiness(
            ai_credential_configured=ai_credential_configured,
            ai_authenticated=ai_authenticated,
            hcp_token_configured=hcp_token_configured,
            hcp_authenticated=hcp_authenticated,
            github_token_configured=github_token_configured,
            github_authenticated=github_authenticated,
            git_ssh_key_configured=git_ssh_key_configured,
            git_ssh_authenticated=git_ssh_authenticated,
            gcp_credential_configured=gcp_credential_configured,
            gcp_authenticated=gcp_authenticated,
        )
        return value


class SettingsStore:
    def __init__(self, path: Path, persistence: "SettingsPersistence | None" = None) -> None:
        self.path = path
        self.persistence = persistence

    def load(self) -> Settings:
        persisted = self.persistence.load_settings() if self.persistence else None
        if persisted is not None:
            settings = self._from_payload(persisted)
            if "mode" in persisted and self.persistence:
                self.persistence.save_settings(asdict(settings))
            return settings
        if self.persistence and self.path.exists():
            payload = json.loads(self.path.read_text())
            settings = self._from_payload(payload)
            if "mode" in payload:
                try:
                    self.path.write_text(json.dumps(asdict(settings), indent=2) + "\n")
                except OSError:
                    # A read-only legacy file can still be normalized into the
                    # active persistence backend without blocking startup.
                    pass
            self.persistence.save_settings(asdict(settings))
            return settings
        if not self.path.exists():
            enabled = os.getenv("TERRAMIG_ENABLE_HCP_SUBMISSION", "true").lower()
            return Settings(
                hcp_hostname=os.getenv("TERRAMIG_HCP_HOSTNAME", "app.terraform.io"),
                hcp_organization=os.getenv("TERRAMIG_HCP_ORGANIZATION", ""),
                hcp_workspace=os.getenv("TERRAMIG_HCP_WORKSPACE", ""),
                gcp_project_id=os.getenv("TERRAMIG_GCP_PROJECT_ID", ""),
                ai_provider=os.getenv("TERRAMIG_AI_PROVIDER", "github-copilot"),
                hcp_submission_enabled=enabled not in {"0", "false", "no"},
            )
        payload = json.loads(self.path.read_text())
        settings = self._from_payload(payload)
        if "mode" in payload:
            self.path.write_text(json.dumps(asdict(settings), indent=2) + "\n")
        return settings

    @staticmethod
    def _from_payload(payload: dict[str, Any]) -> Settings:
        normalized = dict(payload)
        # Runtime mode was removed in favor of production-only integrations.
        # Ignore and remove the legacy persisted field during the first load.
        normalized.pop("mode", None)
        return Settings(**normalized)

    def save(self, settings: Settings) -> None:
        settings.validate()
        if self.persistence:
            self.persistence.save_settings(asdict(settings))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(settings), indent=2) + "\n")


class SettingsPersistence(Protocol):
    def load_settings(self) -> dict[str, Any] | None: ...

    def save_settings(self, payload: dict[str, Any]) -> None: ...
