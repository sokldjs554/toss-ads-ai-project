"""시뮬레이션 라벨러(제어된 노이즈 비율)로 파이프라인이 '품질 낮은 라벨러' 를 검증용 항목으로 잡는지 본다.

가상의 라벨러는 진짜 판정을 알고 있고, 정해진 확률로 다른 판정을 낸다(노이즈). 실제 사람의 오류가 이렇게 무작위라는 뜻이 아니다 —
'라벨러 품질 측정이 노이즈를 얼마나 잘 드러내는가' 를 재현 가능하게 보는 장치다. 모든 난수는 시드를 받는다.
재현: PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/test_labeling_simulation.py -s
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import pytest

from core.schemas import AdCopy, Fact
from ops.labeling import AdjudicationRecord, Annotation, LabelDecision, agreement_report
from ops.review_queue import ItemState, ReviewQueue
from tests.test_labeling_common import FakeClock, snap

LABELS = [LabelDecision.APPROVE, LabelDecision.REVISE, LabelDecision.REJECT]
N_REAL, N_GOLD = 60, 12
NOISE = {"정확한 라벨러": 0.0, "보통 라벨러": 0.10, "부주의한 라벨러": 0.40, "무작위 라벨러": 2 / 3}     # 2/3 = 세 판정 중 아무거나 고르는 것과 같다


def make_ad(i: int, truth: LabelDecision) -> AdCopy:
    return AdCopy(headline=f"시뮬레이션 광고 {i:03d}호 {truth.value}", body=f"본문 문장 {i * 17 % 101} 번째", cta="신청", disclosures=[])


def answer(item_id: str, who: str, copy: AdCopy, decision: LabelDecision) -> Annotation:
    """어떤 판정이든 규칙·인용 검사를 통과하는 라벨 (인용은 제목 전체)."""
    if decision == LabelDecision.APPROVE:
        return Annotation(item_id=item_id, annotator=who, decision=decision, rationale="문제 없음", seconds_spent=30.0)
    rule = "FIN-GUARANTEE-01" if decision == LabelDecision.REJECT else "TA-URGENCY-01"
    return Annotation(item_id=item_id, annotator=who, decision=decision, rule_ids=[rule], cited_spans=[copy.headline], rationale="이유", seconds_spent=30.0)


def noisy(truth: LabelDecision, noise: float, rng: random.Random) -> LabelDecision:
    if rng.random() < noise:
        return rng.choice([d for d in LABELS if d != truth])
    return truth


@dataclass
class Sim:
    queue: ReviewQueue
    quality: dict
    kappa: dict
    final_accuracy: float
    n_adjudicated: int


def simulate(seed: int, noise: dict[str, float] = NOISE, *, gold_rate: float = 0.25) -> Sim:
    rng = random.Random(seed)
    q = ReviewQueue(required_labels=2, gold_rate=gold_rate, max_concurrent_claims=3, seed=f"sim{seed}", clock=FakeClock())
    truths: dict[str, LabelDecision] = {}
    for i in range(N_REAL):
        t = LABELS[rng.randrange(3)]
        it = q.enqueue_ad(make_ad(i, t), dataset_id=f"SIM-{seed}-{i}", category="loan", facts=[Fact(id="F1", text="x")], verdict=snap(round(rng.uniform(0.3, 0.9), 2)))
        truths[it.id] = t
    gold_rows = []
    for i in range(N_GOLD):
        t = LABELS[i % 3]
        c = make_ad(1000 + i, t)
        gold_rows.append({"id": f"SG-{i}", "category": "loan", "headline": c.headline, "body": c.body, "cta": c.cta, "label": t.value,
                          "rule_ids": [] if t == LabelDecision.APPROVE else ["FIN-GUARANTEE-01" if t == LabelDecision.REJECT else "TA-URGENCY-01"]})
    assert q.enqueue_gold(gold_rows) == N_GOLD
    names = list(noise)
    active = set(names)
    while active:
        for who in names:
            if who not in active:
                continue
            v = q.claim(who)
            if v is None:
                active.discard(who)
                continue
            it = q.get(v.item_id)
            truth = it.gold.decision if it.gold is not None else truths[it.id]
            q.submit(answer(it.id, who, it.ad_copy, noisy(truth, noise[who], rng)))
    n_adj = 0
    for it in q.disagreements():                       # 조정자는 항상 정답을 안다고 가정한다
        t = truths[it.id]
        q.adjudicate(AdjudicationRecord(item_id=it.id, adjudicator="조정자", decision=t, rule_ids=[] if t == LabelDecision.APPROVE else ["FIN-GUARANTEE-01" if t == LabelDecision.REJECT else "TA-URGENCY-01"],
                                        cited_spans=[] if t == LabelDecision.APPROVE else [it.ad_copy.headline], reason="시뮬레이션 조정: 정답 라벨로 확정"))
        n_adj += 1
    finals = q.final_labels()
    acc = sum(1 for i, f in finals.items() if f.decision == truths[i]) / max(1, len(finals))
    rep = agreement_report(q.annotations())
    kappa = {(p.annotator_a, p.annotator_b): p.result.kappa for p in rep.pairs}
    return Sim(queue=q, quality={x.annotator: x for x in q.quality()}, kappa=kappa, final_accuracy=acc, n_adjudicated=n_adj)


def test_one_simulation_is_reproducible_from_its_seed():
    a, b = simulate(5), simulate(5)
    assert {k: (v.n_gold, v.gold_correct) for k, v in a.quality.items()} == {k: (v.n_gold, v.gold_correct) for k, v in b.quality.items()}
    assert a.kappa == b.kappa


def test_gold_items_reach_every_labeler_without_repeats_or_leaks_to_the_dataset():
    s = simulate(1)
    for name, qa in s.quality.items():
        assert qa.n_gold >= 5, (name, qa.n_gold)                      # 판단하기에 충분한 검증용 항목이 나갔다
        assert qa.n_gold <= N_GOLD
    assert s.queue.stats().total == N_REAL                            # 검증용 항목은 통계에 안 잡힌다
    assert all(i.synthetic is False for i in s.queue.items())


def test_the_pipeline_catches_careless_labelers_and_does_not_flag_accurate_ones():
    """40개 시드를 돌려 라벨러마다 '주의' 로 표시된 비율을 잰다."""
    seeds = range(40)
    watch = {n: 0 for n in NOISE}
    confirmed = {n: 0 for n in NOISE}
    acc = {n: [] for n in NOISE}
    gold_n = []
    for sd in seeds:
        s = simulate(sd)
        for n, qa in s.quality.items():
            watch[n] += qa.status == "watch"
            confirmed[n] += qa.confirmed_low
            acc[n].append(qa.gold_accuracy)
            gold_n.append(qa.n_gold)
    rate = {n: watch[n] / len(seeds) for n in NOISE}
    print(f"\n[시뮬레이션] 시드 40개 · 실제 항목 60건 · 검증용 풀 12건 · 검증용 비율 25% · 검증용 항목 수(라벨러당) 평균 {sum(gold_n) / len(gold_n):.1f}")
    for n in NOISE:
        print(f"  {n} (노이즈 {NOISE[n]:.0%}): 검증용 정확도 평균 {sum(acc[n]) / len(acc[n]):.2f} · '주의' 표시 {rate[n]:.0%} · 신뢰구간까지 기준 미만 {confirmed[n] / len(seeds):.0%}")
    assert rate["정확한 라벨러"] == 0.0                                  # 노이즈 0 이면 검증용 정확도가 1.0 이라 한 번도 표시되지 않는다
    assert rate["보통 라벨러"] <= 0.30                                   # 10% 노이즈는 가끔 표시될 수 있다 (검증용 항목이 적으면 우연히 기준 아래로 내려감)
    assert rate["부주의한 라벨러"] >= 0.90
    assert rate["무작위 라벨러"] >= 0.90
    assert min(rate["부주의한 라벨러"], rate["무작위 라벨러"]) > rate["보통 라벨러"] + 0.5          # 품질이 낮은 라벨러가 훨씬 자주 잡힌다
    mean = {n: sum(acc[n]) / len(acc[n]) for n in NOISE}
    assert mean["정확한 라벨러"] == 1.0 and mean["정확한 라벨러"] > mean["보통 라벨러"] > mean["부주의한 라벨러"] > mean["무작위 라벨러"]
    assert confirmed["정확한 라벨러"] == 0 and confirmed["무작위 라벨러"] / len(seeds) >= 0.5


def test_noise_shows_up_in_agreement_and_in_deviation_from_the_final_label():
    kappas = {"clean": [], "noisy": []}
    dev = {n: [] for n in NOISE}
    for sd in range(20):
        s = simulate(sd)
        kappas["clean"].append(s.kappa[("보통 라벨러", "정확한 라벨러")] if ("보통 라벨러", "정확한 라벨러") in s.kappa else s.kappa[("정확한 라벨러", "보통 라벨러")])
        key = ("무작위 라벨러", "정확한 라벨러") if ("무작위 라벨러", "정확한 라벨러") in s.kappa else ("정확한 라벨러", "무작위 라벨러")
        kappas["noisy"].append(s.kappa[key])
        for n, qa in s.quality.items():
            dev[n].append(qa.deviation_rate)
    mk = {k: sum(v) / len(v) for k, v in kappas.items() if all(x is not None for x in v)}
    md = {n: sum(v) / len(v) for n, v in dev.items()}
    print(f"\n[시뮬레이션] 짝별 kappa 평균: 정확한×보통 {mk['clean']:.2f} · 정확한×무작위 {mk['noisy']:.2f} / 확정 라벨과 다른 비율 평균: " + " · ".join(f"{n} {v:.2f}" for n, v in md.items()))
    assert mk["clean"] > mk["noisy"] + 0.3
    assert md["정확한 라벨러"] < md["보통 라벨러"] < md["부주의한 라벨러"] < md["무작위 라벨러"]


def test_agreement_between_labelers_is_not_the_same_as_being_right():
    """조정자가 정답을 아는 가정에서도 최종 라벨이 100% 맞지는 않는다 — 두 라벨러가 같은 오답을 내면 일치로 확정되기 때문이다 (일치도가 정확도가 아닌 이유)."""
    accs = []
    for sd in range(10):
        s = simulate(sd)
        assert s.n_adjudicated > 0
        assert all(i.state in (ItemState.LABELED, ItemState.ADJUDICATED) for i in s.queue.items())      # 모든 실제 항목이 두 명씩 라벨돼 확정됐다
        accs.append(s.final_accuracy)
    print(f"\n[시뮬레이션] 조정자가 항상 정답을 아는 가정에서 최종 라벨 정확도: 시드별 {min(accs):.2f} ~ {max(accs):.2f}, 평균 {sum(accs) / len(accs):.3f}")
    assert 0.9 <= min(accs) and min(accs) < 1.0 and sum(accs) / len(accs) < 1.0       # 어느 시드에선가는 일치한 오답이 최종 라벨에 남는다


def test_without_gold_items_the_pipeline_cannot_tell_a_careless_labeler_from_a_clean_one():
    """검증용 항목 없이 합의 이탈만 보면 같은 두 사람이 서로 갈릴 때 누가 틀렸는지 알 수 없다 — 검증용 항목이 필요한 이유."""
    s = simulate(2, {"정확한 라벨러": 0.0, "부주의한 라벨러": 0.40}, gold_rate=0.0)
    for qa in s.quality.values():
        assert qa.n_gold == 0 and qa.status == "insufficient"
