import unittest

from terramig.adapters.mcp import TerraformMCPContextProvider


class FakeMCPClient:
    instances = []

    def __init__(self, binary, environment, timeout_seconds):
        self.binary = binary
        self.environment = environment
        self.timeout_seconds = timeout_seconds
        self.calls = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "tools/list":
            search_schema = {
                "type": "object",
                "properties": {
                    "terraform_org_name": {"type": "string"},
                    "search_query": {"type": "string"},
                    "page_number": {"type": "number"},
                    "page_size": {"type": "number"},
                },
                "required": ["terraform_org_name"],
            }
            details_schema = {
                "type": "object",
                "properties": {
                    "terraform_org_name": {"type": "string"},
                    "private_module_id": {"type": "string"},
                    "private_module_version": {"type": "string"},
                    "registry_name": {
                        "type": "string",
                        "default": "private",
                    },
                },
                "required": ["terraform_org_name", "private_module_id"],
            }
            return {
                "tools": [
                    {"name": "search_private_modules", "inputSchema": search_schema},
                    {"name": "get_private_module_details", "inputSchema": details_schema},
                ]
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": "inputs: bucket_name; outputs: bucket_id; resource: google_storage_bucket.this",
                }
            ]
        }


class TerraformMCPContextTests(unittest.TestCase):
    def test_public_registry_candidates_are_not_sent_to_private_mcp_tools(self):
        sources = TerraformMCPContextProvider._candidate_modules(
            {
                "resources": [
                    {
                        "module_candidates": [
                            {
                                "source": "terraform-google-modules/network/google",
                                "version": "18.1.2",
                                "registry_kind": "public",
                            },
                            {
                                "source": "app.terraform.io/acme/network/google",
                                "version": "1.2.0",
                                "registry_kind": "private",
                            },
                        ]
                    }
                ]
            }
        )
        self.assertEqual(
            sources,
            {
                "app.terraform.io/acme/network/google": {
                    "version": "1.2.0",
                    "registry_name": "private",
                }
            },
        )

    def setUp(self):
        FakeMCPClient.instances.clear()

    def test_live_private_module_details_are_added_to_generation_context(self):
        provider = TerraformMCPContextProvider(
            "app.terraform.io",
            "customer-org",
            "token",
            client_factory=FakeMCPClient,
        )
        prompt = {
            "resources": [
                {
                    "terraform_type": "google_storage_bucket",
                    "module_candidates": [
                        {
                            "source": "app.terraform.io/customer-org/terraform-google-gcsbucket/gcp",
                            "version": "1.2.0",
                        }
                    ],
                }
            ]
        }
        enriched = provider.enrich(prompt)

        context = enriched["terraform_mcp"]
        self.assertEqual(context["mode"], "read-only")
        self.assertEqual(context["organization"], "customer-org")
        self.assertEqual(
            context["private_module_context"][0]["query"],
            "app.terraform.io/customer-org/terraform-google-gcsbucket/gcp",
        )
        calls = FakeMCPClient.instances[0].calls
        tool_calls = [params for method, params in calls if method == "tools/call"]
        self.assertEqual(
            [call["name"] for call in tool_calls],
            ["search_private_modules", "get_private_module_details"],
        )
        self.assertEqual(
            tool_calls[1]["arguments"]["private_module_id"],
            "customer-org/terraform-google-gcsbucket/gcp",
        )
        self.assertEqual(tool_calls[1]["arguments"]["private_module_version"], "1.2.0")
        self.assertEqual(tool_calls[1]["arguments"]["registry_name"], "private")
        self.assertEqual(
            FakeMCPClient.instances[0].environment["ENABLE_TF_OPERATIONS"],
            "false",
        )

    def test_hcp_curated_public_module_uses_public_registry_details(self):
        provider = TerraformMCPContextProvider(
            "app.terraform.io",
            "customer-org",
            "token",
            client_factory=FakeMCPClient,
        )
        prompt = {
            "resources": [
                {
                    "terraform_type": "google_storage_bucket",
                    "module_candidates": [
                        {
                            "source": "registry.terraform.io/example-publisher/storage-bucket/google",
                            "version": "2.7.2",
                            "registry_kind": "hcp-public",
                        }
                    ],
                }
            ]
        }

        enriched = provider.enrich(prompt)

        calls = FakeMCPClient.instances[0].calls
        tool_calls = [params for method, params in calls if method == "tools/call"]
        self.assertEqual(
            tool_calls[1]["arguments"]["private_module_id"],
            "example-publisher/storage-bucket/google",
        )
        self.assertEqual(tool_calls[1]["arguments"]["registry_name"], "public")
        self.assertIn(
            "details", enriched["terraform_mcp"]["private_module_context"][0]
        )

    def test_module_detail_failure_keeps_search_context(self):
        class DetailFailureClient(FakeMCPClient):
            def request(self, method, params=None):
                if (
                    method == "tools/call"
                    and (params or {}).get("name") == "get_private_module_details"
                ):
                    self.calls.append((method, params or {}))
                    return {
                        "isError": True,
                        "content": [{"type": "text", "text": "details unavailable"}],
                    }
                return super().request(method, params)

        provider = TerraformMCPContextProvider(
            "app.terraform.io",
            "customer-org",
            "token",
            client_factory=DetailFailureClient,
        )
        enriched = provider.enrich(
            {
                "resources": [
                    {
                        "module_candidates": [
                            {
                                "source": "app.terraform.io/customer-org/network/google",
                                "version": "1.2.0",
                                "registry_kind": "private",
                            }
                        ]
                    }
                ]
            }
        )

        record = enriched["terraform_mcp"]["private_module_context"][0]
        self.assertIn("search", record)
        self.assertIn("details unavailable", record["details_error"])

    def test_missing_private_registry_tools_fail_closed(self):
        class MissingToolsClient(FakeMCPClient):
            def request(self, method, params=None):
                if method == "tools/list":
                    return {"tools": []}
                return {}

        provider = TerraformMCPContextProvider(
            "app.terraform.io",
            "acme",
            "token",
            client_factory=MissingToolsClient,
        )
        with self.assertRaisesRegex(RuntimeError, "required read-only"):
            provider.enrich({"resources": []})


if __name__ == "__main__":
    unittest.main()
