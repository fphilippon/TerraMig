from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from typing import Any
from urllib.parse import urlparse


READ_ONLY_TOOLS = ("search_private_modules", "get_private_module_details")


class MCPStdioClient:
    """Minimal MCP stdio client used only for read-only Terraform registry tools."""

    def __init__(
        self,
        binary: str,
        environment: dict[str, str],
        timeout_seconds: int = 45,
    ) -> None:
        self.binary = binary
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.request_id = 0

    def __enter__(self) -> MCPStdioClient:
        self.process = subprocess.Popen(
            [
                self.binary,
                f"--tools={','.join(READ_ONLY_TOOLS)}",
                "--log-level=error",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, **self.environment},
        )
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "TerraMig", "version": "0.1.0"},
            },
        )
        self.notify("notifications/initialized")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if not self.process:
            return
        self.process.terminate()
        try:
            self.process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None) -> dict:
        self.request_id += 1
        request_id = self.request_id
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        deadline = time.monotonic() + self.timeout_seconds
        assert self.process and self.process.stdout
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    detail = self.process.stderr.read().strip() if self.process.stderr else ""
                    raise RuntimeError(
                        f"Terraform MCP server exited unexpectedly: {detail[-1200:] or 'no diagnostic output'}"
                    )
                events = selector.select(max(0.1, deadline - time.monotonic()))
                if not events:
                    continue
                line = self.process.stdout.readline()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    error = message["error"]
                    raise RuntimeError(
                        f"Terraform MCP {method} failed: {error.get('message', error)}"
                    )
                result = message.get("result", {})
                return result if isinstance(result, dict) else {"value": result}
        finally:
            selector.close()
        raise TimeoutError(f"Terraform MCP {method} timed out")

    def _write(self, message: dict) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Terraform MCP client is not running")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()


class TerraformMCPContextProvider:
    """Ground AI composition in live HCP private-module documentation."""

    def __init__(
        self,
        hostname: str,
        organization: str,
        token: str,
        binary: str = "terraform-mcp-server",
        timeout_seconds: int = 45,
        max_modules: int = 12,
        max_context_chars: int = 80_000,
        client_factory: type[MCPStdioClient] = MCPStdioClient,
    ) -> None:
        self.hostname = hostname
        self.organization = organization
        self.token = token
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.max_modules = max_modules
        self.max_context_chars = max_context_chars
        self.client_factory = client_factory

    def enrich(self, prompt: dict) -> dict:
        if not self.token:
            raise ValueError("TERRAMIG_HCP_TOKEN is required for Terraform MCP")
        module_sources = self._candidate_modules(prompt)
        queries = list(module_sources) or self._resource_queries(prompt)
        environment = {
            "TFE_ADDRESS": self._address(),
            "TFE_TOKEN": self.token,
            "ENABLE_TF_OPERATIONS": "false",
            "LOG_LEVEL": "error",
        }
        records: list[dict[str, Any]] = []
        with self.client_factory(
            self.binary, environment, self.timeout_seconds
        ) as client:
            tools = client.request("tools/list").get("tools", [])
            available = {
                tool.get("name"): tool
                for tool in tools
                if isinstance(tool, dict) and tool.get("name")
            }
            search_tool = self._find_tool(available, "search_private_modules")
            details_tool = self._find_tool(available, "get_private_module_details")
            if not search_tool or not details_tool:
                raise RuntimeError(
                    "Terraform MCP server does not expose the required read-only private-module tools"
                )
            for query in queries[: self.max_modules]:
                module_metadata = module_sources.get(query, {})
                module = self._parse_source(query) if module_metadata else {}
                search_arguments = self._arguments(
                    available[search_tool].get("inputSchema", {}),
                    query=module.get("name", query),
                )
                search_result = client.request(
                    "tools/call",
                    {"name": search_tool, "arguments": search_arguments},
                )
                record: dict[str, Any] = {
                    "query": query,
                    "search": self._content(search_result),
                }
                if module_metadata:
                    detail_arguments = self._arguments(
                        available[details_tool].get("inputSchema", {}),
                        source=query,
                        version=module_metadata["version"],
                        registry_name=module_metadata["registry_name"],
                    )
                    try:
                        detail_result = client.request(
                            "tools/call",
                            {"name": details_tool, "arguments": detail_arguments},
                        )
                        record["details"] = self._content(detail_result)
                    except (RuntimeError, TimeoutError) as error:
                        # Search metadata is still useful grounding. Module details
                        # are supplemental and must not block the entire AI phase.
                        record["details_error"] = str(error)[:500]
                records.append(record)

        context = {
            "server": "hashicorp/terraform-mcp-server",
            "hcp_address": self._address(),
            "organization": self.organization,
            "mode": "read-only",
            "tools": list(READ_ONLY_TOOLS),
            "private_module_context": records,
            "instructions": [
                "Treat this live MCP result as the authoritative module documentation for this generation.",
                "Use only exact HCP library module source addresses and pinned versions present in approved candidates.",
                "Map discovered configuration to documented module inputs and use documented outputs for dependencies.",
                "Never infer sensitive workspace variables or invoke HCP Terraform mutation operations.",
            ],
        }
        encoded = json.dumps(context)
        if len(encoded) > self.max_context_chars:
            context["private_module_context"] = self._truncate_records(
                records, self.max_context_chars
            )
            context["truncated"] = True
        enriched = dict(prompt)
        enriched["terraform_mcp"] = context
        return enriched

    def _address(self) -> str:
        return self.hostname if self.hostname.startswith("http") else f"https://{self.hostname}"

    @staticmethod
    def _find_tool(tools: dict[str, dict], suffix: str) -> str:
        return next((name for name in tools if name == suffix or name.endswith(suffix)), "")

    @staticmethod
    def _candidate_modules(prompt: dict) -> dict[str, dict[str, str]]:
        sources: dict[str, dict[str, str]] = {}
        for resource in prompt.get("resources", []):
            for candidate in resource.get("module_candidates", []):
                source = candidate.get("source", "")
                registry_kind = candidate.get("registry_kind", "private")
                if (
                    registry_kind in {"private", "hcp-public"}
                    and source
                    and source not in sources
                ):
                    sources[source] = {
                        "version": str(candidate.get("version", "")),
                        "registry_name": (
                            "public" if registry_kind == "hcp-public" else "private"
                        ),
                    }
        return sources

    @staticmethod
    def _resource_queries(prompt: dict) -> list[str]:
        queries: list[str] = []
        for resource in prompt.get("resources", []):
            query = resource.get("terraform_type") or resource.get("type") or resource.get("name")
            if query and query not in queries:
                queries.append(query)
        return queries

    def _arguments(
        self,
        schema: dict,
        *,
        query: str = "",
        source: str = "",
        version: str = "",
        registry_name: str = "",
    ) -> dict[str, Any]:
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        parsed = self._parse_source(source)
        values: dict[str, Any] = {}
        aliases = {
            "organization": self.organization,
            "organization_name": self.organization,
            "organizationName": self.organization,
            "terraform_org_name": self.organization,
            "namespace": parsed.get("namespace", self.organization),
            "module_namespace": parsed.get("namespace", self.organization),
            "moduleNamespace": parsed.get("namespace", self.organization),
            "name": parsed.get("name", query),
            "module_name": parsed.get("name", query),
            "moduleName": parsed.get("name", query),
            "provider": parsed.get("provider", "google"),
            "module_provider": parsed.get("provider", "google"),
            "moduleProvider": parsed.get("provider", "google"),
            "query": query or parsed.get("name", ""),
            "search": query or parsed.get("name", ""),
            "search_query": query or parsed.get("name", ""),
            "searchQuery": query or parsed.get("name", ""),
            "source": source,
            "module_source": source,
            "moduleSource": source,
            "private_module_id": (
                f"{parsed['namespace']}/{parsed['name']}/{parsed['provider']}"
                if parsed
                else ""
            ),
            "private_module_version": version,
            "registry_name": registry_name,
        }
        for name, definition in properties.items():
            if name in aliases and aliases[name] not in ("", None):
                values[name] = aliases[name]
            elif name in {"limit", "page_size", "pageSize"}:
                values[name] = min(self.max_modules, 20)
            elif name in {"page_number", "pageNumber"}:
                values[name] = 1
            elif "default" in definition:
                values[name] = definition["default"]
            elif name in required and definition.get("enum"):
                values[name] = definition["enum"][0]
        missing = [name for name in required if name not in values]
        if missing:
            raise RuntimeError(
                "Terraform MCP tool schema contains unsupported required arguments: "
                + ", ".join(sorted(missing))
            )
        return values

    @staticmethod
    def _parse_source(source: str) -> dict[str, str]:
        if not source:
            return {}
        path = urlparse(f"//{source}").path.strip("/").split("/")
        if len(path) < 3:
            return {}
        return {
            "namespace": path[-3],
            "name": path[-2],
            "provider": path[-1],
        }

    @staticmethod
    def _content(result: dict) -> list[dict[str, Any]]:
        if result.get("isError"):
            raise RuntimeError(f"Terraform MCP tool failed: {result.get('content', [])}")
        content = result.get("content", [])
        if not isinstance(content, list):
            return [{"type": "json", "value": content}]
        return [
            item
            for item in content
            if isinstance(item, dict) and item.get("type") in {"text", "resource", "json"}
        ]

    @staticmethod
    def _truncate_records(records: list[dict], limit: int) -> list[dict]:
        truncated: list[dict] = []
        remaining = max(1_000, limit - 2_000)
        for record in records:
            encoded = json.dumps(record)
            if len(encoded) <= remaining:
                truncated.append(record)
                remaining -= len(encoded)
                continue
            if remaining > 500:
                truncated.append(
                    {
                        "query": record.get("query", ""),
                        "truncated_context": encoded[:remaining],
                    }
                )
            break
        return truncated
