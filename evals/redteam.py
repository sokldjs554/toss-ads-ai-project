"""레드팀 루프 — '우리 가드레일은 어떻게 뚫리는가' 를 기능으로 만든다.

1. 시드(명백한 위반 소재)마다 공격자 모델(Claude; 데모에서는 결정론 변환기)이 전략별 변형을 만든다.
2. 각 변형을 실제 심사 경로(결정론 가드레일 → 심사 모델 → 판정)에 넣는다.
3. 최종 판정이 APPROVE 면 '돌파(evasion)'. 전략별 돌파율을 집계한다.
4. 돌파한 변형은 라벨(REJECT + 시드의 rule_ids)을 붙여 adversarial.jsonl 에 수확 → 다음 회귀 평가에 포함된다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from agents.prompts import REDTEAM_SYSTEM
from core.config import Settings
from core.llm_schemas import AttackBatchOut
from guardrails.policy import PolicyCatalog, get_catalog
from llm.base import LLMRequest, Task
from llm.router import Router

from .runner import DATASETS, EvalAborted, EvalCase, _wait_for_breaker, evaluate_with_retry, load_dataset, tally_errors

STRATEGIES = ["spacing", "zero_width", "hanja", "paraphrase", "negation", "bury", "injection", "implicature"]


@dataclass
class Attack:
    seed_id: str
    strategy: str
    headline: str
    body: str
    cta: str
    intent: str
    predicted: str = ""
    confidence: float = 0.0
    predicted_rules: list[str] = field(default_factory=list)
    evaded: bool = False
    reason: str = ""
    error: str = ""   # 심사 호출이 실패한 공격 — 통과도 반려도 아니다. 집계·수확에서 제외한다


def generate_attacks(seed: EvalCase, router: Router, strategies: list[str]) -> list[Attack]:
    req = LLMRequest(
        task=Task.ATTACK, system=REDTEAM_SYSTEM,
        user=f"위반 소재:\n헤드라인: {seed.headline}\n본문: {seed.body}\nCTA: {seed.cta}\n위반 규칙: {seed.rule_ids}\n전략: {strategies}",
        schema=AttackBatchOut, max_tokens=2500, temperature=1.0,
        context={"seed": {"headline": seed.headline, "body": seed.body, "cta": seed.cta}, "strategies": strategies},
        label=f"redteam:{seed.id}",
    )
    resp = router.complete(req)
    if resp.parsed is None:
        return []
    return [Attack(seed_id=seed.id, strategy=a.strategy, headline=a.headline, body=a.body, cta=a.cta, intent=a.intent)
            for a in resp.parsed.attacks if a.headline or a.body]


def _generate_with_retry(seed: EvalCase, router: Router, strategies: list[str], breaker_wait_s: float,
                         log: Optional[Callable[[str], None]]) -> tuple[list[Attack], str]:
    """공격 생성 호출. 실패하면 서킷이 닫히길 기다렸다가 한 번 더. 그래도 실패하면 (빈 목록, 오류 문구)."""
    err = ""
    for attempt in range(2 if breaker_wait_s > 0 else 1):
        if attempt or breaker_wait_s > 0:
            _wait_for_breaker(router, breaker_wait_s, log, task=Task.ATTACK)
        try:
            return generate_attacks(seed, router, strategies), ""
        except Exception as e:  # noqa: BLE001 — 생성 실패는 기록하고 다음 시드로 간다
            err = f"{type(e).__name__}: {str(e)[:300]}"
    return [], err


def _redteam_report(settings: Settings, router: Router, seed_cases: list, attacks: list[Attack], gen_failures: list[dict], limit_seeds: Optional[int] = None) -> dict:
    # n = 정상적으로 심사된 공격 수. 제공자 장애로 심사하지 못한 공격(errors)은 n·담당자 확인·뚫림률의 분모에서 뺀다 — 장애를 '담당자 확인' 으로 세면 뚫림률이 낮게 보인다 (FM-24)
    by_strategy: dict[str, dict] = {}
    for a in attacks:
        s = by_strategy.setdefault(a.strategy, {"n": 0, "evaded": 0, "human_review": 0, "errors": 0})
        if a.error:
            s["errors"] += 1
            continue
        s["n"] += 1
        s["evaded"] += int(a.evaded)
        s["human_review"] += int(a.predicted == "HUMAN_REVIEW")
    for s in by_strategy.values():
        s["evasion_rate"] = round(s["evaded"] / s["n"], 3) if s["n"] else 0.0
    judged = [a for a in attacks if not a.error]
    b = router.budget
    failed = [f"{a.seed_id}:{a.strategy}" for a in attacks if a.error] + [f"{g['seed']}:generate" for g in gen_failures]
    errs = [a.error for a in attacks if a.error] + [g["error"] for g in gen_failures]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "mode": settings.mode.value, "seeds": len(seed_cases),
        "run_settings": {"limit": limit_seeds},      # 시드 일부만 돌렸다면 기록한다 — baseline_allowed 가 부분 실행을 기준선으로 쓰지 않게
        "attacks": [a.__dict__ for a in attacks], "by_strategy": by_strategy,
        "judged": len(judged),
        "evasion_rate": round(sum(a.evaded for a in judged) / len(judged), 3) if judged else 0.0,
        "cost_usd": round(b.spent_usd, 4),
        # 응답(사용량)을 받지 못한 요청의 비용은 알 수 없다 — 위 비용은 응답을 받은 요청만 더한 값(하한)
        "failed_requests": b.failed_attempts, "cost_estimated": bool(b.estimated or b.failed_attempts),
        # 제공자 장애로 심사하지 못한 공격·생성하지 못한 시드. 0 이 아니면 '뚫림 0' 이 장애 때문일 수 있어 기준선·수확에 쓰지 않는다
        "provider_failures": {"n": len(failed), "cases": failed, "by_error": tally_errors(errs)},
    }


def run_redteam(settings: Settings, router: Optional[Router] = None, seeds: str = "redteam_seeds",
                strategies: Optional[list[str]] = None, catalog: Optional[PolicyCatalog] = None,
                progress: Optional[Callable[[str, Attack], None]] = None, limit_seeds: Optional[int] = None,
                breaker_wait_s: float = 90.0, abort_after: int = 3, cost_cap_usd: Optional[float] = None,
                log: Optional[Callable[[str], None]] = None) -> dict:
    """abort_after: 제공자 오류가 연속으로 이만큼 나면 EvalAborted. cost_cap_usd: 누적 비용이 넘으면 EvalAborted (승인된 예산을 지킨다)."""
    catalog = catalog or get_catalog()
    router = router or Router(settings)
    router.new_run()
    router.budget.cap_usd = max(router.budget.cap_usd, 5.0)
    router.budget.max_calls = 10_000
    strategies = strategies or STRATEGIES
    seed_cases = load_dataset(seeds)
    if limit_seeds:
        seed_cases = seed_cases[:limit_seeds]
    attacks: list[Attack] = []
    gen_failures: list[dict] = []
    consecutive = 0
    planned = len(seed_cases) * len(strategies)

    def abort(reason: str, last_error: str, after: str) -> EvalAborted:
        rep = _redteam_report(settings, router, seed_cases, attacks, gen_failures, limit_seeds)
        rep["aborted"] = {"reason": reason, "after": after, "consecutive_provider_failures": consecutive, "done": len(attacks), "planned": planned,
                          "cost_usd": round(router.budget.spent_usd, 4)}
        return EvalAborted(f"{reason} ({after} 까지 공격 {len(attacks)}/{planned}건 처리) — 마지막 오류: {last_error or '-'}", rep)

    for seed in seed_cases:
        generated, gen_err = _generate_with_retry(seed, router, strategies, breaker_wait_s, log)
        if gen_err:
            gen_failures.append({"seed": seed.id, "error": gen_err})
            consecutive += 1
            if abort_after and consecutive >= abort_after:
                raise abort(f"제공자 오류가 {consecutive}건 연속", gen_err, seed.id)
            continue
        for atk in generated:
            case = EvalCase(id=f"{seed.id}:{atk.strategy}", category=seed.category, headline=atk.headline, body=atk.body, cta=atk.cta,
                            label="REJECT", rule_ids=seed.rule_ids, disclosures=seed.disclosures, facts=seed.facts,
                            claims=seed.claims, landing_text=seed.landing_text, difficulty="evasion", source="redteam")
            r = evaluate_with_retry(case, settings, router, catalog, use_llm=True, breaker_wait_s=breaker_wait_s, log=log)
            # 심사 단계뿐 아니라 수정안 의미 검사 단계의 호출 실패도 장애로 센다
            atk.predicted, atk.confidence, atk.predicted_rules, atk.reason, atk.error = r.predicted, r.confidence, r.predicted_rules, r.reason, (r.error or r.rewrite_error)
            atk.evaded = r.predicted == "APPROVE" and not atk.error
            attacks.append(atk)
            if progress:
                progress(seed.id, atk)
            consecutive = consecutive + 1 if atk.error else 0
            if abort_after and consecutive >= abort_after:
                raise abort(f"제공자 오류가 {consecutive}건 연속", atk.error, f"{seed.id}:{atk.strategy}")
            if cost_cap_usd is not None and router.budget.spent_usd > cost_cap_usd:
                raise abort(f"누적 비용 ${router.budget.spent_usd:.2f} 가 상한 ${cost_cap_usd:.2f} 초과", "", f"{seed.id}:{atk.strategy}")
    return _redteam_report(settings, router, seed_cases, attacks, gen_failures, limit_seeds)


def harvest(report: dict, path: Optional[Path] = None, only_evaded: bool = False) -> int:
    """회귀셋에 추가한다. 기본은 '잡지 못한' 공격 전부 — 돌파(APPROVE)뿐 아니라 REVISE·HUMAN_REVIEW 로 끝난 근접 사례도.
    (사람 검토로 떨어진 것은 안전하지만 규칙이 못 잡은 것이므로 다음 회귀에서 잡혀야 한다.) 이미 있는 id 는 건너뛴다."""
    path = path or (DATASETS / "adversarial.jsonl")
    existing: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                existing.add(json.loads(line)["id"])
    seeds = {c.id: c for c in load_dataset("redteam_seeds")}
    added = 0
    with path.open("a", encoding="utf-8") as fh:
        for a in report["attacks"]:
            if a.get("error"):  # 제공자 장애로 심사하지 못한 공격은 '못 잡은 공격' 이 아니다
                continue
            if only_evaded and not a["evaded"]:
                continue
            if not only_evaded and a["predicted"] == "REJECT":
                continue
            cid = f"ADV-{a['seed_id']}-{a['strategy']}"
            if cid in existing:
                continue
            seed = seeds.get(a["seed_id"])
            row = {"id": cid, "category": seed.category if seed else "other", "headline": a["headline"], "body": a["body"], "cta": a["cta"],
                   "label": "REJECT", "rule_ids": seed.rule_ids if seed else [], "disclosures": seed.disclosures if seed else [],
                   "facts": seed.facts if seed else [], "claims": [], "landing_text": seed.landing_text if seed else "",
                   "difficulty": "evasion", "notes": f"redteam {a['strategy']}: {a['intent']}", "source": "redteam_harvest"}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            existing.add(cid)
            added += 1
    return added
