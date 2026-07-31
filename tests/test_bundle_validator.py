import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "adopt-gcp-infrastructure" / "scripts" / "validate_bundle.py"
SPEC = importlib.util.spec_from_file_location("validate_bundle", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class BundleValidatorTests(unittest.TestCase):
    def test_valid_bundle(self) -> None:
        bundle = {
            "terraform": 'module "x" {}',
            "imports": [{"address": "module.x.r.this", "remote_id": "remote/x", "command": "terraform import module.x.r.this remote/x"}],
            "decisions": [],
            "warnings": [],
        }
        self.assertEqual(MODULE.validate(bundle), [])

    def test_duplicate_remote_object_is_rejected(self) -> None:
        bundle = {
            "terraform": "resource x y {}",
            "imports": [
                {"address": "x.a", "remote_id": "same", "command": "one"},
                {"address": "x.b", "remote_id": "same", "command": "two"},
            ],
        }
        self.assertTrue(any("duplicate remote id" in error for error in MODULE.validate(bundle)))


if __name__ == "__main__":
    unittest.main()
