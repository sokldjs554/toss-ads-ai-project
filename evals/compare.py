"""방식 비교 — 같은 고정 사례를 세 방식으로 판정해 놓고 나란히 본다.

  code_only  코드 검사만: 표현 규칙·필수 안내 문구·상품 정보 일치(숫자·조건)·형식·중복. 모델 호출 없음. 코드 검사 결과를 그대로 판정으로 쓴다
             (finding 이 없으면 통과). 실제 서비스는 코드 혼자 통과를 확정하지 않는다 — 그 운영 방식은 `baseline_deterministic_only.json` 에 따로 있다.
  llm_only   검토 모델만: 같은 규칙 카탈로그·같은 출력 스키마를 받지만 코드 검사 결과를 받지 않고, 모델이 돌려준 판정을 **코드의 보정 없이** 그대로 쓴다
             (인용 검증·코드 확정 계층 보호·공통 판정 함수·담당자 확인 경로 없음). 확신 기준(τ)을 얹은 파생 판정은 같은 응답으로 따로 계산한다.
  combined   지금 서비스가 쓰는 방식: 코드 검사 → 검토 모델(코드 결과를 받음) → 인용 검증 → 공통 판정 함수(τ) → 수정안 재검사.

지표는 '통과 후보를 사람에게 넘긴 것' 과 '올바르게 통과시킨 것' 을 섞지 않는다. 담당자 확인은 따로 세고, 전부 담당자 확인으로 넘기는 방식(위험 통과 0)은
올바른 통과가 0 이라는 점을 trivial 기준으로 나란히 적는다. 대역(통제된 응답)으로 돌린 결과는 하네스 검증용이며 모델 품질이 아니다 (run_kind).
실제 모델 호출은 승인된 범위에서만 한다 — 승인된 네 데이터(conditions_extra 는 승인 밖)만, `--cost-cap` 으로 승인 예산을 넘기지 않게 막고, 상한에 닿으면 즉시 멈춘다 (FM-24).

저장 규칙 — 돈을 쓴 측정은 버리지 않되 완료된 결과와 섞지 않는다:
  compare_live.json              끝까지 마쳤고 · 상한 안이고 · 호출 실패가 없고 · 기준 제공자 하나가 전부 판정한 실제 모델 결과만 (run_kind live)
  compare_live_incomplete.json   그 밖의 모든 실행(상한 도달·초과, 호출 실패, 다른 제공자 혼입, 연속 실패 중단, 일부 범위만 실행)이 측정한 행까지 (run_kind live_incomplete, complete false,
                                 incomplete_reason 목록). 세 방식 비교의 결과가 아니다. 종료 코드는 2
  compare_controlled.json        대역(통제된 응답)으로 돌린 하네스 검증용 (run_kind controlled). 실제 결과 파일로는 절대 저장하지 않는다

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
from evals import runner as _runner
from evals.metrics import UNSAFE_LABELS, wilson
from evals.runner import (REPORTS, EvalAborted, EvalCase, _wait_for_breaker, dataset_fingerprint, dataset_path, evaluate_with_retry, load_dataset, load_report, save_report)
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

APPROVED_DATASETS = ("golden", "holdout", "contrast_g069", "conditions")   # 실제 모델 비교에 승인된 네 데이터 (110건). conditions_extra(99건)는 승인 밖이다
APPROVED_COST_CAP_USD = 3.5                                                # 승인된 누적 비용 상한 (USD)
LIVE_NAME, CONTROLLED_NAME, INCOMPLETE_NAME = "compare_live.json", "compare_controlled.json", "compare_live_incomplete.json"

# 각 방식이 받는 정보 — 비교가 공정한지 읽는 사람이 직접 판단할 수 있게 적어 둔다.
INFO_GIVEN = {
    "code_only": {
        "광고 문구·안내 문구": "예", "광고가 가리킨 근거(주장→근거 연결)": "예 — 코드가 주장이 가리킨 사실을 확인", "상품 정보(사실 목록)": "예 — 코드가 숫자·조건을 대조", "랜딩 페이지 문구": "예 — 코드가 숫자·표현을 대조",
        "검사 기준(규칙)": "예 — 코드로 구현된 규칙(어휘·고지 체크리스트·숫자/조건 대조·형식)", "검토 모델": "아니오", "수정안 제안·재검사": "아니오", "코드 보정·판정 함수": "코드 검사 결과를 그대로 판정으로 사용",
    },
    "llm_only": {
        "광고 문구·안내 문구": "예", "광고가 가리킨 근거(주장→근거 연결)": "예 — 합친 방식과 같은 줄을 프롬프트에 포함", "상품 정보(사실 목록)": "예 — 프롬프트에 텍스트로 제공", "랜딩 페이지 문구": "예 — 프롬프트에 앞 1,200자",
        "검사 기준(규칙)": f"예 — 같은 규칙 카탈로그를 프롬프트로 제공 (비교 전용 프롬프트 {LLM_ONLY_PROMPT_VERSION}; 규칙 설명에서 ‘코드가 ~한다’ 는 문장은 뺌)", "검토 모델": "예",
        "수정안 제안·재검사": "아니오 — 수정안을 만들지 않는다. 그래서 호출 수·시간·비용이 합친 방식보다 작은 것은 방식의 효율이 아니라 단계가 적어서다",
        "코드 보정·판정 함수": "없음 — 모델이 돌려준 판정을 그대로 사용 (인용 검증·확정 계층 보호·담당자 확인 경로 없음)",
    },
    "combined": {
        "광고 문구·안내 문구": "예", "광고가 가리킨 근거(주장→근거 연결)": "예", "상품 정보(사실 목록)": "예 — 코드 대조 + 프롬프트", "랜딩 페이지 문구": "예",
        "검사 기준(규칙)": f"예 — 코드 규칙 + 같은 카탈로그를 프롬프트로 (검토 프롬프트 {PROMPT_VERSION})", "검토 모델": "예 — 코드 검사 결과를 받음",
        "수정안 제안·재검사": "예 — 수정 제안과 그 의미 재검사까지 호출한다 (호출 수·시간·비용에 포함)",
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
    served_by: str = ""             # 모델 판정을 낸 제공자. 형식 수리를 다른 제공자가 했다면 수리 전 텍스트를 만든(판단한) 쪽 — 기준 제공자가 아니면 그 모델의 측정이 아니다
    judged_by: dict = field(default_factory=dict)      # 이 행이 받은 모델 응답 전부의 판단 제공자별 수 (수정안 재검사 호출 포함)
    repaired_by: dict = field(default_factory=dict)    # 형식 수리를 판단과 다른 제공자가 한 응답의 수리 제공자별 수 (판단은 judged_by 쪽)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ───────────────────────────── 누가 판정했는가 ─────────────────────────────
def stamped_provider(settings: Settings) -> str:
    """이 실행이 '측정하려는' 기준 제공자 (평가 실행기의 리포트 provider 와 같은 규칙). 다른 제공자가 판정한 행은 그 모델의 측정이 아니다."""
    return "mock" if settings.mode.value == "demo" else ("openai" if settings.has_openai else "anthropic")


def stamped_model(settings: Settings) -> str:
    return "mock" if settings.mode.value == "demo" else (settings.openai_model if settings.has_openai else settings.anthropic_model)


def _judge_of(resp) -> str:
    """응답의 판단 제공자. 형식 수리를 다른 제공자가 했다면 수리 전 텍스트를 만든 쪽(origin_provider)이 판단한 것이고 provider 는 수리한 쪽이다."""
    return resp.origin_provider or resp.provider


def _fill_attribution(row: Row, seen: list[tuple[str, str, str]], fallback: str = "") -> None:
    """seen: (요청 이름, 판단 제공자, 수리 제공자) 목록. 수정안 재검사 호출(':rewrite')은 행의 주 판정이 아니므로 served_by 에서 뺀다."""
    row.judged_by = _tally(j for _, j, _ in seen)
    row.repaired_by = _tally(r for _, j, r in seen if r and r != j)
    main = [j for label, j, _ in seen if not label.endswith(":rewrite")]
    row.served_by = main[-1] if main else (seen[-1][1] if seen else fallback)


class _Attribution:
    """한 행이 받은 모델 응답을 router.complete 에서 지켜본다 — 평가 실행기 안쪽은 건드리지 않고, 어느 제공자의 판단인지(수리 경로 포함)만 모은다."""

    def __init__(self, router: Router):
        self.router, self.seen = router, []
        self._own = None

    def __enter__(self) -> "_Attribution":
        self._own = self.router.__dict__.get("complete")
        real = self.router.complete

        def recording(req):
            resp = real(req)
            self.seen.append((getattr(req, "label", "") or "", _judge_of(resp), resp.provider if getattr(resp, "repaired", False) else ""))
            return resp

        self.router.complete = recording
        return self

    def __exit__(self, *exc) -> bool:
        if self._own is None:
            self.router.__dict__.pop("complete", None)
        else:
            self.router.complete = self._own
        return False


# ───────────────────────────── 방식별 판정 ─────────────────────────────
def run_code_only(case: EvalCase, catalog: PolicyCatalog) -> Row:
    copy, product = case.to_copy(), case.to_product()
    t0 = time.perf_counter()
    cats = catalog.categories_for(product, copy.full_text())
    det = output_guard.check(copy, product, cats, case.landing_text, [], catalog)
    rules = sorted({f.rule_id for f in det.findings if f.severity.value != "warn"})
    return Row(dataset="", system="code_only", case_id=case.id, label=case.label, difficulty=case.difficulty, predicted=det.decision.value, confidence=1.0 if det.findings else 0.0,
               rules=rules, reason="코드 검사 결과 그대로" + (f" ({', '.join(rules)})" if rules else " (finding 없음 → 통과)"), latency_ms=round((time.perf_counter() - t0) * 1000, 2))


def _llm_only_user(copy: AdCopy, facts: str, landing_text: str) -> str:
    # 합친 방식의 검토 프롬프트(agents/reviewer._user_prompt)와 같은 입력에서 '코드 검사 결과' 블록만 뺀다 — 주장→근거 연결은 같이 준다
    return (f"[소재]\n헤드라인: {copy.headline}\n본문: {copy.body}\nCTA: {copy.cta}\n고지: {' / '.join(copy.disclosures) or '(없음)'}\n"
            f"주장→근거: {[(c.text, c.fact_id) for c in copy.claims]}\n\n[광고주 사실 목록]\n{facts}\n\n"
            + (f"[랜딩 페이지 텍스트(발췌)]\n{landing_text[:1200]}\n" if landing_text else ""))


def run_llm_only(case: EvalCase, settings: Settings, router: Router, catalog: PolicyCatalog, breaker_wait_s: float = 0.0) -> Row:
    copy, product = case.to_copy(), case.to_product()
    cats = catalog.categories_for(product, copy.full_text())
    facts = "\n".join(f"- {f.id}: {f.text}" for f in product.facts) or "- (없음)"
    rule_ids = [r.id for r in catalog.rules_for(cats)]
    req = LLMRequest(task=Task.REVIEW, system=llm_only_system(catalog, cats), user=_llm_only_user(copy, facts, case.landing_text),
                     schema=build_review_schema(rule_ids), max_tokens=1800, temperature=0.0, context={"rule_ids": rule_ids, "copy": copy.model_dump(mode="json"), "required_disclosures": []},
                     label=f"compare_llm_only:{case.id}")
    b = router.budget
    before = (b.spent_usd, b.calls, b.attempts, b.failed_attempts)
    t0 = time.perf_counter()
    row = Row(dataset="", system="llm_only", case_id=case.id, label=case.label, difficulty=case.difficulty, predicted="")
    try:
        try:
            resp = router.complete(req)
        except Exception:  # noqa: BLE001 — 일시 장애는 서킷이 닫힐 때까지 기다렸다가 한 번 더 (평가 실행기와 같은 규칙). 첫 실패의 요청 수·비용은 위 집계에 그대로 남는다
            if breaker_wait_s <= 0:
                raise
            _wait_for_breaker(router, breaker_wait_s)
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
        _fill_attribution(row, [(req.label, _judge_of(resp), resp.provider if getattr(resp, "repaired", False) else "")])
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
    with _Attribution(router) as att:
        r = evaluate_with_retry(case, settings, router, catalog, use_llm=True, breaker_wait_s=breaker_wait_s)
    err = r.error or r.rewrite_error
    row = Row(dataset="", system="combined", case_id=case.id, label=case.label, difficulty=case.difficulty, predicted=("ERROR" if err else r.predicted), confidence=r.confidence,
              rules=list(r.predicted_rules), reason=r.reason[:200], latency_ms=r.latency_ms, cost_usd=r.cost_usd, error=err, model_decision=r.model_decision,
              failed_requests=r.failed_requests)
    row.calls, row.attempts = b.calls - before[1], b.attempts - before[2]
    _fill_attribution(row, att.seen, r.served_by)
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


# 비교 결과를 읽을 때 알아야 하는 비대칭 — 리포트·화면·문서에 그대로 싣는다
CAVEATS = [
    "라벨은 ‘코드가 내야 할 판정’ 이고 규칙과 라벨을 같은 저자가 썼다. 그래서 코드 검사에 유리하고, 개발용 사례의 코드 검사 수치는 일반화의 증거가 아니다.",
    "검토 모델만 방식은 수정안을 만들지 않는다 — 호출 수·시간·비용이 작은 것은 단계가 적어서이지 효율의 증거가 아니다.",
    "비교 전용 프롬프트(compare-1)는 검토 프롬프트만큼 다듬지 않았다 — 검토 모델만 방식이 낮게 나오면 모델의 한계가 아니라 프롬프트 차이일 수 있다.",
    "호출 실패는 통과도 담당자 확인도 아니라 실패로 센다. 한 번의 측정이다.",
]


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
    """데이터의 라벨 변경 이력(변경 전 값·이유). 라벨을 바꾼 사례는 점수를 바꾸므로 따로 보여 준다.
    label_changed = 이 사례의 라벨(APPROVE/REVISE/REJECT) 값이 실제로 바뀌었는가. 기대 규칙이나 사례 분류만 바뀐 사례(G-081)는 False — 점수를 바꾸는 라벨 변경이 아니다."""
    out = []
    for name in names:
        if not dataset_path(name).exists():
            continue
        for c in load_dataset(name):
            labels = {h.get("label") for h in c.label_history} | {c.label}
            for h in c.label_history:
                out.append({"dataset": name, "case_id": c.id, "current_label": c.label, "label_version": h.get("version"), "label_then": h.get("label"), "reason_then": h.get("reason"),
                            "rules_then": h.get("rule_ids", []), "label_changed": len(labels) > 1})
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


# ───────────────────────────── 완료 여부 ─────────────────────────────
def unapproved_datasets(names: list[str]) -> list[str]:
    """실제 모델 비교에 승인되지 않은 데이터 이름 (conditions_extra 등)."""
    return [n for n in names if n not in APPROVED_DATASETS]


def incomplete_reasons(rep: dict) -> list[str]:
    """이 결과가 '끝까지 마친 실제 모델 결과' 가 아닌 이유 코드 목록 (비어 있으면 완료). 리포트의 필드에서 직접 읽으므로 라벨이 잘못 붙은 파일도 걸러진다.
    cost_cap_reached 상한에 닿아 남은 호출을 하지 않음 · cost_over_cap 모두 마쳤지만 누적 비용이 상한 초과 · consecutive_failures 호출 실패가 연속되어 중단 ·
    consecutive_fallback 기준 제공자가 아닌 쪽이 연속으로 판정해 중단 · harness_error 하네스 자체의 오류로 중단 · failed_rows 호출이 실패한 행이 있음 · mixed_provider 기준 제공자가 아닌 쪽이 판정한 행이 있음 ·
    partial_scope 승인된 전체 범위(네 데이터 전부·세 방식·사례 수 제한 없음)가 아님 · cases_not_run 실행하지 못한 사례가 남음"""
    out: list[str] = []
    ab = rep.get("aborted") or {}
    if ab:
        out.append({"cost_cap": "cost_cap_reached", "consecutive_failures": "consecutive_failures", "consecutive_fallback": "consecutive_fallback", "harness_error": "harness_error"}.get(ab.get("kind"), "aborted"))
    if (rep.get("cost") or {}).get("over_cap") and not ab:
        out.append("cost_over_cap")
    if (rep.get("failures") or {}).get("n"):
        out.append("failed_rows")
    if (rep.get("mixed_provider") or {}).get("n"):
        out.append("mixed_provider")
    sc = rep.get("scope") or {}
    if sc.get("required") and not sc.get("full_approved_scope"):
        out.append("partial_scope")
    if not ab and any(c.get("completed", 0) < c.get("total", 0) for c in (rep.get("case_counts") or {}).values()):
        out.append("cases_not_run")
    return out


def _incomplete_note(rep: dict, reasons: Optional[list[str]] = None) -> str:
    """사람이 읽는 한 문장 — 왜 완료된 결과가 아닌지, 무엇이 담겼는지."""
    reasons = incomplete_reasons(rep) if reasons is None else reasons
    if not reasons:
        return ""
    ab, cost = rep.get("aborted") or {}, rep.get("cost") or {}
    cap = cost.get("cap_usd") if cost.get("cap_usd") is not None else (rep.get("settings") or {}).get("cost_cap_usd")
    where = ab.get("after_case", "")
    stamped = rep.get("provider", "?")
    parts = []
    for code in reasons:
        if code == "cost_cap_reached":
            parts.append(f"누적 비용이 상한(${(cap or 0):.2f})에 닿아 {where} 에서 멈췄다")
        elif code == "cost_over_cap":
            parts.append(f"모든 사례를 마쳤지만 누적 비용 ${cost.get('spent_usd', 0):.2f} 가 상한 ${(cap or 0):.2f} 를 넘었다")
        elif code == "consecutive_failures":
            parts.append(f"모델 호출 실패가 연속으로 이어져 {where} 에서 멈췄다")
        elif code == "consecutive_fallback":
            parts.append(f"기준 제공자({stamped}) 대신 다른 제공자가 연속으로 판정해 {where} 에서 멈췄다")
        elif code == "harness_error":
            parts.append(f"하네스 오류로 {where} 에서 멈췄다 ({ab.get('reason', '')})")
        elif code == "failed_rows":
            parts.append(f"모델 호출이 실패한 행이 {(rep.get('failures') or {}).get('n', 0)}건 있다")
        elif code == "mixed_provider":
            m = rep.get("mixed_provider") or {}
            parts.append(f"기준 제공자({stamped}) 대신 다른 제공자가 판정한 행이 {m.get('n', 0)}건 있다 {m.get('by_provider', {})}")
        elif code == "partial_scope":
            parts.append("승인된 전체 범위(네 데이터 전부·세 방식·사례 수 제한 없음)를 실행한 것이 아니다")
        elif code == "cases_not_run":
            parts.append("실행하지 못한 사례가 남았다")
        else:
            parts.append(f"비교가 중단됨 ({ab.get('reason', '')})")
    counts = rep.get("case_counts") or {}
    total, done = sum(c.get("total", 0) for c in counts.values()), sum(c.get("completed", 0) for c in counts.values())
    scope = f"측정한 사례({done}/{total}건)만의 수치이며" if counts else "측정한 사례만의 수치이며"
    return f"완료되지 않은 실행 — {'; '.join(parts)}. {scope} 세 방식 비교의 결과가 아닙니다."


def compare_allowed(report: dict) -> tuple[bool, str]:
    """compare_live.json 으로 저장해도 되는 결과인가: 끝까지 마쳤고, 비용 상한 안이고, 호출 실패와 다른 제공자가 섞이지 않았다. 아니면 이유 문장을 돌려준다."""
    reasons = incomplete_reasons(report)
    return (True, "") if not reasons else (False, _incomplete_note(report, reasons))


# ───────────────────────────── 실행 ─────────────────────────────
def run_compare(datasets: list[str], settings: Settings, router: Optional[Router] = None, catalog: Optional[PolicyCatalog] = None, systems: tuple[str, ...] = SYSTEMS,
                limit: Optional[int] = None, cost_cap_usd: Optional[float] = None, progress: Optional[Callable[[str, str, int, int], None]] = None,
                run_kind: Optional[str] = None, breaker_wait_s: float = 0.0, abort_after: int = 0, approved_scope: bool = False) -> dict:
    """breaker_wait_s: 호출이 실패하면 제공자 서킷이 닫힐 때까지(최대) 기다렸다가 같은 사례를 한 번 더 시도한다. abort_after: 호출 실패 행(또는 기준 제공자가 아닌 쪽이 판정한 행)이
    이 수만큼 연속되면 중단한다 (FM-24). 비용 상한(cost_cap_usd)에 닿으면 그 행에서 바로 멈춘다 — 남은 호출은 하지 않는다.
    approved_scope: 이 실행이 '승인된 전체 범위' 여야 하는가 (워크플로·CLI 실행). 실제 모델 실행이 그 범위를 채우지 못하면 완료된 결과로 세지 않는다.
    어떤 경우에도 측정한 행은 리포트에 남는다 — 완료 여부는 리포트의 complete·incomplete_reason 이 말한다 (저장 위치는 main 이 정한다)."""
    catalog = catalog or get_catalog()
    needs_model = any(s in systems for s in ("llm_only", "combined"))
    kind = run_kind or ("controlled" if settings.mode.value == "demo" else "live")
    if needs_model and (kind == "live" or settings.mode.value != "demo"):
        bad = unapproved_datasets(datasets)
        if bad:   # 어떤 호출도 하기 전에 막는다 — 승인 밖의 데이터는 실제 모델 비교에 들어가지 않는다
            raise ValueError(f"승인되지 않은 데이터는 실제 모델 비교에 쓸 수 없다: {', '.join(bad)} (승인된 범위: {', '.join(APPROVED_DATASETS)})")
    if needs_model:
        router = router or Router(settings, tracer=Tracer())
        router.budget.cap_usd = cost_cap_usd if cost_cap_usd is not None else max(router.budget.cap_usd, 5.0)
        router.budget.max_calls = 10_000
    prov = json.loads((DATASETS_DIR / "PROVENANCE.json").read_text(encoding="utf-8"))
    plan: list[tuple[str, list[EvalCase]]] = []
    ds_info: dict[str, dict] = {}
    for ds in datasets:   # 모두 먼저 읽는다 — 데이터 이름이 틀렸다면 돈을 쓰기 전에 알게 되고, 중단되더라도 데이터별 전체 건수가 리포트에 남는다
        all_cases = load_dataset(ds)
        cases = all_cases[:limit] if limit else all_cases
        ds_info[ds] = {"n": len(cases), "n_total": len(all_cases), **dataset_fingerprint(ds), **{k: v for k, v in prov.get(ds, {}).items()}}
        plan.append((ds, cases))
    total_rows = sum(len(c) for _, c in plan) * len(systems)
    stamped = stamped_provider(settings)
    rows: list[Row] = []
    aborted = None
    consecutive = foreign = 0
    where = ""
    try:
        for ds, cases in plan:
            for i, case in enumerate(cases, start=1):
                for system in systems:
                    where = f"{ds}:{case.id}"
                    try:
                        if system == "code_only":
                            row = run_code_only(case, catalog)
                        elif system == "llm_only":
                            row = run_llm_only(case, settings, router, catalog, breaker_wait_s)
                        else:
                            row = run_combined(case, settings, router, catalog, breaker_wait_s)
                    except Exception as e:  # noqa: BLE001 — 하네스 쪽 오류도 이미 쓴 비용을 버릴 이유가 아니다: 이 행만 실패로 남기고 계속한다
                        row = Row(dataset="", system=system, case_id=case.id, label=case.label, difficulty=case.difficulty, predicted="ERROR", error=f"하네스 오류 {type(e).__name__}: {str(e)[:200]}")
                    row.dataset = ds
                    rows.append(row)
                    if system != "code_only":
                        consecutive = consecutive + 1 if row.error else 0
                        if abort_after and consecutive >= abort_after:
                            aborted = {"kind": "consecutive_failures", "reason": f"모델 호출 실패가 {consecutive}행 연속 ({row.error[:80]})", "after_case": where}
                            raise EvalAborted(aborted["reason"], {})
                        if row.judged_by and any(j != stamped for j in row.judged_by):
                            foreign += 1
                        elif row.judged_by:
                            foreign = 0
                        if abort_after and foreign >= abort_after:
                            aborted = {"kind": "consecutive_fallback", "reason": f"기준 제공자({stamped}) 대신 다른 제공자가 {foreign}행 연속 판정 ({', '.join(sorted(row.judged_by))})", "after_case": where}
                            raise EvalAborted(aborted["reason"], {})
                    if progress:
                        progress(ds, system, i, len(cases))
                    if cost_cap_usd is not None and router is not None and len(rows) < total_rows and router.budget.spent_usd >= cost_cap_usd:
                        # 상한에 닿았다 — 다음 호출은 어차피 거절되므로 여기서 멈춘다. 마친 행은 버리지 않고 리포트에 남긴다
                        aborted = {"kind": "cost_cap", "reason": f"누적 비용 ${router.budget.spent_usd:.2f} 가 상한 ${cost_cap_usd:.2f} 에 닿음", "after_case": where}
                        raise EvalAborted(aborted["reason"], {})
    except EvalAborted:
        pass
    except Exception as e:  # noqa: BLE001 — 반복문 자체의 오류(진행 표시 등)가 나도 지금까지 측정한 행은 리포트로 남긴다
        aborted = {"kind": "harness_error", "reason": f"{type(e).__name__}: {str(e)[:200]}", "after_case": where}
    spent = round(router.budget.spent_usd, 6) if router is not None and needs_model else None
    return build_report(rows, ds_info, settings, catalog, systems, kind, aborted, cost_cap_usd, spent_usd=spent, approved_scope=approved_scope, limit=limit)


def build_report(rows: list[Row], ds_info: dict, settings: Settings, catalog: PolicyCatalog, systems: tuple[str, ...], kind: str, aborted: Optional[dict] = None,
                 cost_cap_usd: Optional[float] = None, *, spent_usd: Optional[float] = None, approved_scope: bool = False, limit: Optional[int] = None) -> dict:
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
    stamped = stamped_provider(settings)
    # 누가 판정했는가 — 기준 제공자가 아닌 쪽(대체 경로·수리 전 판단)이 한 행이 있으면 그 모델의 측정이 아니다 (평가 실행기의 served_by 와 같은 규칙)
    served = _tally(p for r in dicts for p, k in (r.get("judged_by") or {}).items() for _ in range(k))
    mixed_rows = [r for r in dicts if any(p != stamped for p in (r.get("judged_by") or {}))]
    mixed = {"n": len(mixed_rows), "cases": [f"{r['dataset']}:{r['case_id']}:{r['system']}" for r in mixed_rows],
             "by_provider": {p: k for p, k in served.items() if p != stamped}}
    counts: dict[str, dict] = {}
    for ds, info in ds_info.items():
        seen: dict[str, set] = {}
        for r in dicts:
            if r["dataset"] == ds:
                seen.setdefault(r["case_id"], set()).add(r["system"])
        counts[ds] = {"total": info.get("n", 0), "completed": sum(1 for v in seen.values() if v >= set(systems)),
                      "failed_rows": sum(1 for r in dicts if r["dataset"] == ds and r["error"])}
    full = set(ds_info) == set(APPROVED_DATASETS) and set(systems) == set(SYSTEMS) and not limit and all(i.get("n") == i.get("n_total", i.get("n")) for i in ds_info.values())
    spent = round(sum(r["cost_usd"] for r in dicts), 6) if spent_usd is None else spent_usd
    rep = {
        "kind": "comparison", "run_kind": kind, "generated_at": datetime.now(timezone.utc).isoformat(), "aborted": aborted,
        "provider": stamped, "model": stamped_model(settings),
        "run_kind_note": ("대역(통제된 응답)으로 돌린 결과 — 하네스가 맞게 움직이는지 보는 용도이며 모델 품질이 아니다" if kind == "controlled"
                          else "실제 모델로 돌린 결과 — 이 설정·이 데이터에서 관측된 값이며 한 번의 측정이다"),
        "settings": {"mode": settings.mode.value, "openai_model": settings.openai_model, "anthropic_model": settings.anthropic_model, "threshold": settings.auto_decision_confidence,
                     "reviewer_prompt_version": PROMPT_VERSION, "llm_only_prompt_version": LLM_ONLY_PROMPT_VERSION, "policy_version": catalog.version, "cost_cap_usd": cost_cap_usd},
        "scope": {"datasets": list(ds_info), "systems": list(systems), "limit": limit, "full_approved_scope": full, "required": bool(approved_scope and kind == "live")},
        "cost": {"spent_usd": spent, "cap_usd": cost_cap_usd, "over_cap": bool(cost_cap_usd is not None and spent > cost_cap_usd + 1e-9)},
        "caveats": CAVEATS,
        "systems": {s: {"label": SYSTEM_LABEL[s], "info_given": INFO_GIVEN[s], **({"controlled_note": CONTROLLED_NOTES[s]} if kind == "controlled" else {})} for s in systems},
        "datasets": ds_info, "case_counts": counts, "results": results, "overall_by_split": overall,
        "failures": {"n": len(failed), "cases": [f"{r['dataset']}:{r['case_id']}:{r['system']}" for r in failed], "by_error": _tally(r["error"].split(" ")[0] for r in failed)},
        "served_by": served, "repaired_by": _tally(p for r in dicts for p, k in (r.get("repaired_by") or {}).items() for _ in range(k)), "mixed_provider": mixed,
        "examples": pick_examples(dicts), "label_changes": label_changes(list(ds_info)), "g069_effects": separate_g069_effects(),
        "usage": {s: {"calls": sum(r["calls"] for r in dicts if r["system"] == s), "attempts": sum(r["attempts"] for r in dicts if r["system"] == s),
                      "cost_usd": round(sum(r["cost_usd"] for r in dicts if r["system"] == s), 6), "failed_requests": sum(r["failed_requests"] for r in dicts if r["system"] == s),
                      "judged_by": _tally(p for r in dicts if r["system"] == s for p, k in (r.get("judged_by") or {}).items() for _ in range(k))} for s in systems},
        "rows": dicts,
    }
    reasons = incomplete_reasons(rep)
    rep["complete"] = not reasons
    rep["incomplete_reason"] = reasons
    rep["incomplete_note"] = _incomplete_note(rep, reasons)
    if reasons and kind == "live":
        # 끝까지 마치지 못한 실제 모델 실행은 'live' 가 아니다 — 측정한 행은 남기되 세 방식 비교의 결과로 읽히지 않게 이름부터 다르게 붙인다
        rep["run_kind"] = "live_incomplete"
        rep["run_kind_note"] = "실제 모델로 돌렸지만 끝까지 마치지 못했거나 장애·다른 제공자가 섞인 실행 — 측정한 사례만의 수치이며 세 방식 비교의 결과가 아니다"
    return rep


def _tally(items) -> dict:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


# ───────────────────────────── 저장 ─────────────────────────────
def _result_file(kind: str, out: Optional[str]) -> tuple[str, str]:
    """완료된 결과를 저장할 파일 이름과 (바꿨다면) 그 사정. 실제 결과와 대역 결과는 서로의 이름으로 저장하지 않고, 완료되지 못한 실행의 파일을 완료된 결과 이름으로 쓰지 않는다."""
    default = LIVE_NAME if kind == "live" else CONTROLLED_NAME
    if not out:
        return default, ""
    name = Path(out).name
    taken = {LIVE_NAME, INCOMPLETE_NAME} if kind != "live" else {CONTROLLED_NAME, INCOMPLETE_NAME}
    if name in taken:
        return default, f"--out {name} 는 이 결과({kind})의 파일이 아니라서 {default} 로 저장한다"
    return name, ""


def _keep_previous(old: bytes, name: str, directory: Path) -> None:
    """같은 이름의 이전 결과를 history/ 에 한 부 남긴다 — 돈을 쓴 측정을 덮어써서 잃지 않게. 보관이 실패해도 새 결과의 저장은 막지 않는다."""
    try:
        stamp = str(json.loads(old).get("generated_at", ""))[:19]
        stamp = "".join(ch for ch in stamp if ch.isdigit() or ch == "T").replace("T", "_") or "prev"
        dest = directory / "history" / f"{Path(name).stem}_{stamp}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(old)
    except Exception:  # noqa: BLE001
        pass


def persist(rep: dict, name: str) -> Path:
    """리포트를 저장한다. 돈을 쓴 결과 이름(compare_live·compare_live_incomplete)은 덮어쓰기 전에 이전 파일을 history/ 에 남긴다."""
    directory = _runner.REPORTS
    old = (directory / name).read_bytes() if name in (LIVE_NAME, INCOMPLETE_NAME) and (directory / name).exists() else None
    path = save_report(rep, name)
    if old is not None:
        _keep_previous(old, name, directory)
    return path


def _positive_float(text: str) -> float:
    v = float(text)
    if not (v > 0 and v < float("inf")):
        raise argparse.ArgumentTypeError("0 보다 큰 숫자여야 한다")
    return v


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="코드 검사만 · 검토 모델만 · 합친 방식 비교")
    ap.add_argument("--datasets", default=",".join(APPROVED_DATASETS), help="비교에 쓸 데이터 (기본: 실제 모델 비교에 승인된 네 데이터)")
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    ap.add_argument("--limit", type=int, default=None, help="데이터별 사례 수 제한 (실제 모델 실행에서 쓰면 완료된 결과로 세지 않는다)")
    ap.add_argument("--cost-cap", type=_positive_float, default=None, help=f"누적 비용 상한(USD). 상한에 닿으면 즉시 멈춘다. 실제 모델 실행에서 비우면 승인된 ${APPROVED_COST_CAP_USD}")
    ap.add_argument("--out", default=None, help="완료된 결과를 저장할 파일 이름 (기본: compare_<controlled|live>.json). 완료되지 못한 실행은 항상 compare_live_incomplete.json")
    ap.add_argument("--estimate", action="store_true", help="호출 수와 추정 비용 범위만 계산하고 끝낸다 (호출 없음)")
    ap.add_argument("--json", action="store_true", help="--estimate 결과를 JSON 으로 출력")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    names = [d.strip() for d in args.datasets.split(",") if d.strip()]
    systems = tuple(s.strip() for s in args.systems.split(",") if s.strip())
    if args.estimate:
        e = estimate(names, systems, args.limit, args.cost_cap)
        print(json.dumps(e, ensure_ascii=False, indent=1) if args.json else format_estimate(e))
        return 0
    spends = settings.mode.value != "demo" and any(s in systems for s in ("llm_only", "combined"))
    bad = unapproved_datasets(names) if spends else []
    if bad:
        print(f"실행하지 않음 — 승인되지 않은 데이터는 실제 모델 비교에 쓸 수 없다: {', '.join(bad)} (승인된 범위: {', '.join(APPROVED_DATASETS)})")
        return 2
    cap = args.cost_cap if args.cost_cap is not None else (APPROVED_COST_CAP_USD if spends else None)

    def progress(ds: str, system: str, i: int, n: int) -> None:
        if system == systems[-1] and (i % 10 == 0 or i == n):
            print(f"  {ds} {i}/{n}", file=sys.stderr)

    rep = run_compare(names, settings, systems=systems, limit=args.limit, cost_cap_usd=cap, progress=progress,
                      breaker_wait_s=(60.0 if settings.mode.value != "demo" else 0.0), abort_after=3, approved_scope=True)
    ok, why = compare_allowed(rep)
    # 저장이 먼저, 요약 출력은 그 다음 — 출력 중 오류가 나도 돈을 쓴 측정을 잃지 않는다
    if ok:
        out, note = _result_file(rep["run_kind"], args.out)
        persist(rep, out)
        code, tail = 0, f"{note}\n" * bool(note) + f"saved evals/reports/{out}"
    elif rep["run_kind"] == "live_incomplete":
        # 돈을 쓴 측정은 버리지 않는다: 측정한 행까지 compare_live_incomplete.json 에 남기고, compare_live.json 은 만들지도 덮어쓰지도 않는다. 종료 코드 2 = 완료되지 않음
        persist(rep, INCOMPLETE_NAME)
        code, tail = 2, f"{why}\n  측정한 부분: evals/reports/{INCOMPLETE_NAME} (run_kind live_incomplete — 세 방식 비교의 결과가 아님). {LIVE_NAME} 은 만들지 않았다"
    else:
        code, tail = 2, f"결과를 저장하지 않음 — {why}\n  (대역 실행은 완료되지 않은 결과를 실제 결과 파일에 남기지 않는다)"
    try:
        print_summary(rep)
    except Exception as e:  # noqa: BLE001
        print(f"요약 출력 실패 ({type(e).__name__}: {e}) — 결과 파일은 이미 저장됐다")
    print(tail)
    return code


# ───────────────────────────── 호출 수 · 비용 추정 ─────────────────────────────
REF_REPORTS = {"golden": "baseline_partial.json", "holdout": "holdout_partial.json", "contrast_g069": "contrast_g069_partial.json"}   # 5차 실제 모델 실행(저장된 리포트)
INPUT_COST_SHARE = 0.83     # 검토 호출 한 번의 비용에서 입력(프롬프트) 토큰이 차지하는 몫. 저장된 실제 트레이스 19건(입력 평균 약 4,950·출력 약 254 토큰)을 가격표로 환산한 값
LLM_ONLY_PROMPT_RATIO = {"low": 0.8, "central": 0.9, "high": 1.0}     # ‘검토 모델만’ 프롬프트 길이 ÷ 검토 프롬프트 길이 (가정). 현재 프롬프트로 네 데이터를 재면 0.88 — 테스트가 이 가정을 계속 확인한다
PROMPT_GROWTH = {"low": 1.0, "central": 1.06, "high": 1.10}           # 측정 때보다 검토 프롬프트가 길어진 만큼 (시스템 프롬프트 약 +7% × 입력 몫). 같은 버전으로 다시 재면 1
CAP_NEAR = 0.85             # 중간 추정이 상한의 이 비율 이상이면(상한과 15% 이내) 경고


def _calibration() -> Optional[dict]:
    """저장된 실제 모델 리포트에서 데이터별 사례 수·호출 수·비용과 라벨별 ‘수정안 재검사 호출’ 비율을 뽑는다. 호출 수 = 검토 1회 + (수정안이 있고 의미 재검사를 불렀다면 1회)."""
    per_ds: dict[str, dict] = {}
    labels: dict[str, list[int]] = {}     # 라벨 → [재검사 호출이 있었던 사례 수, 사례 수]
    one_call: list[float] = []
    versions = set()
    for ds, name in REF_REPORTS.items():
        rep = load_report(name)
        if not rep or not rep.get("cases") or rep.get("mode") in (None, "demo"):
            continue
        calls = 0
        cost = 0.0
        for c in rep["cases"]:
            i = c.get("inputs") or {}
            extra = 1 if (i.get("has_rewrite") and i.get("rewrite_semantic") is not None) else 0
            calls += 1 + extra
            cost += c.get("cost_usd", 0.0)
            lab = labels.setdefault(c["label"], [0, 0])
            lab[0] += extra
            lab[1] += 1
            if not extra:
                one_call.append(c.get("cost_usd", 0.0))
        per_ds[ds] = {"n": len(rep["cases"]), "calls": calls, "cost": cost, "sha": rep.get("dataset_sha256")}
        versions.add(rep.get("prompt_version"))
    if not per_ds:
        return None
    tc, tk, tn = sum(v["cost"] for v in per_ds.values()), sum(v["calls"] for v in per_ds.values()), sum(v["n"] for v in per_ds.values())
    unit = [v["cost"] / v["calls"] for v in per_ds.values()]
    return {"per_ds": per_ds, "labels": labels, "pooled": [sum(v[0] for v in labels.values()), tn], "one_call": (sum(one_call) / len(one_call)) if one_call else tc / tk,
            "unit": {"low": min(unit), "central": tc / tk, "high": max(unit)}, "prompt_versions": sorted(v for v in versions if v)}


def estimate(datasets: list[str], systems: tuple[str, ...], limit: Optional[int] = None, cost_cap_usd: Optional[float] = None) -> dict:
    """호출 수·비용 추정 (호출하지 않는다). 낮게·중간·높게 세 값을 돌려주며 실제 값은 달라질 수 있다.
    합친 방식: 5차 실제 모델 실행에서 이미 잰 데이터는 그 호출 수·비용을, 그 밖의 데이터는 라벨별 호출 수와 호출당 비용(데이터별 최소·전체·최대)을 쓴다.
    검토 모델만: 호출 1회(수정안 없음)의 실측 평균 비용에 프롬프트 길이 비(가정 0.9)와 입력 비중을 곱한다. 낮게/높게는 가정을 모두 낮게/높게 잡은 값이다."""
    cap = APPROVED_COST_CAP_USD if cost_cap_usd is None else cost_cap_usd
    sel = {d: (load_dataset(d)[:limit] if limit else load_dataset(d)) for d in datasets}
    n = sum(len(v) for v in sel.values())
    cal = _calibration()
    xs = ("low", "central", "high")
    calls = {"code_only": {x: 0 for x in xs}, "llm_only": {x: n for x in xs}, "combined": {x: 0 for x in xs}}
    cost: dict[str, Optional[dict]] = {"code_only": {x: 0.0 for x in xs}, "llm_only": None, "combined": None}
    if cal is not None:
        grow = PROMPT_GROWTH if cal["prompt_versions"] != [PROMPT_VERSION] else {"low": 1.0, "central": 1.0, "high": 1.03}   # 측정 때와 같은 프롬프트면 늘어난 만큼이 없다
        pk, pn = cal["pooled"]
        c_calls, c_cost = {x: 0.0 for x in xs}, {x: 0.0 for x in xs}
        for d, cases in sel.items():
            ref = cal["per_ds"].get(d)
            if ref and len(cases) == ref["n"] and ref["sha"] == dataset_fingerprint(d)["dataset_sha256"]:      # 이미 같은 사례로 잰 데이터 — 잰 값 그대로 (데이터가 바뀌었다면 라벨별 계산으로)
                for x in xs:
                    c_calls[x] += ref["calls"]
                    c_cost[x] += ref["cost"] * grow[x]
                continue
            by_label = _tally(c.label for c in cases)
            for x in xs:                            # 라벨별 재검사 호출 비율(윌슨 구간)로 호출 수를 정한다
                k_total = 0.0
                for lab, cnt in by_label.items():
                    k, m = cal["labels"].get(lab) or [pk, pn]
                    lo, hi = wilson(k, m)
                    k_total += cnt * (1 + {"low": lo, "central": (k / m if m else 0.0), "high": hi}[x])
                c_calls[x] += k_total
                c_cost[x] += k_total * cal["unit"][x] * grow[x]
        for x in xs:
            calls["combined"][x] = int(round(c_calls[x]))
        cost["combined"] = {x: c_cost[x] for x in xs}
        cost["llm_only"] = {x: n * cal["one_call"] * grow[x] * (INPUT_COST_SHARE * LLM_ONLY_PROMPT_RATIO[x] + (1 - INPUT_COST_SHARE)) for x in xs}
    chosen = {s: {"calls": calls[s]["central"], "cost_usd": (None if cost[s] is None else round(cost[s]["central"], 6))} for s in systems}
    total_calls = {x: sum(calls[s][x] for s in systems) for x in xs}
    ranged = cal is not None and all(cost[s] is not None for s in systems)
    total = {x: sum(cost[s][x] for s in systems) for x in xs} if ranged else None
    warnings: list[str] = []
    if total is not None:
        if total["high"] > cap:
            warnings.append(f"높게 잡은 추정(${total['high']:.2f})이 비용 상한(${cap:.2f})을 넘습니다. 상한에 닿으면 그 자리에서 멈추고, 측정한 사례까지만 ‘완료되지 않은 실행’ 파일({INCOMPLETE_NAME})로 남습니다 — 세 방식 비교의 결과로는 쓸 수 없습니다.")
        if total["central"] > cap:
            warnings.append(f"중간 추정(${total['central']:.2f})도 비용 상한(${cap:.2f})을 넘습니다. 마지막 데이터까지 가기 전에 상한에 닿아 멈출 가능성이 큽니다.")
        elif total["central"] >= cap * CAP_NEAR:
            warnings.append(f"중간 추정(${total['central']:.2f})이 비용 상한(${cap:.2f})의 {CAP_NEAR:.0%} 이상입니다(상한과 15% 이내). 추정은 실제와 다를 수 있어 상한을 넘길 수 있습니다.")
    return {"cases": n, "datasets": datasets, "systems": chosen, "total_calls": total_calls["central"], "calls_range": total_calls,
            "total_cost_usd": (round(total["central"], 2) if total is not None else None),
            "cost_range_usd": ({x: round(total[x], 2) for x in xs} if total is not None else None),
            "cost_by_system_usd": {s: ({x: round(cost[s][x], 3) for x in xs} if cost[s] is not None else None) for s in systems},
            "cap_usd": cap, "warnings": warnings,
            "assumptions": {"llm_only_prompt_ratio": LLM_ONLY_PROMPT_RATIO, "input_cost_share": INPUT_COST_SHARE, "prompt_growth": PROMPT_GROWTH,
                            "unit_cost_per_call_usd": ({x: round(cal["unit"][x], 5) for x in xs} if cal is not None else None)},
            "basis": ("추정이며 실제 값은 달라질 수 있다 — 합친 방식은 5차 실제 모델 실행(골든셋 55·holdout 12·contrast 4건)의 호출 수와 호출당 비용에서, 아직 재지 않은 데이터는 라벨별 호출 수로 계산했다. "
                      f"검토 모델만은 호출 1회 비용에 프롬프트 길이 비 {LLM_ONLY_PROMPT_RATIO['central']} 배를 가정했다(현재 프롬프트로 재면 약 0.88배). "
                      f"측정 때보다 검토 프롬프트가 길어진 만큼 {PROMPT_GROWTH['central']:.2f}배를 더했다. 낮게/높게는 가정을 모두 낮게/높게 잡은 값이다")}


def format_estimate(e: dict) -> str:
    """--estimate 출력. 한 숫자가 아니라 낮게·중간·높게 범위로 적고, 상한과 경고를 함께 적는다."""
    lines = ["== 방식 비교 호출 수·비용 추정 (호출하지 않음 · 모두 추정)",
             f"사례 {e['cases']}건: {', '.join(e['datasets'])}  ×  {' · '.join(SYSTEM_LABEL[s] for s in e['systems'])}"]
    cr = e["calls_range"]
    lines.append(f"모델 호출 약 {e['total_calls']}회 (낮게 {cr['low']} · 높게 {cr['high']}) — " + " · ".join(f"{SYSTEM_LABEL[s]} {v['calls']}" for s, v in e["systems"].items()))
    if e["cost_range_usd"] is None:
        lines.append("추정 비용: 계산 못 함 (저장된 실제 모델 실행 리포트가 없다)")
    else:
        r = e["cost_range_usd"]
        lines.append(f"추정 비용: 낮게 ${r['low']:.2f} · 중간 ${r['central']:.2f} · 높게 ${r['high']:.2f}   (비용 상한 ${e['cap_usd']:.2f})")
        for s, v in e["cost_by_system_usd"].items():
            if v is not None and v["high"] > 0:
                lines.append(f"  {SYSTEM_LABEL[s]}: 낮게 ${v['low']:.2f} · 중간 ${v['central']:.2f} · 높게 ${v['high']:.2f}")
    for w in e["warnings"]:
        lines.append(f"경고: {w}")
    lines.append(f"근거: {e['basis']}")
    return "\n".join(lines)


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
    if rep.get("served_by"):
        print(f"   판정한 제공자: {rep['served_by']} (기준 제공자 {rep.get('provider')})")
    if rep.get("case_counts"):
        print("   사례 수(마친 것/전체): " + " · ".join(f"{ds} {c['completed']}/{c['total']}" for ds, c in rep["case_counts"].items()))
    if not rep.get("complete", True):
        print(f"   {rep['incomplete_note']}  [사유 코드: {', '.join(rep['incomplete_reason'])}]")


if __name__ == "__main__":
    sys.exit(main())
