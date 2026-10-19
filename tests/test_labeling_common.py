"""검수 대기열·라벨링 테스트의 공통 도구 (D1). 네트워크·모델 호출 없음 — 시계는 가짜 시계, 광고는 손으로 쓴 가상 문구."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.schemas import AdCopy, Fact
from ops.labeling import Annotation, LabelDecision
from ops.review_queue import ReviewItem, ReviewQueue, VerdictSnapshot

T0 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)


class FakeClock:
    """테스트가 시간을 직접 움직이는 시계 — 같은 입력이면 같은 결과를 내기 위해 실제 시계를 쓰지 않는다."""

    def __init__(self, start: datetime = T0):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


RISKY = dict(headline="원금 보장 투자", body="월 3% 확정 수익, 손실 없음", cta="가입하기", disclosures=["투자에는 원금 손실 가능성이 있습니다."])
PLAIN = dict(headline="전 상품 10% 할인", body="10월 31일까지, 1인 1회", cta="쇼핑하기", disclosures=[])


def ad(**over) -> AdCopy:
    base = dict(RISKY)
    base.update(over)
    return AdCopy(**base)


def snap(conf: float | None = 0.5, *, disagreement: bool = False, final: str = "HUMAN_REVIEW", model: str = "REVISE", code: str = "APPROVE", reason: str = "") -> VerdictSnapshot:
    return VerdictSnapshot(final_decision=final, model_decision=model, code_decision=code, confidence=conf, disagreement=disagreement, decision_reason=reason, run_mode="demo")


def make_queue(required: int = 2, **kw) -> tuple[ReviewQueue, FakeClock]:
    clock = FakeClock()
    kw.setdefault("gold_rate", 0.0)
    return ReviewQueue(required_labels=required, clock=clock, **kw), clock


def add_item(q: ReviewQueue, n: int, *, conf: float | None = 0.5, category: str = "loan", disagreement: bool = False, **copy_over) -> ReviewItem:
    """n 번째 광고(문구가 서로 다르다)를 대기열에 넣는다."""
    copy_over.setdefault("headline", f"원금 보장 투자 {n}호")
    return q.enqueue_ad(ad(**copy_over), dataset_id=f"T-{n:03d}", category=category, facts=[Fact(id="F1", text="총보수 연 0.15%")],
                        verdict=snap(conf, disagreement=disagreement))


def ann(item_id: str, who: str, decision: LabelDecision | str = LabelDecision.REJECT, *, rules=None, spans=None, why: str = "원금 보장 표현", seconds: float | None = 30.0, **kw) -> Annotation:
    dec = LabelDecision(decision)
    if dec == LabelDecision.APPROVE:
        rules = [] if rules is None else rules
        spans = [] if spans is None else spans
    else:
        rules = ["FIN-GUARANTEE-01"] if rules is None else rules
        spans = ["원금 보장"] if spans is None else spans
    return Annotation(item_id=item_id, annotator=who, decision=dec, rule_ids=rules, cited_spans=spans, rationale=why, seconds_spent=seconds, **kw)


def label(q: ReviewQueue, who: str, item_id: str, decision: LabelDecision | str = LabelDecision.REJECT, **kw):
    """점유한 뒤 라벨을 낸다 (실제 경로 그대로)."""
    q.claim_item(who, item_id)
    return q.submit(ann(item_id, who, decision, **kw))


def test_helpers_build_a_working_queue():
    q, clock = make_queue()
    it = add_item(q, 1)
    res = label(q, "가", it.id)
    assert res.state.value == "queued" and q.get(it.id).labelers() == ["가"]
    assert clock() == T0
