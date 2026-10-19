"""JSON-schema validation of read results (Draft 2020-12, the published output schemas)."""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from suv_deals.mcp.schemas import tool_output_schema
from suv_deals.persistence.queries import QueryResult
from suv_deals.views.common import envelope_model_for
from suv_deals.views.jsonschema import model_schema

REQUEST_ID = "req-synthetic-read-queries"


@cache
def _validator_for_tool(tool: str) -> Draft202012Validator:
    schema = tool_output_schema(tool)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@cache
def _validator_for_model(model: type[BaseModel]) -> Draft202012Validator:
    schema = model_schema(envelope_model_for(model), mode="serialization")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def envelope_json(result: QueryResult[Any]) -> dict[str, Any]:
    """The serialized envelope exactly as the API/MCP layer emits it (JSON round trip)."""
    text = result.envelope(REQUEST_ID).to_text()
    document: dict[str, Any] = json.loads(text)
    return document


def assert_valid(result: QueryResult[Any], *, tool: str | None = None) -> dict[str, Any]:
    """Validate the envelope against the tool's published output schema (or the model schema)."""
    document = envelope_json(result)
    validator = _validator_for_tool(tool) if tool is not None else _validator_for_model(type(result.data))
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    return document


def walk_numbers(value: Any, path: str = "") -> list[str]:
    """Paths of every JSON number in a document (money must never be one)."""
    found: list[str] = []
    if isinstance(value, bool):
        return found
    if isinstance(value, float):
        found.append(path)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(walk_numbers(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(walk_numbers(item, f"{path}[{index}]"))
    return found


def amounts(value: Any) -> list[dict[str, Any]]:
    """Every AmountView-shaped object (``status`` + ``amount`` + ``currency``) in a document."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if {"status", "amount", "currency"} <= value.keys() and value.get("status") in (
            "known",
            "unknown",
            "not_applicable",
        ):
            found.append(value)
        for item in value.values():
            found.extend(amounts(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(amounts(item))
    return found
