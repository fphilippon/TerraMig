# Contributing to TerraMig

Thank you for helping improve TerraMig. Contributions are welcome when they
make brownfield discovery, Terraform generation, validation, Git delivery, or
HCP Terraform adoption safer and easier to operate.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## Before you start

* Search existing issues and pull requests before opening a new one.
* For security vulnerabilities, use the private process described in
  [SECURITY.md](SECURITY.md); do not open a public issue.
* Do not include credentials, tokens, customer names, cloud project IDs,
  workspace IDs, private repository URLs, or other sensitive data in issues,
  fixtures, logs, screenshots, or pull requests.
* Keep changes focused. Separate unrelated refactoring from behavior changes.

## Local development

The supported local workflow uses the repository's container stack:

```sh
cp .env.example .env
docker compose up --build
```

The application is then available at `http://localhost:8080`. Real cloud,
HCP Terraform, GitHub, and AI credentials are optional for development; use
mocked or fixture-backed workflows whenever possible.

For direct Python development:

```sh
python3 -m pip install -r requirements.txt
```

## Tests and checks

Run the relevant checks before opening a pull request:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

If you change container or dependency files, also build the stack locally and
include the result in the pull request. If you change Terraform generation or
validation, include a representative fixture and explain how drift and import
addresses were checked.

## Design and implementation guidance

* Preserve the explicit workflow boundaries: discover, match, generate,
  verify, deliver, and import.
* Keep cloud discovery read-only. Any operation that can mutate cloud, Git, or
  HCP Terraform state must remain explicit and clearly visible to the operator.
* Prefer deterministic behavior and stable import addresses. AI-generated
  proposals must remain reviewable, bounded, and covered by assurance checks.
* Redact secrets and customer identifiers from logs and test data.
* Add or update tests for changed behavior, including failure paths and retry
  behavior where relevant.
* Update user-facing documentation when configuration, safeguards, or workflow
  behavior changes.

## Pull requests

A good pull request explains the problem, the chosen approach, and how the
change was verified. Please complete the pull-request template and include:

1. A concise summary of the user or operator problem.
2. The scope and any compatibility or migration considerations.
3. Tests and commands run, including any limitations.
4. UI screenshots for meaningful user-interface changes, with sensitive data
   removed.
5. Security, privacy, and operational impact notes.

Maintainers may ask for smaller commits, additional tests, documentation, or a
follow-up issue before merging. Review is collaborative: respond to feedback
constructively and keep the discussion focused on the code and its impact.

## Commit messages

Use short, imperative commit subjects, for example:

```text
Add structured discovery progress events
```

Keep commits focused and avoid including generated artifacts, local state,
credentials, or unrelated formatting changes.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE).
