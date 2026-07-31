import unittest

from terramig.services.observability import Observability


class ObservabilityTests(unittest.TestCase):
    def test_metrics_and_recent_activity_are_bounded_and_structured(self) -> None:
        telemetry = Observability(recent_limit=2)
        telemetry.record_request("GET", "/api/workflows/abc", 200, 4, "r1")
        telemetry.record_operation("adoption.generate", "succeeded", 10, subject_id="w1")
        telemetry.record_operation("adoption.validate", "failed", 12, subject_id="w2")
        snapshot = telemetry.snapshot([])
        self.assertEqual(len(snapshot["recent"]), 2)
        self.assertEqual(snapshot["recent"][0]["name"], "adoption.validate")
        metrics = telemetry.prometheus([])
        self.assertIn('operation="adoption.validate",status="failed"', metrics)
        self.assertNotIn("subject_id", metrics)

    def test_prometheus_label_values_are_escaped(self) -> None:
        telemetry = Observability()
        telemetry.record_request("GET", '/api/query/"unsafe"\nvalue', 200, 1, "r1")
        metrics = telemetry.prometheus([])
        self.assertIn('route="/api/query/\\"unsafe\\"\\nvalue"', metrics)


if __name__ == "__main__":
    unittest.main()
