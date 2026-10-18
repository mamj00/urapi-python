import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("generate_sdk_python", ROOT / "scripts/generate_sdk_python.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class GeneratorTests(unittest.TestCase):
    def test_canonical_catalog_is_current(self):
        source = generator.SOURCE.read_bytes()
        output = generator.generate(json.loads(source), hashlib.sha256(source).hexdigest())
        self.assertEqual(output, generator.TARGET.read_bytes())
        self.assertEqual(json.loads(output)["operation_count"], 45)

    def test_unsupported_contract_changes_fail_closed(self):
        original = json.loads(generator.SOURCE.read_bytes())
        for kind in ("header", "reference", "placeholder", "body_media", "parameter_schema"):
            document = copy.deepcopy(original)
            operation = document["paths"]["/v1/jobs/{job_id}"]["get"]
            if kind == "header":
                operation.setdefault("parameters", []).append({"in": "header", "name": "Authorization", "schema": {"type": "string"}})
            elif kind == "reference":
                operation["parameters"] = [{"$ref": "https://other.invalid/parameter"}]
            elif kind == "placeholder":
                document["paths"]["/v1/jobs/{other}"] = document["paths"].pop("/v1/jobs/{job_id}")
            elif kind == "body_media":
                operation["requestBody"] = {"content": {"text/plain": {}}}
            else:
                operation["parameters"] = [{"in": "query", "name": "x", "schema": {"type": "array"}}]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                generator.generate(document, "a"*64)

    def test_replacement_requires_expected_hash_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "operations.json"
            self.assertTrue(generator.update(target, b"original"))
            self.assertFalse(generator.update(target, b"original"))
            for expected in (None, "0"*64):
                with self.assertRaises(ValueError):
                    generator.update(target, b"replacement", expected)
                self.assertEqual(target.read_bytes(), b"original")
            digest = hashlib.sha256(b"original").hexdigest()
            self.assertTrue(generator.update(target, b"replacement", digest))
            self.assertEqual(target.read_bytes(), b"replacement")
            self.assertEqual(target.with_name(target.name + "." + digest + ".previous").read_bytes(), b"original")


if __name__ == "__main__":
    unittest.main()
