"""The MCP -> Gemini schema translation.

FastMCP emits JSON Schema with `title`, `default`, `additionalProperties` and
lowercase type names. Gemini's Schema accepts none of them. Every one of these
tests is a 400 that happened before it was written.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host import _convert, to_function_declaration  # noqa: E402


def _decl(schema: dict):
    return to_function_declaration("weather_Israel__t", "[weather_Israel] does a thing", schema)


def test_no_argument_tool_gets_no_parameters():
    schema = {"type": "object", "properties": {}, "title": "openArguments"}
    assert _convert(schema) is None
    assert _decl(schema).parameters is None


def test_required_string_argument():
    schema = {
        "type": "object",
        "title": "enterArguments",
        "properties": {"city": {"type": "string", "title": "City"}},
        "required": ["city"],
    }
    assert _convert(schema) == {
        "type": "OBJECT",
        "properties": {"city": {"type": "STRING"}},
        "required": ["city"],
    }


def test_title_and_default_are_dropped():
    schema = {
        "type": "object",
        "properties": {"index": {"type": "integer", "default": 0, "title": "Index"}},
        "additionalProperties": False,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
    }
    assert _convert(schema) == {"type": "OBJECT", "properties": {"index": {"type": "INTEGER"}}}


def test_optional_collapses_to_nullable():
    schema = {
        "type": "object",
        "properties": {
            "index": {"anyOf": [{"type": "integer"}, {"type": "null"}], "title": "Index"}
        },
    }
    assert _convert(schema)["properties"]["index"] == {"type": "INTEGER", "nullable": True}


def test_arrays_and_descriptions_survive():
    schema = {
        "type": "object",
        "properties": {
            "cities": {"type": "array", "description": "City names", "items": {"type": "string"}}
        },
    }
    assert _convert(schema)["properties"]["cities"] == {
        "type": "ARRAY",
        "description": "City names",
        "items": {"type": "STRING"},
    }


def test_declaration_builds_a_real_gemini_schema():
    schema = {
        "type": "object",
        "properties": {"index": {"type": "integer", "default": 0, "title": "Index"}},
    }
    declaration = _decl(schema)
    assert declaration.name == "weather_Israel__t"
    assert declaration.parameters is not None
    assert declaration.parameters.type == "OBJECT"
    assert declaration.parameters.properties["index"].type == "INTEGER"


def test_namespaced_tool_names_fit_geminis_limit():
    # host.py exposes `{client_name}__{tool_name}`. Gemini caps function names at
    # 64 characters and rejects anything outside [A-Za-z0-9_.-].
    longest = "weather_Israel__select_weather_forecast_city_israel"
    assert len(longest) <= 64
    assert to_function_declaration(longest, "d", {"type": "object", "properties": {}}).name == longest
