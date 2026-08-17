## Summary

<!-- What problem does this change solve? Keep this focused on user or operator value. -->

## Scope

- [ ] This change is limited to the stated problem.
- [ ] Compatibility, migration, or rollout implications are documented below.

## Validation

- [ ] `PYTHONPATH=src python3 -m unittest discover -s tests -v`
- [ ] `git diff --check`
- [ ] I tested the affected workflow or failure path locally.
- [ ] UI changes include a screenshot or a clear reason why one is not needed.

## Security and privacy

- [ ] No credentials, tokens, customer names, cloud project IDs, workspace IDs,
      private repository URLs, or other sensitive data are committed.
- [ ] Logs, fixtures, and screenshots are redacted.
- [ ] Any change affecting cloud, Git, HCP Terraform, or AI permissions is
      explicitly described and remains operator-controlled.

## Operational impact

<!-- Describe performance, observability, retry, state, or deployment impact. -->

## Documentation

- [ ] User-facing documentation and configuration examples are updated when needed.
- [ ] Tests cover the new behavior and relevant failure cases.

## Review notes

<!-- Call out trade-offs, follow-up work, or areas where reviewer attention is needed. -->
