"""Real retrieval, validation, guardrails and bounded orchestration; SDK is the seam."""
import json
import pytest
from core.schemas import AdCopy, Fact, ProductFacts, ProductCategory
from agents.live_pilot import retrieve, validate_review, run_workflow, ReviewEvidenceError


def inputs(headline="가을 쿠폰 안내"):
    return AdCopy(headline=headline, body="쿠폰! 지금 이용 조건을 확인하세요", cta="확인하기"), ProductFacts(
        name="합성 쿠폰", category=ProductCategory.ECOMMERCE,
        facts=[Fact(id="F1", text="가을 쿠폰을 제공합니다. 이용 조건을 확인하세요.")])


def answer(docs, decision="APPROVE"):
    return {"recommendation": decision, "reason": "제공된 자료를 검토했습니다.", "findings": [],
            "citations": [{"source_id": d["id"], "quote": d["text"]} for d in docs[:1] + [next(d for d in docs if d["kind"] == "fact")]]}


def message(content=None, tool_calls=None):
    return {"role": "assistant", "content": json.dumps(content, ensure_ascii=False) if content is not None else None,
            **({"tool_calls": tool_calls} if tool_calls else {})}


def structured_answer(docs):
    raw = answer(docs)
    policy, fact = raw.pop("citations")
    return {**raw, "policy_citation": policy, "fact_citation": fact, "additional_citations": []}


def test_rag_requests_two_required_source_kinds_in_strict_provider_schema():
    copy, product = inputs()
    seen = []
    def provider(**kw):
        seen.append(kw)
        return message(structured_answer(retrieve(copy, product)))
    result = run_workflow("rag", copy, product, provider)
    fmt = seen[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert {"policy_citation", "fact_citation"} <= set(schema["required"])
    for field,kind in (("policy_citation","policy"),("fact_citation","fact")):
        assert set(schema["properties"][field]["properties"]["source_id"]["enum"]) == {d["id"] for d in retrieve(copy,product) if d["kind"]==kind}
    assert result["status"] == "human_review"
    assert len(result["review"]["citations"]) == 2


def test_structured_field_cannot_pass_a_fact_as_policy_evidence():
    copy, product = inputs()
    raw = structured_answer(retrieve(copy, product))
    raw["policy_citation"] = raw["fact_citation"]
    result = run_workflow("rag", copy, product, lambda **kw: message(raw))
    assert result["status"] == "hold" and result["reason"] == "citation_kind_mismatch"


def test_failed_structured_review_retains_retrieved_sources_for_authenticated_review():
    copy, product = inputs()
    raw = structured_answer(retrieve(copy, product))
    del raw["fact_citation"]
    result = run_workflow("rag", copy, product, lambda **kw: message(raw))
    assert result["status"] == "hold" and result["review"] is None
    assert result["sources"] == retrieve(copy, product)


def test_retrieval_excludes_unrelated_category_and_includes_triggered_policy():
    copy, product = inputs("업계 최저 가을 쿠폰")
    docs = retrieve(copy, product)
    assert "TA-SUPERLATIVE-01" in [d["id"] for d in docs]
    assert not any(d["id"] == "FIN-DISCLOSE-LOAN-01" for d in docs)
    assert any(d["id"] == "fact:F1" for d in docs)


@pytest.mark.parametrize("mutation", ["unknown", "wrong_quote", "empty", "missing_fact", "invented_ad_span"])
def test_source_and_ad_citation_mismatch_fails_closed(mutation):
    copy, product = inputs()
    docs = retrieve(copy, product)
    response = answer(docs)
    if mutation == "unknown": response["citations"][0]["source_id"] = "unretrieved"
    if mutation == "wrong_quote": response["citations"][0]["quote"] = "없는 근거"
    if mutation == "empty": response["citations"] = []
    if mutation == "missing_fact": response["citations"] = response["citations"][:1]
    if mutation == "invented_ad_span":
        response["findings"] = [{"rule_id": docs[0]["id"], "ad_quote": "없는 광고 구절", "reason": "위반"}]
    with pytest.raises(ReviewEvidenceError): validate_review(response, docs, copy)


def test_model_approval_never_overrides_existing_block():
    copy, product = inputs("다! 단 계 영업")
    result = run_workflow("rag", copy, product, lambda **kw: message(structured_answer(retrieve(copy, product))))
    assert result["code_decision"] == "REJECT"
    assert result["decision"] == "REJECT"
    assert result["status"] == "human_review"


def test_rag_failure_is_hold_without_demo_or_retry():
    copy, product = inputs()
    calls = []
    def unavailable(**kw):
        calls.append(kw)
        raise TimeoutError("sensitive provider text")
    result = run_workflow("rag", copy, product, unavailable)
    assert result["status"] == "hold" and result["reason"] == "provider_timeout"
    assert len(calls) == 1 and "sensitive" not in json.dumps(result)


def tool(copy, identifier="call_1", name="check_policy"):
    return [{"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps({
        "headline": copy.headline, "body": copy.body, "cta": copy.cta, "disclosures": copy.disclosures})}}]


def test_model_tool_call_runs_real_guardrail_and_returns_result_before_handoff():
    original, product = inputs("업계 최저 가을 쿠폰")
    revised, _ = inputs()
    calls = []
    def provider(**kw):
        calls.append(kw)
        if len(calls) == 1: return message(tool_calls=tool(revised))
        if len(calls) == 2:
            assert kw["messages"][-1]["role"] == "tool"
            assert json.loads(kw["messages"][-1]["content"])["decision"] == "APPROVE"
            return message({"finish": True})
        return message(structured_answer(retrieve(revised, product)))
    result = run_workflow("tools", original, product, provider)
    assert result["status"] == "human_review" and result["decision"] == "APPROVE"
    assert result["revisions"] == 1 and len(calls) == 3
    assert result["tool_checks"][0]["tool_call_id"] == "call_1"
    assert result["copy"]["headline"] == revised.headline


def test_unresolved_tool_loop_is_bounded_and_held():
    copy, product = inputs("업계 최저 가을 쿠폰")
    calls = []
    def provider(**kw):
        calls.append(kw)
        return message(tool_calls=tool(copy, "call_" + str(len(calls))))
    result = run_workflow("tools", copy, product, provider)
    assert result["status"] == "hold" and result["reason"] == "revision_limit"
    assert len(calls) == 2 and result["revisions"] == 2


@pytest.mark.parametrize("response", [message({"finish": True}), message(tool_calls=[{"id":"x","type":"function","function":{"name":"delete_data","arguments":"{}"}}])])
def test_unchecked_or_unknown_tool_output_is_held(response):
    copy, product = inputs()
    result = run_workflow("tools", copy, product, lambda **kw: response)
    assert result["status"] == "hold"


def test_model_approval_with_its_own_fix_finding_is_held():
    copy, product = inputs("업계 최저 가을 쿠폰")
    docs = retrieve(copy, product)
    response = answer(docs)
    response["findings"] = [{"rule_id": "TA-SUPERLATIVE-01", "ad_quote": "업계 최저", "reason": "입증 없는 최상급"}]
    with pytest.raises(ReviewEvidenceError): validate_review(response, docs, copy)
