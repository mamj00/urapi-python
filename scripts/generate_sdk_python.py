"""Generate only the private Python operation catalog; no network or contract edits."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "contracts/public.openapi.json"
TARGET = ROOT / "src/urapi/operations.json"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def reject() -> None:
    raise ValueError("Python SDK generation rejected unsupported or inconsistent input")


def ref(document: dict, value: dict, section: str) -> dict:
    if "$ref" not in value:
        return value
    if set(value) != {"$ref"}:
        reject()
    prefix = f"#/components/{section}/"
    name = value["$ref"]
    if not isinstance(name, str) or not name.startswith(prefix):
        reject()
    name = name[len(prefix):]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        reject()
    result = document.get("components", {}).get(section, {}).get(name)
    if not isinstance(result, dict) or "$ref" in result:
        reject()
    return result


def scalar_schema(document: dict, value: dict) -> dict:
    schema = ref(document, value, "schemas")
    allowed = {"type", "enum", "minLength", "maxLength", "pattern", "minimum", "maximum", "format", "description", "default", "example", "examples", "title"}
    if set(schema) - allowed or schema.get("type") not in {"string", "integer", "number", "boolean"}:
        reject()
    result = {key: value for key, value in schema.items() if key not in {"description", "default", "example", "examples", "title"}}
    if "pattern" in result:
        re.compile(result["pattern"])
    if "format" in result and result["format"] not in {"date-time", "uuid"}:
        reject()
    return result


def generate(document: dict, source_sha256: str) -> bytes:
    if not document.get("openapi", "").startswith("3.1.") or not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        reject()
    specs = {}
    for path, item in document["paths"].items():
        if not re.fullmatch(r"/v1/[A-Za-z0-9_{}/-]+", path) or "//" in path or ".." in path or "$ref" in item:
            reject()
        for method, operation in item.items():
            if method not in METHODS:
                if method not in {"parameters", "summary", "description"} and not method.startswith("x-"):
                    reject()
                continue
            name = operation.get("operationId", "")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in specs:
                reject()
            parameters = {}
            for group in (item.get("parameters", []), operation.get("parameters", [])):
                seen = set()
                for raw in group:
                    param = ref(document, raw, "parameters")
                    key = (param["in"], param["name"].lower() if param["in"] == "header" else param["name"])
                    if key in seen:
                        reject()
                    seen.add(key)
                    parameters[key] = param
            spec = {"method": method.upper(), "path": path, "path_params": [], "query_params": [], "idempotency": None, "has_body": False, "body_required": False, "statuses": []}
            for param in parameters.values():
                location = param["in"]
                if "content" in param or param.get("allowReserved", False) or param.get("style", "form" if location == "query" else "simple") != ("form" if location == "query" else "simple"):
                    reject()
                schema = scalar_schema(document, param["schema"])
                required = param.get("required", False)
                if type(required) is not bool:
                    reject()
                if location == "header":
                    if param["name"].lower() != "idempotency-key" or schema["type"] != "string":
                        reject()
                    spec["idempotency"] = {"required": required, "schema": schema}
                elif location in {"path", "query"}:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", param["name"]) or location == "path" and not required:
                        reject()
                    spec[location + "_params"].append({"name": param["name"], "required": required, "schema": schema})
                else:
                    reject()
            placeholders = re.findall(r"\{([^}]+)\}", path)
            if len(placeholders) != len(set(placeholders)) or sorted(placeholders) != sorted(p["name"] for p in spec["path_params"]):
                reject()
            if "requestBody" in operation:
                body = operation["requestBody"]
                if "$ref" in body or set(body.get("content", {})) != {"application/json"} or type(body.get("required", False)) is not bool:
                    reject()
                spec["has_body"] = True
                spec["body_required"] = body.get("required", False)
            for status, response in operation["responses"].items():
                if re.fullmatch(r"2[0-9]{2}", status):
                    if "$ref" in response or set(response.get("content", {})) != {"application/json"}:
                        reject()
                    spec["statuses"].append(int(status))
            if not spec["statuses"]:
                reject()
            specs[name] = spec
    return (json.dumps({"source": "contracts/public.openapi.json", "source_sha256": source_sha256, "operation_count": len(specs), "operations": specs}, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def ordinary(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or part.exists() and part.resolve() != part.absolute():
            raise ValueError("Generator paths must not contain symbolic links or junctions")


def update(target: Path, content: bytes, expected_sha256: str | None = None) -> bool:
    ordinary(target)
    old = target.read_bytes() if target.exists() else None
    if old == content:
        return False
    if old is not None and hashlib.sha256(old).hexdigest() != expected_sha256:
        raise ValueError("Supply the current generated file SHA256 before replacement")
    if old is None and expected_sha256 is not None:
        raise ValueError("Expected generated file is missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    ordinary(target.parent)
    if old is not None:
        backup = target.parent / (target.name + "." + hashlib.sha256(old).hexdigest() + ".previous")
        if backup.exists():
            ordinary(backup)
            if backup.read_bytes() != old:
                raise ValueError("Previous generated backup differs")
        else:
            with backup.open("xb") as output:
                output.write(old)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        ordinary(target)
        if (target.read_bytes() if target.exists() else None) != old:
            raise ValueError("Generated output changed concurrently")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in {"--check", "--write", "--stdout"} or len(args) > (2 if args[0] == "--write" else 1):
        raise ValueError("Use --check, --stdout or --write [expected-current-sha256]")
    ordinary(SOURCE)
    source = SOURCE.read_bytes()
    generated = generate(json.loads(source), hashlib.sha256(source).hexdigest())
    if args[0] == "--stdout":
        sys.stdout.buffer.write(generated)
        return
    if args[0] == "--write":
        changed = update(TARGET, generated, args[1] if len(args) == 2 else None)
    else:
        ordinary(TARGET)
        if TARGET.read_bytes() != generated:
            raise ValueError("Generated Python catalog differs from the canonical contract")
        changed = False
    result = json.loads(generated)
    print(json.dumps({"passed": True, "changed": changed, "operations": result["operation_count"], "source_sha256": result["source_sha256"], "generated_sha256": hashlib.sha256(generated).hexdigest()}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, re.error):
        print("Python SDK generation failed; check canonical contract, supported structure, paths and expected output hash.", file=sys.stderr)
        sys.exit(1)
