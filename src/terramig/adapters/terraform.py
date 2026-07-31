from __future__ import annotations

import json
import os
import re
import queue
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from ..domain import GenerationBundle, VerificationResult
from .gcp_auth import gcloud_credential_environment


class LocalTerraformVerifier:
    """Validate and plan declarative imports in an isolated local working directory."""

    def __init__(
        self,
        binary: str = "terraform",
        timeout_seconds: int = 300,
        hcp_hostname: str = "app.terraform.io",
        hcp_token: str = "",
        gcp_credential: str = "",
    ) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.environment = os.environ.copy()
        if hcp_token:
            token_name = "TF_TOKEN_" + hcp_hostname.replace(".", "_").replace("-", "__")
            self.environment[token_name] = hcp_token
        self.gcp_credential = gcp_credential

    def verify(self, project_id: str, bundle: GenerationBundle, progress=None) -> VerificationResult:
        emit = progress or (lambda _message: None)
        emit(f"Terraform verification prepared for {len(bundle.imports)} import operations")
        local_terraform = self.without_cloud_block(bundle.terraform)
        providers_terraform = bundle.providers_tf
        plan_arguments = [
            self.binary,
            "plan",
            "-input=false",
            "-refresh=true",
            "-detailed-exitcode",
            "-out=adoption.tfplan",
        ]
        if self.declares_project_variable(local_terraform + "\n" + providers_terraform):
            plan_arguments.append(f"-var=project_id={project_id}")
        plan_arguments.append("-no-color")
        commands = [
            "terraform init -backend=false -input=false",
            "terraform validate -json",
            " ".join(plan_arguments),
            "terraform show -json adoption.tfplan",
        ]
        with ExitStack() as stack:
            credential_environment = stack.enter_context(
                gcloud_credential_environment(self.gcp_credential)
            )
            environment = dict(self.environment)
            if credential_environment:
                for name in (
                    "GOOGLE_APPLICATION_CREDENTIALS",
                    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
                ):
                    environment[name] = credential_environment[name]
            environment["GOOGLE_PROJECT"] = project_id
            environment["GOOGLE_CLOUD_PROJECT"] = project_id
            directory = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="terramig-verify-")
            )
            root = Path(directory)
            (root / "main.tf").write_text(local_terraform)
            if providers_terraform.strip():
                (root / "providers.tf").write_text(providers_terraform)
            (root / "imports.tf").write_text(self.import_blocks(bundle))
            version = self._run(
                [self.binary, "version", "-json"], root, environment=environment,
                progress=progress,
            )
            terraform_version = "unknown"
            if version.returncode == 0:
                try:
                    terraform_version = json.loads(version.stdout).get("terraform_version", "unknown")
                except json.JSONDecodeError:
                    terraform_version = version.stdout.splitlines()[0] if version.stdout else "unknown"

            init = self._run(
                [self.binary, "init", "-backend=false", "-input=false", "-no-color"],
                root,
                environment=environment,
                progress=progress,
            )
            if init.returncode:
                return self._failure(terraform_version, commands, "Terraform initialization failed", init)

            validate = self._run(
                [self.binary, "validate", "-json"], root, environment=environment,
                progress=progress,
            )
            try:
                validation = json.loads(validate.stdout)
            except json.JSONDecodeError:
                return self._failure(terraform_version, commands, "Terraform validation returned invalid output", validate)
            diagnostics = []
            for item in validation.get("diagnostics", []):
                summary = item.get("summary", "Terraform diagnostic")
                detail = str(item.get("detail", "")).strip()
                diagnostic = f"{item.get('severity', 'error')}: {summary}"
                if detail:
                    diagnostic += " — " + re.sub(r"\s+", " ", detail)[:700]
                diagnostics.append(diagnostic)
            if validate.returncode or not validation.get("valid"):
                return VerificationResult(False, False, terraform_version, commands, "Terraform validation failed", diagnostics, validate.stdout + validate.stderr)

            plan = self._run(
                plan_arguments,
                root,
                accepted=(0, 2),
                environment=environment,
                progress=progress,
            )
            if plan.returncode not in (0, 2):
                return self._failure(terraform_version, commands, "Terraform plan failed", plan, diagnostics)
            show = self._run(
                [self.binary, "show", "-json", "adoption.tfplan"],
                root,
                environment=environment,
                progress=progress,
            )
            if show.returncode:
                return self._failure(terraform_version, commands, "Unable to inspect Terraform plan", show, diagnostics)
            try:
                plan_json = json.loads(show.stdout)
            except json.JSONDecodeError:
                return self._failure(terraform_version, commands, "Terraform plan JSON is invalid", show, diagnostics)

            changes = plan_json.get("resource_changes", [])
            planned_imports = sum(
                1 for item in changes if item.get("change", {}).get("importing") is not None
            )
            drift = [
                item.get("address", "unknown")
                for item in changes
                if item.get("change", {}).get("actions", []) not in ([], ["no-op"], ["read"])
            ]
            if drift:
                diagnostics.append("Drift-producing actions: " + ", ".join(drift))
            if planned_imports != len(bundle.imports):
                diagnostics.append(
                    f"Expected {len(bundle.imports)} planned imports but Terraform reported {planned_imports}"
                )
            success = not drift and planned_imports == len(bundle.imports)
            emit(
                f"Terraform plan inspected: {planned_imports} imports, "
                f"{len(drift)} drift-producing actions"
            )
            return VerificationResult(
                success=success,
                drift_detected=bool(drift),
                terraform_version=terraform_version,
                commands=commands,
                summary=(
                    f"Import-only plan verified for {planned_imports} resources; no drift detected."
                    if success
                    else "Local plan is not import-only; HCP import is blocked."
                ),
                diagnostics=diagnostics,
                output=(plan.stdout + "\n" + plan.stderr)[-8000:],
                planned_imports=planned_imports,
            )

    @staticmethod
    def import_blocks(bundle: GenerationBundle) -> str:
        return "\n\n".join(
            f'import {{\n  to = {item.address}\n  id = {json.dumps(item.remote_id)}\n}}'
            for item in bundle.imports
        ) + "\n"

    @staticmethod
    def without_cloud_block(terraform: str) -> str:
        match = re.search(r"\bcloud\s*\{", terraform)
        if not match:
            return terraform
        depth = 0
        for index in range(match.end() - 1, len(terraform)):
            if terraform[index] == "{":
                depth += 1
            elif terraform[index] == "}":
                depth -= 1
                if depth == 0:
                    return terraform[: match.start()] + terraform[index + 1 :]
        raise ValueError("generated Terraform contains an unterminated cloud block")

    @staticmethod
    def declares_project_variable(terraform: str) -> bool:
        return bool(re.search(r'\bvariable\s+"project_id"\s*\{', terraform))

    def _run(
        self,
        command: list[str],
        cwd: Path,
        accepted: tuple[int, ...] = (0,),
        environment: dict[str, str] | None = None,
        progress=None,
    ) -> subprocess.CompletedProcess[str]:
        emit = progress or (lambda _message: None)
        rendered = " ".join(command)
        emit(f"$ {rendered}")
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment or self.environment,
        )
        stdout, stderr = self._stream_process(
            process,
            emit,
            stream_output=not ("show" in command and "-json" in command),
        )
        emit(
            f"Terraform command exited {process.returncode} in "
            f"{round((time.monotonic() - started) * 1000)} ms"
        )
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _stream_process(self, process, emit, *, stream_output: bool) -> tuple[str, str]:
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
        deadline = time.monotonic() + self.timeout_seconds
        next_heartbeat = time.monotonic() + 5
        while len(completed) < 2:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(process.args, self.timeout_seconds)
            try:
                name, line = output.get(timeout=1)
            except queue.Empty:
                name, line = "", ""
            if line is None and name:
                completed.add(name)
            elif line:
                (stdout_parts if name == "stdout" else stderr_parts).append(line)
                if stream_output and line.strip():
                    emit(f"terraform {name}: {line.strip()[:800]}")
            if time.monotonic() >= next_heartbeat:
                emit("Terraform command is still running")
                next_heartbeat = time.monotonic() + 5
        process.wait()
        return "".join(stdout_parts), "".join(stderr_parts)

    @staticmethod
    def _failure(
        version: str,
        commands: list[str],
        summary: str,
        process: subprocess.CompletedProcess[str],
        diagnostics: list[str] | None = None,
    ) -> VerificationResult:
        output = (process.stdout + "\n" + process.stderr).strip()
        return VerificationResult(False, False, version, commands, summary, diagnostics or [output[-1000:]], output[-8000:])
