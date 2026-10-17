"""평가 실행기. 데이터셋의 각 사례를 실제 심사 경로(결정론 가드레일 → 심사 모델 → 판정)로 돌린다."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from agents import adjudicator, reviewer
from agents.rewrite_check import check_rewrite
from agents.prompts import PROMPT_VERSION
from core.config import Settings
from core.schemas import AdCopy, Claim, Decision, Fact, ProductCategory, ProductFacts, Severity
from guardrails import output_guard
from guardrails.policy import PolicyCatalog, get_catalog
from llm.router import Router
from observability.tracer import Tracer

from .metrics import EvalRecord, recommend_threshold, summarize

DATASETS = Path(__file__).with_name("datasets")
REPORTS = Path(__file__).with_name("reports")


@dataclass
class EvalCase:
    id: str
    category: str
    headline: str
    body: str
    cta: str
    label: str
    rule_ids: list[str] = field(default_factory=list)
    disclosures: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    landing_text: str = ""
    difficulty: str = "clear"
    notes: str = ""
    source: str = "authored"
    split: str = "dev"                                   # dev: 규칙·프롬프트 수정에 쓴 사례 | holdout: 최종 확인용
    label_history: list[dict] = field(default_factory=list)  # 라벨 변경 이력 (버전·이전 라벨·이유)

    def to_copy(self) -> AdCopy:
        return AdCopy(variant_id=self.id, headline=self.headline, body=self.body, cta=self.cta,
                      claims=[Claim(**c) for c in self.claims], disclosures=list(self.disclosures))

    def to_product(self) -> ProductFacts:
        cat = ProductCategory(self.category) if self.category in {c.value for c in ProductCategory} else ProductCategory.OTHER
        return ProductFacts(name=self.id, category=cat, facts=[Fact(**f) for f in self.facts])


def dataset_path(name_or_path: str | Path) -> Path:
    path = Path(name_or_path)
    return path if path.exists() else DATASETS / f"{name_or_path}.jsonl"


def dataset_fingerprint(name_or_path: str | Path) -> dict:
    """데이터셋의 버전(첫 줄 주석의 'v1.x')과 내용 해시 — 추천 임계값이 어떤 데이터로 나온 것인지 묶어 둔다."""
    path = dataset_path(name_or_path)
    text = path.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text else ""
    m = re.search(r"v(\d+(?:\.\d+)*)", first)
    return {"dataset_version": m.group(1) if m else "", "dataset_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}


def load_dataset(name_or_path: str | Path) -> list[EvalCase]:
    path = dataset_path(name_or_path)
    cases: list[EvalCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        cases.append(EvalCase(**{k: v for k, v in d.items() if k in EvalCase.__dataclass_fields__}))
    return cases


@dataclass
class CaseResult:
    case: EvalCase
    predicted: str
    confidence: float
    predicted_rules: list[str]
    reason: str
    latency_ms: float
    cost_usd: float
    unverified_quotes: int = 0
    inputs: dict = field(default_factory=dict)   # 판정 원본 (DecisionInputs) — 다른 τ 로 그대로 재생 가능
    rewrite_status: str = "none"
    model_decision: str = ""
    cost_estimated: bool = False


def _tracer_of(router: Optional[Router]) -> Tracer:
    return router.tracer if router is not None else Tracer()


def evaluate_case(case: EvalCase, settings: Settings, router: Optional[Router], catalog: PolicyCatalog,
                  use_llm: bool = True) -> CaseResult:
    copy, product = case.to_copy(), case.to_product()
    categories = catalog.effective_categories(product.category, copy.full_text())
    t0 = time.perf_counter()
    cost_before = router.budget.spent_usd if router else 0.0
    det = output_guard.check(copy, product, categories, case.landing_text, [], catalog)
    verdict = None
    unverified = 0
    if use_llm and router is not None:
        facts = "\n".join(f"- {f.id}: {f.text}" for f in product.facts) or "- (없음)"
        try:
            ro = reviewer.review(copy, det, categories, facts, case.landing_text, catalog, router)
            verdict = ro.verdict
            unverified = ro.unverified_quotes
        except Exception:  # noqa: BLE001 — 실패는 HUMAN_REVIEW 로 떨어진다 (fail-closed)
            verdict = None
    if not use_llm:
        from core.schemas import ReviewVerdict

        verdict = ReviewVerdict(variant_id=copy.variant_id, decision=det.decision, confidence=0.95 if det.findings else 0.7,
                                findings=det.findings, rationale="결정론 계층만 (의미 심사 없음)", provider="none", model="none")
    recheck = None
    if verdict is not None and verdict.proposed_rewrite is not None:
        facts = "\n".join(f"- {f.id}: {f.text}" for f in product.facts) or "- (없음)"
        recheck = check_rewrite(verdict.proposed_rewrite, product, categories, case.landing_text, [], catalog, router if use_llm else None, facts, _tracer_of(router))
    report = adjudicator.adjudicate(copy, det, verdict, settings.auto_decision_confidence, recheck, None)
    inputs = adjudicator.inputs_from(det, verdict, recheck)
    pred_rules = sorted({f.rule_id for f in report.verdict.findings if f.severity != Severity.WARN and f.verified_quote})
    conf = report.verdict.confidence if verdict is not None else 0.0
    return CaseResult(case=case, predicted=report.final_decision.value, confidence=conf, predicted_rules=pred_rules,
                      reason=report.decision_reason, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                      cost_usd=round((router.budget.spent_usd - cost_before) if router else 0.0, 6), unverified_quotes=unverified,
                      inputs=inputs.to_dict(), rewrite_status=report.rewrite_status.value,
                      model_decision=verdict.decision.value if verdict is not None else "",
                      cost_estimated=bool(router and router.budget.estimated))


def run_eval(dataset: str, settings: Settings, use_llm: bool = True, router: Optional[Router] = None,
             catalog: Optional[PolicyCatalog] = None, progress: Optional[Callable[[int, int, CaseResult], None]] = None,
             limit: Optional[int] = None) -> dict:
    catalog = catalog or get_catalog()
    cases = load_dataset(dataset)
    excluded_semantic_only = 0
    if not use_llm or settings.mode.value == "demo":
        # 의미 심사가 없으면(결정론만 / 대역) 구조적으로 잡을 수 없는 사례는 평가에서 빼고 그 수를 기록한다 — 숨기는 게 아니라 분모를 정직하게
        keep = [c for c in cases if c.difficulty != "semantic_only"]
        excluded_semantic_only = len(cases) - len(keep)
        cases = keep
    if limit:
        cases = cases[:limit]
    if use_llm:
        router = router or Router(settings, tracer=Tracer())
        router.budget.cap_usd = max(router.budget.cap_usd, 5.0)  # 평가는 실행 예산보다 크게
        router.budget.max_calls = 10_000
    results: list[CaseResult] = []
    for i, c in enumerate(cases):
        r = evaluate_case(c, settings, router, catalog, use_llm=use_llm)
        results.append(r)
        if progress:
            progress(i + 1, len(cases), r)
    records = [EvalRecord(case_id=r.case.id, label=r.case.label, predicted=r.predicted, confidence=r.confidence,
                          label_rules=set(r.case.rule_ids), predicted_rules=set(r.predicted_rules), difficulty=r.case.difficulty,
                          category=r.case.category, latency_ms=r.latency_ms, cost_usd=r.cost_usd, inputs=r.inputs or None) for r in results]
    summary = summarize(records) if records else {"n": 0, "coverage": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "unsafe_approve": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "false_flag": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "human_review": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "accuracy_decided": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "catch_rate_decided": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "by_difficulty": {}, "by_category": {}, "rules": {},
                                                  "calibration": {"n": 0, "ece": None, "brier": None, "bins": [], "high_confidence_error": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)}, "confidently_wrong": []},
                                                  "coverage_curve": [], "curve_method": "replay_decide", "cost_usd": 0.0, "latency_ms_mean": 0.0}
    mode = settings.mode.value if use_llm else "deterministic_only"
    return {
        "dataset": str(dataset), "n": len(cases), "excluded_semantic_only": excluded_semantic_only,
        **dataset_fingerprint(dataset),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "reviewer": (f"{settings.openai_model} (structured) / fallback {settings.anthropic_model}" if use_llm and settings.mode.value != "demo"
                     else ("mock" if use_llm else "none")),
        "provider": ("none" if not use_llm else ("mock" if settings.mode.value == "demo" else ("openai" if settings.has_openai else "anthropic"))),
        "model": ("none" if not use_llm else ("mock" if settings.mode.value == "demo" else (settings.openai_model if settings.has_openai else settings.anthropic_model))),
        "prompt_version": PROMPT_VERSION, "policy_version": catalog.version,
        "threshold": settings.auto_decision_confidence,
        # 평가 경로의 범위: 결정론 가드레일 → 1차 심사 → 수정안 재검사 → 판정. 2차 의견·가상 유저는 포함하지 않는다 (Studio 와 다른 점).
        "scope": {"second_opinion": False, "audience": False, "stages": "output_guard → reviewer → rewrite_recheck(code + semantic) → adjudicate"},
        "curve_method": summary.get("curve_method", "replay_decide"),
        "recommended_threshold_for_zero_unsafe": recommend_threshold(records, 0.0),
        "recommended_threshold_for_zero_unsafe_and_zero_false_flag": recommend_threshold(records, 0.0, 0.0),
        "cost_estimated": any(r.cost_estimated for r in results),
        "summary": summary,
        "cases": [{"id": r.case.id, "label": r.case.label, "predicted": r.predicted, "confidence": r.confidence,
                   "label_rules": r.case.rule_ids, "predicted_rules": r.predicted_rules, "difficulty": r.case.difficulty,
                   "category": r.case.category, "reason": r.reason, "latency_ms": r.latency_ms, "cost_usd": r.cost_usd,
                   "unverified_quotes": r.unverified_quotes, "inputs": r.inputs, "rewrite_status": r.rewrite_status,
                   "model_decision": r.model_decision} for r in results],
    }


def records_from_report(report: dict) -> list[EvalRecord]:
    """저장된 리포트를 EvalRecord 로 되살린다 (원본 입력이 있으면 τ 재생 가능)."""
    return [EvalRecord(case_id=c["id"], label=c["label"], predicted=c["predicted"], confidence=c["confidence"],
                       label_rules=set(c.get("label_rules", [])), predicted_rules=set(c.get("predicted_rules", [])),
                       difficulty=c.get("difficulty", "clear"), category=c.get("category", "other"),
                       latency_ms=c.get("latency_ms", 0.0), cost_usd=c.get("cost_usd", 0.0), inputs=c.get("inputs")) for c in report.get("cases", [])]


def recommendation_for(settings: Settings, catalog: Optional[PolicyCatalog] = None, dataset: str = "golden") -> dict:
    """현재 설정(제공자·모델·프롬프트·정책·데이터셋)과 '같은 조건에서 평가된' 추천 임계값만 돌려준다.
    조건이 하나라도 다르면 tau=None 과 그 이유를 돌려준다 — 다른 조건의 추천값을 빌려 쓰지 않는다."""
    catalog = catalog or get_catalog()
    base = load_report(f"baseline_{settings.mode.value}.json")
    if base is None:
        return {"tau": None, "reason": "이 모드의 저장된 평가 결과 없음", "report": None, "mismatches": []}
    want = {
        "provider": ("mock" if settings.mode.value == "demo" else ("openai" if settings.has_openai else "anthropic")),
        "model": ("mock" if settings.mode.value == "demo" else (settings.openai_model if settings.has_openai else settings.anthropic_model)),
        "prompt_version": PROMPT_VERSION, "policy_version": catalog.version,
        "dataset_sha256": dataset_fingerprint(dataset)["dataset_sha256"],
    }
    mismatches = [f"{k}: 평가 {base.get(k)!r} ≠ 현재 {v!r}" for k, v in want.items() if base.get(k) != v]
    if base.get("curve_method") != "replay_decide":
        mismatches.append("곡선이 실제 판정 재생이 아닌 옛 방식(신뢰도 필터)으로 계산됨")
    tau = base.get("recommended_threshold_for_zero_unsafe_and_zero_false_flag")
    if mismatches or tau is None:
        return {"tau": None, "reason": "이 설정의 검증된 추천값 없음", "report": base, "mismatches": mismatches}
    return {"tau": float(tau), "reason": "같은 제공자·모델·프롬프트·정책·데이터셋에서 평가됨", "report": base, "mismatches": []}


def save_report(report: dict, name: str = "latest.json") -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report(name: str) -> Optional[dict]:
    path = REPORTS / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
