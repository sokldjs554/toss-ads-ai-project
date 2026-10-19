"""문구 심사와 이미지 검사를 한 소재의 판정으로 합친다 — 순수 함수, 결정론.

규칙: 어느 한쪽이 게재 불가(REJECT)면 REJECT. 이미지를 읽지 못했으면(UNREADABLE) 문구가 통과여도 최소 담당자 확인(HUMAN_REVIEW).
두 쪽이 모두 통과(APPROVE)일 때만 APPROVE. 그 사이의 순서는 `APPROVE < REVISE < HUMAN_REVIEW < REJECT` 이다 — 아직 확정하지 못한 상태(HUMAN_REVIEW)는
고칠 곳이 확인된 상태(REVISE)보다 앞선다 (agents/adjudicator.decide 가 불확실성 검사를 수정 필요보다 먼저 보는 것과 같은 순서). 어느 쪽의 이유도 버리지 않고 합친다.
이미지 없이 문구만이면 결과는 문구 판정 그대로다. 파이프라인의 decide() 는 바꾸지 않는다 — 이 함수는 그 위에서 합산만 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from core.schemas import Decision
from ui.labels import DECISION_LABEL

# 판정 심각도 순서 (큰 쪽이 이긴다)
RANK: dict[Decision, int] = {Decision.APPROVE: 0, Decision.REVISE: 1, Decision.HUMAN_REVIEW: 2, Decision.REJECT: 3}


@dataclass(frozen=True)
class CombinedDecision:
    decision: Decision
    reasons: tuple
    text_decision: Decision
    image_decision: Optional[Decision]
    image_unreadable: bool = False
    decided_by: str = "text"          # text | image | both — 최종 판정이 어느 쪽에서 왔는가 (같으면 both)


def _as_decision(x) -> Decision:
    if isinstance(x, Decision):
        return x
    fd = getattr(x, "final_decision", None)      # PreflightReport
    if isinstance(fd, Decision):
        return fd
    return Decision(str(x))


def combine(text_decision: Union[Decision, str, object], image_review=None, *, text_reason: str = "") -> CombinedDecision:
    """text_decision: Decision 또는 PreflightReport(final_decision 을 쓴다). image_review: agents.vision.ImageReview 또는 None."""
    td = _as_decision(text_decision)
    t_reason = text_reason or (getattr(text_decision, "decision_reason", "") if not isinstance(text_decision, (Decision, str)) else "")
    text_line = f"문구 검사: {DECISION_LABEL[td]}" + (f" — {t_reason}" if t_reason else "")
    if image_review is None:
        return CombinedDecision(decision=td, reasons=(text_line,), text_decision=td, image_decision=None, decided_by="text")
    idec = _as_decision(image_review.decision)
    unreadable = bool(getattr(image_review, "unreadable", False))
    if unreadable and RANK[idec] < RANK[Decision.HUMAN_REVIEW]:
        idec = Decision.HUMAN_REVIEW        # 읽지 못한 이미지는 어떤 경우에도 통과·수정이 될 수 없다
    final = max(td, idec, key=RANK.__getitem__)
    reasons = [text_line, f"이미지 검사: {DECISION_LABEL[idec]}"]
    reasons += [f"이미지 — {r}" for r in getattr(image_review, "reasons", ())]
    if RANK[td] == RANK[idec]:
        by = "both"
    else:
        by = "text" if RANK[td] > RANK[idec] else "image"
    return CombinedDecision(decision=final, reasons=tuple(reasons), text_decision=td, image_decision=idec, image_unreadable=unreadable, decided_by=by)
