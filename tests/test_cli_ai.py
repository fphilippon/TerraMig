import unittest
import os
import signal
import subprocess
import io
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from terramig.adapters.cli_ai import CliGenerationAgent
from terramig.domain import GenerationBundle, ImportOperation, VerificationResult
from terramig.services.assurance import sanitize_untrusted


class CliGenerationAgentTests(unittest.TestCase):
    @staticmethod
    def _direct_resource_response(
        full_prompt: str, _emit=None
    ) -> str:
        task_json = full_prompt.split("<task-json>\n", 1)[1].split(
            "\n</task-json>", 1
        )[0]
        task = __import__("json").loads(task_json)
        resources = task.get("resources", [])
        namespace = task.get("batch_context", {}).get(
            "address_namespace", "batch_001"
        )
        terraform = "\n".join(
            f'resource "google_storage_bucket" "{namespace}_bucket_{index}" '
            f'{{ name = "{resource["name"]}" location = "US" }}'
            for index, resource in enumerate(resources)
        )
        imports = [
            {
                "resource_id": resource["id"],
                "address": (
                    f"google_storage_bucket.{namespace}_bucket_{index}"
                ),
            }
            for index, resource in enumerate(resources)
        ]
        return __import__("json").dumps(
            {
                "terraform": terraform,
                "imports": imports,
                "decisions": [],
                "warnings": [],
            }
        )

    def test_copilot_uses_programmatic_silent_mode(self) -> None:
        agent = CliGenerationAgent("github-copilot", Path("."), Path("skills"))
        self.assertEqual(agent.command("task")[:4], ["copilot", "-p", "task", "-s"])

    def test_bob_exposes_intermediary_output_for_live_tail(self) -> None:
        agent = CliGenerationAgent("ibm-bob", Path("."), Path("skills"))
        self.assertEqual(
            agent.command("task"),
            ["bob", "--accept-license", "-p", "task"],
        )

    def test_bob_uses_headless_api_key_authentication_when_configured(self) -> None:
        agent = CliGenerationAgent("ibm-bob", Path("."), Path("skills"))
        with patch.dict(os.environ, {"BOBSHELL_API_KEY": "secret"}):
            command = agent.command("task")
        self.assertEqual(
            command[:4],
            ["bob", "--accept-license", "--auth-method", "api-key"],
        )

    def test_json_can_be_extracted_from_incidental_text(self) -> None:
        output = 'result:\n```json\n{"terraform":"module \\\"x\\\" {}","imports":[{"address":"module.x.r","remote_id":"x","command":"terraform import module.x.r x"}],"decisions":[],"warnings":[]}\n```'
        bundle = CliGenerationAgent.parse_bundle(output)
        self.assertEqual(bundle.imports[0].remote_id, "x")

    def test_non_json_output_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON object"):
            CliGenerationAgent.parse_bundle("No result")

    def test_generate_launches_selected_binary_as_background_child(self) -> None:
        root = Path(__file__).resolve().parents[1]
        process = MagicMock(returncode=0)
        process.stdout = io.StringIO(
            'Bob is composing\n{"terraform":"module \\\"x\\\" {}","imports":[{"address":"module.x.r","remote_id":"x","command":"terraform import module.x.r x"}],"decisions":[],"warnings":[]}\n'
        )
        process.stderr = io.StringIO("")
        agent = CliGenerationAgent(
            "ibm-bob", root, root / "skills" / "adopt-gcp-infrastructure"
        )
        progress = []
        with patch("terramig.adapters.cli_ai.subprocess.Popen", return_value=process) as popen:
            bundle = agent.generate({"response_schema": {}}, progress=progress.append)
        self.assertEqual(popen.call_args.args[0][0], "bob")
        self.assertNotIn("<skill>", " ".join(popen.call_args.args[0]))
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(progress, bundle.composition_log)
        self.assertTrue(any("launching bob" in item for item in progress))
        self.assertTrue(any("bob stdout: Bob is composing" in item for item in progress))
        self.assertTrue(any("deterministic import IDs" in item for item in progress))

    def test_oversized_bob_prompt_is_streamed_over_stdin(self) -> None:
        process = MagicMock(returncode=0)
        process.stdin = MagicMock()
        process.stdout = io.StringIO("{}\n")
        process.stderr = io.StringIO("")
        agent = CliGenerationAgent("ibm-bob", Path("."), Path("skills"))
        prompt = "resource-context-" * 200_000
        progress = []

        with patch(
            "terramig.adapters.cli_ai.subprocess.Popen", return_value=process
        ) as popen:
            self.assertEqual(agent._run_agent(prompt, progress.append).strip(), "{}")

        command = popen.call_args.args[0]
        self.assertLess(sum(len(argument) for argument in command), 2_000)
        self.assertNotIn(prompt, command)
        self.assertIn("--hide-intermediary-output", command)
        self.assertEqual(
            command[command.index("--output-format") + 1], "text"
        )
        self.assertIn("attempt completion tool", " ".join(command))
        process.stdin.write.assert_called_once_with(prompt)
        process.stdin.close.assert_called_once()
        self.assertTrue(any("over stdin" in item for item in progress))

    def test_high_scale_generation_batches_and_merges_every_resource(self) -> None:
        root = Path(__file__).resolve().parents[1]
        resources = [
            {
                "id": f"asset-{index}",
                "name": f"bucket-{index}",
                "import_id": f"bucket-{index}",
                "terraform_type": "google_storage_bucket",
                "support_status": "supported",
                "dependencies": [f"asset-{index - 1}"] if index else [],
                "module_candidates": [],
            }
            for index in range(105)
        ]
        agent = CliGenerationAgent(
            "ibm-bob",
            root,
            root / "skills" / "adopt-gcp-infrastructure",
            batch_size=25,
            max_batch_prompt_bytes=1_000_000,
        )
        progress = []

        with patch.object(
            agent,
            "_run_agent",
            side_effect=self._direct_resource_response,
        ) as run:
            bundle = agent.generate(
                {"resources": resources, "response_schema": {}},
                progress.append,
            )

        self.assertEqual(run.call_count, 5)
        self.assertEqual(len(bundle.imports), 105)
        self.assertEqual(
            {operation.resource_id for operation in bundle.imports},
            {resource["id"] for resource in resources},
        )
        self.assertIn("# TerraMig AI composition batch 5", bundle.terraform)
        self.assertTrue(
            any("partitioned 105 resources into 5" in item for item in progress)
        )
        second_prompt = run.call_args_list[1].args[0]
        self.assertIn('"asset-24"', second_prompt)
        self.assertIn(
            '"google_storage_bucket.batch_001_bucket_24"',
            second_prompt,
        )

    def test_high_scale_generation_reports_the_failing_batch(self) -> None:
        root = Path(__file__).resolve().parents[1]
        resources = [
            {
                "id": f"asset-{index}",
                "name": f"bucket-{index}",
                "import_id": f"bucket-{index}",
                "terraform_type": "google_storage_bucket",
                "support_status": "supported",
                "dependencies": [],
                "module_candidates": [],
            }
            for index in range(41)
        ]
        agent = CliGenerationAgent(
            "ibm-bob",
            root,
            root / "skills" / "adopt-gcp-infrastructure",
            repair_attempts=0,
            batch_size=20,
            max_batch_prompt_bytes=1_000_000,
        )
        calls = 0

        def respond(full_prompt, _emit):
            nonlocal calls
            calls += 1
            if calls == 2:
                return "\n"
            return self._direct_resource_response(full_prompt)

        with patch.object(agent, "_run_agent", side_effect=respond):
            with self.assertRaisesRegex(
                ValueError, "batch 2/3 failed.*JSON object"
            ):
                agent.generate({"resources": resources, "response_schema": {}})

    def test_high_scale_generation_retries_only_the_current_batch_process(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        resources = [
            {
                "id": f"asset-{index}",
                "name": f"bucket-{index}",
                "import_id": f"bucket-{index}",
                "terraform_type": "google_storage_bucket",
                "support_status": "supported",
                "dependencies": [],
                "module_candidates": [],
            }
            for index in range(21)
        ]
        agent = CliGenerationAgent(
            "ibm-bob",
            root,
            root / "skills" / "adopt-gcp-infrastructure",
            repair_attempts=0,
            batch_size=20,
            max_batch_prompt_bytes=1_000_000,
            batch_process_attempts=2,
        )
        calls = 0

        def respond(full_prompt, _emit):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("transient Bob process failure")
            return self._direct_resource_response(full_prompt)

        progress = []
        with patch.object(agent, "_run_agent", side_effect=respond):
            bundle = agent.generate(
                {"resources": resources, "response_schema": {}},
                progress.append,
            )

        self.assertEqual(calls, 3)
        self.assertEqual(len(bundle.imports), 21)
        self.assertTrue(
            any(
                "retrying batch 2/2 process 2/2" in message
                for message in progress
            )
        )

    def test_high_scale_generation_identifies_terminal_process_failure(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        resources = [
            {
                "id": f"asset-{index}",
                "name": f"bucket-{index}",
                "import_id": f"bucket-{index}",
                "terraform_type": "google_storage_bucket",
                "support_status": "supported",
                "dependencies": [],
                "module_candidates": [],
            }
            for index in range(21)
        ]
        agent = CliGenerationAgent(
            "ibm-bob",
            root,
            root / "skills" / "adopt-gcp-infrastructure",
            repair_attempts=0,
            batch_size=20,
            max_batch_prompt_bytes=1_000_000,
            batch_process_attempts=2,
        )

        with patch.object(
            agent,
            "_run_agent",
            side_effect=RuntimeError("Bob timed out"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"batch 1/2 process failed after 2 attempt\(s\): Bob timed out",
            ):
                agent.generate({"resources": resources, "response_schema": {}})

    def test_incomplete_batch_is_adaptively_subdivided_and_recovered(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        resources = [
            {
                "id": f"asset-{index}",
                "name": f"bucket-{index}",
                "import_id": f"bucket-{index}",
                "terraform_type": "google_storage_bucket",
                "support_status": "supported",
                "dependencies": [f"asset-{index - 1}"] if index else [],
                "module_candidates": [],
            }
            for index in range(40)
        ]
        agent = CliGenerationAgent(
            "ibm-bob",
            root,
            root / "skills" / "adopt-gcp-infrastructure",
            batch_size=20,
            max_batch_prompt_bytes=1_000_000,
        )
        calls = []

        def respond(full_prompt, _emit):
            task_json = full_prompt.split("<task-json>\n", 1)[1].split(
                "\n</task-json>", 1
            )[0]
            task = __import__("json").loads(task_json)
            calls.append(task)
            response = __import__("json").loads(
                self._direct_resource_response(full_prompt)
            )
            if (
                len(task["resources"]) == 20
                and task["resources"][0]["id"] == "asset-20"
            ):
                response["imports"] = response["imports"][1:]
                response["decisions"] = response["decisions"][1:]
            return __import__("json").dumps(response)

        progress = []
        with patch.object(agent, "_run_agent", side_effect=respond):
            bundle = agent.generate(
                {"resources": resources, "response_schema": {}},
                progress.append,
            )

        self.assertEqual(
            [len(task["resources"]) for task in calls],
            [20, 20, 10, 10],
        )
        self.assertEqual(len(bundle.imports), 40)
        self.assertEqual(
            {operation.resource_id for operation in bundle.imports},
            {resource["id"] for resource in resources},
        )
        self.assertEqual(
            calls[2]["batch_context"]["address_namespace"],
            "batch_002_01",
        )
        self.assertEqual(
            calls[3]["batch_context"]["address_namespace"],
            "batch_002_02",
        )
        self.assertEqual(
            calls[2]["batch_context"]["external_dependency_addresses"][
                "asset-19"
            ],
            "google_storage_bucket.batch_001_bucket_19",
        )
        self.assertEqual(
            calls[3]["batch_context"]["external_dependency_addresses"][
                "asset-29"
            ],
            "google_storage_bucket.batch_002_01_bucket_9",
        )
        self.assertTrue(
            any("adaptively subdividing 20 resources" in item for item in progress)
        )
        self.assertTrue(
            any("recovered through adaptive subdivision" in item for item in progress)
        )

    def test_timeout_terminates_the_complete_agent_process_group(self) -> None:
        process = MagicMock(pid=4321)
        process.wait.side_effect = [
            subprocess.TimeoutExpired("bob", 2),
            0,
        ]

        with patch("terramig.adapters.cli_ai.os.killpg") as killpg:
            CliGenerationAgent._terminate_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                unittest.mock.call(4321, signal.SIGTERM),
                unittest.mock.call(4321, signal.SIGKILL),
            ],
        )

    def test_oversized_copilot_prompt_uses_private_attachment(self) -> None:
        process = MagicMock(returncode=0)
        process.stdout = io.StringIO("{}\n")
        process.stderr = io.StringIO("")
        agent = CliGenerationAgent("github-copilot", Path("."), Path("skills"))
        prompt = "resource-context-" * 200_000
        observed = {}

        def launch(command, **kwargs):
            attachment = Path(command[command.index("--attachment") + 1])
            observed["path"] = attachment
            observed["content"] = attachment.read_text()
            observed["mode"] = attachment.stat().st_mode & 0o777
            observed["command"] = command
            observed["stdin"] = kwargs["stdin"]
            return process

        with patch(
            "terramig.adapters.cli_ai.subprocess.Popen", side_effect=launch
        ):
            self.assertEqual(agent._run_agent(prompt, lambda _: None).strip(), "{}")

        self.assertEqual(observed["content"], prompt)
        self.assertEqual(observed["mode"], 0o600)
        self.assertEqual(observed["stdin"], subprocess.DEVNULL)
        self.assertLess(sum(len(argument) for argument in observed["command"]), 4_000)
        self.assertFalse(observed["path"].exists())

    def test_agent_environment_does_not_inherit_cloud_credentials(self) -> None:
        agent = CliGenerationAgent(
            "github-copilot",
            Path("."),
            Path("skills"),
            credential="stored-copilot-token",
        )
        with patch.dict(
            os.environ,
            {
                "TERRAMIG_HCP_TOKEN": "hcp-secret",
                "GOOGLE_APPLICATION_CREDENTIALS": "/secret/adc.json",
                "COPILOT_GITHUB_TOKEN": "copilot-token",
            },
            clear=False,
        ):
            environment = agent._agent_environment()
        self.assertNotIn("TERRAMIG_HCP_TOKEN", environment)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", environment)
        self.assertEqual(
            environment["COPILOT_GITHUB_TOKEN"], "stored-copilot-token"
        )

    def test_only_selected_provider_credential_is_injected(self) -> None:
        agent = CliGenerationAgent(
            "ibm-bob",
            Path("."),
            Path("skills"),
            credential="stored-bob-key",
        )
        with patch.dict(
            os.environ,
            {"COPILOT_GITHUB_TOKEN": "do-not-inherit"},
            clear=False,
        ):
            environment = agent._agent_environment()
        self.assertEqual(environment["BOBSHELL_API_KEY"], "stored-bob-key")
        self.assertNotIn("COPILOT_GITHUB_TOKEN", environment)

    def test_connection_probe_uses_isolated_temporary_directory(self) -> None:
        process = MagicMock(returncode=0, stdout="TERRAMIG_AUTH_OK", stderr="")
        agent = CliGenerationAgent(
            "ibm-bob",
            Path("."),
            Path("skills"),
            credential="stored-bob-key",
        )
        with patch(
            "terramig.adapters.cli_ai.subprocess.run", return_value=process
        ) as run:
            result = agent.test_connection()
        self.assertEqual(result["status"], "verified")
        self.assertIn("terramig-agent-check-", run.call_args.kwargs["cwd"])
        self.assertEqual(
            run.call_args.kwargs["env"]["BOBSHELL_API_KEY"],
            "stored-bob-key",
        )

    def test_connection_probe_rejects_an_empty_success_response(self) -> None:
        process = MagicMock(returncode=0, stdout="\n", stderr="")
        agent = CliGenerationAgent(
            "ibm-bob",
            Path("."),
            Path("skills"),
            credential="stored-bob-key",
        )
        with patch(
            "terramig.adapters.cli_ai.subprocess.run", return_value=process
        ):
            with self.assertRaisesRegex(
                RuntimeError, "returned no verified response"
            ):
                agent.test_connection()

    def test_sensitive_resource_values_are_redacted_before_ai(self) -> None:
        sanitized, count = sanitize_untrusted(
            {
                "attributes": {
                    "name": "safe",
                    "client_secret": "do-not-send",
                    "description": "Bearer abcdefghijklmnopqrstuvwxyz",
                }
            }
        )
        self.assertEqual(sanitized["attributes"]["client_secret"], "[REDACTED]")
        self.assertEqual(sanitized["attributes"]["description"], "[REDACTED]")
        self.assertEqual(count, 2)

    def test_execution_provisioner_is_rejected(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"resource \\"null_resource\\" \\"x\\" { provisioner \\"local-exec\\" { command = \\"whoami\\" } }",'
            '"imports":[{"address":"null_resource.x","remote_id":"x","command":"terraform import null_resource.x x"}],'
            '"decisions":[],"warnings":[]}'
        )
        with self.assertRaisesRegex(ValueError, "execution provisioner"):
            CliGenerationAgent._validate_against_task(bundle, {})

    def test_unexpected_response_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            CliGenerationAgent.parse_bundle(
                '{"terraform":"resource \\"x\\" \\"y\\" {}",'
                '"imports":[{"address":"x.y","remote_id":"x","command":"terraform import x.y x"}],'
                '"decisions":[],"warnings":[],"debug":"leak"}'
            )

    def test_agent_cannot_change_deterministic_import_ids(self) -> None:
        output = '{"terraform":"resource \\\"x\\\" \\\"y\\\" {}","imports":[{"address":"x.y","remote_id":"wrong","command":"terraform import x.y wrong"}],"decisions":[],"warnings":[]}'
        bundle = CliGenerationAgent.parse_bundle(output)
        with self.assertRaisesRegex(ValueError, "deterministic provider import IDs"):
            CliGenerationAgent._validate_against_task(
                bundle,
                {"resources": [{"import_id": "projects/p/things/right"}]},
            )

    def test_resource_identity_reconstructs_import_id_and_command(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"resource \\"google_storage_bucket\\" \\"x\\" { name = \\"bucket\\" location = \\"US\\" }",'
            '"imports":[{"resource_id":"asset-bucket","address":"google_storage_bucket.x"}],'
            '"decisions":[],"warnings":[]}'
        )
        checks = CliGenerationAgent._validate_against_task(
            bundle,
            {
                "resources": [
                    {
                        "id": "asset-bucket",
                        "import_id": "bucket",
                        "terraform_type": "google_storage_bucket",
                        "module_candidates": [],
                    }
                ]
            },
        )
        self.assertEqual(bundle.imports[0].resource_id, "asset-bucket")
        self.assertEqual(bundle.imports[0].remote_id, "bucket")
        self.assertEqual(
            bundle.imports[0].command,
            "terraform import google_storage_bucket.x bucket",
        )
        self.assertIn(
            "Remote IDs and commands reconstructed from reviewed resource identities",
            checks,
        )

    def test_unverified_resource_cannot_reenter_ai_bundle(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"resource \\"google_compute_route\\" \\"local\\" {}",'
            '"imports":[{"resource_id":"local-route","address":"google_compute_route.local"}],'
            '"decisions":[],"warnings":[]}'
        )
        with self.assertRaisesRegex(ValueError, "not verified adoption targets"):
            CliGenerationAgent._validate_against_task(
                bundle,
                {
                    "resources": [
                        {
                            "id": "local-route",
                            "import_id": "",
                            "terraform_type": "google_compute_route",
                            "support_status": "unsupported",
                            "module_candidates": [],
                        }
                    ]
                },
            )

    def test_resource_identity_proposals_must_cover_every_selected_resource(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"resource \\"google_storage_bucket\\" \\"x\\" { name = \\"bucket\\" location = \\"US\\" }",'
            '"imports":[{"resource_id":"asset-a","address":"google_storage_bucket.x"}],'
            '"decisions":[],"warnings":[]}'
        )
        with self.assertRaisesRegex(ValueError, "proposal count"):
            CliGenerationAgent._validate_against_task(
                bundle,
                {
                    "resources": [
                        {"id": "asset-a", "import_id": "a"},
                        {"id": "asset-b", "import_id": "b"},
                    ]
                },
            )

    def test_rewritten_identity_is_reconciled_by_unique_address_contract(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"resource \\"google_storage_bucket\\" \\"archive\\" { name = \\"archive\\" location = \\"US\\" } resource \\"google_pubsub_topic\\" \\"events\\" { name = \\"events\\" }",'
            '"imports":[{"resource_id":"projects/p/buckets/archive","address":"google_storage_bucket.archive"},{"resource_id":"events-topic","address":"google_pubsub_topic.events"}],'
            '"decisions":[],"warnings":[]}'
        )
        CliGenerationAgent._validate_against_task(
            bundle,
            {
                "resources": [
                    {
                        "id": "//storage.googleapis.com/projects/_/buckets/archive",
                        "name": "archive",
                        "import_id": "archive",
                        "terraform_type": "google_storage_bucket",
                        "module_candidates": [],
                    },
                    {
                        "id": "//pubsub.googleapis.com/projects/p/topics/events",
                        "name": "events",
                        "import_id": "projects/p/topics/events",
                        "terraform_type": "google_pubsub_topic",
                        "module_candidates": [],
                    },
                ]
            },
        )
        self.assertEqual(
            [operation.resource_id for operation in bundle.imports],
            [
                "//storage.googleapis.com/projects/_/buckets/archive",
                "//pubsub.googleapis.com/projects/p/topics/events",
            ],
        )
        self.assertTrue(any("reconciled 2" in warning for warning in bundle.warnings))

    def test_ambiguous_rewritten_identity_is_rejected(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"resource \\"google_storage_bucket\\" \\"first\\" { name = \\"first\\" location = \\"US\\" } resource \\"google_storage_bucket\\" \\"second\\" { name = \\"second\\" location = \\"US\\" }",'
            '"imports":[{"resource_id":"rewritten-a","address":"google_storage_bucket.bucket_a"},{"resource_id":"rewritten-b","address":"google_storage_bucket.bucket_b"}],'
            '"decisions":[],"warnings":[]}'
        )
        resources = [
            {
                "id": "asset-a",
                "name": "first",
                "import_id": "first",
                "terraform_type": "google_storage_bucket",
                "module_candidates": [],
            },
            {
                "id": "asset-b",
                "name": "second",
                "import_id": "second",
                "terraform_type": "google_storage_bucket",
                "module_candidates": [],
            },
        ]
        with self.assertRaisesRegex(ValueError, "could not be deterministically reconciled"):
            CliGenerationAgent._validate_against_task(bundle, {"resources": resources})

    def test_generate_uses_bounded_self_repair(self) -> None:
        root = Path(__file__).resolve().parents[1]
        agent = CliGenerationAgent(
            "ibm-bob",
            root,
            root / "skills" / "adopt-gcp-infrastructure",
            repair_attempts=1,
        )
        rejected = (
            '{"terraform":"resource \\"google_storage_bucket\\" \\"archive\\" { name = \\"archive\\" location = \\"US\\" }",'
            '"imports":[{"resource_id":"wrong","address":"google_compute_network.wrong"}],"decisions":[],"warnings":[]}'
        )
        repaired = (
            '{"terraform":"resource \\"google_storage_bucket\\" \\"archive\\" { name = \\"archive\\" location = \\"US\\" }",'
            '"imports":[{"resource_id":"asset-bucket","address":"google_storage_bucket.archive"}],"decisions":[],"warnings":[]}'
        )
        progress = []
        with patch.object(agent, "_run_agent", side_effect=[rejected, repaired]) as run:
            bundle = agent.generate(
                {
                    "resources": [
                        {
                            "id": "asset-bucket",
                            "name": "archive",
                            "import_id": "archive",
                            "terraform_type": "google_storage_bucket",
                            "module_candidates": [],
                        }
                    ]
                },
                progress.append,
            )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(bundle.imports[0].remote_id, "archive")
        self.assertTrue(any("bounded self-repair 1/1" in item for item in progress))

    def test_validation_repair_can_correct_only_module_instance_cardinality(self) -> None:
        root = Path(__file__).resolve().parents[1]
        agent = CliGenerationAgent(
            "ibm-bob", root, root / "skills" / "adopt-gcp-infrastructure"
        )
        previous = GenerationBundle(
            'module "subnet" { source = "terraform-google-modules/network/google//modules/subnets" version = "18.1.2" }',
            [
                ImportOperation(
                    "module.subnet.google_compute_subnetwork.subnetwork[0]",
                    "projects/p/regions/r/subnetworks/s",
                    "terraform import old",
                    "subnet-asset",
                )
            ],
            [],
            [],
        )
        verification = VerificationResult(
            False,
            False,
            "1.15.8",
            [],
            "Terraform validation failed",
            ["error: Invalid import 'to' expression"],
            "The target resource does not use count.",
        )
        task = {
            "resources": [
                {
                    "id": "subnet-asset",
                    "name": "subnet",
                    "import_id": "projects/p/regions/r/subnetworks/s",
                    "terraform_type": "google_compute_subnetwork",
                    "support_status": "supported",
                    "module_candidates": [
                        {
                            "source": "terraform-google-modules/network/google//modules/subnets",
                            "version": "18.1.2",
                            "managed_resource_address": "google_compute_subnetwork.subnetwork[0]",
                        }
                    ],
                }
            ]
        }
        response = (
            '{"terraform":"module \\"subnet\\" { source = \\"terraform-google-modules/network/google//modules/subnets\\" '
            'version = \\"18.1.2\\" }","imports":[{"resource_id":"subnet-asset",'
            '"address":"module.subnet.google_compute_subnetwork.subnetwork"}],'
            '"decisions":["Removed stale count selector"],"warnings":[]}'
        )
        progress = []
        with patch.object(agent, "_run_agent", return_value=response) as run:
            repaired = agent.repair_after_verification(
                task, previous, verification, progress.append
            )
        self.assertEqual(
            repaired.imports[0].address,
            "module.subnet.google_compute_subnetwork.subnetwork",
        )
        self.assertEqual(
            repaired.imports[0].remote_id,
            "projects/p/regions/r/subnetworks/s",
        )
        self.assertIn("target resource does not use count", run.call_args.args[0])
        self.assertTrue(any("passed host identity" in item for item in progress))

    def test_agent_process_emits_live_heartbeat(self) -> None:
        class DelayedStream:
            def __init__(self):
                self.called = False

            def readline(self):
                if self.called:
                    return ""
                self.called = True
                time.sleep(1.1)
                return "{}\n"

        process = MagicMock(returncode=0)
        process.stdout = DelayedStream()
        process.stderr = io.StringIO("")
        agent = CliGenerationAgent(
            "ibm-bob",
            Path("."),
            Path("skills"),
            progress_interval_seconds=1,
        )
        progress = []
        with patch("terramig.adapters.cli_ai.subprocess.Popen", return_value=process):
            self.assertEqual(agent._run_agent("prompt", progress.append).strip(), "{}")
        self.assertTrue(any("bob is running" in item for item in progress))

    def test_module_instance_address_matches_reviewed_ownership(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"module \\"routes\\" { source = \\"terraform-google-modules/network/google//modules/routes\\" version = \\"11.1.0\\" }",'
            '"imports":[{"resource_id":"asset-route","address":"module.routes.google_compute_route.route[\\"default\\"]"}],'
            '"decisions":[],"warnings":[]}'
        )
        checks = CliGenerationAgent._validate_against_task(
            bundle,
            {
                "resources": [
                    {
                        "id": "asset-route",
                        "import_id": "projects/p/global/routes/default",
                        "terraform_type": "google_compute_route",
                        "module_candidates": [
                            {
                                "source": "terraform-google-modules/network/google//modules/routes",
                                "managed_resource_address": "google_compute_route.route",
                            }
                        ],
                    }
                ]
            },
        )
        self.assertEqual(
            bundle.imports[0].remote_id,
            "projects/p/global/routes/default",
        )
        self.assertIn("Operator-selected module ownership addresses enforced", checks)

    def test_trusted_multi_resource_module_uses_type_compatible_address(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"module \\"stack\\" { source = \\"app.terraform.io/acme/network-stack/google\\" version = \\"1.2.3\\" }",'
            '"imports":[{"resource_id":"asset-subnet","address":"module.stack.google_compute_subnetwork.this"}],'
            '"decisions":[],"warnings":[]}'
        )
        checks = CliGenerationAgent._validate_against_task(
            bundle,
            {
                "resources": [
                    {
                        "id": "asset-subnet",
                        "import_id": "projects/p/regions/r/subnetworks/s",
                        "terraform_type": "google_compute_subnetwork",
                        "module_candidates": [
                            {
                                "source": "app.terraform.io/acme/network-stack/google",
                                "managed_resource_address": "",
                                "managed_resource_addresses": [
                                    "google_compute_network.this",
                                    "google_compute_subnetwork.this",
                                ],
                            }
                        ],
                    }
                ]
            },
        )
        self.assertEqual(
            bundle.imports[0].remote_id,
            "projects/p/regions/r/subnetworks/s",
        )
        self.assertIn("Operator-selected module ownership addresses enforced", checks)

    def test_unapproved_private_module_source_is_rejected(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"module \\"x\\" { source = \\"app.terraform.io/acme/unapproved/gcp\\" }",'
            '"imports":[{"address":"module.x.r","remote_id":"right","command":"terraform import module.x.r right"}],'
            '"decisions":[],"warnings":[]}'
        )
        with self.assertRaisesRegex(ValueError, "did not use any approved"):
            CliGenerationAgent._validate_against_task(
                bundle,
                {
                    "resources": [
                        {
                            "import_id": "right",
                            "module_candidates": [
                                {"source": "app.terraform.io/acme/approved/gcp"}
                            ],
                        }
                    ]
                },
            )

    def test_raw_import_is_rejected_when_private_module_is_approved(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"module \\"x\\" { source = \\"app.terraform.io/acme/approved/gcp\\" }",'
            '"imports":[{"address":"google_storage_bucket.x","remote_id":"bucket","command":"terraform import google_storage_bucket.x bucket"}],'
            '"decisions":[],"warnings":[]}'
        )
        with self.assertRaisesRegex(ValueError, "raw-resource imports"):
            CliGenerationAgent._validate_against_task(
                bundle,
                {
                    "resources": [
                        {
                            "import_id": "bucket",
                            "module_candidates": [
                                {"source": "app.terraform.io/acme/approved/gcp"}
                            ],
                        }
                    ]
                },
            )

    def test_direct_resource_fallback_allows_required_provider_source(self) -> None:
        bundle = CliGenerationAgent.parse_bundle(
            '{"terraform":"terraform { required_providers { google = { source = \\\"hashicorp/google\\\" version = \\\"7.39.0\\\" } } } resource \\\"google_storage_bucket\\\" \\\"x\\\" { name = \\\"bucket\\\" }",'
            '"imports":[{"address":"google_storage_bucket.x","remote_id":"bucket","command":"terraform import google_storage_bucket.x bucket"}],'
            '"decisions":[],"warnings":["direct fallback"]}'
        )
        checks = CliGenerationAgent._validate_against_task(
            bundle,
            {
                "resources": [
                    {
                        "import_id": "bucket",
                        "module_candidates": [],
                    }
                ]
            },
        )
        self.assertIn("Only approved module sources used", checks)


if __name__ == "__main__":
    unittest.main()
