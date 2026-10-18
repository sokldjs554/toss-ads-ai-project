"""방식 비교 — 같은 고정 사례를 세 방식으로 판정해 놓고 나란히 본다.

  code_only  코드 검사만: 표현 규칙·필수 안내 문구·상품 정보 일치(숫자·조건)·형식·중복. 모델 호출 없음. 코드 검사 결과를 그대로 판정으로 쓴다
             (finding 이 없으면 통과). 실제 서비스는 코드 혼자 통과를 확정하지 않는다 — 그 운영 방식은 `baseline_deterministic_only.json` 에 따로 있다.
  llm_only   검토 모델만: 같은 규칙 카탈로그·같은 출력 스키마를 받지만 코드 검사 결과를 받지 않고, 모델이 돌려준 판정을 **코드의 보정 없이** 그대로 쓴다
             (인용 검증·코드 확정 계층 보호·공통 판정 함수·담당자 확인 경로 없음). 확신 기준(τ)을 얹은 파생 판정은 같은 응답으로 따로 계산한다.
  combined   지금 서비스가 쓰는 방식: 코드 검사 → 검토 모델(코드 결과를 받음) → 인용 검증 → 공통 판정 함수(τ) → 수정안 재검사.

지표는 '통과 후보를 사람에게 넘긴 것' 과 '올바르게 통과시킨 것' 을 섞지 않는다. 담당자 확인은 따로 세고, 전부 담당자 확인으로 넘기는 방식(위험 통과 0)은
올바른 통과가 0 이라는 점을 trivial 기준으로 나란히 적는다. 대역(통제된 응답)으로 돌린 결과는 하네스 검증용이며 모델 품질이 아니다 (run_kind).
실제 모델 호출은 승인된 범위에서만 한다 — `--cost-cap` 으로 승인 예산을 넘기지 않게 막고, 제공자 장애가 섞이면 결과를 저장하지 않는다 (FM-24).

    python -m evals.compare --datasets golden,holdout,contrast_g069,conditions --out compare_controlled.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from agents.prompts import LLM_ONLY_PROMPT_VERSION, PROMPT_VERSION, llm_only_system
from core.config import Settings
from core.llm_schemas import build_review_schema
from core.schemas import AdCopy
from evals.metrics import UNSAFE_LABELS
from evals.runner import (REPORTS, EvalAborted, EvalCase, dataset_fingerprint, dataset_path, evaluate_with_retry, load_dataset, load_report, save_report)
from guardrails import output_guard
from guardrails.failsafe import classify_exception
from guardrails.policy import PolicyCatalog, get_catalog
from llm.base import LLMRequest, Task
from llm.router import Router
from observability.tracer import Tracer

DATASETS_DIR = Path(__file__).parent / "datasets"
SYSTEMS = ("code_only", "llm_only", "combined")
SYSTEM_LABEL = {"code_only": "코드 검사만", "llm_only": "검토 모델만", "combined": "합친 방식 (현재 서비스)"}
FLAG = ("REVISE", "REJECT")

# 각 방식이 받는 정보 — 비교가 공정한지 읽는 사람이 직접 판단할 수 있게 적어 둔다.
INFO_GIVEN = {
    "code_only": {
        "광고 문구·안내 문구": "예", "상품 정보(사실 목록)": "예 — 코드가 숫자·조건을 대조", "랜딩 페이지 문구": "예 — 코드가 숫자·표현을 대조",
        "검사 기준(규칙)": "예 — 코드로 구현된 규칙(어휘·고지 체크리스트·숫자/조건 대조·형식)", "검토 모델": "아니오", "코드 보정·판정 함수": "코드 검사 결과를 그대로 판정으로 사용",
    },
    "llm_only": {
        "광고 문구·안내 문구": "예", "상품 정보(사실 목록)": "예 — 프롬프트에 텍스트로 제공", "랜딩 페이지 문구": "예 — 프롬프트에 앞 1,200자",
        "검사 기준(규칙)": f"예 — 같은 규칙 카탈로그를 프롬프트로 제공 (비교 전용 프롬프트 {LLM_ONLY_PROMPT_VERSION})", "검토 모델": "예",
        "코드 보정·판정 함수": "없음 — 모델이 돌려준 판정을 그대로 사용 (인용 검증·확정 계층 보호·담당자 확인 경로 없음)",
    },
    "combined": {
        "광고 문구·안내 문구": "예", "상품 정보(사실 목록)": "예 — 코드 대조 + 프롬프트", "랜딩 페이지 문구": "예",
        "검사 기준(규칙)": f"예 — 코드 규칙 + 같은 카탈로그를 프롬프트로 (검토 프롬프트 {PROMPT_VERSION})", "검토 모델": "예 — 코드 검사 결과를 받음",
        "코드 보정·판정 함수": "있음 — 인용 검증, 코드가 확정한 계층 보호, 공통 판정 함수(확신 기준 τ), 수정안 재검사. 평가 경로에는 교차 확인·가상 사용자가 없음",
    },
}


@dataclass
class Row:
    dataset: str
    system: str
    case_id: str
    label: str
    difficulty: str
    predicted: str
    confidence: float = 0.0
    rules: list = field(default_factory=list)
    reason: str = ""
    calls: int = 0
    attempts: int = 0
    failed_requests: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    error: str = ""
    model_decision: str = ""        # llm_only: 모델이 돌려준 판정 그대로
    predicted_tau: str = ""         # llm_only: 같은 응답에 확신 기준(τ)을 얹은 판정 (파생)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ───────────────────────────── 방식별 판정 ─────────────────────────────
def run_code_only(case: EvalCase, catalog: PolicyCatalog) -> Row:
    copy, product = case.to_copy(), case.to_product()
    t0 = time.perf_counter()
    cats = catalog.effective_categories(product.category, copy.full_text())
    det = output_guard.check(copy, product, cats, case.landing_text, [], catalog)
    rules = sorted({f.rule_id for f in det.findings if f.severity.value != "warn"})
    return Row(dataset="", system="code_only", case_id=case.id, label=case.label, difficulty=case.difficulty, predicted=det.decision.value, confidence=1.0 if det.findings else 0.0,
               rules=rules, reason="코드 검사 결과 그대로" + (f" ({', '.join(rules)})" if rules else " (finding 없음 → 통과)"), latency_ms=round((time.perf_counter() - t0) * 1000, 2))


def _llm_only_user(copy: AdCopy, facts: str, landing_text: str) -> str:
    return (f"[소재]\n헤드라인: {copy.headline}\n본문: {copy.body}\nCTA: {copy.cta}\n고지: {' / '.join(copy.disclosures) or '(없음)'}\n\n[광고주 사실 목록]\n{facts}\n\n"
            + (f"[랜딩 페이지 텍스트(발췌)]\n{landing_text[:1200]}\n" if landing_text else ""))


def run_llm_only(case: EvalCase, settings: Settings, router: Router, catalog: PolicyCatalog) -> Row:
    copy, product = case.to_copy(), case.to_product()
    cats = catalog.effective_categories(product.category, copy.full_text())
    facts = "\n".join(f"- {f.id}: {f.text}" for f in product.facts) or "- (없음)"
    rule_ids = [r.id for r in catalog.rules_for(cats)]
    req = LLMRequest(task=Task.REVIEW, system=llm_only_system(catalog, cats), user=_llm_only_user(copy, facts, case.landing_text),
                     schema=build_review_schema(rule_ids), max_tokens=1200, temperature=0.0, context={"rule_ids": rule_ids, "copy": copy.model_dump(mode="json"), "required_disclosures": []},
                     label=f"compare_llm_only:{case.id}")
    b = router.budget
    before = (b.spent_usd, b.calls, b.attempts, b.failed_attempts)
    t0 = time.perf_counter()
    row = Row(dataset="", system="llm_only", case_id=case.id, label=case.label, difficulty=case.difficulty, predicted="")
    try:
        resp = router.complete(req)
        if resp.parsed is None:
            raise ValueError("심사 출력이 비어 있음")
        p = resp.parsed
        row.model_decision = p.decision                      # 모델이 돌려준 판정 그대로 — 코드 보정 없음
        row.predicted = p.decision
        row.confidence = max(0.0, min(1.0, float(p.confidence)))
        row.rules = sorted({f.rule_id for f in p.findings})
        row.reason = (p.rationale or "").strip()[:200]
        row.predicted_tau = p.decision if row.confidence >= settings.auto_decision_confidence else "HUMAN_REVIEW"
    except Exception as e:  # noqa: BLE001 — 호출 실패는 판정이 아니다: 통과로도 담당자 확인으로도 세지 않고 실패로 기록한다
        row.error = f"{classify_exception(e)} {type(e).__name__}: {str(e)[:200]}"
        row.predicted = "ERROR"
    row.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    row.cost_usd = round(b.spent_usd - before[0], 6)
    row.calls, row.attempts, row.failed_requests = b.calls - before[1], b.attempts - before[2], b.failed_attempts - before[3]
    return row


def run_combined(case: EvalCase, settings: Settings, router: Router, catalog: PolicyCatalog, breaker_wait_s: float = 0.0) -> Row:
    b = router.budget
    before = (b.spent_usd, b.calls, b.attempts, b.failed_attempts)
    r = evaluate_with_retry(case, settings, router, catalog, use_llm=True, breaker_wait_s=breaker_wait_s)
    err = r.error or r.rewrite_error
    row = Row(dataset="", system="combined", case_id=case.id, label=case.label, difficulty=case.difficulty, predicted=("ERROR" if err else r.predicted), confidence=r.confidence,
              rules=list(r.predicted_rules), reason=r.reason[:200], latency_ms=r.latency_ms, cost_usd=r.cost_usd, error=err, model_decision=r.model_decision,
              failed_requests=r.failed_requests)
    row.calls, row.attempts = b.calls - before[1], b.attempts - before[2]
    return row


# ───────────────────────────── 지표 ─────────────────────────────
def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": (round(k / n, 4) if n else None)}


def summarize_rows(rows: list[dict], predicted_key: str = "predicted") -> dict:
    """한 방식의 한 데이터 묶음 지표. 호출이 실패한 행(error)은 판정이 아니므로 어느 항목에도 넣지 않고 따로 센다."""
    scored = [r for r in rows if not r.get("error")]
    bad = [r for r in scored if r["label"] in UNSAFE_LABELS]       # 수정·게재 제한이 필요한 광고
    good = [r for r in scored if r["label"] == "APPROVE"]          # 문제없는 광고
    pred = lambda r: r.get(predicted_key) or r["predicted"]        # noqa: E731
    cost = round(sum(r.get("cost_usd", 0.0) for r in rows), 6)
    failed_requests = sum(r.get("failed_requests", 0) for r in rows)
    return {
        "n": len(rows), "scored": len(scored), "failed": len(rows) - len(scored),
        "unsafe_pass": _rate(sum(pred(r) == "APPROVE" for r in bad), len(bad)),             # 잘못 통과한 광고
        "caught": _rate(sum(pred(r) in FLAG for r in bad), len(bad)),                       # 수정·게재 불가로 잡음
        "bad_to_human": _rate(sum(pred(r) == "HUMAN_REVIEW" for r in bad), len(bad)),        # 문제 있는 광고를 담당자 확인으로 넘김
        "correct_pass": _rate(sum(pred(r) == "APPROVE" for r in good), len(good)),           # 올바른 통과
        "unnecessary_revision": _rate(sum(pred(r) in FLAG for r in good), len(good)),        # 불필요한 수정 요청
        "good_to_human": _rate(sum(pred(r) == "HUMAN_REVIEW" for r in good), len(good)),     # 문제없는 광고를 담당자 확인으로 넘김
        "human_review": _rate(sum(pred(r) == "HUMAN_REVIEW" for r in scored), len(scored)),
        "calls": sum(r.get("calls", 0) for r in rows), "attempts": sum(r.get("attempts", 0) for r in rows), "failed_requests": failed_requests,
        "latency_ms_total": round(sum(r.get("latency_ms", 0.0) for r in rows), 1), "latency_ms_mean": round(sum(r.get("latency_ms", 0.0) for r in rows) / len(rows), 2) if rows else 0.0,
        "cost_usd": cost, "cost_is_lower_bound": failed_requests > 0,
    }


CONTROLLED_NOTES = {
    "code_only": "코드 검사는 대역과 무관하게 실제 코드가 돈 결과다 (이 열은 실제 값)",
    "llm_only": "대역 심사 모델은 코드 검사 결과가 입력에 있을 때만 위반을 말한다. 이 방식에는 코드 검사 결과가 없어 항상 '통과' 로 답한다 — 이 열의 숫자는 모델 품질이 아니다",
    "combined": "코드 검사 부분은 실제 값이고 의미 검사 부분은 대역이다 — 합친 방식의 모델 품질로 읽지 않는다",
}


def by_difficulty(rows: list[dict]) -> dict:
    """사례 종류별 (명백한 위반·우회·경계·무서운 단어·모델만 잡을 수 있는 사례 등) 방식별 지표 — 평균 뒤에 가려진 차이를 본다."""
    out: dict[str, dict] = {}
    for d in sorted({r["difficulty"] for r in rows}):
        out[d] = {s: summarize_rows([r for r in rows if r["difficulty"] == d and r["system"] == s]) for s in sorted({r["system"] for r in rows})}
    return out


def trivial_baselines(rows: list[dict]) -> dict:
    """아무것도 판단하지 않는 두 방식 — 숫자만 보고 좋은 방식이라고 오해하지 않게 나란히 둔다."""
    bad = sum(r["label"] in UNSAFE_LABELS for r in rows)
    good = sum(r["label"] == "APPROVE" for r in rows)
    return {
        "전부 담당자 확인": {"unsafe_pass": _rate(0, bad), "correct_pass": _rate(0, good), "unnecessary_revision": _rate(0, good), "human_review": _rate(len(rows), len(rows)),
                          "note": "잘못 통과한 광고는 0 이지만 올바른 통과도 0 이고 모든 건을 사람이 봐야 한다 — 좋은 성능이 아니다"},
        "전부 통과": {"unsafe_pass": _rate(bad, bad), "correct_pass": _rate(good, good), "unnecessary_revision": _rate(0, good), "human_review": _rate(0, len(rows)),
                    "note": "문제 있는 광고를 전부 통과시킨다"},
    }


def pick_examples(all_rows: list[dict], limit: int = 2) -> dict:
    """방식끼리 갈린 대표 사례. 사례 id 순으로 고정해 같은 입력이면 같은 사례가 뽑히게 한다."""
    by_case: dict[tuple, dict] = {}
    for r in all_rows:
        by_case.setdefault((r["dataset"], r["case_id"]), {})[r["system"]] = r

    def pick(pred) -> list[dict]:
        out = []
        for (ds, cid), s in sorted(by_case.items()):
            if pred(s):
                out.append({"dataset": ds, "case_id": cid, "label": next(iter(s.values()))["label"], **{k: {"predicted": v["predicted"], "reason": v["reason"][:120]} for k, v in s.items()}})
            if len(out) >= limit:
                break
        return out

    ok = lambda s, k: k in s and not s[k].get("error")  # noqa: E731
    bad = lambda s: next(iter(s.values()))["label"] in UNSAFE_LABELS   # noqa: E731
    good = lambda s: next(iter(s.values()))["label"] == "APPROVE"      # noqa: E731
    return {
        "코드 검사가 잡고 검토 모델만으로는 통과시킨 사례": pick(lambda s: bad(s) and ok(s, "code_only") and ok(s, "llm_only") and s["code_only"]["predicted"] in FLAG and s["llm_only"]["predicted"] == "APPROVE"),
        "검토 모델이 잡고 코드 검사는 통과시킨 사례": pick(lambda s: bad(s) and ok(s, "code_only") and ok(s, "llm_only") and s["llm_only"]["predicted"] in FLAG and s["code_only"]["predicted"] == "APPROVE"),
        "합친 방식이 잘못 통과시킨 사례": pick(lambda s: bad(s) and ok(s, "combined") and s["combined"]["predicted"] == "APPROVE"),
        "합친 방식이 불필요하게 수정을 요청한 사례": pick(lambda s: good(s) and ok(s, "combined") and s["combined"]["predicted"] in FLAG),
        "검토 모델만 쓰면 불필요하게 수정을 요청하는데 합친 방식은 통과시킨 사례": pick(lambda s: good(s) and ok(s, "llm_only") and ok(s, "combined") and s["llm_only"]["predicted"] in FLAG and s["combined"]["predicted"] == "APPROVE"),
        "합친 방식이 담당자 확인으로 넘긴 사례": pick(lambda s: ok(s, "combined") and s["combined"]["predicted"] == "HUMAN_REVIEW"),
    }


# ───────────────────────────── 라벨 변경 · G-069 효과 분리 ─────────────────────────────
def label_changes(names: list[str]) -> list[dict]:
    """데이터의 라벨 변경 이력(변경 전 값·이유). 라벨을 바꾼 사례는 점수를 바꾸므로 따로 보여 준다."""
    out = []
    for name in names:
        if not dataset_path(name).exists():
            continue
        for c in load_dataset(name):
            for h in c.label_history:
                out.append({"dataset": name, "case_id": c.id, "current_label": c.label, "label_version": h.get("version"), "label_then": h.get("label"), "reason_then": h.get("reason"), "rules_then": h.get("rule_ids", [])})
    return out


def _benign_from_report(rep: dict, label_override: dict[str, str]) -> dict:
    cases = rep.get("cases", [])
    rows = [{"id": c["id"], "label": label_override.get(c["id"], c["label"]), "predicted": c["predicted"]} for c in cases]
    good = [r for r in rows if r["label"] == "APPROVE"]
    return {"benign_n": len(good), "correct_pass": sum(r["predicted"] == "APPROVE" for r in good), "unnecessary_revision": sum(r["predicted"] in FLAG for r in good),
            "to_human": sum(r["predicted"] == "HUMAN_REVIEW" for r in good)}


def separate_g069_effects() -> Optional[dict]:
    """G-069 의 점수 변화를 '모델·코드가 바뀐 효과' 와 '라벨이 바뀐 효과' 로 가른다.
    4차(골든셋 v1.1, 라벨 APPROVE)와 5차(v1.2, 라벨 REVISE)의 저장된 판정을 같은 라벨 두 벌로 각각 다시 채점한다."""
    r4 = load_report("history/baseline_partial_run4_golden_v1.1_policy_2026.10.1.json")
    r5 = load_report("baseline_partial.json")
    if not r4 or not r5:
        return None
    def pred(rep): return next((c["predicted"] for c in rep["cases"] if c["id"] == "G-069"), None)  # noqa: E704
    v11, v12 = {"G-069": "APPROVE"}, {"G-069": "REVISE"}
    rows = []
    for name, rep in (("4차 (검사 기준 2026.10.1)", r4), ("5차 (검사 기준 2026.10.2, 조건 생략 규칙 추가)", r5)):
        g = next((c for c in rep["cases"] if c["id"] == "G-069"), {})
        rows.append({"round": name, "g069_predicted": pred(rep), "g069_rules": g.get("predicted_rules", []), "policy_version": rep.get("policy_version"), "prompt_version": rep.get("prompt_version"),
                     "under_v1.1_label": _benign_from_report(rep, v11), "under_v1.2_label": _benign_from_report(rep, v12)})
    return {"rows": rows, "reading": "같은 라벨(v1.2) 로 4차·5차를 다시 채점하면 문제없는 광고의 불필요한 수정 요청 수는 4차 1건이 아니라 0건이 된다 — 4차→5차에서 보인 '수정 요청 1→0' 은 "
                                      "모델이나 코드가 좋아진 것이 아니라 G-069 의 라벨을 APPROVE 에서 REVISE 로 바로잡은 효과다. 모델·코드 변경의 효과는 G-069 에서 'TA-OMISSION-01(모델) → GRND-COND-01(코드)' 로 "
                                      "잡는 주체가 바뀐 것에 한정된다."}


# ───────────────────────────── 실행 ─────────────────────────────
def compare_allowed(report: dict) -> tuple[bool, str]:
    f = report.get("failures", {})
    if report.get("aborted"):
        return False, f"비교가 중단됨 ({report['aborted']})"
    if f.get("n"):
        return False, f"모델 호출 실패 {f['n']}건 ({', '.join(f.get('cases', [])[:8])}) — 장애가 섞인 결과는 측정이 아니다"
    return True, ""


def run_compare(datasets: list[str], settings: Settings, router: Optional[Router] = None, catalog: Optional[PolicyCatalog] = None, systems: tuple[str, ...] = SYSTEMS,
                limit: Optional[int] = None, cost_cap_usd: Optional[float] = None, progress: Optional[Callable[[str, str, int, int], None]] = None,
                run_kind: Optional[str] = None) -> dict:
    catalog = catalog or get_catalog()
    needs_model = any(s in systems for s in ("llm_only", "combined"))
    if needs_model:
        router = router or Router(settings, tracer=Tracer())
        router.budget.cap_usd = cost_cap_usd if cost_cap_usd is not None else max(router.budget.cap_usd, 5.0)
        router.budget.max_calls = 10_000
    kind = run_kind or ("controlled" if settings.mode.value == "demo" else "live")
    prov = json.loads((DATASETS_DIR / "PROVENANCE.json").read_text(encoding="utf-8"))
    rows: list[Row] = []
    ds_info: dict[str, dict] = {}
    aborted = None
    try:
        for ds in datasets:
            cases = load_dataset(ds)
            if limit:
                cases = cases[:limit]
            fp = dataset_fingerprint(ds)
            ds_info[ds] = {"n": len(cases), **fp, **{k: v for k, v in prov.get(ds, {}).items()}}
            for i, case in enumerate(cases, start=1):
                for system in systems:
                    if system == "code_only":
                        row = run_code_only(case, catalog)
                    elif system == "llm_only":
                        row = run_llm_only(case, settings, router, catalog)
                    else:
                        row = run_combined(case, settings, router, catalog)
                    row.dataset = ds
                    rows.append(row)
                    if progress:
                        progress(ds, system, i, len(cases))
                if cost_cap_usd is not None and router is not None and router.budget.spent_usd > cost_cap_usd:
                    aborted = {"reason": f"누적 비용 ${router.budget.spent_usd:.2f} 가 상한 ${cost_cap_usd:.2f} 초과", "after_case": f"{ds}:{case.id}"}
                    raise EvalAborted(aborted["reason"], {})
    except EvalAborted:
        pass
    return build_report(rows, ds_info, settings, catalog, systems, kind, aborted, cost_cap_usd)


def build_report(rows: list[Row], ds_info: dict, settings: Settings, catalog: PolicyCatalog, systems: tuple[str, ...], kind: str, aborted: Optional[dict] = None,
                 cost_cap_usd: Optional[float] = None) -> dict:
    dicts = [r.to_dict() for r in rows]
    results: dict[str, dict] = {}
    for ds in ds_info:
        results[ds] = {s: summarize_rows([r for r in dicts if r["dataset"] == ds and r["system"] == s]) for s in systems}
        results[ds]["_trivial"] = trivial_baselines([r for r in dicts if r["dataset"] == ds and r["system"] == systems[0]])
    splits: dict[str, list[str]] = {}
    for ds, info in ds_info.items():
        splits.setdefault(info.get("split", "dev"), []).append(ds)
    overall = {}
    for split, names in splits.items():
        sel = [r for r in dicts if r["dataset"] in names]
        overall[split] = {"datasets": names, **{s: summarize_rows([r for r in sel if r["system"] == s]) for s in systems}, "by_difficulty": by_difficulty(sel),
                          "llm_only_with_tau": summarize_rows([r for r in sel if r["system"] == "llm_only"], "predicted_tau") if "llm_only" in systems else None,
                          "_trivial": trivial_baselines([r for r in sel if r["system"] == systems[0]])}
    failed = [r for r in dicts if r["error"]]
    return {
        "kind": "comparison", "run_kind": kind, "generated_at": datetime.now(timezone.utc).isoformat(), "aborted": aborted,
        "run_kind_note": ("대역(통제된 응답)으로 돌린 결과 — 하네스가 맞게 움직이는지 보는 용도이며 모델 품질이 아니다" if kind == "controlled"
                          else "실제 모델로 돌린 결과 — 이 설정·이 데이터에서 관측된 값이며 한 번의 측정이다"),
        "settings": {"mode": settings.mode.value, "openai_model": settings.openai_model, "anthropic_model": settings.anthropic_model, "threshold": settings.auto_decision_confidence,
                     "reviewer_prompt_version": PROMPT_VERSION, "llm_only_prompt_version": LLM_ONLY_PROMPT_VERSION, "policy_version": catalog.version, "cost_cap_usd": cost_cap_usd},
        "systems": {s: {"label": SYSTEM_LABEL[s], "info_given": INFO_GIVEN[s], **({"controlled_note": CONTROLLED_NOTES[s]} if kind == "controlled" else {})} for s in systems},
        "datasets": ds_info, "results": results, "overall_by_split": overall,
        "failures": {"n": len(failed), "cases": [f"{r['dataset']}:{r['case_id']}:{r['system']}" for r in failed], "by_error": _tally(r["error"].split(" ")[0] for r in failed)},
        "examples": pick_examples(dicts), "label_changes": label_changes(list(ds_info)), "g069_effects": separate_g069_effects(),
        "usage": {s: {"calls": sum(r["calls"] for r in dicts if r["system"] == s), "attempts": sum(r["attempts"] for r in dicts if r["system"] == s),
                      "cost_usd": round(sum(r["cost_usd"] for r in dicts if r["system"] == s), 6), "failed_requests": sum(r["failed_requests"] for r in dicts if r["system"] == s)} for s in systems},
        "rows": dicts,
    }


def _tally(items) -> dict:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="코드 검사만 · 검토 모델만 · 합친 방식 비교")
    ap.add_argument("--datasets", default="golden,holdout,contrast_g069,conditions")
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--cost-cap", type=float, default=None, help="누적 비용 상한(USD). 승인된 예산을 넘기지 않게 중단한다")
    ap.add_argument("--out", default=None, help="저장할 리포트 파일 이름 (기본: compare_<controlled|live>.json)")
    ap.add_argument("--estimate", action="store_true", help="호출 수와 예상 비용만 계산하고 끝낸다 (호출 없음)")
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    names = [d.strip() for d in args.datasets.split(",") if d.strip()]
    systems = tuple(s.strip() for s in args.systems.split(",") if s.strip())
    if args.estimate:
        print(json.dumps(estimate(names, systems, args.limit), ensure_ascii=False, indent=1))
        return 0
    def progress(ds: str, system: str, i: int, n: int) -> None:
        if system == systems[-1] and (i % 10 == 0 or i == n):
            print(f"  {ds} {i}/{n}", file=sys.stderr)

    rep = run_compare(names, settings, systems=systems, limit=args.limit, cost_cap_usd=args.cost_cap, progress=progress)
    save_report(rep, "compare_latest.json")
    print_summary(rep)
    ok, why = compare_allowed(rep)
    if not ok:
        print(f"결과를 저장하지 않음 — {why}")
        return 2
    out = args.out or f"compare_{rep['run_kind']}.json"
    save_report(rep, out)
    print(f"saved evals/reports/{out}")
    return 0


def estimate(datasets: list[str], systems: tuple[str, ...], limit: Optional[int] = None) -> dict:
    """호출 수·비용 추정 (호출하지 않는다). 사례당 비용은 5차 실제 모델 실행(저장된 baseline_partial.json)의 사례당 평균에서 가져온다."""
    n = sum(min(len(load_dataset(d)), limit) if limit else len(load_dataset(d)) for d in datasets)
    ref = load_report("baseline_partial.json")
    per_case_combined = (ref["summary"]["cost_usd"] / ref["n"]) if ref and ref.get("n") else None
    calls = {"code_only": 0, "llm_only": n, "combined": int(round(n * 1.3))}
    cost = {"code_only": 0.0, "llm_only": (per_case_combined * 0.6 * n) if per_case_combined is not None else None,
            "combined": (per_case_combined * n) if per_case_combined is not None else None}
    chosen = {s: {"calls": calls[s], "cost_usd": cost[s]} for s in systems}
    total = sum(v["cost_usd"] for v in chosen.values() if v["cost_usd"] is not None)
    return {"cases": n, "datasets": datasets, "systems": chosen, "total_calls": sum(v["calls"] for v in chosen.values()), "total_cost_usd": round(total, 2),
            "basis": "사례당 평균 비용은 5차 실제 모델 실행(골든셋 55건, 합친 방식) 기준 — llm_only 는 호출 1회라 그 0.6배로 가정. 실제 값은 달라질 수 있다"}


def print_summary(rep: dict) -> None:
    print(f"== 방식 비교 | {rep['run_kind']} | {rep['run_kind_note']}")
    for split, o in rep["overall_by_split"].items():
        print(f"-- {split}: {', '.join(o['datasets'])}")
        for s in rep["systems"]:
            m = o[s]
            print(f"   {SYSTEM_LABEL[s]:18s} 잘못 통과 {m['unsafe_pass']['k']}/{m['unsafe_pass']['n']} · 올바른 통과 {m['correct_pass']['k']}/{m['correct_pass']['n']} · "
                  f"불필요한 수정 요청 {m['unnecessary_revision']['k']}/{m['unnecessary_revision']['n']} · 담당자 확인 {m['human_review']['k']}/{m['human_review']['n']} · "
                  f"호출 {m['calls']} · {m['latency_ms_total']/1000:.1f}초 · ${m['cost_usd']}" + (" 이상" if m["cost_is_lower_bound"] else "") + (f" · 실패 {m['failed']}" if m["failed"] else ""))
    f = rep["failures"]
    if f["n"]:
        print(f"   모델 호출 실패 {f['n']}건: {f['by_error']}")


if __name__ == "__main__":
    sys.exit(main())
