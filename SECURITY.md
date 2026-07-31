# Security policy

Report suspected vulnerabilities privately through GitHub Security Advisories for this
repository. Do not include HCP Terraform tokens, GCP credentials, Terraform state, AI
transcripts, or customer resource attributes in an issue.

TerraMig releases are supported until the next minor release. Every release image must pass
unit tests, CodeQL, dependency auditing, container vulnerability scanning, SBOM generation,
and build-provenance attestation. Production deployments must use immutable image digests.
Any vulnerability exception must be limited to the exact affected image path, explain why a
patched upstream artifact is unavailable, and include a short expiration date in
`.trivyignore.yaml`. Expired exceptions fail the image scan and must not be extended without a
new review of the vendor release and exploitability.

The `admin` / `admin` account is a bootstrap mechanism for an empty local database only.
TerraMig forces an immediate password change. Production deployments must enable PostgreSQL,
set `TERRAMIG_REQUIRE_POSTGRES=true`, use TLS with `TERRAMIG_SECURE_COOKIES=true`, restrict
database network access, and protect database backups as sensitive migration metadata.

Git clone and push credentials may be provided through an application-managed SSH deploy key,
`TERRAMIG_GIT_SSH_PRIVATE_KEY`, a forwarded SSH agent, or `TERRAMIG_GITHUB_TOKEN` for HTTPS.
Application-managed keys are encrypted before PostgreSQL storage and written only to mode-`0600`
files in short-lived Git job directories. Repository URLs containing embedded HTTPS credentials
are rejected. Use dedicated, least-privilege SSH keys or GitHub tokens limited to the intended
target repositories. GitHub pull-request creation always requires `TERRAMIG_GITHUB_TOKEN` for
the API call, even when SSH authenticates clone and push. Treat model repositories as trusted
executable supply-chain inputs: review their workflow files and protect their default branches.
