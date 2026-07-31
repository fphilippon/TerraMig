from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from ..adapters.terraform import LocalTerraformVerifier
from ..domain import GenerationBundle, GitDelivery
from ..services.assurance import canonical_sha256
from ..services.artifacts import (
    terraform_declaration_addresses,
    terraform_files,
    terraform_resource_files,
)
from ..services.ssh import private_key_fingerprint


GITHUB_KNOWN_HOSTS = (
    Path(__file__).resolve().parents[1] / "data" / "github_known_hosts"
)


def validate_delivery_payload(payload: dict) -> None:
    _validate_repository(
        str(payload.get("model_repository", "")).strip(), "model_repository"
    )
    _validate_repository(
        str(payload.get("target_repository", "")).strip(), "target_repository"
    )
    _validate_ref(str(payload.get("model_ref", "main")).strip() or "main", "model_ref")
    _validate_ref(
        str(payload.get("base_branch", "main")).strip() or "main", "base_branch"
    )
    if str(payload.get("branch", "")).strip():
        _validate_ref(str(payload["branch"]).strip(), "branch")
    _validate_target_path(str(payload.get("target_path", "terraform")).strip())


class MockGitDeliveryEngine:
    def deliver(
        self, workflow_id: str, bundle: GenerationBundle, delivery: GitDelivery,
        progress=None,
    ) -> GitDelivery:
        if progress:
            progress("Fixture Git delivery started")
        delivery.branch = delivery.branch or f"terramig/{workflow_id[:8]}"
        delivery.status = "pull-request-open"
        delivery.commit_sha = f"fixture-{workflow_id.replace('-', '')[:12]}"
        delivery.pull_request_url = (
            f"https://github.example/{_repository_slug(delivery.target_repository)}"
            f"/pull/42"
        )
        generated_files = terraform_files(bundle)
        if delivery.update_existing_directory:
            generated_files = {
                name: content
                for name, content in generated_files.items()
                if name != "main.tf"
            }
            generated_files.update(terraform_resource_files(bundle))
        else:
            generated_files["imports.tf"] = LocalTerraformVerifier.import_blocks(
                bundle
            )
        delivery.changed_files = [
            *(f"{delivery.target_path}/{name}" for name in generated_files),
            f"{delivery.target_path}/terramig.json",
        ]
        delivery.output = (
            "Fixture delivery copied the model repository, overlaid the generated "
            "Terraform bundle, pushed a migration branch, and opened a mock pull request."
        )
        delivery.delivered_at = _now()
        if progress:
            progress(f"Fixture Git branch pushed: {delivery.branch}")
            progress(f"Fixture pull request: {delivery.pull_request_url}")
        return delivery


class GitDeliveryEngine:
    """Safely materialize a model repository into an existing target repository."""

    def __init__(
        self,
        *,
        github_token: str = "",
        ssh_private_key: str = "",
        command_timeout: int = 180,
    ) -> None:
        self.github_token = github_token
        self.ssh_private_key = ssh_private_key
        self.command_timeout = command_timeout

    def test_ssh_connection(self, repository: str) -> dict[str, str]:
        _validate_repository(repository, "repository")
        if not self.ssh_private_key:
            raise ValueError("Save a Git SSH private key before testing the connection")
        ssh_repository = _https_to_ssh(repository)
        if not _uses_ssh(ssh_repository):
            raise ValueError(
                "SSH connection testing requires an SSH URL or a standard HTTPS repository URL"
            )
        with tempfile.TemporaryDirectory(prefix="terramig-git-test-") as temporary:
            environment = self._git_environment(Path(temporary))
            self._run(
                ["git", "ls-remote", "--heads", "--", ssh_repository],
                environment=environment,
            )
        return {
            "status": "verified",
            "message": "Git SSH repository access verified",
            "fingerprint": private_key_fingerprint(self.ssh_private_key),
        }

    def test_github_connection(self) -> dict[str, str]:
        if not self.github_token:
            raise ValueError("Save a GitHub API token before testing the connection")
        try:
            account = self._github_request("/user", method="GET")
        except Exception as error:
            raise RuntimeError(
                f"GitHub API authentication failed: {error}"
            ) from error
        login = str(account.get("login", "")).strip() if isinstance(account, dict) else ""
        if not login:
            raise RuntimeError(
                "GitHub API authentication failed: no account identity returned"
            )
        return {
            "status": "verified",
            "message": "GitHub API authentication verified",
            "account": login,
        }

    def deliver(
        self, workflow_id: str, bundle: GenerationBundle, delivery: GitDelivery,
        progress=None,
    ) -> GitDelivery:
        emit = progress or (lambda _message: None)
        _validate_repository(delivery.model_repository, "model_repository")
        _validate_repository(delivery.target_repository, "target_repository")
        delivery.model_ref = _validate_ref(delivery.model_ref, "model_ref")
        delivery.base_branch = _validate_ref(delivery.base_branch, "base_branch")
        delivery.branch = _validate_ref(
            delivery.branch or f"terramig/{workflow_id[:8]}", "branch"
        )
        delivery.target_path = _validate_target_path(delivery.target_path)
        if delivery.branch == delivery.base_branch:
            raise ValueError("delivery branch must differ from the target base branch")

        emit(
            f"Git delivery request accepted: branch={delivery.branch}, "
            f"base={delivery.base_branch}, destination={delivery.target_path}, "
            f"layout={('per-resource-update' if delivery.update_existing_directory else 'consolidated')}, "
            f"replacement={'enabled' if delivery.allow_overwrite else 'disabled'}"
        )

        model_repository = self._repository_for_git(delivery.model_repository)
        target_repository = self._repository_for_git(delivery.target_repository)
        emit(
            "Git delivery transport selected: "
            + (
                "SSH"
                if _uses_ssh(target_repository)
                else "HTTPS token (GitHub token takes precedence)"
            )
        )
        if urlparse(target_repository).scheme == "https" and not self.github_token:
            raise ValueError(
                "HTTPS target delivery requires a verified GitHub API token or a "
                "verified SSH private key; configure either credential before delivery"
            )
        if delivery.create_pull_request and not self.github_token:
            raise ValueError(
                "GitHub pull-request creation requires a verified GitHub API token; "
                "configure the token or disable pull-request creation to push the branch only"
            )

        with tempfile.TemporaryDirectory(prefix="terramig-git-") as temporary:
            root = Path(temporary)
            model = root / "model"
            target = root / "target"
            environment = self._git_environment(root)
            emit(f"Cloning model repository at {delivery.model_ref}")
            self._run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    delivery.model_ref,
                    "--",
                    model_repository,
                    str(model),
                ],
                environment=environment,
                progress=progress,
            )
            emit(f"Cloning target repository at {delivery.base_branch}")
            self._run(
                [
                    "git",
                    "clone",
                    "--branch",
                    delivery.base_branch,
                    "--",
                    target_repository,
                    str(target),
                ],
                environment=environment,
                progress=progress,
            )
            remote_branch = self._run(
                [
                    "git",
                    "ls-remote",
                    "--heads",
                    "origin",
                    f"refs/heads/{delivery.branch}",
                ],
                cwd=target,
                environment=environment,
                progress=progress,
            ).strip()
            if remote_branch:
                self._run(
                    ["git", "fetch", "origin", delivery.branch],
                    cwd=target,
                    environment=environment,
                    progress=progress,
                )
                self._run(
                    ["git", "switch", "-C", delivery.branch, "FETCH_HEAD"],
                    cwd=target,
                    environment=environment,
                    progress=progress,
                )
            else:
                self._run(
                    ["git", "switch", "-c", delivery.branch],
                    cwd=target,
                    environment=environment,
                    progress=progress,
                )

            copied, skipped = self._copy_model(model, target)
            changed, preserved = self._write_bundle(
                target, workflow_id, bundle, delivery
            )
            emit(
                f"Model overlay prepared: {len(copied)} copied, "
                f"{len(skipped) + len(preserved)} preserved, "
                f"{len(changed)} generated files changed"
            )
            self._run(["git", "add", "--all"], cwd=target, environment=environment, progress=progress)
            staged = self._run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=target,
                environment=environment,
                progress=progress,
            ).splitlines()
            if staged:
                commit_environment = {
                    **environment,
                    "GIT_AUTHOR_NAME": os.getenv(
                        "TERRAMIG_GIT_AUTHOR_NAME", "TerraMig"
                    ),
                    "GIT_AUTHOR_EMAIL": os.getenv(
                        "TERRAMIG_GIT_AUTHOR_EMAIL", "terramig@localhost"
                    ),
                    "GIT_COMMITTER_NAME": os.getenv(
                        "TERRAMIG_GIT_AUTHOR_NAME", "TerraMig"
                    ),
                    "GIT_COMMITTER_EMAIL": os.getenv(
                        "TERRAMIG_GIT_AUTHOR_EMAIL", "terramig@localhost"
                    ),
                }
                self._run(
                    [
                        "git",
                        "-c",
                        "commit.gpgsign=false",
                        "commit",
                        "-m",
                        f"TerraMig: adopt {workflow_id[:8]} into Terraform",
                    ],
                    cwd=target,
                    environment=commit_environment,
                    progress=progress,
                )
            delivery.commit_sha = self._run(
                ["git", "rev-parse", "HEAD"], cwd=target, environment=environment,
                progress=progress,
            ).strip()
            emit(f"Git commit ready: {delivery.commit_sha[:12]}")
            emit(f"Pushing branch {delivery.branch}")
            self._run(
                ["git", "push", "--set-upstream", "origin", delivery.branch],
                cwd=target,
                environment=environment,
                progress=progress,
            )
            emit(f"Git branch pushed: {delivery.branch}")

            delivery.changed_files = sorted(set(copied + changed + staged))
            delivery.skipped_template_files = sorted(set(skipped + preserved))
            delivery.status = "branch-pushed"
            if delivery.create_pull_request:
                delivery.pull_request_url = self._create_or_find_pull_request(
                    delivery, workflow_id, len(bundle.imports)
                )
                delivery.status = "pull-request-open"
                emit(f"GitHub pull request ready: {delivery.pull_request_url}")
            delivery.output = (
                f"Pushed {delivery.branch} at {delivery.commit_sha[:12]} with "
                f"{len(delivery.changed_files)} changed files. "
                f"Preserved {len(delivery.skipped_template_files)} target-owned "
                "files that collided with the model repository. "
                f"Git transport: {'SSH' if _uses_ssh(target_repository) else 'HTTPS'}."
            )
            delivery.delivered_at = _now()
            return delivery

    def _repository_for_git(self, repository: str) -> str:
        """Select a non-interactive transport from the configured credential."""
        if self.github_token:
            return _github_to_https(repository)
        if _uses_ssh(repository):
            return repository
        ssh_available = bool(
            self.ssh_private_key
            or os.getenv("SSH_AUTH_SOCK", "").strip()
            or os.getenv("GIT_SSH_COMMAND", "").strip()
        )
        return _https_to_ssh(repository) if ssh_available else repository

    @staticmethod
    def _copy_model(model: Path, target: Path) -> tuple[list[str], list[str]]:
        copied: list[str] = []
        skipped: list[str] = []
        for source in sorted(model.rglob("*")):
            relative = source.relative_to(model)
            if not relative.parts or relative.parts[0] == ".git":
                continue
            destination = target / relative
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if source.is_symlink():
                skipped.append(relative.as_posix())
                continue
            if destination.exists():
                if destination.is_file() and destination.read_bytes() == source.read_bytes():
                    continue
                skipped.append(relative.as_posix())
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative.as_posix())
        return copied, skipped

    @staticmethod
    def _write_bundle(
        target: Path,
        workflow_id: str,
        bundle: GenerationBundle,
        delivery: GitDelivery,
    ) -> tuple[list[str], list[str]]:
        destination = target.joinpath(*PurePosixPath(delivery.target_path).parts)
        destination.mkdir(parents=True, exist_ok=True)
        if delivery.update_existing_directory:
            files = {
                name: content
                for name, content in terraform_files(bundle).items()
                if name != "main.tf"
            }
            files.update(terraform_resource_files(bundle))
        else:
            files = terraform_files(bundle)
            files["imports.tf"] = LocalTerraformVerifier.import_blocks(bundle)

        existing_workflow_id = ""
        existing_managed_files: set[str] = set()
        existing_layout = ""
        existing_metadata = destination / "terramig.json"
        if existing_metadata.is_file():
            try:
                previous = json.loads(existing_metadata.read_text())
                existing_workflow_id = str(previous.get("workflow_id", ""))
                existing_layout = str(
                    previous.get("layout", "consolidated")
                )
                existing_managed_files = {
                    str(name)
                    for name in previous.get("managed_files", [])
                    if isinstance(name, str)
                }
                if (
                    previous.get("schema_version") == 1
                    and existing_workflow_id
                ):
                    existing_managed_files.update(
                        {"backend.tf", "providers.tf", "main.tf", "imports.tf"}
                    )
            except json.JSONDecodeError:
                existing_workflow_id = ""

        preserved: list[str] = []
        obsolete_managed_paths: list[Path] = []
        if delivery.update_existing_directory:
            desired_addresses = terraform_declaration_addresses(bundle.terraform)
            duplicate_addresses: dict[str, str] = {}
            for path in destination.glob("*.tf"):
                migrating_consolidated_file = (
                    existing_layout == "consolidated"
                    and path.name in {"main.tf", "imports.tf"}
                    and path.name in existing_managed_files
                )
                if path.name in files or migrating_consolidated_file:
                    continue
                for address in (
                    terraform_declaration_addresses(path.read_text())
                    & desired_addresses
                ):
                    duplicate_addresses[address] = path.name
            if duplicate_addresses:
                detail = ", ".join(
                    f"{address} ({duplicate_addresses[address]})"
                    for address in sorted(duplicate_addresses)
                )
                raise ValueError(
                    "existing Terraform directory already declares selected "
                    f"addresses: {detail}; deselect those resources or remove the "
                    "duplicate declarations before delivery"
                )
            if existing_layout == "consolidated":
                obsolete_managed_paths = [
                    destination / filename
                    for filename in ("main.tf", "imports.tf")
                    if filename in existing_managed_files
                    and (destination / filename).is_file()
                    and filename not in files
                ]
            for filename in ("backend.tf", "providers.tf"):
                path = destination / filename
                if (
                    filename in files
                    and path.is_file()
                    and filename not in existing_managed_files
                    and existing_workflow_id != workflow_id
                ):
                    files.pop(filename)
                    preserved.append(path.relative_to(target).as_posix())
            for filename, content in list(files.items()):
                path = destination / filename
                if (
                    path.is_file()
                    and path.read_text() == content
                    and filename not in existing_managed_files
                ):
                    files.pop(filename)
                    preserved.append(path.relative_to(target).as_posix())
        elif existing_layout == "per-resource":
            raise ValueError(
                "target Terraform destination already uses TerraMig's per-resource "
                "layout; keep Update an existing Terraform directory enabled"
            )

        metadata = {
            "schema_version": 2,
            "workflow_id": workflow_id,
            "generated_at": _now(),
            "model_repository": delivery.model_repository,
            "model_ref": delivery.model_ref,
            "layout": (
                "per-resource"
                if delivery.update_existing_directory
                else "consolidated"
            ),
            "terraform_sha256": canonical_sha256(
                {name: files[name] for name in sorted(files)}
            ),
            "import_operations": len(bundle.imports),
            "agent": bundle.agent,
            "prompt_sha256": bundle.prompt_sha256,
            "response_sha256": bundle.response_sha256,
            "preserved_files": sorted(preserved),
            "managed_files": sorted(
                (
                    (
                        existing_managed_files
                        - {path.name for path in obsolete_managed_paths}
                    )
                    if delivery.update_existing_directory
                    else set()
                )
                | set(files)
            ),
        }
        files["terramig.json"] = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        collisions = [
            filename
            for filename, content in files.items()
            if filename != "terramig.json"
            and (destination / filename).is_file()
            and (destination / filename).read_text() != content
            and filename not in existing_managed_files
        ]
        if (
            collisions
            and not delivery.allow_overwrite
            and (
                delivery.update_existing_directory
                or existing_workflow_id != workflow_id
            )
        ):
            raise ValueError(
                "target Terraform destination already contains different files: "
                + ", ".join(collisions)
                + (
                    "; these files are not owned by a previous TerraMig delivery; "
                    "choose another destination or explicitly allow replacement"
                    if delivery.update_existing_directory
                    else "; choose an empty path or explicitly allow replacement"
                )
            )
        changed: list[str] = []
        for path in obsolete_managed_paths:
            path.unlink()
            changed.append(path.relative_to(target).as_posix())
        for filename, content in files.items():
            path = destination / filename
            if not path.exists() or path.read_text() != content:
                path.write_text(content)
                changed.append(path.relative_to(target).as_posix())
        return changed, preserved

    def _create_or_find_pull_request(
        self, delivery: GitDelivery, workflow_id: str, import_count: int
    ) -> str:
        if not self.github_token:
            raise ValueError(
                "TERRAMIG_GITHUB_TOKEN is required to create a GitHub pull request; "
                "disable pull-request creation to push the branch only"
            )
        slug = _github_slug(delivery.target_repository)
        owner = slug.split("/", 1)[0]
        query = urlencode(
            {
                "state": "open",
                "head": f"{owner}:{delivery.branch}",
                "base": delivery.base_branch,
            }
        )
        existing = self._github_request(
            f"/repos/{slug}/pulls?{query}", method="GET"
        )
        if isinstance(existing, list) and existing:
            return str(existing[0]["html_url"])
        created = self._github_request(
            f"/repos/{slug}/pulls",
            method="POST",
            payload={
                "title": f"TerraMig adoption {workflow_id[:8]}",
                "head": delivery.branch,
                "base": delivery.base_branch,
                "body": (
                    "Generated by TerraMig from the configured model repository.\n\n"
                    f"- Workflow: `{workflow_id}`\n"
                    f"- Import operations: `{import_count}`\n"
                    "- Local validation: import-only plan with no create, update, or delete\n\n"
                    "Review and merge this change before completing the HCP Terraform import."
                ),
            },
        )
        return str(created["html_url"])

    def _github_request(
        self, path: str, *, method: str, payload: dict | None = None
    ):
        request = Request(
            f"https://api.github.com{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=30) as response:
            body = response.read()
        return json.loads(body) if body else {}

    def _git_environment(self, root: Path) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "SSH_AUTH_SOCK",
                "SSH_AGENT_PID",
                "GIT_SSH_COMMAND",
            }
        }
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        if self.github_token:
            askpass = root / "askpass.py"
            askpass.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "prompt = ' '.join(sys.argv[1:]).lower()\n"
                "print('x-access-token' if 'username' in prompt else "
                "os.environ.get('TERRAMIG_GITHUB_TOKEN', ''))\n"
            )
            askpass.chmod(0o700)
            environment["GIT_ASKPASS"] = str(askpass)
            environment["TERRAMIG_GITHUB_TOKEN"] = self.github_token
        if self.ssh_private_key:
            private_key_fingerprint(self.ssh_private_key)
            private_key = root / "id_terramig"
            private_key.write_text(self.ssh_private_key.strip() + "\n")
            private_key.chmod(0o600)
            known_hosts = root / "known_hosts"
            known_hosts.write_text(self._known_hosts())
            known_hosts.chmod(0o600)
            environment.pop("SSH_AUTH_SOCK", None)
            environment.pop("SSH_AGENT_PID", None)
            environment["GIT_SSH_COMMAND"] = shlex.join(
                [
                    "ssh",
                    "-i",
                    str(private_key),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={known_hosts}",
                ]
            )
        return environment

    @staticmethod
    def _known_hosts() -> str:
        if configured := os.getenv("TERRAMIG_GIT_KNOWN_HOSTS", "").strip():
            if len(configured) > 1_048_576:
                raise ValueError("TERRAMIG_GIT_KNOWN_HOSTS exceeds the 1 MiB limit")
            return configured + "\n"
        if configured_file := os.getenv(
            "TERRAMIG_GIT_KNOWN_HOSTS_FILE", ""
        ).strip():
            path = Path(configured_file)
            if not path.is_file() or path.stat().st_size > 1_048_576:
                raise ValueError(
                    "TERRAMIG_GIT_KNOWN_HOSTS_FILE must be a file below 1 MiB"
                )
            return path.read_text().strip() + "\n"
        return GITHUB_KNOWN_HOSTS.read_text()

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str],
        progress=None,
    ) -> str:
        emit = progress or (lambda _message: None)
        safe_command = _redact(shlex.join(command), self.github_token, self.ssh_private_key)
        emit(f"$ {safe_command}")
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def reader(name, stream):
            try:
                for line in iter(stream.readline, ""):
                    output.put((name, line))
            finally:
                try:
                    stream.close()
                except (AttributeError, OSError):
                    pass
                output.put((name, None))

        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            threading.Thread(target=reader, args=(name, stream), daemon=True).start()
        completed: set[str] = set()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        deadline = time.monotonic() + self.command_timeout
        next_heartbeat = time.monotonic() + 5
        while len(completed) < 2:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, self.command_timeout)
            try:
                name, line = output.get(timeout=1)
            except queue.Empty:
                name, line = "", ""
            if line is None and name:
                completed.add(name)
            elif line:
                (stdout_parts if name == "stdout" else stderr_parts).append(line)
                if line.strip():
                    emit(
                        f"git {name}: "
                        + _redact(line.strip()[:800], self.github_token, self.ssh_private_key)
                    )
            if time.monotonic() >= next_heartbeat:
                emit("Git command is still running")
                next_heartbeat = time.monotonic() + 5
        process.wait()
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        if process.returncode:
            detail = (stderr or stdout).strip()
            raise RuntimeError(
                f"{command[0]} {command[1]} failed: "
                f"{_redact(detail, self.github_token, self.ssh_private_key)}"
            )
        emit(f"Git command exited {process.returncode}")
        return stdout


def _validate_repository(value: str, name: str) -> None:
    if not value or len(value) > 500 or any(character in value for character in "\r\n\0"):
        raise ValueError(f"{name} must be a valid Git SSH or HTTPS repository URL")
    parsed = urlparse(value)
    embedded_http_identity = parsed.scheme == "https" and bool(
        parsed.username or parsed.password
    )
    if embedded_http_identity or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            f"{name} must not embed credentials, query parameters, or fragments"
        )
    https_or_ssh = parsed.scheme in {"https", "ssh"} and bool(parsed.hostname)
    local_test_repository = (
        os.getenv("TERRAMIG_ALLOW_LOCAL_GIT", "").lower() in {"1", "true", "yes"}
        and parsed.scheme == "file"
        and bool(parsed.path)
    )
    scp_match = re.fullmatch(
        r"[A-Za-z0-9._-]+@([A-Za-z0-9.-]+):[A-Za-z0-9._/-]+(?:\.git)?",
        value,
    )
    scp_style = bool(scp_match)
    if not (https_or_ssh or scp_style or local_test_repository):
        raise ValueError(
            f"{name} must use an HTTPS, ssh://, or git@host:path repository URL"
        )
    host = (parsed.hostname or (scp_match.group(1) if scp_match else "")).lower()
    allowed_hosts = {
        item.strip().lower()
        for item in os.getenv(
            "TERRAMIG_GIT_ALLOWED_HOSTS", "github.com"
        ).split(",")
        if item.strip()
    }
    if not local_test_repository and host not in allowed_hosts:
        raise ValueError(
            f"{name} host {host} is not in TERRAMIG_GIT_ALLOWED_HOSTS"
        )


def _validate_ref(value: str, name: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 180
        or value.startswith("-")
        or ".." in value
        or "@{" in value
        or value.endswith(("/", "."))
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", value)
    ):
        raise ValueError(f"{name} is not a safe Git ref")
    return value


def _uses_ssh(value: str) -> bool:
    return urlparse(value).scheme == "ssh" or bool(
        re.fullmatch(
            r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+(?:\.git)?",
            value,
        )
    )


def _https_to_ssh(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in {None, 443}
        or not parsed.path.strip("/")
    ):
        return value
    return f"git@{parsed.hostname}:{parsed.path.lstrip('/')}"


def _github_to_https(value: str) -> str:
    """Use token-authenticated HTTPS for any standard GitHub repository URL."""
    try:
        slug = _github_slug(value)
    except ValueError:
        return value
    return f"https://github.com/{slug}.git"


def _validate_target_path(value: str) -> str:
    path = PurePosixPath(value.strip() or "terraform")
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise ValueError("target_path must be a relative path outside .git")
    return path.as_posix()


def _repository_slug(value: str) -> str:
    try:
        return _github_slug(value)
    except ValueError:
        return re.sub(r"[^A-Za-z0-9._/-]+", "-", value).strip("-")[-100:]


def _github_slug(value: str) -> str:
    if value.startswith("git@github.com:"):
        slug = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com":
            raise ValueError("pull-request creation currently supports GitHub targets")
        slug = parsed.path.lstrip("/")
    slug = slug.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise ValueError("target repository must identify a GitHub owner/repository")
    return slug


def _redact(value: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value[-3000:]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
