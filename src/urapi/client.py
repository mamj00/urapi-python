"""Direct, bounded JSON transport for the private server SDK. No automatic retries."""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
from importlib.resources import files
import ipaddress
import json
import math
import re
import ssl
from typing import Any
from urllib.parse import quote, urlsplit

_CATALOG = json.loads(files(__package__).joinpath("operations.json").read_text(encoding="utf-8"))
CONTRACT_SHA256: str = _CATALOG["source_sha256"]
OPERATION_IDS: tuple[str, ...] = tuple(_CATALOG["operations"])


class UrapiClientError(Exception):
    """Sanitized local/transport failure. Never contains response bodies or credentials."""
    def __init__(self, code: str, *, outcome_unknown: bool = False):
        super().__init__(code)
        self.code = code
        self.outcome_unknown = outcome_unknown


class UrapiApiError(UrapiClientError):
    def __init__(self, status: int, code: str, *, request_id: str | None = None,
                 retry_after_seconds: int | None = None, outcome_unknown: bool = False):
        super().__init__(code, outcome_unknown=outcome_unknown)
        self.status = status
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ApiResult:
    data: dict[str, Any] = field(repr=False)
    status: int
    request_id: str | None


def _invalid() -> None:
    raise UrapiClientError("invalid_input")


def _number(value: Any, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)


def _scalar(value: Any, schema: dict) -> str:
    kind = schema["type"]
    valid = ((kind == "string" and type(value) is str)
             or (kind == "boolean" and type(value) is bool)
             or (kind == "integer" and type(value) is int and abs(value) <= 2**53 - 1)
             or (kind == "number" and _number(value, -(2**53 - 1), 2**53 - 1)))
    if not valid:
        _invalid()
    if "enum" in schema and value not in schema["enum"]:
        _invalid()
    if type(value) is str:
        invalid_unicode = False
        try:
            value.encode("utf-8")
        except UnicodeError:
            invalid_unicode = True
        if invalid_unicode:
            _invalid()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            _invalid()
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 8192):
            _invalid()
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            _invalid()
        if schema.get("format") == "uuid" and re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", value) is None:
            _invalid()
    if type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            _invalid()
    return str(value).lower() if type(value) is bool else str(value)


def _json_bytes(value: Any, limit: int) -> bytes:
    nodes = 0
    ancestors: set[int] = set()
    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 100000:
            _invalid()
        if item is None or type(item) in (bool, str):
            return
        if type(item) in (int, float):
            if not _number(item, -(2**53 - 1), 2**53 - 1):
                _invalid()
            return
        if type(item) not in (dict, list) or id(item) in ancestors:
            _invalid()
        ancestors.add(id(item))
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                _invalid()
            for child in item.values():
                visit(child, depth + 1)
        else:
            for child in item:
                visit(child, depth + 1)
        ancestors.remove(id(item))
    visit(value, 0)
    failed = False
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, OverflowError, RecursionError):
        failed = True
    if failed:
        _invalid()
    if len(encoded) > limit:
        raise UrapiClientError("request_too_large")
    return encoded


class UrapiClient:
    """One direct connection per request; timeout is per socket operation, not a total deadline."""
    __slots__ = ("_host", "_port", "_secure", "_key", "_timeout", "_response_limit", "_request_limit")

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 10,
                 max_response_bytes: int = 2 * 1024 * 1024, max_request_bytes: int = 1024 * 1024,
                 allow_http_loopback_for_tests: bool = False):
        failed = False
        try:
            if type(base_url) is not str or not base_url.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in base_url):
                raise ValueError()
            parsed = urlsplit(base_url)
            host = parsed.hostname
            if not host or parsed.scheme not in {"https", "http"} or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or any(c in base_url for c in "@%\\?#"):
                raise ValueError()
            port = parsed.port
            if port is not None and not 1 <= port <= 65535:
                raise ValueError()
            try:
                address = ipaddress.ip_address(host)
                if str(address) != host:
                    raise ValueError()
                authority = f"[{host}]" if address.version == 6 else host
            except ValueError:
                if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", host) or re.fullmatch(r"[0-9.]+", host) or any(not part or len(part) > 63 or part.startswith("-") or part.endswith("-") for part in host.split(".")):
                    raise ValueError()
                authority = host
            if port is not None:
                authority += f":{port}"
            if parsed.netloc != authority:
                raise ValueError()
            if type(api_key) is not str or re.fullmatch(r"[A-Za-z0-9._-]{16,512}", api_key) is None:
                raise ValueError()
            if type(allow_http_loopback_for_tests) is not bool or (parsed.scheme == "http" and not (allow_http_loopback_for_tests and host == "127.0.0.1" and re.fullmatch(r"ur_test_[a-f0-9]{24}\.[A-Za-z0-9_-]{43}", api_key))):
                raise ValueError()
            if not _number(timeout_seconds, 0.01, 120) or type(max_response_bytes) is not int or not 256 <= max_response_bytes <= 64 * 1024 * 1024 or type(max_request_bytes) is not int or not 256 <= max_request_bytes <= 8 * 1024 * 1024:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            failed = True
        if failed:
            raise UrapiClientError("invalid_configuration")
        self._host, self._port, self._secure = host, port, parsed.scheme == "https"
        self._key, self._timeout = api_key, timeout_seconds
        self._response_limit, self._request_limit = max_response_bytes, max_request_bytes

    def __repr__(self) -> str:
        return "UrapiClient(credentials=<redacted>)"

    def request(self, operation_id: str, input: dict[str, Any] | None = None,
                options: dict[str, Any] | None = None) -> ApiResult:
        if type(operation_id) is not str or operation_id not in _CATALOG["operations"]:
            _invalid()
        spec = _CATALOG["operations"][operation_id]
        values = {} if input is None else input
        opts = {} if options is None else options
        if type(values) is not dict or set(values) - {"path", "query", "body", "idempotency_key"} or type(opts) is not dict or set(opts) - {"timeout_seconds"}:
            _invalid()
        timeout = opts.get("timeout_seconds", self._timeout)
        if not _number(timeout, 0.01, 120):
            _invalid()
        path = spec["path"]
        query = []
        for location in ("path", "query"):
            supplied = values.get(location, {})
            parameters = spec[location + "_params"]
            if type(supplied) is not dict or set(supplied) - {p["name"] for p in parameters}:
                _invalid()
            for param in parameters:
                name = param["name"]
                if name not in supplied:
                    if param["required"]:
                        _invalid()
                    continue
                scalar = _scalar(supplied[name], param["schema"])
                if location == "path":
                    if scalar in {".", ".."}:
                        _invalid()
                    path = path.replace("{" + name + "}", quote(scalar, safe=""))
                else:
                    query.append(quote(name, safe="") + "=" + quote(scalar, safe=""))
        if query:
            path += "?" + "&".join(query)
        headers = {"Authorization": "Bearer " + self._key, "Accept": "application/json"}
        idem = spec["idempotency"]
        if "idempotency_key" in values:
            if idem is None:
                _invalid()
            headers["Idempotency-Key"] = _scalar(values["idempotency_key"], idem["schema"])
        elif idem and idem["required"]:
            _invalid()
        body = None
        if "body" in values:
            if not spec["has_body"]:
                _invalid()
            body = _json_bytes(values["body"], self._request_limit)
            headers["Content-Type"] = "application/json"
        elif spec["body_required"]:
            _invalid()
        return self._send(spec, path, headers, body, timeout)

    def _safe(self, value: Any, pattern: str) -> str | None:
        return value if type(value) is str and self._key not in value and re.fullmatch(pattern, value) else None

    def _send(self, spec: dict, path: str, headers: dict, body: bytes | None, timeout: float) -> ApiResult:
        connection = None
        error = None
        result = None
        mutation = spec["method"] not in {"GET", "HEAD", "OPTIONS"}
        try:
            if self._secure:
                connection = http.client.HTTPSConnection(self._host, self._port, timeout=timeout, context=ssl.create_default_context())
            else:
                connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
            connection.request(spec["method"], path, body=body, headers=headers)
            response = connection.getresponse()
            request_id = self._safe(response.getheader("X-Request-Id"), r"[A-Za-z0-9_.:-]{1,80}")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise UrapiClientError("invalid_response", outcome_unknown=mutation)
            length = response.getheader("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,12}", length) or int(length) > self._response_limit):
                raise UrapiClientError("response_too_large", outcome_unknown=mutation)
            chunks = bytearray()
            while True:
                chunk = response.read1(min(65536, self._response_limit + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > self._response_limit:
                    raise UrapiClientError("response_too_large", outcome_unknown=mutation)
            if length is not None and len(chunks) != int(length):
                raise UrapiClientError("invalid_response", outcome_unknown=mutation)
            payload = None
            media = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if media in {"application/json", "application/problem+json"}:
                try:
                    def reject_constant(_: str) -> None:
                        raise ValueError()
                    def finite_float(value: str) -> float:
                        result = float(value)
                        if not math.isfinite(result):
                            raise ValueError()
                        return result
                    payload = json.loads(chunks.decode("utf-8"), parse_constant=reject_constant, parse_float=finite_float)
                except (ValueError, UnicodeError, RecursionError):
                    pass
            if response.status >= 400:
                code = self._safe(payload.get("code"), r"[a-z][a-z0-9_]{0,79}") if type(payload) is dict else None
                retry = response.getheader("Retry-After", "")
                seconds = min(int(retry), 86400) if re.fullmatch(r"[0-9]{1,8}", retry) else None
                error = UrapiApiError(response.status, code or "http_error", request_id=request_id, retry_after_seconds=seconds, outcome_unknown=mutation and (response.status >= 500 or response.status in {408, 409}))
            elif response.status not in spec["statuses"] or media != "application/json" or type(payload) is not dict:
                error = UrapiClientError("invalid_response", outcome_unknown=mutation)
            else:
                result = ApiResult(payload, response.status, request_id)
        except UrapiClientError as caught:
            error = caught
        except TimeoutError:
            error = UrapiClientError("request_timeout", outcome_unknown=mutation)
        except Exception:
            error = UrapiClientError("transport_error", outcome_unknown=mutation)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if error is not None:
            error.__traceback__ = None
            error.__context__ = None
            raise error from None
        return result
