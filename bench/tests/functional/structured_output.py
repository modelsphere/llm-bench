"""
§6.3 Structured Output Tests

Verifies that the service can constrain its output to a given JSON Schema.

NOTE: MiniMax-M2.5 may not fully support strict JSON Schema output (per spec §6.3).
      These tests use `response_format` with `json_object` type as a fallback,
      and validate the response against the expected schema using jsonschema.

Pass threshold: ≥95% of scenarios.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from bench.tests.functional.base import BaseFunctionalTest
from utils.logger import logger

try:
    import jsonschema as _jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _jsonschema = None  # type: ignore[assignment]
    _HAS_JSONSCHEMA = False
    logger.warning(
        "jsonschema not installed — structured output validation will use basic key checks. "
        "Install with: pip install jsonschema"
    )


def _validate_against_schema(data: Any, schema: Dict) -> Tuple[bool, str]:
    """Validate data against a JSON Schema. Returns (passed, reason_if_failed)."""
    if _HAS_JSONSCHEMA and _jsonschema is not None:
        try:
            _jsonschema.validate(instance=data, schema=schema)
            return True, ""
        except _jsonschema.ValidationError as exc:
            return False, f"schema validation error: {exc.message}"
    # Fallback: check required keys exist at top level
    required = schema.get("required", [])
    if not isinstance(data, dict):
        return False, f"expected dict, got {type(data).__name__}"
    missing = [k for k in required if k not in data]
    if missing:
        return False, f"missing required keys: {missing}"
    return True, ""


class StructuredOutputTest(BaseFunctionalTest):
    """§6.3 — validates JSON Schema-constrained output across several scenarios."""

    name = "structured_output"
    PASS_THRESHOLD = 0.95

    def _run_scenarios(self) -> list[Tuple[str, bool]]:
        return [
            self._scenario_person_info(),
            self._scenario_product_listing(),
            self._scenario_event_extraction(),
            self._scenario_classification(),
            self._scenario_nested_object(),
        ]

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _chat_json(
        self,
        prompt: str,
        system: str = "You are a helpful assistant that always responds with valid JSON.",
    ) -> Tuple[Optional[Dict], str]:
        """Send a chat request asking for JSON. Returns (parsed_data, failure_reason)."""
        response_format = {"type": "json_object"}
        resp = self._chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            response_format=response_format,
            max_tokens=512,
        )
        if resp is None:
            return None, "API call returned None"
        msg = resp.get("choices", [{}])[0].get("message", {})
        content = msg.get("content")
        if not content:
            reasoning = msg.get("reasoning_content")
            if reasoning:
                logger.warning(
                    "[structured_output] content is empty — falling back to reasoning_content. "
                    "The model may be a thinking model that does not populate the content field."
                )
                content = reasoning
            else:
                content = ""
        # Reasoning models (e.g. MiniMax-M2.5) prepend <think>...</think> before the JSON.
        # Strip it so json.loads sees only the actual output.
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        try:
            return json.loads(content), ""
        except (json.JSONDecodeError, TypeError) as exc:
            return None, f"response content not valid JSON ({exc}): {str(content)[:300]!r}"

    # ------------------------------------------------------------------
    # Scenarios
    # ------------------------------------------------------------------

    def _scenario_person_info(self) -> Tuple[str, bool]:
        """Extract person info into {name, age, occupation}."""
        name = "person_info"
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
                "occupation": {"type": "string"},
            },
            "required": ["name", "age", "occupation"],
        }
        data, fetch_err = self._chat_json(
            "Extract info: 'Alice is a 30-year-old software engineer.' "
            "Respond with JSON matching: {name, age, occupation}",
        )
        ok, schema_err = _validate_against_schema(data, schema) if data is not None else (False, "no data")
        passed = data is not None and ok
        if not passed:
            reason = fetch_err or schema_err
            logger.warning("[structured_output] %s FAILED: %s — data=%s", name, reason, data)
        return name, passed

    def _scenario_product_listing(self) -> Tuple[str, bool]:
        """Generate a product listing with {title, price, in_stock, tags}."""
        name = "product_listing"
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "price": {"type": "number"},
                "in_stock": {"type": "boolean"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "price", "in_stock", "tags"],
        }
        data, fetch_err = self._chat_json(
            "Create a JSON product listing for a red USB-C cable priced at $9.99, in stock, "
            "with tags ['cable', 'usb-c', 'charging']. Fields: title, price, in_stock, tags.",
        )
        ok, schema_err = _validate_against_schema(data, schema) if data is not None else (False, "no data")
        passed = data is not None and ok
        if not passed:
            reason = fetch_err or schema_err
            logger.warning("[structured_output] %s FAILED: %s — data=%s", name, reason, data)
        return name, passed

    def _scenario_event_extraction(self) -> Tuple[str, bool]:
        """Extract event {event_name, date, location, attendees_count} from text."""
        name = "event_extraction"
        schema = {
            "type": "object",
            "properties": {
                "event_name": {"type": "string"},
                "date": {"type": "string"},
                "location": {"type": "string"},
                "attendees_count": {"type": "integer"},
            },
            "required": ["event_name", "date", "location"],
        }
        data, fetch_err = self._chat_json(
            "Extract: 'The AI Summit 2025 will be held on March 15 in San Francisco with 500 attendees.' "
            "Return JSON with event_name, date, location, attendees_count.",
        )
        ok, schema_err = _validate_against_schema(data, schema) if data is not None else (False, "no data")
        passed = data is not None and ok
        if not passed:
            reason = fetch_err or schema_err
            logger.warning("[structured_output] %s FAILED: %s — data=%s", name, reason, data)
        return name, passed

    def _scenario_classification(self) -> Tuple[str, bool]:
        """Classify text sentiment as {label, confidence}."""
        name = "classification"
        schema = {
            "type": "object",
            "properties": {
                "label": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["label", "confidence"],
        }
        data, fetch_err = self._chat_json(
            "Classify sentiment of 'I absolutely love this product!' "
            "Return JSON with label (positive/negative/neutral) and confidence (0-1).",
        )
        ok, schema_err = _validate_against_schema(data, schema) if data is not None else (False, "no data")
        passed = data is not None and ok
        if not passed:
            reason = fetch_err or schema_err
            logger.warning("[structured_output] %s FAILED: %s — data=%s", name, reason, data)
        return name, passed

    def _scenario_nested_object(self) -> Tuple[str, bool]:
        """Return a nested object {user: {id, name}, permissions: [...]}."""
        name = "nested_object"
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                    },
                    "required": ["id", "name"],
                },
                "permissions": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["user", "permissions"],
        }
        data, fetch_err = self._chat_json(
            "Create a JSON user record for admin user Bob (id=1) with permissions "
            "['read', 'write', 'delete']. Fields: user: {id, name}, permissions: [...].",
        )
        ok, schema_err = _validate_against_schema(data, schema) if data is not None else (False, "no data")
        passed = data is not None and ok
        if not passed:
            reason = fetch_err or schema_err
            logger.warning("[structured_output] %s FAILED: %s — data=%s", name, reason, data)
        return name, passed
