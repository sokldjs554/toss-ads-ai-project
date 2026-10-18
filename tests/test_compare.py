"""방식 비교(evals/compare.py) — 지표 계산, '검토 모델만' 이 코드 보정 없이 모델 판정을 그대로 쓰는지, 장애·비용 상한 처리, 데이터 출처, 라벨 변경·G-069 효과 분리.

통제된 응답(대역·가짜 제공자)으로 하네스의 동작을 검증한다. 실제 모델 품질은 이 테스트가 보장하지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import Settings
from core.schemas import Decision
from evals import compare
from evals.compare import (Row, build_report, compare_allowed, estimate, label_changes, pick_examples, run_code_only, run_combined, run_compare, run_llm_only,
                           separate_g069_effects, summarize_rows, trivial_baselines)
from evals.runner import EvalCase, load_dataset
from guardrails.policy import get_catalog
from llm.errors import ProviderError
from llm.router import Router
from observability.tracer import Tracer
from tests.fakes import FakeProvider, scripted_review_provider

CAT = get_catalog()
PARTIAL = Settings.from_env(openai_api_key="sk-fake-not-a-real-key", anthropic_api_key="")
DEMO = Settings.from_env(openai_api_key="", anthropic_api_key="", force_demo=True)
APPROVE = {"decision": "APPROVE", "confidence": 0.95, "findings": [], "rationale": "통제된 응답: 문제 없음", "rewrite": None}


def router_with(response, settings=PARTIAL):
    r = Router(settings, providers={"openai": scripted_review_provider(response), "anthropic": FakeProvider("anthropic", available=False)}, tracer=Tracer())
    r.new_run()
    return r


def case(id="X-1", label="REJECT", headline="타사보다 2배 적립", body="지금 구매", **kw):
    return EvalCase(id=id, category="ecommerce", headline=headline, body=body, cta="자세히 보기", label=label, rule_ids=kw.pop("rule_ids", []), **kw)


def row(label, predicted, **kw):
    return {"dataset": "d", "system": "s", "case_id": kw.pop("case_id", "c"), "label": label, "difficulty": kw.pop("difficulty", "clear"), "predicted": predicted,
            "reason": "", "calls": 0, "attempts": 0, "failed_requests": 0, "latency_ms": 0.0, "cost_usd": 0.0, "error": "", **kw}


# ───────────── 지표 ─────────────
def test_summary_separates_human_review_from_correct_pass_and_excludes_failures():
    rows = [row("REJECT", "REJECT"), row("REVISE", "APPROVE"), row("REVISE", "HUMAN_REVIEW"), row("APPROVE", "APPROVE"), row("APPROVE", "REVISE"), row("APPROVE", "HUMAN_REVIEW"),
            row("APPROVE", "ERROR", error="FM-07 ProviderUnavailable: x", failed_requests=2, cost_usd=0.0)]
    m = summarize_rows(rows)
    assert m["n"] == 7 and m["scored"] == 6 and m["failed"] == 1
    assert (m["unsafe_pass"]["k"], m["unsafe_pass"]["n"]) == (1, 3) and (m["caught"]["k"], m["bad_to_human"]["k"]) == (1, 1)
    assert (m["correct_pass"]["k"], m["correct_pass"]["n"]) == (1, 2 + 1)           # 담당자 확인으로 넘긴 건은 '올바른 통과' 가 아니다 (분모는 문제없는 광고 3건)
    assert m["unnecessary_revision"]["k"] == 1 and m["good_to_human"]["k"] == 1 and m["human_review"]["k"] == 2
    assert m["cost_is_lower_bound"] is True and m["failed_requests"] == 2             # 응답을 못 받은 요청이 있으면 비용은 '이상'


def test_all_human_review_is_not_reported_as_good():
    rows = [row("REJECT", "REJECT"), row("REVISE", "REVISE"), row("APPROVE", "APPROVE"), row("APPROVE", "APPROVE")]
    t = trivial_baselines(rows)
    h = t["전부 담당자 확인"]
    assert h["unsafe_pass"]["k"] == 0 and h["correct_pass"]["k"] == 0 and h["human_review"]["k"] == 4        # 위험 통과 0 이어도 올바른 통과도 0
    assert "좋은 성능이 아니다" in h["note"]
    assert t["전부 통과"]["unsafe_pass"] == {"k": 2, "n": 2, "rate": 1.0}


# ───────────── 방식별 판정 ─────────────
def test_code_only_uses_the_code_decision_without_any_model():
    r = run_code_only(case(), CAT)
    assert r.predicted == "REJECT" and r.calls == 0 and r.cost_usd == 0.0 and "TA-COMPARE-01" in r.rules
    clean = run_code_only(case(label="APPROVE", headline="가을 신상품", body="자세한 조건은 상세 페이지에서 확인하세요"), CAT)
    assert clean.predicted == "APPROVE"                                        # finding 이 없으면 통과 (코드 혼자 확정하는 방식 — 서비스 운영 방식이 아님)


def test_llm_only_takes_the_model_decision_as_is_with_no_code_correction():
    """코드 검사가 게재 불가(block)로 확정하는 문구라도, 모델이 APPROVE 라고 답하면 이 방식에서는 그대로 통과다. 합친 방식은 REJECT."""
    c = case()
    assert run_code_only(c, CAT).predicted == "REJECT"
    lo = run_llm_only(c, PARTIAL, router_with(APPROVE), CAT)
    assert lo.predicted == "APPROVE" and lo.model_decision == "APPROVE" and lo.calls == 1 and lo.cost_usd > 0 and not lo.error
    co = run_combined(c, PARTIAL, router_with(APPROVE), CAT)
    assert co.predicted == "REJECT"                                                   # 코드가 확정한 반려는 모델 응답과 무관


def test_llm_only_prompt_does_not_claim_a_code_check_and_gets_no_code_findings():
    seen = {}

    def spy(req):
        seen["system"], seen["user"] = req.system, req.user
        return APPROVE

    run_llm_only(case(landing_text="랜딩 문구"), PARTIAL, router_with(spy), CAT)
    assert "별도의 검사 도구는 없다" in seen["system"] and "결정론" not in seen["system"] and "코드가 이미" not in seen["system"]
    assert "[결정론 검사가 찾은 확정 finding]" not in seen["user"] and "[광고주 사실 목록]" in seen["user"] and "[랜딩 페이지 텍스트(발췌)]" in seen["user"]


def test_llm_only_confidence_variant_is_derived_from_the_same_response():
    low = dict(APPROVE, confidence=0.3)
    r = run_llm_only(case(), PARTIAL, router_with(low), CAT)
    assert r.predicted == "APPROVE" and r.predicted_tau == "HUMAN_REVIEW"             # 원판정은 그대로, τ 를 얹은 파생 판정만 담당자 확인
    hi = run_llm_only(case(), PARTIAL, router_with(APPROVE), CAT)
    assert hi.predicted_tau == "APPROVE"


def test_provider_failure_is_a_failure_not_a_pass_or_a_human_review():
    r = run_llm_only(case(), PARTIAL, router_with(ProviderError("boom", provider="openai")), CAT)
    assert r.error and r.predicted == "ERROR" and r.failed_requests >= 1
    r.dataset = "d"
    rep = build_report([r], {"d": {"n": 1, "split": "dev"}}, PARTIAL, CAT, ("llm_only",), "live")
    assert rep["failures"]["n"] == 1 and compare_allowed(rep)[0] is False
    m = rep["results"]["d"]["llm_only"]
    assert m["scored"] == 0 and m["human_review"]["k"] == 0 and m["unsafe_pass"]["k"] == 0     # 실패가 판정으로 세어지지 않는다


def test_run_compare_labels_controlled_vs_live_and_records_settings(monkeypatch):
    rep = run_compare(["contrast_g069"], DEMO, systems=("code_only", "combined"))
    assert rep["run_kind"] == "controlled" and "모델 품질이 아니다" in rep["run_kind_note"] and rep["settings"]["mode"] == "demo"
    assert rep["systems"]["combined"]["controlled_note"]
    assert rep["settings"]["policy_version"] == CAT.version and rep["settings"]["reviewer_prompt_version"]
    assert set(rep["systems"]["code_only"]["info_given"]) >= {"광고 문구·안내 문구", "상품 정보(사실 목록)", "검사 기준(규칙)", "코드 보정·판정 함수"}
    live = run_compare(["contrast_g069"], PARTIAL, router=router_with(APPROVE), systems=("llm_only",))
    assert live["run_kind"] == "live" and "controlled_note" not in live["systems"]["llm_only"] and compare_allowed(live) == (True, "")
    assert live["datasets"]["contrast_g069"]["dataset_sha256"] and live["datasets"]["contrast_g069"]["split"] == "dev"


def test_cost_cap_stops_the_run_and_the_result_is_not_savable():
    class Pricey(FakeProvider):
        pass

    from llm.base import LLMResponse

    def behave(req):
        parsed = req.schema.model_validate(APPROVE)
        return LLMResponse(text="{}", parsed=parsed, provider="openai", model="controlled", input_tokens=10, output_tokens=5, latency_ms=1.0, cost_usd=0.4, finish_reason="stop")

    r = Router(PARTIAL, providers={"openai": FakeProvider("openai", model="controlled", behavior=behave), "anthropic": FakeProvider("anthropic", available=False)}, tracer=Tracer())
    rep = run_compare(["golden"], PARTIAL, router=r, systems=("llm_only",), cost_cap_usd=1.0)
    assert rep["aborted"] and "상한" in rep["aborted"]["reason"] and len(rep["rows"]) < 55
    assert compare_allowed(rep)[0] is False


def test_estimate_makes_no_calls_and_gives_calls_and_cost(monkeypatch):
    monkeypatch.setattr("evals.compare.Router", lambda *a, **k: (_ for _ in ()).throw(AssertionError("estimate 는 호출하지 않는다")))
    e = estimate(["golden", "holdout"], ("code_only", "llm_only", "combined"))
    assert e["cases"] == 67 and e["systems"]["code_only"] == {"calls": 0, "cost_usd": 0.0}
    assert e["systems"]["llm_only"]["calls"] == 67 and e["total_cost_usd"] > 0 and "5차" in e["basis"]


# ───────────── 대표 사례 ─────────────
def test_examples_are_picked_where_systems_disagree():
    rows = [
        {**row("REJECT", "REJECT", case_id="a"), "system": "code_only", "dataset": "d"}, {**row("REJECT", "APPROVE", case_id="a"), "system": "llm_only", "dataset": "d"},
        {**row("REVISE", "APPROVE", case_id="b"), "system": "code_only", "dataset": "d"}, {**row("REVISE", "REVISE", case_id="b"), "system": "llm_only", "dataset": "d"},
        {**row("APPROVE", "APPROVE", case_id="c"), "system": "combined", "dataset": "d"}, {**row("APPROVE", "REVISE", case_id="c"), "system": "llm_only", "dataset": "d"},
    ]
    ex = pick_examples(rows)
    assert [e["case_id"] for e in ex["코드 검사가 잡고 검토 모델만으로는 통과시킨 사례"]] == ["a"]
    assert [e["case_id"] for e in ex["검토 모델이 잡고 코드 검사는 통과시킨 사례"]] == ["b"]
    assert [e["case_id"] for e in ex["검토 모델만 쓰면 불필요하게 수정을 요청하는데 합친 방식은 통과시킨 사례"]] == ["c"]


# ───────────── 데이터 출처 · 라벨 변경 · G-069 효과 분리 ─────────────
def test_every_dataset_has_provenance_and_none_is_called_independent_real_data():
    prov = json.loads((Path(compare.__file__).parent / "datasets" / "PROVENANCE.json").read_text(encoding="utf-8"))
    for name in ("golden", "holdout", "contrast_g069", "conditions", "adversarial"):
        p = prov[name]
        assert {"split", "author", "label_basis", "human_review", "used_for_tuning", "derived_from", "independent_real_data"} <= set(p), name
        assert p["independent_real_data"] is False, name                          # 바꿔 쓴 데이터·같은 저자의 라벨을 독립 실제 데이터라고 부르지 않는다
    assert prov["holdout"]["split"] == "holdout" and prov["holdout"]["used_for_tuning"] is False and prov["golden"]["used_for_tuning"] is True
    assert "독립" in prov["holdout"]["human_review"] or "같은 저자" in prov["holdout"]["human_review"]
    assert "바꿔 쓴" in prov["contrast_g069"]["derived_from"] or "G-069" in prov["contrast_g069"]["derived_from"]


def test_label_changes_record_previous_value_and_reason():
    ch = label_changes(["golden"])
    # G-069 은 라벨이 한 번 바뀌었고 이력에는 각 버전의 라벨과 그 이유가 남는다 (현재 라벨과 변경 전 라벨을 함께 보여 준다)
    versions = {c["label_version"]: c for c in ch if c["case_id"] == "G-069"}
    assert versions["1.1"]["label_then"] == "APPROVE" and versions["1.2"]["label_then"] == "REVISE" and versions["1.2"]["current_label"] == "REVISE"
    assert "놓침" in versions["1.1"]["reason_then"] and "GRND-COND-01" in versions["1.2"]["rules_then"] and versions["1.2"]["reason_then"]


def test_g069_effect_is_separated_into_label_vs_model_and_code():
    eff = separate_g069_effects()
    assert eff is not None
    r4, r5 = eff["rows"]
    assert r4["g069_predicted"] == "REVISE" and r5["g069_predicted"] == "REVISE"           # 두 회차 모두 모델·코드는 G-069 를 REVISE 로 판정
    assert r4["g069_rules"] == ["TA-OMISSION-01"] and r5["g069_rules"] == ["GRND-COND-01"]   # 잡는 주체만 모델(4차) → 코드(5차)로 바뀜
    assert r4["under_v1.1_label"]["unnecessary_revision"] == 1 and r4["under_v1.2_label"]["unnecessary_revision"] == 0   # 라벨만 바꿔도 4차의 수정 요청 1건이 사라진다
    assert r5["under_v1.1_label"]["unnecessary_revision"] == 1 and r5["under_v1.2_label"]["unnecessary_revision"] == 0
    assert "라벨" in eff["reading"] and "좋아진 것이 아니라" in eff["reading"]


def test_stored_controlled_report_is_labelled_and_complete():
    rep = json.loads((Path(compare.__file__).parent / "reports" / "compare_controlled.json").read_text(encoding="utf-8"))
    assert rep["run_kind"] == "controlled" and "모델 품질이 아니다" in rep["run_kind_note"]
    assert set(rep["systems"]) == {"code_only", "llm_only", "combined"} and set(rep["datasets"]) == {"golden", "holdout", "contrast_g069", "conditions"}
    assert rep["failures"]["n"] == 0 and rep["rows"] and rep["label_changes"] and rep["g069_effects"]
    assert set(rep["overall_by_split"]) == {"dev", "holdout"}


# ───────────── 검토 후 보강 ─────────────
@pytest.mark.parametrize("category", ["ecommerce", "loan", "investment", "app", "card", "insurance"])
def test_llm_only_system_prompt_never_mentions_a_code_check_for_any_category(category):
    """규칙 설명 안의 ‘코드가 ~ 본다’ 문장까지 빼서, 코드 검사가 없는 이 방식의 프롬프트에 코드 검사가 있다고 적히지 않는다."""
    from agents.prompts import llm_only_system

    cats = CAT.effective_categories(__import__("core.schemas", fromlist=["ProductCategory"]).ProductCategory(category), "")
    text = llm_only_system(CAT, cats)
    assert "코드" not in text and "결정론" not in text, [l for l in text.splitlines() if "코드" in l or "결정론" in l]
    assert "GRND-COND-01" in text and "GRND-COND-02" in text                     # 규칙 자체는 남아 있고 코드를 말하는 문장만 빠졌다


def test_llm_only_gets_the_same_claim_links_and_output_budget_as_the_combined_reviewer():
    from agents import reviewer
    from core.schemas import Claim

    seen = {}

    def spy(req):
        seen["user"], seen["max_tokens"] = req.user, req.max_tokens
        return APPROVE

    c = case(claims=[{"text": "전 상품 10% 할인", "fact_id": "F1"}], facts=[{"id": "F1", "text": "전 상품 10% 할인"}])
    run_llm_only(c, PARTIAL, router_with(spy), CAT)
    assert "주장→근거: [('전 상품 10% 할인', 'F1')]" in seen["user"] and seen["max_tokens"] == 1800
    combined_user = reviewer._user_prompt(c.to_copy(), run_code_only.__globals__["output_guard"].check(c.to_copy(), c.to_product(), {"ecommerce"}), "- F1: 전 상품 10% 할인", "")
    assert "주장→근거: [('전 상품 10% 할인', 'F1')]" in combined_user


def test_consecutive_provider_failures_abort_the_comparison_and_nothing_is_saved(tmp_path, monkeypatch):
    rep = run_compare(["golden"], PARTIAL, router=router_with(ProviderError("boom", provider="openai")), systems=("llm_only",), abort_after=3)
    assert rep["aborted"] and "연속" in rep["aborted"]["reason"] and len(rep["rows"]) == 3
    assert compare_allowed(rep)[0] is False

    written = []
    monkeypatch.setattr(compare, "save_report", lambda r, name="x.json": written.append(name))
    monkeypatch.setattr(compare, "run_compare", lambda *a, **k: rep)
    monkeypatch.setenv("PREFLIGHT_FORCE_DEMO", "1")
    assert compare.main(["--datasets", "golden", "--systems", "llm_only", "--out", "compare_live.json"]) == 2
    assert written == ["compare_failed.json"]                                     # 저장하지 않는 결과는 추적하지 않는 파일에만


def test_llm_only_retries_once_after_a_transient_failure(monkeypatch):
    n = {"calls": 0}

    def flaky(req):
        n["calls"] += 1
        return ProviderError("429", provider="openai") if n["calls"] == 1 else APPROVE

    monkeypatch.setattr("evals.compare._wait_for_breaker", lambda *a, **k: 0.0)
    r = run_llm_only(case(), PARTIAL, router_with(flaky), CAT, breaker_wait_s=30)
    assert not r.error and r.predicted == "APPROVE" and r.failed_requests >= 1       # 재시도로 판정을 받았고 첫 실패 요청은 기록에 남는다
    r2 = run_llm_only(case(), PARTIAL, router_with(ProviderError("x", provider="openai")), CAT, breaker_wait_s=0)
    assert r2.error                                                                  # 기다림 0 이면 재시도하지 않는다


def test_label_changes_flag_only_real_label_value_changes():
    ch = label_changes(["golden"])
    assert {c["case_id"] for c in ch if c["label_changed"]} == {"G-069"}              # G-081 은 기대 규칙·분류만 바뀌었고 라벨 값은 그대로
    assert any(c["case_id"] == "G-081" and not c["label_changed"] for c in ch)


def test_report_carries_the_asymmetry_caveats_and_the_extra_info_rows():
    rep = run_compare(["contrast_g069"], DEMO, systems=("code_only", "llm_only", "combined"))
    assert any("수정안을 만들지 않는다" in c for c in rep["caveats"]) and any("같은 저자" in c for c in rep["caveats"])
    for s in ("code_only", "llm_only", "combined"):
        assert "수정안 제안·재검사" in rep["systems"][s]["info_given"] and "광고가 가리킨 근거(주장→근거 연결)" in rep["systems"][s]["info_given"]
