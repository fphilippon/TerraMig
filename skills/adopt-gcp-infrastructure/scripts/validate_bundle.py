#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path


def validate(bundle: dict) -> list[str]:
    errors: list[str] = []
    unexpected = set(bundle) - {"terraform", "imports", "decisions", "warnings"}
    if unexpected:
        errors.append("unexpected top-level fields: " + ", ".join(sorted(unexpected)))
    terraform = bundle.get("terraform")
    if not isinstance(terraform, str) or not terraform.strip():
        errors.append("terraform must be a non-empty string")
    imports = bundle.get("imports")
    if not isinstance(imports, list) or not imports:
        errors.append("imports must be a non-empty list")
        imports = []
    seen_addresses: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(imports):
        if not isinstance(item, dict):
            errors.append(f"imports[{index}] must be an object")
            continue
        for field in ("address", "remote_id", "command"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                errors.append(f"imports[{index}].{field} must be a non-empty string")
        address = item.get("address")
        remote_id = item.get("remote_id")
        if address in seen_addresses:
            errors.append(f"duplicate import address: {address}")
        if remote_id in seen_ids:
            errors.append(f"duplicate remote id: {remote_id}")
        if address:
            seen_addresses.add(address)
        if remote_id:
            seen_ids.add(remote_id)
        if all(isinstance(item.get(field), str) for field in ("address", "remote_id", "command")):
            try:
                command = shlex.split(item["command"])
            except ValueError:
                errors.append(f"imports[{index}].command has invalid quoting")
            else:
                if command != ["terraform", "import", address, remote_id]:
                    errors.append(
                        f"imports[{index}].command does not match address and remote id"
                    )
    for field in ("decisions", "warnings"):
        if field in bundle and (
            not isinstance(bundle[field], list)
            or not all(isinstance(item, str) for item in bundle[field])
        ):
            errors.append(f"{field} must be a list of strings")
    if isinstance(terraform, str):
        forbidden = {
            r'\bprovisioner\s+"(?:local-exec|remote-exec)"': "execution provisioner",
            r'\bdata\s+"external"': "external data source",
            r'\bbackend\s+"': "backend block",
        }
        for pattern, label in forbidden.items():
            if re.search(pattern, terraform):
                errors.append(f"terraform contains forbidden {label}")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_bundle.py <bundle.json>", file=sys.stderr)
        return 2
    try:
        bundle = json.loads(Path(sys.argv[1]).read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(f"invalid bundle file: {error}", file=sys.stderr)
        return 2
    errors = validate(bundle)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"valid adoption bundle: {len(bundle['imports'])} import operations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
