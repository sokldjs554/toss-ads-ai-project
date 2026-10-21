"""Evidence-bound, two-order quality judgments; all responses are controlled or saved.

These checks establish aggregation/provenance behavior, not model quality or human
preference accuracy. The frozen reports are read as historical observations.
"""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path

import pytest

from core.schemas import AdCopy, Fact
from llm.base import LLMResponse
from llm.errors import BudgetExceeded, ProviderUnavailable

ROOT = Path(__file__).resolve().parents[1]


def _module():
    assert importlib.util.find_spec("evals.quality_judge") is not None, "quality judge must expose evidence-checked two-order comparison"
    return importlib.import_module("evals.quality_judge")


def _copies():
    return (
        AdCopy(headline="청소 서비스", body="18만원부터 이용해요.", cta="견적 확인하기"),
        AdCopy(headline="청소 서비스", body="18만원부터 이용해요.", cta="자세히 보기"),
    )


def _judgment(a, b, winner="A"):
    dims = []
    for criterion in ("information", "conditions", "action", "expression"):
        field = "cta" if criterion == "action" else "body"
        decisive = criterion == "action" and winner in {"A", "B"}
        dims.append({
            "criterion": criterion, "preference": winner if decisive else "TIE",
            "impact": "material" if decisive else "none",
            "evidence_a": [{"field": field, "quote": getattr(a, field)}],
            "evidence_b": [{"field": field, "quote": getattr(b, field)}],
            "fact_ids": ["F1"] if criterion in {"information", "conditions"} else [],
            "reason": "다음 행동을 구체적으로 알려준다." if decisive else "표시된 내용이 같다.",
        })
    return {"dimensions": dims, "winner": winner,
            "decisive_criteria": ["action"] if winner in {"A", "B"} else [],
            "reason": "버튼의 다음 행동이 구체적이다." if winner in {"A", "B"} else "뚜렷한 차이가 없다."}


class SavedResponses:
    """Returns only supplied test observations; never instantiates a provider."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, req):
        self.requests.append(req)
        raw = self.responses.pop(0)
        if isinstance(raw, Exception):
            raise raw
        return LLMResponse(text=json.dumps(raw, ensure_ascii=False), parsed=req.schema.model_validate(raw),
                           provider="controlled", model="saved-test-response")


def _compare(first="A", second="B", *, mutate=None):
    q = _module()
    a, b = _copies()
    responses = [_judgment(a, b, first), _judgment(b, a, second)]
    if mutate:
        mutate(responses)
    router = SavedResponses(responses)
    result = q.compare(a, b, [Fact(id="F1", text="청소 서비스 18만원부터")], router, label="test-pair")
    return result, router


def test_swapping_positions_maps_both_votes_to_the_same_source_copy():
    result, router = _compare()
    assert result["winner"] == "a" and result["status"] == "consensus"
    assert result["position_consistent"] is True and result["evidence_valid"] is True
    assert [(o["order"], o["displayed_winner"], o["winner"]) for o in result["orders"]] == [("ab", "A", "a"), ("ba", "B", "a")]
    assert len(router.requests) == 2
    assert all(o["response"]["reason"] for o in result["orders"])


def test_position_follower_abstains_instead_of_becoming_a_tie():
    result, _ = _compare("B", "B")
    assert result["winner"] == "abstain" and result["status"] == "order_disagreement"
    assert result["position_consistent"] is False
    assert [o["winner"] for o in result["orders"]] == ["b", "a"]


def test_consistent_tie_is_distinct_from_abstention():
    result, _ = _compare("TIE", "TIE")
    assert result["winner"] == "tie" and result["status"] == "consensus"
    assert result["position_consistent"] is True


def test_tie_versus_preference_is_an_abstention():
    result, _ = _compare("TIE", "B")
    assert result["winner"] == "abstain" and result["status"] == "order_disagreement"


def test_missing_or_invented_copy_quote_cannot_support_a_consensus():
    def mutate(responses):
        responses[1]["dimensions"][2]["evidence_a"][0]["quote"] = "없는 문장"
    result, _ = _compare(mutate=mutate)
    assert result["winner"] == "abstain" and result["status"] == "invalid_evidence"
    assert result["position_consistent"] is True and result["evidence_valid"] is False
    assert "quote_not_in_source" in " ".join(result["orders"][1]["issues"])
    assert result["orders"][1]["response"]["dimensions"][2]["evidence_a"][0]["quote"] == "없는 문장"


@pytest.mark.parametrize("mutation, issue", [
    (lambda r: r[0]["dimensions"][0].update(fact_ids=["invented"]), "unknown_fact_id"),
    (lambda r: r[0]["dimensions"].pop(), "incomplete_rubric"),
    (lambda r: r[0]["dimensions"][2].update(evidence_b=[]), "missing_side_evidence"),
    (lambda r: r[0].update(decisive_criteria=[]), "unsupported_preference"),
    (lambda r: r[0]["dimensions"][2].update(impact="none"), "preference_impact_conflict"),
    (lambda r: r[0].update(decisive_criteria=["expression"]), "unsupported_preference"),
])
def test_incomplete_or_contradictory_evidence_is_not_given_preference_credit(mutation, issue):
    result, _ = _compare(mutate=mutation)
    assert result["winner"] == "abstain" and result["evidence_valid"] is False
    assert issue in " ".join(result["orders"][0]["issues"])


def test_empty_quote_only_verifies_an_actually_empty_field():
    def mutate(responses):
        responses[0]["dimensions"][0]["evidence_a"][0]["quote"] = ""
    result, _ = _compare(mutate=mutate)
    assert result["winner"] == "abstain"
    assert "quote_not_in_source" in " ".join(result["orders"][0]["issues"])


def test_failure_in_second_order_keeps_the_first_response_and_abstains():
    q = _module()
    a, b = _copies()
    router = SavedResponses([_judgment(a, b), ProviderUnavailable("private detail must not be saved")])
    result = q.compare(a, b, [Fact(id="F1", text="청소 서비스 18만원부터")], router)
    assert result["winner"] == "abstain" and result["status"] == "incomplete"
    assert result["orders"][0]["response"]["winner"] == "A"
    assert result["orders"][1]["error_type"] == "ProviderUnavailable"
    assert "private detail" not in json.dumps(result)
    assert result["position_consistent"] is None


def test_budget_stop_never_starts_an_extra_order_or_loses_prior_response():
    q = _module()
    a, b = _copies()
    router = SavedResponses([BudgetExceeded("budget")])
    result = q.compare(a, b, [Fact(id="F1", text="청소 서비스 18만원부터")], router)
    assert result["winner"] == "abstain" and result["budget_stopped"] is True
    assert len(router.requests) == 1 and len(result["orders"]) == 1


def test_request_preserves_complete_copy_and_never_supplies_labels_or_signal_scores():
    result, router = _compare()
    first, second = router.requests
    assert first.schema is second.schema
    assert first.context["copies"] == {"A": _copies()[0].model_dump(include={"headline", "body", "cta"}),
                                       "B": _copies()[1].model_dump(include={"headline", "body", "cta"})}
    assert second.context["copies"]["A"] == first.context["copies"]["B"]
    assert second.context["copies"]["B"] == first.context["copies"]["A"]
    for req in router.requests:
        for forbidden in ("better", "reason_code", "signal_winner", "score", "variant_id"):
            assert forbidden not in req.user
        assert "untrusted_data" in req.user
        assert "정책" in req.system and "클릭률" in req.system
    assert result["rubric_version"]


def test_saved_84_pairs_preserve_all_observed_disagreements_without_human_claims():
    q = _module()
    totals = []
    for number in (1, 2, 3):
        path = ROOT / f"evals/reports/live_features/features-20261020-run{number}.json"
        report = json.loads(path.read_text())
        copied = copy.deepcopy(report["results"]["quality"])
        results = [q.aggregate_orders(row["orders"], require_evidence=False) for row in copied]
        totals.append((sum(r["winner"] == "abstain" for r in results),
                       sum(r["winner"] == "tie" for r in results)))
        for old, new in zip(copied, results):
            assert new["winner"] == ("abstain" if old["model_winner"] == "unstable" else old["model_winner"])
            assert new["orders"] == old["orders"]
            assert new["evidence_valid"] is None
            assert new["evidence_basis"] == "legacy_unchecked"
        assert copied == report["results"]["quality"]
    assert totals == [(5, 1), (6, 1), (6, 1)]


def test_historical_winners_without_new_evidence_are_not_accepted_by_default():
    q = _module()
    result = q.aggregate_orders([{"winner": "a"}, {"winner": "a"}])
    assert result["winner"] == "abstain" and result["evidence_valid"] is False


def test_same_winner_with_conflicting_material_reasoning_still_abstains():
    def mutate(responses):
        # The reverse-order response suddenly rates the other source copy's
        # information as better, while keeping its overall preference unchanged.
        responses[1]["dimensions"][0].update(preference="A", impact="material")
    result, _ = _compare(mutate=mutate)
    assert result["position_consistent"] is True
    assert result["dimension_consistent"] is False
    assert result["winner"] == "abstain" and result["status"] == "rubric_disagreement"
    assert result["inconsistent_criteria"] == ["information"]


def test_tie_cannot_contradict_an_unopposed_material_advantage():
    def mutate(responses):
        responses[0].update(winner="TIE", decisive_criteria=[])
    result, _ = _compare(mutate=mutate)
    assert result["winner"] == "abstain" and result["evidence_valid"] is False
    assert "tie_conflicts_with_material_advantage" in result["orders"][0]["issues"]


def test_action_comparison_must_quote_both_buttons():
    def mutate(responses):
        responses[0]["dimensions"][2]["evidence_a"] = [{"field": "body", "quote": _copies()[0].body}]
    result, _ = _compare(mutate=mutate)
    assert result["evidence_valid"] is False
    assert "action:A:missing_cta_evidence" in result["orders"][0]["issues"]


def test_budget_stop_on_reverse_order_keeps_the_completed_original_judgment():
    q = _module()
    a, b = _copies()
    router = SavedResponses([_judgment(a, b), BudgetExceeded("budget")])
    result = q.compare(a, b, [Fact(id="F1", text="청소 서비스 18만원부터")], router)
    assert result["winner"] == "abstain" and result["budget_stopped"] is True
    assert len(router.requests) == 2 and result["orders"][0]["response"]["winner"] == "A"
    assert result["orders"][1]["error_type"] == "BudgetExceeded"


def test_judge_abstention_is_preserved_without_claiming_positional_stability():
    result, _ = _compare("ABSTAIN", "ABSTAIN")
    assert result["winner"] == "abstain" and result["status"] == "judge_abstained"
    assert result["position_consistent"] is None


def test_all_unknown_dimensions_cannot_be_promoted_to_a_consensus_tie():
    def mutate(responses):
        for response in responses:
            for dimension in response["dimensions"]:
                dimension.update(preference="UNKNOWN", impact="unknown", reason="비교할 근거를 알 수 없음")
    result, _ = _compare("TIE", "TIE", mutate=mutate)
    assert result["winner"] == "abstain" and result["evidence_valid"] is False
    assert "tie_without_comparison" in result["orders"][0]["issues"]
