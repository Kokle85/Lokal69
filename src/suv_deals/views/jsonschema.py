"""JSON Schema (2020-12) export helpers for read models and tool inputs (spec 6, 21).

Pydantic emits ``$defs``/``$ref``, ``anyOf [X, null]`` for optionals and ``title`` keys. The
published schemas are self-contained (clients need not resolve references), so:

1. ``inline_refs`` resolves every local ``$ref`` and drops ``$defs`` (recursive models refused);
2. ``collapse_nullable`` rewrites ``anyOf [{type: X, ...}, {type: null}]`` to
   ``{type: [X, "null"], ...}`` (the spec 21 notation);
3. ``close_objects`` sets ``additionalProperties: false`` on every object schema that does not
   declare one (maps keep their value schema);
4. ``strip_titles`` removes generated ``title`` keywords except on object schemas.

Only schema positions are walked; ``default``/``enum``/``const``/``examples`` values are data and
are never rewritten. ``render`` produces deterministic text (sorted keys, two-space indent).
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any, Final, Literal

from pydantic import BaseModel

JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

_MAP_KEYWORDS: Final = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_LIST_KEYWORDS: Final = ("anyOf", "oneOf", "allOf", "prefixItems")
_SINGLE_KEYWORDS: Final = (
    "items",
    "additionalProperties",
    "not",
    "if",
    "then",
    "else",
    "contains",
    "propertyNames",
    "unevaluatedProperties",
    "unevaluatedItems",
    "additionalItems",
)
_COMPOSITE: Final = frozenset({"$ref", "anyOf", "oneOf", "allOf"})

Schema = dict[str, Any]
SchemaMode = Literal["validation", "serialization"]


class SchemaExportError(ValueError):
    """The model cannot be exported as a self-contained schema (e.g. a recursive model)."""


def walk(schema: Schema, visit: Callable[[Schema], None]) -> None:
    """Call ``visit`` on ``schema`` and every nested subschema (schema positions only)."""
    visit(schema)
    for key in _MAP_KEYWORDS:
        mapping = schema.get(key)
        if isinstance(mapping, dict):
            for sub in mapping.values():
                if isinstance(sub, dict):
                    walk(sub, visit)
    for key in _LIST_KEYWORDS:
        items = schema.get(key)
        if isinstance(items, list):
            for sub in items:
                if isinstance(sub, dict):
                    walk(sub, visit)
    for key in _SINGLE_KEYWORDS:
        sub = schema.get(key)
        if isinstance(sub, dict):
            walk(sub, visit)


def inline_refs(schema: Schema) -> Schema:
    """Resolve every ``#/$defs/...`` reference in place of its use and drop ``$defs``."""
    defs: dict[str, Schema] = schema.get("$defs", {})

    def resolve(node: Schema, stack: tuple[str, ...]) -> Schema:
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise SchemaExportError(f"unsupported reference {ref!r}")
            name = ref.removeprefix("#/$defs/")
            if name in stack:
                raise SchemaExportError(f"recursive model {name!r} cannot be inlined")
            if name not in defs:
                raise SchemaExportError(f"unknown reference {ref!r}")
            target = resolve(defs[name], (*stack, name))
            siblings = resolve({k: v for k, v in node.items() if k != "$ref"}, stack)
            return {**target, **siblings}
        out: Schema = {}
        for key, value in node.items():
            if key in ("$defs", "definitions"):
                continue
            if key in _MAP_KEYWORDS and isinstance(value, dict):
                out[key] = {
                    name: resolve(sub, stack) if isinstance(sub, dict) else copy.deepcopy(sub)
                    for name, sub in value.items()
                }
            elif key in _LIST_KEYWORDS and isinstance(value, list):
                out[key] = [
                    resolve(sub, stack) if isinstance(sub, dict) else copy.deepcopy(sub) for sub in value
                ]
            elif key in _SINGLE_KEYWORDS and isinstance(value, dict):
                out[key] = resolve(value, stack)
            else:  # data-valued or scalar keywords (default, enum, const, required, type, ...)
                out[key] = copy.deepcopy(value)
        return out

    return resolve(schema, ())


def collapse_nullable(schema: Schema) -> Schema:
    """``anyOf [{type: X, ...}, {type: null}]`` -> ``{type: [X, "null"], ...}`` where unambiguous."""

    def visit(node: Schema) -> None:
        options = node.get("anyOf")
        if not isinstance(options, list) or len(options) != 2:
            return
        nulls = [o for o in options if o == {"type": "null"}]
        others = [o for o in options if o != {"type": "null"}]
        if len(nulls) != 1 or len(others) != 1:
            return
        other = others[0]
        if not isinstance(other.get("type"), str) or _COMPOSITE & other.keys():
            return
        merged = dict(other)
        merged["type"] = [other["type"], "null"]
        if "const" in merged:
            merged["enum"] = [merged.pop("const"), None]
        elif "enum" in merged and None not in merged["enum"]:
            merged["enum"] = [*merged["enum"], None]
        del node["anyOf"]
        for key, value in merged.items():
            node.setdefault(key, value)
        node["type"] = merged["type"]
        if "enum" in merged:
            node["enum"] = merged["enum"]

    walk(schema, visit)
    return schema


def _is_object(node: Schema) -> bool:
    kind = node.get("type")
    return "properties" in node or kind == "object" or (isinstance(kind, list) and "object" in kind)


def close_objects(schema: Schema) -> Schema:
    """Set ``additionalProperties: false`` on object schemas that do not declare it."""

    def visit(node: Schema) -> None:
        if _is_object(node) and "additionalProperties" not in node:
            node["additionalProperties"] = False

    walk(schema, visit)
    return schema


def strip_titles(schema: Schema, *, keep_object_titles: bool = True) -> Schema:
    """Drop generated ``title`` keywords (field titles); keep model titles on objects if asked."""
    root = schema

    def visit(node: Schema) -> None:
        if "title" not in node or node is root:
            return
        if keep_object_titles and "properties" in node:
            return
        del node["title"]

    walk(schema, visit)
    return schema


def find_refs(schema: Schema) -> list[str]:
    """Every ``$ref`` value left in the schema (for contract tests)."""
    found: list[str] = []

    def visit(node: Schema) -> None:
        if "$ref" in node:
            found.append(str(node["$ref"]))

    walk(schema, visit)
    return found


def open_objects(schema: Schema) -> list[Schema]:
    """Object schemas that allow undeclared properties (for contract tests)."""
    found: list[Schema] = []

    def visit(node: Schema) -> None:
        if _is_object(node) and node.get("additionalProperties") is not False:
            found.append(node)

    walk(schema, visit)
    return found


def model_schema(
    model: type[BaseModel],
    *,
    mode: SchemaMode,
    title: str | None = None,
    schema_id: str | None = None,
    keep_object_titles: bool = True,
) -> Schema:
    """Self-contained 2020-12 schema of ``model`` (refs inlined, nullables collapsed, objects closed)."""
    raw = model.model_json_schema(mode=mode)
    schema = inline_refs(raw)
    collapse_nullable(schema)
    close_objects(schema)
    strip_titles(schema, keep_object_titles=keep_object_titles)
    if title is not None:
        schema["title"] = title
    document: Schema = {"$schema": JSON_SCHEMA_DIALECT}
    if schema_id is not None:
        document["$id"] = schema_id
    document.update(schema)
    return document


def render(document: Any) -> str:
    """Deterministic text for committed snapshots: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
