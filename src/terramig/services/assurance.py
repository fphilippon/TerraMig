from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|secret_data|token|credential|private_key|"
    r"client_secret|access_key|api_key|authorization)(?:$|_)",
    re.IGNORECASE,
)
SENSITIVE_VALUE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:gh[opusr]_|AIza|ya29\.)[A-Za-z0-9._-]{12,}|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]{12,})"
)
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
FORBIDDEN_TERRAFORM = (
    (re.compile(r"\bprovisioner\s+\"(?:local-exec|remote-exec)\""), "execution provisioner"),
    (re.compile(r"\bdata\s+\"external\""), "external data source"),
    (re.compile(r"\bbackend\s+\""), "backend block"),
)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def sanitize_untrusted(
    value: Any,
    *,
    max_string: int = 12_000,
    max_list_items: int = 500,
) -> tuple[Any, int]:
    """Redact secret-shaped fields and bound untrusted strings before model input."""

    redactions = 0

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal redactions
        if depth > 12:
            return "[depth-limit]"
        if isinstance(item, dict):
            clean: dict[str, Any] = {}
            for key, child in item.items():
                normalized_key = CONTROL_CHARACTERS.sub("", str(key))[:256]
                security_key = re.sub(
                    r"([a-z0-9])([A-Z])", r"\1_\2", normalized_key
                ).replace("-", "_")
                if SENSITIVE_KEY.search(security_key):
                    clean[normalized_key] = "[REDACTED]"
                    if child != "[REDACTED]":
                        redactions += 1
                else:
                    clean[normalized_key] = visit(child, depth + 1)
            return clean
        if isinstance(item, list):
            return [
                visit(child, depth + 1)
                for child in item[: max(1, max_list_items)]
            ]
        if isinstance(item, str):
            clean = CONTROL_CHARACTERS.sub("", item)
            if SENSITIVE_VALUE.search(clean):
                redactions += 1
                return "[REDACTED]"
            return clean[:max_string]
        return item

    return visit(value), redactions


def assert_safe_terraform(terraform: str) -> list[str]:
    checks: list[str] = []
    for pattern, label in FORBIDDEN_TERRAFORM:
        if pattern.search(terraform):
            raise ValueError(f"AI configuration contains forbidden {label}")
    if SENSITIVE_VALUE.search(terraform):
        raise ValueError("AI configuration appears to contain a credential or private key")
    checks.extend(
        [
            "No execution provisioners",
            "No external data source",
            "No generated backend block",
            "No credential-shaped values",
        ]
    )
    return checks
