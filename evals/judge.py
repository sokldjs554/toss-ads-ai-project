"""소재 품질 쌍대 비교 (LLM-as-judge) — 위치 편향을 측정하기 위해 순서를 바꿔 두 번 묻는다."""
from __future__ import annotations

from dataclasses import dataclass

from agents.prompts import JUDGE_SYSTEM
from core.llm_schemas import JudgeOut
from core.schemas import AdCopy
from llm.base import LLMRequest, Task
from llm.router import Router


@dataclass
class PairResult:
    winner: str          # "A" | "B" | "TIE"
    consistent: bool     # 순서를 바꿔도 같은 결론인가
    first: str
    second: str
    reason: str


def _ask(a: AdCopy, b: AdCopy, router: Router, label: str) -> JudgeOut:
    user = (f"[A]\n{a.full_text()}\n\n[B]\n{b.full_text()}")
    resp = router.complete(LLMRequest(task=Task.JUDGE, system=JUDGE_SYSTEM, user=user, schema=JudgeOut, max_tokens=400, temperature=0.0,
                                      context={"a": a.full_text(), "b": b.full_text()}, label=label))
    return resp.parsed  # type: ignore[return-value]


def compare(a: AdCopy, b: AdCopy, router: Router) -> PairResult:
    r1 = _ask(a, b, router, "judge:ab")
    r2 = _ask(b, a, router, "judge:ba")
    w1 = r1.winner
    w2 = {"A": "B", "B": "A", "TIE": "TIE"}[r2.winner]  # 두 번째 호출의 A 는 원래 b
    consistent = w1 == w2
    winner = w1 if consistent else "TIE"
    return PairResult(winner=winner, consistent=consistent, first=w1, second=w2, reason=r1.reason if consistent else "순서를 바꾸자 결론이 달라져 무승부 처리 (위치 편향)")


def rank(copies: list[AdCopy], router: Router) -> tuple[list[tuple[AdCopy, int]], float]:
    """단순 라운드 로빈 승점. 반환: [(copy, points)], 위치 뒤집기 불일치율."""
    points = {c.variant_id: 0 for c in copies}
    flips = 0
    pairs = 0
    for i in range(len(copies)):
        for j in range(i + 1, len(copies)):
            pr = compare(copies[i], copies[j], router)
            pairs += 1
            flips += int(not pr.consistent)
            if pr.winner == "A":
                points[copies[i].variant_id] += 1
            elif pr.winner == "B":
                points[copies[j].variant_id] += 1
    ranked = sorted(((c, points[c.variant_id]) for c in copies), key=lambda x: -x[1])
    return ranked, (flips / pairs if pairs else 0.0)
