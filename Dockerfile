# syntax=docker/dockerfile:1

ARG NODE_IMAGE=node:22-bookworm-slim
ARG RUNTIME_IMAGE=python:3.13-slim-bookworm
ARG GO_IMAGE=golang:1.27.3-bookworm@sha256:d9b6a1beb9474adc2a1ce842573639b44b2a0dd49b6669f51d3821baf9ec37c9

# Build the verified MCP 1.4.1 release with a patched Go toolchain.
FROM ${GO_IMAGE} AS terraform-mcp-tools

ARG TARGETARCH
ARG TERRAFORM_MCP_REVISION=3781509a674482ca7e2821fab8159a25e3b29bdf
ARG TERRAFORM_MCP_SOURCE_SHA256=2e509eb8c605bcca3e8fbf66175ff8fb459eb21a43ae297e66bebed3af33f08b
ARG TERRAFORM_MCP_GRPC_VERSION=1.83.2

WORKDIR /build
RUN curl --fail --silent --show-error --location \
      "https://codeload.github.com/hashicorp/terraform-mcp-server/tar.gz/${TERRAFORM_MCP_REVISION}" \
      --output /tmp/terraform-mcp-source.tar.gz \
    && printf '%s  %s\n' "${TERRAFORM_MCP_SOURCE_SHA256}" /tmp/terraform-mcp-source.tar.gz \
      | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/terraform-mcp-source.tar.gz --strip-components=1 \
    && go get "google.golang.org/grpc@v${TERRAFORM_MCP_GRPC_VERSION}" \
    && go test ./cmd/terraform-mcp-server ./version \
    && CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build \
      -ldflags="-s -w -X github.com/hashicorp/terraform-mcp-server/version.VersionMetadata=terramig.grpc${TERRAFORM_MCP_GRPC_VERSION}" \
      -o /bin/terraform-mcp-server ./cmd/terraform-mcp-server

FROM ${NODE_IMAGE} AS ai-cli-tools

ARG COPILOT_VERSION=1.0.92
ARG BOB_VERSION=1.0.6
ARG BOB_SHA256=6ec51abec4251d41ec45709030988b90baa659f535fc8d14dd003023dd163a5b

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# GitHub documents the @github/copilot npm package for Linux and automation.
RUN npm_config_update_notifier=false npm install --global "@github/copilot@${COPILOT_VERSION}" \
    && npm cache clean --force \
    && copilot --version

# Install the fixed Bob package directly and verify the vendor artifact.
RUN curl --fail --silent --show-error --location \
      "https://s3.us-south.cloud-object-storage.appdomain.cloud/bob-shell/bobshell-${BOB_VERSION}.tgz" \
      --output /tmp/bobshell.tgz \
    && printf '%s  %s\n' "${BOB_SHA256}" /tmp/bobshell.tgz | sha256sum --check --strict \
    && npm install --global /tmp/bobshell.tgz \
    && rm -f /tmp/bobshell.tgz \
    && npm cache clean --force \
    && bob --version

FROM ${RUNTIME_IMAGE} AS hashicorp-cli-tools

ARG TARGETARCH
ARG TERRAFORM_VERSION=1.17.1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --silent --show-error --location \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
      --output "/tmp/terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" \
    && curl --fail --silent --show-error --location \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_SHA256SUMS" \
      --output /tmp/terraform_SHA256SUMS \
    && cd /tmp \
    && grep "terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip$" /tmp/terraform_SHA256SUMS \
      | sha256sum --check --strict \
    && unzip "terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip" -d /usr/local/bin \
    && terraform version

FROM ${RUNTIME_IMAGE}

ARG TERRAMIG_UID=10001
ARG TERRAMIG_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TERRAMIG_HOST=0.0.0.0 \
    TERRAMIG_PORT=8080 \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir --only-binary=:all: --requirement requirements.txt
COPY scripts/patch_gcloud_dependencies.py /tmp/patch_gcloud_dependencies.py

# Keep the base image's packaging tooling outside known vulnerable versions.
RUN python3 -m pip install --no-cache-dir --only-binary=:all: \
      --upgrade "setuptools>=78.1.1"

# Install gcloud from Google's signed Debian repository.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl git gnupg openssh-client \
    && curl --fail --silent --show-error --location \
      https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor --output /usr/share/keyrings/cloud.google.gpg \
    && printf '%s\n' \
      'deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main' \
      > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update \
    && CLOUDSDK_SKIP_PY_COMPILATION=1 apt-get install --yes --no-install-recommends google-cloud-cli \
    && python3 -m pip download --no-cache-dir --only-binary=:all: --no-deps \
      --dest /tmp/gcloud-patches \
      "msgpack==1.2.4" "setuptools>=78.1.1" \
    && python3 /tmp/patch_gcloud_dependencies.py \
    && rm -rf /tmp/gcloud-patches /tmp/patch_gcloud_dependencies.py /var/lib/apt/lists/*

# pip is needed only while building. Removing it also removes its vendored
# urllib3 copy, which currently has no patched pip release.
RUN python3 -m pip uninstall --yes pip

ENV HOME=/app

# Both AI CLIs are Node applications. Copy the Node runtime and package payloads
# from the build stage without retaining npm caches. Recreate Copilot's launcher
# as a symlink so Node resolves dependencies relative to its npm package.
COPY --from=ai-cli-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=ai-cli-tools /usr/local/bin/bob /usr/local/bin/bob
COPY --from=ai-cli-tools /usr/local/lib/node_modules/@github /usr/local/lib/node_modules/@github
COPY --from=ai-cli-tools /usr/local/lib/node_modules/bobshell /usr/local/lib/node_modules/bobshell
COPY --from=hashicorp-cli-tools /usr/local/bin/terraform /usr/local/bin/terraform
COPY --from=terraform-mcp-tools /bin/terraform-mcp-server /usr/local/bin/terraform-mcp-server
COPY --from=terraform-mcp-tools /build/LICENSE /usr/local/share/licenses/terraform-mcp-server/LICENSE

RUN ln -s /usr/local/lib/node_modules/@github/copilot/npm-loader.js /usr/local/bin/copilot \
    && groupadd --gid "${TERRAMIG_GID}" --system terramig \
    && useradd --uid "${TERRAMIG_UID}" --gid terramig --system \
       --home-dir /app --shell /usr/sbin/nologin terramig \
    && command -v gcloud \
    && command -v copilot \
    && command -v bob \
    && command -v terraform \
    && command -v terraform-mcp-server

COPY --chown=terramig:terramig pyproject.toml README.md LICENSE ./
COPY --chown=terramig:terramig src ./src
COPY --chown=terramig:terramig web ./web
COPY --chown=terramig:terramig skills ./skills
COPY --chown=terramig:terramig scripts ./scripts

RUN mkdir -p \
       /app/.terramig /app/.config/gcloud /app/.cache /app/.bob/settings /app/.copilot /app/.terraform.d/plugin-cache \
    && chown -R terramig:terramig \
       /app/.terramig /app/.config /app/.cache /app/.bob /app/.copilot /app/.terraform.d

USER terramig

EXPOSE 8080
VOLUME ["/app/.terramig"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2).read()" || exit 1

CMD ["python3", "-m", "terramig.server"]
