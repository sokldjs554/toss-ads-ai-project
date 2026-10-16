"""카피라이터 — 창의 생성 (Claude, 비제약). 형식이 어긋나면 라우터가 구조화 제공자에게 수리를 맡긴다 (FM-01)."""
from __future__ import annotations

from dataclasses import dataclass, field

from core.llm_schemas import CopyBatchOut
from core.schemas import AdCopy, CampaignBrief, CampaignPlan, Claim
from guardrails.disclosure import canonical_lines
from guardrails.injection import spotlight
from guardrails.policy import PolicyCatalog
from llm.base import LLMRequest, Task
from llm.router import Router

from .prompts import COPYWRITER_SYSTEM


@dataclass
class CopyResult:
    variants: list[AdCopy]
    provider: str = ""
    model: str = ""
    repaired: bool = False
    dropped: list[str] = field(default_factory=list)


def required_disclosure_lines(catalog: PolicyCatalog, categories: set[str]) -> list[str]:
    lines: list[str] = []
    for cat in sorted(categories):
        spec = catalog.disclosure_for(cat)
        if spec:
            lines.extend(canonical_lines(list(spec.items)))
    # 중복 제거 (순서 유지)
    seen: set[str] = set()
    return [l for l in lines if not (l in seen or seen.add(l))]


def write_copy(brief: CampaignBrief, plan: CampaignPlan | None, categories: set[str], description: str,
               catalog: PolicyCatalog, router: Router, n: int = 3) -> CopyResult:
    facts = "\n".join(f"- {f.id}: {f.text}" for f in brief.product.facts) or "- (사실 없음 — 상품명 외에는 아무 주장도 하지 말 것)"
    disc = required_disclosure_lines(catalog, categories)
    plan_line = (f"캠페인 목표: {plan.objective.value}, KPI: {plan.kpi}, 핵심 타겟: {plan.ad_sets[0].audience}" if plan else "캠페인 목표: 미정")
    user = (
        f"상품명: {brief.product.name}\n업종: {', '.join(sorted(categories))}\n{plan_line}\n변형 수: {n}\n\n"
        f"사실 목록 (이 밖의 주장은 금지):\n{facts}\n\n"
        f"필수 고지 (disclosures 에 포함):\n" + ("\n".join(f"- {d}" for d in disc) or "- (없음)") + "\n\n"
        + (spotlight("product_description", description[:1500]) if description else "")
    )
    req = LLMRequest(
        task=Task.COPY, system=COPYWRITER_SYSTEM, user=user, schema=CopyBatchOut, max_tokens=2500, temperature=0.9,
        context={"product_name": brief.product.name, "category": brief.product.category.value,
                 "facts": [f.model_dump() for f in brief.product.facts], "n": n},
        label="copywriter",
    )
    resp = router.complete(req)
    if resp.parsed is None:
        raise ValueError("카피라이터 출력이 비어 있음")
    variants: list[AdCopy] = []
    dropped: list[str] = []
    for v in resp.parsed.variants[: max(1, n)]:
        if not v.headline.strip() or not v.body.strip():
            dropped.append("빈 headline/body")
            continue
        variants.append(AdCopy(
            headline=v.headline.strip(), body=v.body.strip(), cta=(v.cta or "자세히 보기").strip(),
            claims=[Claim(text=c.text, fact_id=c.fact_id) for c in v.claims], disclosures=[d for d in v.disclosures if d.strip()],
            tone=v.tone,
        ))
    if not variants:
        raise ValueError("유효한 소재 변형이 없음")
    return CopyResult(variants=variants, provider=resp.provider, model=resp.model, repaired=resp.repaired, dropped=dropped)
