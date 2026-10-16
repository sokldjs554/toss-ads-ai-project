"""가상 유저 패널 (Claude). 점수 분산으로 아첨(FM-15)을 탐지한다."""
from __future__ import annotations

import statistics

from core.llm_schemas import AudienceOut
from core.schemas import AdCopy, AudienceReport, PersonaFeedback
from llm.base import LLMRequest, Task
from llm.router import Router

from .personas import PERSONAS, persona_prompt
from .prompts import AUDIENCE_SYSTEM

_NAMES = {p["persona_id"]: p["name"] for p in PERSONAS}
SYCOPHANCY_DIVERSITY_THRESHOLD = 0.45


def _clamp(v: int) -> int:
    return max(1, min(5, int(v)))


def summarize(variant_id: str, out: AudienceOut) -> AudienceReport:
    feedbacks: list[PersonaFeedback] = []
    for f in out.feedbacks:
        if f.persona_id not in _NAMES:
            continue  # 모델이 지어낸 페르소나는 버린다
        feedbacks.append(PersonaFeedback(persona_id=f.persona_id, persona_name=_NAMES[f.persona_id], appeal=_clamp(f.appeal),
                                         trust=_clamp(f.trust), clarity=_clamp(f.clarity), would_click=bool(f.would_click),
                                         concern=f.concern.strip(), quote=f.quote.strip()))
    if not feedbacks:
        raise ValueError("유효한 페르소나 피드백이 없음")
    appeal = [f.appeal for f in feedbacks]
    trust = [f.trust for f in feedbacks]
    clarity = [f.clarity for f in feedbacks]
    scores = appeal + trust
    diversity = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    unanimous_click = len({f.would_click for f in feedbacks}) == 1
    sycophancy = diversity < SYCOPHANCY_DIVERSITY_THRESHOLD or (unanimous_click and min(scores) >= 4)
    return AudienceReport(
        variant_id=variant_id, feedbacks=feedbacks, mean_appeal=round(statistics.fmean(appeal), 2),
        mean_trust=round(statistics.fmean(trust), 2), mean_clarity=round(statistics.fmean(clarity), 2),
        click_rate=round(sum(f.would_click for f in feedbacks) / len(feedbacks), 2), diversity=round(diversity, 2),
        sycophancy_flag=sycophancy, summary=out.summary.strip(),
    )


def simulate(copy: AdCopy, router: Router) -> AudienceReport:
    user = (f"가상 유저:\n{persona_prompt()}\n\n광고 소재:\n헤드라인: {copy.headline}\n본문: {copy.body}\nCTA: {copy.cta}\n"
            f"고지: {' / '.join(copy.disclosures) or '(없음)'}")
    req = LLMRequest(task=Task.AUDIENCE, system=AUDIENCE_SYSTEM, user=user, schema=AudienceOut, max_tokens=1600, temperature=0.8,
                     context={"copy_text": copy.full_text(), "has_disclosure": bool(copy.disclosures)}, label=f"audience:{copy.variant_id}")
    resp = router.complete(req)
    if resp.parsed is None:
        raise ValueError("패널 출력이 비어 있음")
    return summarize(copy.variant_id, resp.parsed)
