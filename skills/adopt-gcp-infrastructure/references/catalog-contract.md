# HCP Private Library catalog contract

Build the catalog from exact HCP Terraform Private Library module versions, including native
private modules and public modules curated into the organization library. Use Terraform CLI to
download each exact version, then inspect only root-module `.tf` files.

For every module, record:

- exact `source` and semantic `version`;
- root input and output names;
- GCP Cloud Asset types mapped from managed Terraform resource types;
- every exact root `managed_resource_addresses[]` entry and the legacy singular
  `managed_resource_address` when ownership is unambiguous;
- `schema_status` and inspection diagnostics.

Automatically mark every successfully loaded HCP Private Library module `trusted`. Multi-resource
modules remain eligible: match only catalog addresses whose Terraform type has a deterministic
TerraMig GCP capability mapping. Mark a module `incompatible` when Terraform cannot load its exact
version, and never offer it for composition. Never invent or heuristically rewrite a resource
address.

Canonicalize the complete catalog without its integrity envelope and calculate SHA-256.
When a signing key is configured, sign that digest with HMAC-SHA-256. Verify the digest and
required signature before module matching. Match modules by exact `source@version`; do not
let live MCP documentation override the approved catalog.
