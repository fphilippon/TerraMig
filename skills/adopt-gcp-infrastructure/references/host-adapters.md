# Host adapters

The skill is plain Markdown plus deterministic scripts so agent hosts can consume the same behavior even when invocation syntax differs.

- Codex: expose this directory as a skill and invoke `$adopt-gcp-infrastructure`.
- GitHub Copilot: make the instructions available through the repository's supported custom-instruction or skill location; keep this directory as the canonical source.
- IBM Bob: register the `SKILL.md` workflow as an agent instruction and expose inventory, registry, Terraform, and HCP operations as tools.
- Other hosts: inject `SKILL.md` after the trigger description matches and resolve linked references relative to this directory.

Map host capabilities to five ports:

1. `inventory.list(project)` returns normalized GCP resources.
2. `registry.list()` returns versioned private modules and schemas.
3. `terraform_mcp.private_modules()` exposes only `search_private_modules` and `get_private_module_details` with `ENABLE_TF_OPERATIONS=false`.
4. `terraform.validate(bundle)` formats and validates without applying.
5. `state.import(approved_bundle)` performs an explicitly approved HCP Terraform state operation.

If a host cannot guarantee structured output, require it to write `bundle.json` and run `scripts/validate_bundle.py` before accepting the result. If a host cannot isolate read-only discovery from mutation, remain in mock mode.

For TerraMig programmatic generation, let TerraMig query Terraform MCP first, then enqueue composition as a durable background job and invoke GitHub Copilot with `copilot -p <prompt> -s --no-ask-user --no-color --no-custom-instructions` or IBM Bob with `bob -p <prompt> --hide-intermediary-output`. Embed this skill, `agent-contract.md`, and the bounded MCP result in the prompt. Prohibit further tool use, launch without a shell, and publish safe lifecycle milestones such as process start, elapsed time, parsing, assurance, and bounded repair attempts. Do not stream hidden intermediary reasoning or secrets. Validate every returned JSON proposal before continuing, including every complete replacement produced by self-repair.
