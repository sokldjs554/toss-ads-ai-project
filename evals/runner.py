"""평가 실행기. 데이터셋의 각 사례를 실제 심사 경로(결정론 가드레일 → 심사 모델 → 판정)로 돌린다."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from agents import adjudicator, reviewer
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

    def to_copy(self) -> AdCopy:
        return AdCopy(variant_id=self.id, headline=self.headline, body=self.body, cta=self.cta,
                      claims=[Claim(**c) for c in self.claims], disclosures=list(self.disclosures))

    def to_product(self) -> ProductFacts:
        cat = ProductCategory(self.category) if self.category in {c.value for c in ProductCategory} else ProductCategory.OTHER
        return ProductFacts(name=self.id, category=cat, facts=[Fact(**f) for f in self.facts])


def load_dataset(name_or_path: str | Path) -> list[EvalCase]:
    path = Path(name_or_path)
    if not path.exists():
        path = DATASETS / f"{name_or_path}.jsonl"
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
    if verdict is not None and verdict.compliant_rewrite is not None:
        recheck = output_guard.check(verdict.compliant_rewrite, product, categories, case.landing_text, [], catalog)
    report = adjudicator.adjudicate(copy, det, verdict, settings.auto_decision_confidence, recheck, None)
    pred_rules = sorted({f.rule_id for f in report.verdict.findings if f.severity != Severity.WARN and f.verified_quote})
    conf = report.verdict.confidence if verdict is not None else 0.0
    return CaseResult(case=case, predicted=report.final_decision.value, confidence=conf, predicted_rules=pred_rules,
                      reason=report.decision_reason, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                      cost_usd=round((router.budget.spent_usd - cost_before) if router else 0.0, 6), unverified_quotes=unverified)


def run_eval(dataset: str, settings: Settings, use_llm: bool = True, router: Optional[Router] = None,
             catalog: Optional[PolicyCatalog] = None, progress: Optional[Callable[[int, int, CaseResult], None]] = None,
             limit: Optional[int] = None) -> dict:
    catalog = catalog or get_catalog()
    cases = load_dataset(dataset)
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
                          category=r.case.category, latency_ms=r.latency_ms, cost_usd=r.cost_usd) for r in results]
    summary = summarize(records) if records else {"n": 0, "coverage": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "unsafe_approve": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "false_flag": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "human_review": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "accuracy_decided": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "catch_rate_decided": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)},
                                                  "by_difficulty": {}, "by_category": {}, "rules": {},
                                                  "calibration": {"n": 0, "ece": None, "brier": None, "bins": [], "high_confidence_error": {"k": 0, "n": 0, "rate": None, "wilson95": (0.0, 0.0)}, "confidently_wrong": []},
                                                  "coverage_curve": [], "cost_usd": 0.0, "latency_ms_mean": 0.0}
    return {
        "dataset": str(dataset), "n": len(cases), "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": settings.mode.value if use_llm else "deterministic_only",
        "reviewer": (f"{settings.openai_model} (structured) / fallback {settings.anthropic_model}" if use_llm and settings.mode.value != "demo"
                     else ("mock" if use_llm else "none")),
        "prompt_version": PROMPT_VERSION, "policy_version": catalog.version,
        "threshold": settings.auto_decision_confidence,
        "recommended_threshold_for_zero_unsafe": recommend_threshold(records, 0.0),
        "recommended_threshold_for_zero_unsafe_and_zero_false_flag": recommend_threshold(records, 0.0, 0.0),
        "summary": summary,
        "cases": [{"id": r.case.id, "label": r.case.label, "predicted": r.predicted, "confidence": r.confidence,
                   "label_rules": r.case.rule_ids, "predicted_rules": r.predicted_rules, "difficulty": r.case.difficulty,
                   "category": r.case.category, "reason": r.reason, "latency_ms": r.latency_ms, "cost_usd": r.cost_usd,
                   "unverified_quotes": r.unverified_quotes} for r in results],
    }


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
