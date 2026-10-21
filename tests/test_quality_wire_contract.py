"""Provider contract cannot express contradictory preference/impact pairs.

Only controlled observations and frozen reports are used; no provider is created.
"""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from core.schemas import Fact
from evals import quality_judge as q
from llm.base import LLMResponse
from tests.test_quality_judge import _copies, _judgment


def wire_judgment(a, b, winner="A"):
    """Explicit controlled wire fixture; never used by production normalization."""
    raw = _judgment(a, b, winner)
    mapping = {("A", "material"): "A_material", ("A", "minor"): "A_minor",
               ("B", "material"): "B_material", ("B", "minor"): "B_minor",
               ("TIE", "none"): "TIE", ("UNKNOWN", "unknown"): "UNKNOWN"}
    for dimension in raw["dimensions"]:
        dimension["judgment"] = mapping[(dimension.pop("preference"), dimension.pop("impact"))]
    return raw


class WireResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.texts = []

    def complete(self, request):
        self.requests.append(request)
        raw = self.responses.pop(0)
        # Whitespace is intentional: retain the provider text, not a reserialization.
        text = " \n" + json.dumps(raw, ensure_ascii=False, indent=1) + "\n"
        self.texts.append(text)
        return LLMResponse(text=text, parsed=raw, provider="controlled", model="wire-test")


def run_wire(*, mutate=None):
    a, b = _copies()
    responses = [wire_judgment(a, b), wire_judgment(b, a, "B")]
    if mutate:
        mutate(responses)
    router = WireResponses(responses)
    return q.compare(a, b, [Fact(id="F1", text="청소 서비스 18만원부터")], router), router, responses


def test_requested_schema_rejects_the_old_unrepresentable_combination():
    _, router, _ = run_wire()
    impossible = _judgment(*_copies(), "TIE")
    impossible["dimensions"][3].update(impact="minor")
    # Reproduces the old schema hole: TIE+minor formerly parsed successfully.
    with pytest.raises(ValidationError):
        router.requests[0].schema.model_validate(impossible)


def test_legal_wire_votes_normalize_without_losing_original_text_or_contract():
    result, router, responses = run_wire()
    assert result["winner"] == "a" and result["status"] == "consensus"
    for order, raw, text in zip(result["orders"], responses, router.texts):
        assert order["wire_response"] == raw
        assert order["wire_text"] == text
        assert order["wire_contract_version"] == result["wire_contract_version"]
        assert order["response"]["dimensions"][2]["impact"] == "material"
        assert "judgment" not in order["response"]["dimensions"][2]
    assert result["orders"][1]["response"]["dimensions"][2]["preference"] == "B"
    assert result["orders"][0]["wire_response"] is not responses[0]
    assert all("judgment" in request.system for request in router.requests)


def test_minor_only_winner_is_rejected_without_silently_becoming_a_tie():
    def mutate(responses):
        responses[0]["dimensions"][2]["judgment"] = "A_minor"
    result, _, raw = run_wire(mutate=mutate)
    assert result["status"] == "invalid_evidence" and result["winner"] == "abstain"
    assert result["orders"][0]["response"]["winner"] == "A"
    assert result["orders"][0]["response"]["dimensions"][2]["impact"] == "minor"
    assert result["orders"][0]["issues"] == ["unsupported_preference"]
    assert result["orders"][0]["wire_response"] == raw[0]


@pytest.mark.parametrize("judgment, preference, impact", [
    ("A_material", "A", "material"), ("A_minor", "A", "minor"),
    ("B_material", "B", "material"), ("B_minor", "B", "minor"),
    ("TIE", "TIE", "none"), ("UNKNOWN", "UNKNOWN", "unknown"),
])
def test_wire_states_preserve_each_distinct_domain_meaning(judgment, preference, impact):
    raw = wire_judgment(*_copies())
    raw["dimensions"][2]["judgment"] = judgment
    normalized = q.normalize_wire(q.QualityJudgeWireOut.model_validate(raw))
    action = normalized.dimensions[2]
    assert (action.preference, action.impact) == (preference, impact)
    assert normalized.winner == "A" and normalized.decisive_criteria == ["action"]
    assert raw["dimensions"][2]["judgment"] == judgment


def test_invalid_wire_observation_is_retained_and_never_accepted_as_legacy():
    def mutate(responses):
        responses[0]["dimensions"][0]["judgment"] = "TIE_minor"
    result, router, raw = run_wire(mutate=mutate)
    assert result["winner"] == "abstain" and result["status"] == "incomplete"
    order = result["orders"][0]
    assert order["error_type"] == "InvalidJudgeSchema"
    assert order["wire_response"] == raw[0] and order["wire_text"] == router.texts[0]
    assert order["response"] is None


def test_installed_sdk_strict_schema_contains_only_legal_judgments():
    from openai.lib._pydantic import to_strict_json_schema
    _, router, _ = run_wire()
    schema = to_strict_json_schema(router.requests[0].schema)
    dimension = schema["$defs"]["WireDimensionJudgment"]
    assert set(dimension["properties"]["judgment"]["enum"]) == {
        "A_material", "A_minor", "B_material", "B_minor", "TIE", "UNKNOWN"}
    assert "preference" not in dimension["properties"] and "impact" not in dimension["properties"]
    for obj in [schema, *schema["$defs"].values()]:
        if obj.get("type") == "object":
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])


def test_saved_v2_responses_keep_original_audit_and_aggregation():
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "evals/reports/live_features/features-20261021-run1.json"
    rows = json.loads(path.read_text())["results"]["quality"]
    for row in rows:
        original = copy.deepcopy(row["orders"])
        replayed = q.aggregate_orders(original)
        assert replayed["winner"] == row["accepted_winner"]
        assert replayed["status"] == row["aggregation_status"]
        assert replayed["orders"] == original == row["orders"]
