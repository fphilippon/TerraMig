from __future__ import annotations

import json
import re

from ..domain import GenerationBundle


_HOST_OWNED_BLOCK = re.compile(
    r'(?m)^[ \t]*(?:terraform\s*\{|provider\s+"google"\s*\{|'
    r'variable\s+"project_id"\s*\{)'
)
_TOP_LEVEL_BLOCK = re.compile(
    r'(?m)^[ \t]*(?P<kind>[A-Za-z_][A-Za-z0-9_-]*)'
    r'(?P<labels>(?:[ \t]+"[^"\r\n]+"){0,2})[ \t]*\{'
)


def finalize_terraform_artifacts(
    bundle: GenerationBundle,
    hcp_target: dict[str, str],
    provider_version: str,
    project_id: str = "",
) -> GenerationBundle:
    """Separate host-owned Terraform wiring from AI-composed infrastructure."""

    bundle.terraform = _remove_host_owned_blocks(bundle.terraform).strip() + "\n"
    bundle.providers_tf = _providers_configuration(provider_version, project_id)
    bundle.backend_tf = _cloud_backend_configuration(hcp_target)
    return bundle


def terraform_files(bundle: GenerationBundle) -> dict[str, str]:
    files = {
        "backend.tf": bundle.backend_tf,
        "providers.tf": bundle.providers_tf,
        "main.tf": bundle.terraform,
    }
    return {name: content for name, content in files.items() if content.strip()}


def terraform_resource_files(bundle: GenerationBundle) -> dict[str, str]:
    """Render a verified bundle as stable, additive per-resource Git files."""

    files: dict[str, str] = {}
    for filename, content in _split_top_level_blocks(bundle.terraform):
        _append_file(files, filename, content)
    for operation in bundle.imports:
        filename = terraform_address_filename(operation.address)
        if filename not in files:
            filename = f"import_{filename}"
        _append_file(
            files,
            filename,
            (
                "import {\n"
                f"  to = {operation.address}\n"
                f"  id = {json.dumps(operation.remote_id)}\n"
                "}\n"
            ),
        )
    return {name: content for name, content in files.items() if content.strip()}


def terraform_address_filename(address: str) -> str:
    """Return the file that owns a Terraform resource or module address."""

    parts = address.strip().split(".")
    if len(parts) >= 2 and parts[0] == "module":
        return f"module_{_file_component(_without_index(parts[1]))}.tf"
    if len(parts) >= 2:
        return (
            f"{_file_component(parts[0])}_"
            f"{_file_component(_without_index(parts[1]))}.tf"
        )
    return f"resource_{_file_component(address)}.tf"


def terraform_declaration_addresses(terraform: str) -> set[str]:
    """Return resource/module/data identities declared at Terraform's top level."""

    addresses: set[str] = set()
    for kind, labels, _content in _top_level_blocks(terraform):
        if kind == "resource" and len(labels) >= 2:
            addresses.add(f"{labels[0]}.{labels[1]}")
        elif kind == "module" and labels:
            addresses.add(f"module.{labels[0]}")
        elif kind == "data" and len(labels) >= 2:
            addresses.add(f"data.{labels[0]}.{labels[1]}")
    return addresses


def _split_top_level_blocks(terraform: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for kind, labels, content in _top_level_blocks(terraform):
        filename = _block_filename(kind, labels) if kind else "main.tf"
        blocks.append((filename, content))
    return blocks


def _top_level_blocks(terraform: str) -> list[tuple[str, list[str], str]]:
    masked = _mask_strings_and_comments(terraform)
    depth = 0
    depths = [0] * (len(masked) + 1)
    for index, character in enumerate(masked):
        depths[index] = depth
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)

    blocks: list[tuple[str, list[str], str]] = []
    cursor = 0
    for match in _TOP_LEVEL_BLOCK.finditer(terraform):
        if depths[match.start()] != 0:
            continue
        opening = terraform.find("{", match.start(), match.end())
        block_depth = 0
        end = -1
        for index in range(opening, len(terraform)):
            character = masked[index]
            if character == "{":
                block_depth += 1
            elif character == "}":
                block_depth -= 1
                if block_depth == 0:
                    end = index + 1
                    break
        if end < 0:
            raise ValueError("generated Terraform contains an unterminated top-level block")
        labels = re.findall(r'"([^"\r\n]+)"', match.group("labels"))
        content = terraform[cursor:end].strip()
        if content:
            blocks.append((match.group("kind").lower(), labels, content + "\n"))
        cursor = end

    remainder = terraform[cursor:].strip()
    if remainder:
        blocks.append(("", [], remainder + "\n"))
    if not blocks and terraform.strip():
        blocks.append(("", [], terraform.strip() + "\n"))
    return blocks


def _block_filename(kind: str, labels: list[str]) -> str:
    kind = kind.lower()
    if kind == "resource" and len(labels) >= 2:
        return (
            f"{_file_component(labels[0])}_{_file_component(labels[1])}.tf"
        )
    if kind == "module" and labels:
        return f"module_{_file_component(labels[0])}.tf"
    if kind == "data" and len(labels) >= 2:
        return (
            f"data_{_file_component(labels[0])}_{_file_component(labels[1])}.tf"
        )
    conventional = {
        "locals": "locals.tf",
        "output": "outputs.tf",
        "variable": "variables.tf",
        "moved": "moved.tf",
        "check": "checks.tf",
    }
    if kind in conventional:
        return conventional[kind]
    suffix = "_".join(_file_component(label) for label in labels)
    return f"{_file_component(kind)}{f'_{suffix}' if suffix else ''}.tf"


def _append_file(files: dict[str, str], filename: str, content: str) -> None:
    existing = files.get(filename, "").rstrip()
    addition = content.strip()
    files[filename] = (
        f"{existing}\n\n{addition}\n" if existing else f"{addition}\n"
    )


def _file_component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip()).strip("_").lower()
    return (component or "unnamed")[:100]


def _without_index(value: str) -> str:
    return value.split("[", 1)[0]


def _providers_configuration(provider_version: str, project_id: str) -> str:
    version = provider_version.strip() or "7.39.0"
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError(f"invalid reviewed Google provider version: {version}")
    default = f"  default     = {json.dumps(project_id.strip())}\n" if project_id.strip() else ""
    return (
        "terraform {\n"
        '  required_version = ">= 1.7.0"\n\n'
        "  required_providers {\n"
        "    google = {\n"
        '      source  = "hashicorp/google"\n'
        f'      version = "= {version}"\n'
        "    }\n"
        "  }\n"
        "}\n\n"
        'provider "google" {\n'
        "  project = var.project_id\n"
        "}\n\n"
        'variable "project_id" {\n'
        '  description = "Existing GCP project adopted by TerraMig."\n'
        "  type        = string\n"
        f"{default}"
        "}\n"
    )


def _cloud_backend_configuration(hcp_target: dict[str, str]) -> str:
    hostname = str(hcp_target.get("hostname", "app.terraform.io")).strip()
    organization = str(hcp_target.get("organization", "")).strip()
    workspace = str(hcp_target.get("workspace", "")).strip()
    if not organization or not workspace:
        return ""
    return (
        "terraform {\n"
        "  cloud {\n"
        f"    hostname     = {json.dumps(hostname)}\n"
        f"    organization = {json.dumps(organization)}\n\n"
        "    workspaces {\n"
        f"      name = {json.dumps(workspace)}\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def _remove_host_owned_blocks(terraform: str) -> str:
    masked = _mask_strings_and_comments(terraform)
    depth = 0
    depths = [0] * (len(masked) + 1)
    for index, character in enumerate(masked):
        depths[index] = depth
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
    ranges: list[tuple[int, int]] = []
    for match in _HOST_OWNED_BLOCK.finditer(terraform):
        if depths[match.start()] != 0:
            continue
        opening = terraform.find("{", match.start(), match.end())
        block_depth = 0
        end = -1
        for index in range(opening, len(terraform)):
            character = masked[index]
            if character == "{":
                block_depth += 1
            elif character == "}":
                block_depth -= 1
                if block_depth == 0:
                    end = index + 1
                    break
        if end < 0:
            raise ValueError("generated Terraform contains an unterminated host-owned block")
        ranges.append((match.start(), end))
    result = terraform
    for start, end in reversed(ranges):
        result = result[:start] + result[end:]
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def _mask_strings_and_comments(source: str) -> str:
    masked = list(source)
    index = 0
    while index < len(source):
        heredoc = re.match(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)", source[index:])
        if heredoc:
            delimiter = heredoc.group(1)
            header_end = source.find("\n", index)
            if header_end < 0:
                _blank(masked, index, len(source))
                break
            terminator = re.search(
                rf"(?m)^[ \t]*{re.escape(delimiter)}[ \t]*(?:\n|$)",
                source[header_end + 1 :],
            )
            end = (
                len(source)
                if not terminator
                else header_end + 1 + terminator.end()
            )
            _blank(masked, index, end)
            index = end
        elif source[index] == '"':
            index = _mask_quoted(source, masked, index)
        elif source.startswith("//", index) or source[index] == "#":
            end = source.find("\n", index)
            end = len(source) if end < 0 else end
            _blank(masked, index, end)
            index = end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            _blank(masked, index, end)
            index = end
        else:
            index += 1
    return "".join(masked)


def _mask_quoted(source: str, masked: list[str], start: int) -> int:
    index = start + 1
    while index < len(source):
        if source[index] == "\\":
            index += 2
            continue
        if source[index] == '"':
            index += 1
            break
        index += 1
    _blank(masked, start, min(index, len(source)))
    return index


def _blank(masked: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if masked[index] != "\n":
            masked[index] = " "
