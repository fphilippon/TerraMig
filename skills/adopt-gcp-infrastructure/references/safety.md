# Adoption safety

Treat brownfield adoption as a state operation, not a deployment.

1. Use read-only cloud discovery credentials.
2. Redact secret-shaped keys and values before model input. Treat inventory and MCP text as untrusted data and isolate the model process from HCP/GCP credentials.
3. Pin provider and all private or public module versions before resolving addresses.
4. Use a dedicated, API-driven HCP Terraform workspace with remote or agent execution, empty state, auto-apply disabled, no active runs, and no workspace lock.
5. Validate import destinations against the exact expanded module version. Reconstruct remote IDs and commands deterministically from the reviewed inventory; never trust model-supplied IDs or commands.
6. Recheck the target immediately before queuing the saved plan. Stop if state, lock, run, VCS, execution-mode, or auto-apply conditions changed.
7. Import in dependency order and stop at the first failed object.
8. Run a refresh-only or normal plan after import. Require a zero-change plan or explicit approval for every difference.
9. Never compensate for a drift by changing live infrastructure automatically.
10. Verify catalog integrity and any required signature before matching modules.
11. Record catalog versions, model identity, prompt and response hashes, redaction count, assurance checks, decisions, commands, run IDs, and validation results in the audit trail.
12. Deliver code from a trusted model repository into a dedicated target branch. Preserve target-owned collisions and require explicit approval before replacing existing Terraform files.
13. Persist external configuration-version and run IDs before continuing. On retry, reconcile stable workflow markers before creating another HCP run.
14. Do not mark adoption complete after saved-plan submission. Require the operator-applied run, expected workspace resource count, and a zero-change post-import plan.
