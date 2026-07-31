#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from terramig.adapters.real import HCPRegistry
from terramig.services.capabilities import CapabilityCatalog
from terramig.services.catalog import ModuleCatalogBuilder, TerraformModuleInspector


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic TerraMig catalog from HCP private modules."
    )
    parser.add_argument("--hostname", default="app.terraform.io")
    parser.add_argument("--organization", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--terraform", default="terraform")
    arguments = parser.parse_args()
    token = os.getenv("TERRAMIG_HCP_TOKEN", "")
    if not token:
        parser.error("TERRAMIG_HCP_TOKEN is required")
    capabilities = CapabilityCatalog()
    registry = HCPRegistry(arguments.hostname, arguments.organization, token)
    inspector = TerraformModuleInspector(
        arguments.hostname,
        token,
        terraform_binary=arguments.terraform,
        capability_catalog=capabilities,
    )
    catalog = ModuleCatalogBuilder(
        registry,
        inspector,
        arguments.organization,
        capabilities.provider_version,
    ).build(os.getenv("TERRAMIG_CATALOG_SIGNING_KEY", ""))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(catalog, indent=2) + "\n")
    verified = sum(
        module["schema_status"] == "verified" for module in catalog["modules"]
    )
    print(
        f"Wrote {len(catalog['modules'])} modules ({verified} verified, "
        f"{len(catalog['failures'])} failed) to {arguments.output}"
    )
    return 1 if catalog["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
