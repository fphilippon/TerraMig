from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..persistence import Persistence, SecretRecord
from .ssh import private_key_fingerprint


AI_PROVIDERS = {
    "github-copilot": {
        "secret_name": "ai.github-copilot",
        "environment": (
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ),
        "label": "GitHub Copilot token",
    },
    "ibm-bob": {
        "secret_name": "ai.ibm-bob",
        "environment": ("BOBSHELL_API_KEY",),
        "label": "IBM Bob API key",
    },
}

HCP_TERRAFORM = {
    "secret_name": "hcp.terraform",
    "environment": ("TERRAMIG_HCP_TOKEN",),
    "label": "HCP Terraform token",
}

GITHUB_API = {
    "secret_name": "git.github-api-token",
    "environment": ("TERRAMIG_GITHUB_TOKEN",),
    "label": "GitHub API token",
}

GIT_SSH = {
    "secret_name": "git.ssh-private-key",
    "environment": ("TERRAMIG_GIT_SSH_PRIVATE_KEY",),
    "label": "Git SSH private key",
}

GCP_ADC = {
    "secret_name": "gcp.application-default-credentials",
    "environment": ("TERRAMIG_GCP_CREDENTIALS_JSON",),
    "label": "GCP credential JSON",
}


class SecretVault:
    """Encrypt application-managed secrets before durable persistence."""

    def __init__(
        self,
        persistence: Persistence,
        *,
        key: str = "",
        key_file: Path | None = None,
    ) -> None:
        self.persistence = persistence
        self.environment_verified: dict[str, str] = {}
        material = key.strip() or os.getenv(
            "TERRAMIG_SECRET_ENCRYPTION_KEY", ""
        ).strip()
        if not material:
            target = key_file or Path(
                os.getenv(
                    "TERRAMIG_SECRET_KEY_FILE",
                    ".terramig/secret.key",
                )
            )
            material = self._load_or_create_key(target)
        try:
            self.cipher = Fernet(material.encode())
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "TERRAMIG_SECRET_ENCRYPTION_KEY must be a valid Fernet key"
            ) from error

    def resolve_ai_credential(self, provider: str) -> str:
        return self._resolve(self._definition(provider))

    def save_ai_credential(self, provider: str, credential: str) -> dict:
        definition = self._definition(provider)
        self._save(definition, credential)
        return self.ai_status(provider)

    def clear_ai_credential(self, provider: str) -> dict:
        definition = self._definition(provider)
        self.persistence.delete_secret(definition["secret_name"])
        return self.ai_status(provider)

    def mark_ai_verified(self, provider: str) -> dict:
        definition = self._definition(provider)
        self._mark_verified(definition)
        return self.ai_status(provider)

    def ai_status(self, provider: str) -> dict:
        definition = self._definition(provider)
        return {"provider": provider, **self._status(definition)}

    def ai_statuses(self) -> dict[str, dict]:
        return {provider: self.ai_status(provider) for provider in AI_PROVIDERS}

    def resolve_hcp_credential(self) -> str:
        return self._resolve(HCP_TERRAFORM)

    def save_hcp_credential(self, credential: str) -> dict:
        self._save(HCP_TERRAFORM, credential)
        return self.hcp_status()

    def clear_hcp_credential(self) -> dict:
        self.persistence.delete_secret(HCP_TERRAFORM["secret_name"])
        return self.hcp_status()

    def mark_hcp_verified(self) -> dict:
        self._mark_verified(HCP_TERRAFORM)
        return self.hcp_status()

    def hcp_status(self) -> dict:
        return self._status(HCP_TERRAFORM)

    def resolve_github_credential(self) -> str:
        return self._resolve(GITHUB_API)

    def save_github_credential(self, credential: str) -> dict:
        self._save(GITHUB_API, credential)
        return self.github_status()

    def clear_github_credential(self) -> dict:
        self.persistence.delete_secret(GITHUB_API["secret_name"])
        return self.github_status()

    def mark_github_verified(self) -> dict:
        self._mark_verified(GITHUB_API)
        return self.github_status()

    def github_status(self) -> dict:
        return self._status(GITHUB_API)

    def resolve_git_ssh_credential(self) -> str:
        return self._resolve(GIT_SSH)

    def save_git_ssh_credential(self, credential: str) -> dict:
        value = credential.strip()
        if len(value) > 65_536:
            raise ValueError("Git SSH private key exceeds the 64 KiB limit")
        private_key_fingerprint(value)
        self._save_encrypted(GIT_SSH, value + "\n")
        return self.git_ssh_status()

    def clear_git_ssh_credential(self) -> dict:
        self.persistence.delete_secret(GIT_SSH["secret_name"])
        return self.git_ssh_status()

    def mark_git_ssh_verified(self) -> dict:
        self._mark_verified(GIT_SSH)
        return self.git_ssh_status()

    def git_ssh_status(self) -> dict:
        return self._status(GIT_SSH)

    def resolve_gcp_credential(self) -> str:
        stored = self._resolve(GCP_ADC)
        if stored:
            return self._validate_gcp_credential(stored)
        configured_file = (
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            or os.getenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "").strip()
        )
        if not configured_file:
            return ""
        path = Path(configured_file)
        if not path.is_file() or path.stat().st_size > 131_072:
            raise RuntimeError(
                "GOOGLE_APPLICATION_CREDENTIALS must reference a credential file below 128 KiB"
            )
        return self._validate_gcp_credential(path.read_text())

    def save_gcp_credential(self, credential: str) -> dict:
        value = self._validate_gcp_credential(credential)
        self._save_encrypted(GCP_ADC, value)
        return self.gcp_status()

    def clear_gcp_credential(self) -> dict:
        self.persistence.delete_secret(GCP_ADC["secret_name"])
        return self.gcp_status()

    def mark_gcp_verified(self) -> dict:
        self._mark_verified(GCP_ADC)
        return self.gcp_status()

    def gcp_status(self) -> dict:
        status = self._status(GCP_ADC)
        if status["configured"]:
            return status
        configured_file = (
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            or os.getenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "").strip()
        )
        if configured_file and Path(configured_file).is_file():
            return {
                "configured": True,
                "source": "environment-file",
                "managed": False,
                "verified_at": self.environment_verified.get(
                    GCP_ADC["secret_name"], ""
                ),
                "updated_at": "",
            }
        return status

    def _resolve(self, definition: dict) -> str:
        record = self.persistence.get_secret(definition["secret_name"])
        if record:
            try:
                return self.cipher.decrypt(record.ciphertext.encode()).decode()
            except (InvalidToken, UnicodeDecodeError) as error:
                raise RuntimeError(
                    f"stored {definition['label']} cannot be decrypted with the active master key"
                ) from error
        for variable in definition["environment"]:
            if value := os.getenv(variable, "").strip():
                return value
        return ""

    def _save(self, definition: dict, credential: str) -> None:
        value = credential.strip()
        if len(value) < 8:
            raise ValueError(f"{definition['label']} must contain at least 8 characters")
        if len(value) > 16_384:
            raise ValueError(f"{definition['label']} exceeds the 16 KiB limit")
        if any(character.isspace() for character in value):
            raise ValueError(f"{definition['label']} cannot contain whitespace")
        self._save_encrypted(definition, value)

    def _save_encrypted(self, definition: dict, value: str) -> None:
        now = self._now()
        self.persistence.save_secret(
            SecretRecord(
                name=definition["secret_name"],
                ciphertext=self.cipher.encrypt(value.encode()).decode(),
                created_at=now,
                updated_at=now,
            )
        )

    def _mark_verified(self, definition: dict) -> None:
        record = self.persistence.get_secret(definition["secret_name"])
        if record:
            self.persistence.mark_secret_verified(
                definition["secret_name"], self._now()
            )
        else:
            self.environment_verified[definition["secret_name"]] = self._now()

    def _status(self, definition: dict) -> dict:
        record = self.persistence.get_secret(definition["secret_name"])
        environment_variable = next(
            (
                variable
                for variable in definition["environment"]
                if os.getenv(variable, "").strip()
            ),
            "",
        )
        if record:
            return {
                "configured": True,
                "source": "encrypted-store",
                "managed": True,
                "verified_at": record.verified_at,
                "updated_at": record.updated_at,
            }
        if environment_variable:
            return {
                "configured": True,
                "source": "environment",
                "managed": False,
                "verified_at": self.environment_verified.get(
                    definition["secret_name"], ""
                ),
                "updated_at": "",
            }
        return {
            "configured": False,
            "source": "none",
            "managed": False,
            "verified_at": "",
            "updated_at": "",
        }

    @staticmethod
    def _definition(provider: str) -> dict:
        if provider not in AI_PROVIDERS:
            raise ValueError(f"unsupported AI agent: {provider}")
        return AI_PROVIDERS[provider]

    @staticmethod
    def _validate_gcp_credential(value: str) -> str:
        if not value.strip():
            raise ValueError("GCP credential JSON is required")
        if len(value.encode()) > 131_072:
            raise ValueError("GCP credential JSON exceeds the 128 KiB limit")
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("GCP credential must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("GCP credential JSON must contain an object")
        credential_type = payload.get("type")
        required = {
            "service_account": {"project_id", "private_key", "client_email", "token_uri"},
            "authorized_user": {"client_id", "client_secret", "refresh_token"},
            "external_account": {
                "audience",
                "subject_token_type",
                "token_url",
                "credential_source",
            },
        }
        if credential_type not in required:
            raise ValueError(
                "GCP credential type must be service_account, authorized_user, or external_account"
            )
        missing = sorted(
            field for field in required[credential_type] if not payload.get(field)
        )
        if missing:
            raise ValueError(
                "GCP credential JSON is missing required fields: " + ", ".join(missing)
            )
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def _load_or_create_key(path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            material = path.read_text().strip()
        else:
            material = Fernet.generate_key().decode()
            with os.fdopen(descriptor, "w") as stream:
                stream.write(material + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        if not material:
            raise RuntimeError(f"TerraMig secret key file is empty: {path}")
        return material

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
