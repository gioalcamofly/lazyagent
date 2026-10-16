"""Validate protocol payloads against the v1 JSON Schemas in ``schema/v1/``.

The schemas are plain JSON Schema (draft 2020-12) so a non-Python engine can
use them with any validator. This module implements just the subset they
use, to keep the protocol package free of runtime dependencies. Loading a
schema that uses any other keyword fails, so the subset cannot silently
drift.

Files:

- ``envelope.json``: the req/res/evt message shapes.
- ``methods.json``: ``$defs`` named ``<method>.params`` / ``<method>.result``.
- ``events.json``: ``$defs`` named after each event, describing ``data``.
- ``handshake.json``: ``hello.params`` / ``hello.result``.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).parent / "schema" / "v1"

SUPPORTED_KEYWORDS = frozenset({
    # Annotations, ignored.
    "$schema", "$id", "$comment", "title", "description", "examples", "default",
    # Structure.
    "$defs", "$ref",
    # Assertions.
    "type", "enum", "const", "properties", "required", "additionalProperties",
    "items", "minItems", "minimum", "minLength", "anyOf", "oneOf",
})

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaValidationError(ValueError):
    """An instance that does not match its schema; ``path`` locates it."""

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path or '$'}: {message}")
        self.path = path or "$"
        self.message = message


def _check_keywords(node: Any, where: str) -> None:
    if isinstance(node, dict):
        unknown = set(node) - SUPPORTED_KEYWORDS
        if unknown:
            raise ValueError(f"{where}: unsupported schema keywords {sorted(unknown)}")
        for key in ("properties", "$defs"):
            for name, sub in node.get(key, {}).items():
                _check_keywords(sub, f"{where}/{key}/{name}")
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                _check_keywords(node[key], f"{where}/{key}")
        for key in ("anyOf", "oneOf"):
            for i, sub in enumerate(node.get(key, [])):
                _check_keywords(sub, f"{where}/{key}/{i}")


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Load ``schema/v1/<name>`` (e.g. ``"methods.json"``)."""
    path = SCHEMA_DIR / name
    schema = json.loads(path.read_text(encoding="utf-8"))
    _check_keywords(schema, name)
    return schema


def schema_files() -> list[str]:
    return sorted(p.name for p in SCHEMA_DIR.glob("*.json"))


def _resolve(ref: str, doc: str) -> tuple[dict[str, Any], str]:
    file_part, _, pointer = ref.partition("#")
    target_doc = file_part or doc
    node: Any = load_schema(target_doc)
    for part in filter(None, pointer.split("/")):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, target_doc


class _Validator:
    def __init__(self, strict: bool) -> None:
        self.strict = strict

    def check(self, value: Any, schema: Any, doc: str, path: str) -> None:
        if schema is True or schema == {}:
            return
        if schema is False:
            raise SchemaValidationError(path, "no value is allowed here")
        if "$ref" in schema:
            target, target_doc = _resolve(schema["$ref"], doc)
            self.check(value, target, target_doc, path)
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_TYPES[t](value) for t in types):
                raise SchemaValidationError(
                    path, f"expected {' or '.join(types)}, got {_describe(value)}"
                )
        if "const" in schema and not _equal(value, schema["const"]):
            raise SchemaValidationError(path, f"expected {schema['const']!r}, got {value!r}")
        if "enum" in schema and not any(_equal(value, e) for e in schema["enum"]):
            raise SchemaValidationError(path, f"{value!r} is not one of {schema['enum']}")
        if "minimum" in schema and _TYPES["number"](value) and value < schema["minimum"]:
            raise SchemaValidationError(path, f"{value} is below the minimum {schema['minimum']}")
        if "minLength" in schema and isinstance(value, str) and len(value) < schema["minLength"]:
            raise SchemaValidationError(path, f"string shorter than {schema['minLength']}")
        if isinstance(value, dict):
            self._check_object(value, schema, doc, path)
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                raise SchemaValidationError(path, f"fewer than {schema['minItems']} items")
            if "items" in schema:
                for i, item in enumerate(value):
                    self.check(item, schema["items"], doc, f"{path}[{i}]")
        if "anyOf" in schema:
            errors = self._branches(value, schema["anyOf"], doc, path)
            if len(errors) == len(schema["anyOf"]):
                raise SchemaValidationError(path, "matches none of anyOf: " + "; ".join(errors))
        if "oneOf" in schema:
            errors = self._branches(value, schema["oneOf"], doc, path)
            matched = len(schema["oneOf"]) - len(errors)
            if matched != 1:
                detail = "; ".join(errors) if matched == 0 else f"{matched} branches match"
                raise SchemaValidationError(path, f"must match exactly one of oneOf: {detail}")

    def _branches(self, value: Any, branches: list, doc: str, path: str) -> list[str]:
        errors = []
        for branch in branches:
            try:
                self.check(value, branch, doc, path)
            except SchemaValidationError as e:
                errors.append(str(e))
        return errors

    def _check_object(self, value: dict, schema: dict, doc: str, path: str) -> None:
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaValidationError(path, f"missing required property {key!r}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        if extra is None and self.strict and ("properties" in schema):
            extra = False
        for key, item in value.items():
            sub = f"{path}.{key}"
            if key in props:
                self.check(item, props[key], doc, sub)
            elif extra is False:
                raise SchemaValidationError(sub, "property is not declared in the schema")
            elif isinstance(extra, dict):
                self.check(item, extra, doc, sub)


def _describe(value: Any) -> str:
    for name, test in _TYPES.items():
        if name != "number" and test(value):
            return name
    return type(value).__name__


def _equal(a: Any, b: Any) -> bool:
    # JSON equality: 1 == 1.0, but True is not 1.
    if _TYPES["number"](a) and _TYPES["number"](b):
        return a == b
    return type(a) is type(b) and a == b


def validate(value: Any, schema: dict[str, Any], *, doc: str, strict: bool = False) -> None:
    """Validate ``value`` against ``schema`` (from file ``doc``, for ``$ref``).

    ``strict`` treats every object schema that lists ``properties`` without
    saying otherwise as ``additionalProperties: false``. The wire contract is
    lenient (peers ignore unknown fields); strict mode is for tests that
    prove a payload is fully described by the schema.
    """
    _Validator(strict).check(value, schema, doc, "$")


def _def(doc: str, name: str) -> dict[str, Any]:
    defs = load_schema(doc).get("$defs", {})
    if name not in defs:
        raise KeyError(f"{doc} has no definition {name!r}")
    return defs[name]


def method_names() -> list[str]:
    defs = load_schema("methods.json")["$defs"]
    return sorted({k.rsplit(".", 1)[0] for k in defs if k.endswith((".params", ".result"))})


def event_names() -> list[str]:
    return sorted(k for k in load_schema("events.json")["$defs"] if "." in k)


def validate_message(message: dict[str, Any], *, strict: bool = False) -> None:
    validate(message, load_schema("envelope.json"), doc="envelope.json", strict=strict)


def validate_params(method: str, params: Any, *, strict: bool = False) -> None:
    validate(params, _def("methods.json", f"{method}.params"), doc="methods.json", strict=strict)


def validate_result(method: str, result: Any, *, strict: bool = False) -> None:
    validate(result, _def("methods.json", f"{method}.result"), doc="methods.json", strict=strict)


def validate_event(event: str, data: Any, *, strict: bool = False) -> None:
    validate(data, _def("events.json", event), doc="events.json", strict=strict)


def validate_hello(params: Any, *, strict: bool = False) -> None:
    validate(params, _def("handshake.json", "hello.params"), doc="handshake.json", strict=strict)


def validate_hello_reply(result: Any, *, strict: bool = False) -> None:
    validate(result, _def("handshake.json", "hello.result"), doc="handshake.json", strict=strict)
