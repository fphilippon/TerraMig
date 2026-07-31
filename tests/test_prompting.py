import unittest

from terramig.domain import ModuleCandidate, ModuleVersion, Resource
from terramig.services.prompting import build_generation_prompt


class GenerationPromptTests(unittest.TestCase):
    def test_large_resource_selection_is_not_silently_truncated(self) -> None:
        resources = [
            Resource(
                id=f"bucket-{index}",
                type="storage.googleapis.com/Bucket",
                name=f"bucket-{index}",
                location="US",
                attributes={"name": f"bucket-{index}"},
                terraform_type="google_storage_bucket",
                import_id=f"bucket-{index}",
                support_status="supported",
            )
            for index in range(501)
        ]

        prompt = build_generation_prompt(
            "project",
            resources,
            [resource.id for resource in resources],
            {},
        )

        self.assertEqual(len(prompt["resources"]), 501)
        self.assertEqual(prompt["resources"][-1]["id"], "bucket-500")

    def test_trusted_multi_resource_candidate_is_explicitly_authorized(self) -> None:
        resource = Resource(
            id="bucket-asset",
            type="storage.googleapis.com/Bucket",
            name="bucket",
            location="US",
            attributes={"name": "bucket"},
            terraform_type="google_storage_bucket",
            import_id="bucket",
            support_status="supported",
        )
        module = ModuleVersion(
            id="bucket-stack",
            source="registry.terraform.io/example-publisher/storage-bucket/google",
            version="2.7.2",
            resource_types=(
                "compute.googleapis.com/BackendBucket",
                "storage.googleapis.com/Bucket",
            ),
            inputs=("bucket_name",),
            outputs=("bucket_id",),
            managed_resource_address="",
            description="Curated HCP library module",
            schema_status="trusted",
            registry_kind="hcp-public",
            verified_publisher=False,
            managed_resource_addresses=(
                "google_compute_backend_bucket.bucket_backend",
                "google_storage_bucket.gcs_bucket",
            ),
        )
        prompt = build_generation_prompt(
            "project",
            [resource],
            [resource.id],
            {
                resource.id: [
                    ModuleCandidate(resource.id, module, 0.8, ("HCP library",))
                ]
            },
        )

        candidate = prompt["resources"][0]["module_candidates"][0]
        self.assertEqual(candidate["schema_status"], "trusted")
        self.assertEqual(candidate["registry_kind"], "hcp-public")
        self.assertFalse(candidate["verified_publisher"])
        self.assertEqual(
            candidate["managed_resource_addresses"],
            [
                "google_compute_backend_bucket.bucket_backend",
                "google_storage_bucket.gcs_bucket",
            ],
        )
        constraints = " ".join(prompt["constraints"])
        self.assertIn("never replace it with a direct resource", constraints)
        self.assertIn("exactly matches the resource terraform_type", constraints)


if __name__ == "__main__":
    unittest.main()
