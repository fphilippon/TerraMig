from __future__ import annotations

import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa


SUPPORTED_PRIVATE_KEYS = (
    rsa.RSAPrivateKey,
    ec.EllipticCurvePrivateKey,
    ed25519.Ed25519PrivateKey,
)


def validate_private_key(value: str) -> object:
    """Load a non-interactive OpenSSH/PEM key and reject legacy algorithms."""

    encoded = value.strip().encode()
    if not encoded:
        raise ValueError("Git SSH private key is required")
    loaders = (
        serialization.load_ssh_private_key,
        serialization.load_pem_private_key,
    )
    key = None
    password_protected = False
    for loader in loaders:
        try:
            key = loader(encoded, password=None)
            break
        except TypeError:
            password_protected = True
        except (ValueError, UnsupportedAlgorithm):
            continue
    if key is None:
        if password_protected:
            raise ValueError(
                "Passphrase-protected Git SSH keys are not supported by background jobs; "
                "use a dedicated unencrypted deploy key"
            )
        raise ValueError("Git SSH private key is not valid OpenSSH or PEM data")
    if isinstance(key, dsa.DSAPrivateKey) or not isinstance(
        key, SUPPORTED_PRIVATE_KEYS
    ):
        raise ValueError("Git SSH private key algorithm is not supported")
    return key


def private_key_fingerprint(value: str) -> str:
    key = validate_private_key(value)
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    )
    encoded = public.split()[1]
    digest = hashlib.sha256(base64.b64decode(encoded)).digest()
    return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode()
