"""소재 퀄리티 신호가 쓸 만한지 재는 평가 — 사람(작성자) 선호 쌍과의 일치율.

실행: python -m evals.quality_signals [--split dev|holdout|all] [--out evals/reports/quality_signals.json] [--gate] [--baseline 이전_리포트.json]
데이터: evals/datasets/quality_pairs.jsonl (같은 상품 정보로 쓴 두 문구 a·b 와 작성자의 선호 라벨 a | b | tie)

무엇을 재나
- 8개 신호의 가중 평균(core.creative_quality.compare_cards)이 쌍마다 고른 쪽이 작성자가 고른 쪽과 같은 비율(쌍대 일치율), 신호 하나만 썼을 때의 일치율,
  신호를 하나씩 뺐을 때의 변화(ablation), 이유 코드별 일치율, 틀린 쌍의 목록. 모델은 쓰지 않는다 — 같은 입력이면 같은 숫자가 나온다.
- 무승부 처리: 라벨이 tie 인 쌍은 예측도 tie(두 문구의 종합 점수 차 < TIE_MARGIN)일 때만 맞다. 라벨이 a/b 인 쌍에서 예측이 tie 이면 일치로 세지 않는다(보수적).
  그래서 '결정 쌍 일치율'(라벨이 a/b 인 쌍만)과 '3분류 정확도'(tie 포함 전체)를 따로 적고, 예측이 tie 가 아닌 쌍만 본 일치율과 그 비율(coverage)도 함께 적는다.
- 우연 수준: 라벨이 a/b 인 쌍에서 늘 한쪽만 고르는 방식의 일치율(a/b 위치는 id 해시로 정해 약 절반씩)과 동전 던지기 0.5 중 큰 값.

이 숫자가 의미하지 않는 것 (리포트에도 그대로 적는다)
- 실제 클릭률·전환율·광고주 만족과의 상관이 아니다. 그 상관은 측정하지 않았다(실제 성과 데이터가 없다).
- 라벨은 이 프로젝트의 작성자(AI) 한 명이 달았다. 독립 라벨러가 없고, 신호를 만든 사람이 라벨도 달았으므로 같은 생각이 두 곳에 들어 있다.
- 문구와 상품 정보는 전부 가상이다. 실제 광고의 분포를 대표하지 않는다.
- dev 일치율은 신호·가중치를 만들 때 본 데이터에서 나온 낙관적인 숫자다. 일반화는 holdout 으로만 본다 — 그 holdout 도 같은 작성자의 라벨이라 독립 검증이 아니다.
- 정책 준수는 이 평가가 보지 않는다(심사가 따로 잰다). '과장됐지만 구체적' 쌍에는 위반 여부와 무관하게 문구 전달력만 본 라벨이 있다.

절차 (엄수 — PROCEDURE 에 기록하고 tests/test_creative_quality_eval.py 가 지킨다)
① dev 쌍을 먼저 쓰고 신호·가중치를 만든다 ② 신호 코드를 동결한다(해시 기록) ③ 그 뒤에 holdout 쌍을 쓴다 ④ holdout 결과를 보고 신호·가중치를 고치지 않는다. 나쁜 결과는 나쁜 그대로 보고한다.
동결 뒤에 신호 코드가 바뀌면 해시가 달라져 테스트가 실패하고, 리포트의 procedure.checks 가 이를 알린다. 바꿔야 한다면 PROCEDURE['amendments'] 에 이유를 적는다.

judge.py(LLM 쌍대 비교)와의 관계: 이 모듈은 모델 없이 재현되는 규칙 신호이고, evals/judge.py 는 모델 판단이다. 둘의 일치도는 재지 않는다 — 모델 없이 도는 대역(mock)의 판정은
모델 성능이 아니라 고정된 응답이어서 일치도가 아무것도 말해 주지 않기 때문이다. 실제 모델을 돌릴 수 있게 되면 그때 같은 쌍으로 judge.compare 와 비교한다(지금은 하지 않았다).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from core import creative_quality as cq
from core.schemas import AdCopy, Fact
from evals.metrics import wilson

HERE = Path(__file__).parent
PAIRS_PATH = HERE / "datasets" / "quality_pairs.jsonl"
DEFAULT_REPORT = HERE / "reports" / "quality_signals.json"
SPLITS = ("dev", "holdout")
LABELS = ("a", "b", "tie")
MARGIN_SWEEP = (0.0, 0.02, 0.05, 0.08, 0.12)     # 참고용 — 무승부 기준을 바꾸면 숫자가 어떻게 달라지는지 보일 뿐, 기준을 고르는 데 쓰지 않았다
MAX_DROP_VS_BASELINE = 0.05                      # 기준선 리포트보다 결정 쌍 일치율이 이만큼(5%p) 넘게 떨어지면 게이트 실패 — 쌍 수십 개 규모에서 우연히 흔들리는 크기보다 크게 잡았다
MIN_PAIRS_FOR_GATE = 10                          # 결정 쌍이 이보다 적으면 우연 수준 비교가 무의미해 게이트에서 판정하지 않는다

# ---------------------------------------------------------------------------
# 절차 기록 — 동결한 것과 그 시점
# ---------------------------------------------------------------------------
PROCEDURE: dict[str, Any] = {
    "steps": [
        "1. dev 56쌍을 먼저 썼다 (신호를 만들기 전. 라벨은 작성자 판단이며 신호를 돌려 보지 않았다)",
        "2. dev 쌍으로 신호·가중치·무승부 기준을 만들었다 (그래서 dev 일치율은 낙관적이다)",
        "3. 신호 코드(core/creative_quality.py)를 동결했다 — frozen 의 해시",
        "4. 동결한 뒤에 holdout 쌍을 썼다 (신호를 한 번도 돌려 보지 않은 채 전부 쓴 뒤 처음 돌렸다)",
        "5. holdout 결과를 보고 신호·가중치·무승부 기준을 고치지 않았다 — first_holdout_run 이 처음 돌린 숫자다",
    ],
    "frozen_on": "2026-10-19",
    "frozen": {
        "core_creative_quality_sha256": "308df411f3adaa4c5810f58d4a8db4c46494aa0ccec2b5c2106b746006564e26",
        "dev_pairs_sha256": "4bca862e252dc0023d3a674df40f8d7d52e49924bfb965ef16c28cc8f0dd5bc8",
        "n_dev_pairs": 56,
        "weights": dict(cq.WEIGHTS),
        "tie_margin": cq.TIE_MARGIN,
    },
    "holdout_pairs_sha256": "8fc1843211bbc25d6d292054befa4bf90e2669a9edbc2cfe81211b70a0d4624d",   # holdout 28쌍을 다 쓴 직후(처음 돌리기 전) 기록
    "first_holdout_run": {                 # holdout 을 처음 돌린 결과 — 기록한 뒤 바꾸지 않는다. 틀린 쌍은 길이만 다른 쌍 둘(QP-H15·QP-H16: 길이 신호의 가중치가 작아 종합 점수 차가 무승부 기준 아래)
        "decisive_agreement": {"k": 22, "n": 24, "rate": 0.9167},
        "three_way_accuracy": {"k": 26, "n": 28, "rate": 0.9286},
        "tie_pairs_predicted_tie": {"k": 4, "n": 4},
        "wrong_pairs": ["QP-H15", "QP-H16"],
    },
    "amendments": [],                      # 동결 뒤 신호 코드를 바꿨다면 {"date", "reason", "before_sha256", "after_sha256"} 를 적는다
}

LIMITATIONS = [
    "라벨은 작성자(AI) 한 명이 달았다. 독립 라벨러가 없고, 신호를 만든 사람이 라벨도 달았다.",
    "문구와 상품 정보는 전부 가상이다. 실제 광고의 분포를 대표하지 않는다.",
    "실제 클릭률·전환·광고주 만족과의 상관은 측정하지 않았다. 일치율이 높아도 '성과가 좋다'는 뜻이 아니다.",
    "dev 일치율은 신호를 만들 때 본 데이터라 낙관적이다. 일반화는 holdout 으로만 보며, holdout 도 같은 작성자의 라벨이다.",
    "쌍 수가 적다(dev 56 · holdout 28). 신뢰구간이 넓어 몇 %p 차이는 우연과 구별되지 않는다.",
    "쌍은 대부분 한 가지 차이가 두드러지게 만든 것이라 쉬운 편이다. 여러 차이가 뒤섞인 실제 광고 쌍의 일치율은 이보다 낮을 가능성이 높다(측정하지 않았다). 단순 방식(숫자가 많은 쪽·긴 쪽)이 더 낮게 나온 것이 유일한 비교 근거다.",
    "신호의 단어 목록을 만든 사람이 쌍도 썼다. 쌍이 목록의 말('자세히 보기'·'프로모션'·'서두르세요' 등)을 직접 겨냥한 경우가 있어, holdout 일치율은 '작성자 자신의 기준을 신호가 재현하는 정도'에 가깝다.",
    "가중치가 작은 신호(길이·말투)만 다른 쌍은 종합 점수 차가 무승부 기준(0.05) 아래로 떨어져 tie 로 예측되기 쉽다 — holdout 에서 길이만 다른 두 쌍을 놓쳤다.",
    "조건 표시는 코드가 비교하는 조건 6종류의 표준 표현만 인정한다 — '30만원 쓰면' 같은 풀어 쓴 조건은 빠진 것으로 본다.",
    "형태소 분석기·사전 없이 정규식과 단어 목록으로 읽는다. 어미·한자어·명사 나열을 정확히 가리지 못한다.",
]


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Pair:
    id: str
    split: str
    facts: tuple[Fact, ...]
    categories: frozenset
    a: dict
    b: dict
    better: str
    reason_code: str
    notes: str = ""

    def copy(self, side: str) -> AdCopy:
        d = self.a if side == "a" else self.b
        return AdCopy(headline=d["headline"], body=d["body"], cta=d["cta"])


def _raw_rows(path: Path = PAIRS_PATH) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _validate(r: dict) -> None:
    for k in ("id", "split", "facts", "categories", "a", "b", "better", "reason_code"):
        if k not in r:
            raise ValueError(f"{r.get('id', '?')}: '{k}' 이(가) 없습니다")
    if r["split"] not in SPLITS:
        raise ValueError(f"{r['id']}: split 은 dev | holdout 이어야 합니다 ({r['split']!r})")
    if r["better"] not in LABELS:
        raise ValueError(f"{r['id']}: better 는 a | b | tie 이어야 합니다 ({r['better']!r})")
    for side in ("a", "b"):
        if set(r[side]) != {"headline", "body", "cta"} or not all(isinstance(v, str) for v in r[side].values()):
            raise ValueError(f"{r['id']}: {side} 는 headline·body·cta 문자열이어야 합니다")
    if not r["facts"] or not all({"id", "text"} <= set(f) for f in r["facts"]):
        raise ValueError(f"{r['id']}: facts 는 {{id, text}} 목록이어야 합니다")


def load_pairs(path: Path = PAIRS_PATH, split: str = "all") -> list[Pair]:
    if split not in (*SPLITS, "all"):
        raise ValueError(f"split 은 dev | holdout | all 이어야 합니다 ({split!r})")
    out: list[Pair] = []
    seen: set[str] = set()
    for r in _raw_rows(path):
        _validate(r)
        if r["id"] in seen:
            raise ValueError(f"중복 id: {r['id']}")
        seen.add(r["id"])
        if split in ("all", r["split"]):
            out.append(Pair(r["id"], r["split"], tuple(Fact(**f) for f in r["facts"]), frozenset(r["categories"]), dict(r["a"]), dict(r["b"]), r["better"], r["reason_code"], r.get("notes", "")))
    return out


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rows_sha256(split: str, path: Path = PAIRS_PATH) -> str:
    """한 split 의 쌍 전체의 해시 (id 순서, 키 순서 고정) — 쌍이 나중에 몰래 바뀌지 않았는지 확인한다."""
    rows = sorted((r for r in _raw_rows(path) if r["split"] == split), key=lambda r: r["id"])
    return _sha256_text("\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows))


def signal_code_sha256() -> str:
    return hashlib.sha256(Path(cq.__file__).read_bytes()).hexdigest()


def procedure_checks(path: Path = PAIRS_PATH) -> dict:
    fz = PROCEDURE["frozen"]
    return {
        "signal_code_unchanged_since_freeze": signal_code_sha256() == fz["core_creative_quality_sha256"],
        "weights_unchanged_since_freeze": dict(cq.WEIGHTS) == fz["weights"],
        "tie_margin_unchanged_since_freeze": cq.TIE_MARGIN == fz["tie_margin"],
        "dev_pairs_unchanged_since_freeze": rows_sha256("dev", path) == fz["dev_pairs_sha256"],
        "holdout_pairs_unchanged_since_first_run": rows_sha256("holdout", path) == PROCEDURE["holdout_pairs_sha256"],
        "amendments": len(PROCEDURE["amendments"]),
    }


# ---------------------------------------------------------------------------
# 점수 매기기와 지표
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Scored:
    pair: Pair
    card_a: cq.QualityCard
    card_b: cq.QualityCard


def score_pairs(pairs: list[Pair], platform: cq.PlatformLimits = cq.ASSUMED_LIMITS) -> list[Scored]:
    out = []
    for p in pairs:
        cats = set(p.categories)
        out.append(Scored(p, cq.quality_card(p.copy("a"), list(p.facts), cats, platform=platform), cq.quality_card(p.copy("b"), list(p.facts), cats, platform=platform)))
    return out


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def predict(s: Scored, *, margin: float = cq.TIE_MARGIN, exclude: Any = (), weights: Optional[dict] = None) -> cq.Comparison:
    return cq.compare_cards(s.card_a, s.card_b, exclude=exclude, margin=margin, weights=weights)


def _chars(copy: AdCopy) -> int:
    return len(copy.headline) + len(copy.body) + len(copy.cta)


def _digits(copy: AdCopy) -> int:
    return sum(ch.isdigit() for ch in copy.headline + copy.body + copy.cta)


def evaluate(scored: list[Scored], *, margin: float = cq.TIE_MARGIN, exclude: Any = ()) -> dict:
    """한 묶음의 쌍대 일치 지표 (전체 신호 또는 exclude 를 뺀 신호로)."""
    preds = [predict(s, margin=margin, exclude=exclude) for s in scored]
    dec = [(s, c) for s, c in zip(scored, preds) if s.pair.better != "tie"]
    ties = [(s, c) for s, c in zip(scored, preds) if s.pair.better == "tie"]
    k_dec = sum(c.winner == s.pair.better for s, c in dec)
    decided = [(s, c) for s, c in dec if c.winner in ("a", "b")]
    k_decided = sum(c.winner == s.pair.better for s, c in decided)
    k_three = sum(c.winner == s.pair.better for s, c in zip(scored, preds))
    n_a = sum(s.pair.better == "a" for s, _ in dec)
    majority = max(n_a, len(dec) - n_a) / len(dec) if dec else None
    return {
        "n": len(scored), "n_decisive_labels": len(dec), "n_tie_labels": len(ties),
        "predicted": {w: sum(c.winner == w for c in preds) for w in ("a", "b", "tie", "unknown")},
        "decisive_agreement": _rate(k_dec, len(dec)),
        "decisive_agreement_when_decided": {**_rate(k_decided, len(decided)), "coverage": round(len(decided) / len(dec), 4) if dec else None},
        "tie_pairs": {"n": len(ties), "predicted_tie": sum(c.winner == "tie" for _, c in ties), "separated_anyway": sum(c.winner in ("a", "b") for _, c in ties)},
        "three_way_accuracy": _rate(k_three, len(scored)),
        "chance": {"coin_flip": 0.5, "always_one_side": None if majority is None else round(majority, 4), "level": None if majority is None else round(max(0.5, majority), 4)},
    }


def by_reason(scored: list[Scored], margin: float = cq.TIE_MARGIN) -> dict:
    out: dict[str, dict] = {}
    for s in scored:
        c = predict(s, margin=margin)
        d = out.setdefault(s.pair.reason_code, {"n": 0, "k": 0})
        d["n"] += 1
        d["k"] += c.winner == s.pair.better
    return {k: _rate(v["k"], v["n"]) for k, v in sorted(out.items())}


SIGNAL_REASON = {"length": "length", "specificity": "specificity", "conditions": "conditions", "cta": "cta", "readability": "readability", "redundancy": "redundancy", "pressure": "hype", "tone": "tone"}   # 신호 ↔ 그 신호가 가리키는 이유 코드


def by_signal(scored: list[Scored], margin: float = cq.TIE_MARGIN) -> dict:
    """신호 하나만으로 쌍을 가렸을 때. 한쪽이라도 계산 불가면 의견 없음(abstain), 점수 차 < margin 이면 tie(의견 없음 아님)."""
    out = {}
    for key in cq.SIGNAL_KEYS:
        dec = [s for s in scored if s.pair.better != "tie"]
        calls = [(s, cq.compare_signal(s.card_a, s.card_b, key, margin=margin)) for s in dec]
        decided = [(s, w) for s, w in calls if w in ("a", "b")]
        k = sum(w == s.pair.better for s, w in decided)
        k_miss = sum(w == s.pair.better for s, w in calls)      # tie·의견 없음을 틀린 것으로 센 값
        own = [(s, w) for s, w in calls if s.pair.reason_code == SIGNAL_REASON[key]]
        own_decided = [(s, w) for s, w in own if w in ("a", "b")]
        tie_calls = [cq.compare_signal(s.card_a, s.card_b, key, margin=margin) for s in scored if s.pair.better == "tie"]
        out[key] = {"name": cq.SIGNAL_NAME[key], "weight": cq.WEIGHTS[key],
                    "decisive_pairs": len(dec), "decided": len(decided), "abstained": sum(w is None for _, w in calls), "called_tie": sum(w == "tie" for _, w in calls),
                    "agreement_when_decided": _rate(k, len(decided)), "agreement_all_decisive_pairs": _rate(k_miss, len(dec)),
                    "on_own_reason": {"reason_code": SIGNAL_REASON[key], "pairs": len(own), "decided": len(own_decided), **_rate(sum(w == s.pair.better for s, w in own_decided), len(own_decided))},
                    "on_tie_pairs": {"n": len(tie_calls), "called_tie": sum(w == "tie" for w in tie_calls), "separated": sum(w in ("a", "b") for w in tie_calls), "abstained": sum(w is None for w in tie_calls)}}
    return out


ABLATION_NOTE = ("신호를 빼면 남은 신호의 가중치가 다시 합 1 이 되어 점수 차가 커진다 — 무승부 기준(고정) 근처의 쌍이 a/b 로 갈리는 것은 신호의 정보가 아니라 척도 변화일 수 있다. "
                 "delta 가 음수면 그 신호를 뺄 때 일치율이 떨어졌다(보탬이 됐다)는 뜻이고, 양수면 빼는 편이 일치율이 높았다는 뜻이다. 쌍 수십 개라 한두 쌍 차이는 우연이다.")


def ablation(scored: list[Scored], margin: float = cq.TIE_MARGIN) -> dict:
    """신호를 하나씩 뺐을 때(가중치는 다시 합 1) 일치율 변화 (해석 주의는 ABLATION_NOTE)."""
    full = evaluate(scored, margin=margin)
    base_pred = [predict(s, margin=margin).winner for s in scored]
    out = {}
    for key in cq.SIGNAL_KEYS:
        ev = evaluate(scored, margin=margin, exclude={key})
        changed = sum(predict(s, margin=margin, exclude={key}).winner != w for s, w in zip(scored, base_pred))
        d_dec = (ev["decisive_agreement"]["rate"] or 0) - (full["decisive_agreement"]["rate"] or 0)
        d_three = (ev["three_way_accuracy"]["rate"] or 0) - (full["three_way_accuracy"]["rate"] or 0)
        out[key] = {"name": cq.SIGNAL_NAME[key], "decisive_agreement": ev["decisive_agreement"]["rate"], "three_way_accuracy": ev["three_way_accuracy"]["rate"],
                    "delta_decisive": round(d_dec, 4), "delta_three_way": round(d_three, 4), "predictions_changed": changed}
    return out


def baselines(scored: list[Scored]) -> dict:
    """신호 없이 쓸 수 있는 단순한 방식과의 비교 — 점수가 '그냥 긴 쪽' 같은 겉모습을 따라가는 건 아닌지 본다. 결정 쌍 일치율(동점은 틀린 것으로 센다)."""
    dec = [s for s in scored if s.pair.better != "tie"]
    if not dec:
        return {}

    def agree(pick) -> dict:
        return _rate(sum(pick(s) == s.pair.better for s in dec), len(dec))

    def longer(s: Scored) -> str:
        la, lb = _chars(s.pair.copy("a")), _chars(s.pair.copy("b"))
        return "tie" if la == lb else ("a" if la > lb else "b")

    def more_digits(s: Scored) -> str:
        da, db = _digits(s.pair.copy("a")), _digits(s.pair.copy("b"))
        return "tie" if da == db else ("a" if da > db else "b")

    return {"always_a": agree(lambda s: "a"), "longer_text_wins": agree(longer), "more_digits_wins": agree(more_digits)}


def margin_sweep(scored: list[Scored]) -> dict:
    return {f"{m:.2f}": {"decisive_agreement": evaluate(scored, margin=m)["decisive_agreement"]["rate"], "three_way_accuracy": evaluate(scored, margin=m)["three_way_accuracy"]["rate"]} for m in MARGIN_SWEEP}


def failures(scored: list[Scored], margin: float = cq.TIE_MARGIN) -> list[dict]:
    out = []
    for s in scored:
        c = predict(s, margin=margin)
        if c.winner == s.pair.better:
            continue
        out.append({"id": s.pair.id, "split": s.pair.split, "reason_code": s.pair.reason_code, "label": s.pair.better, "predicted": c.winner,
                    "score_a": None if c.score_a is None else round(c.score_a, 3), "score_b": None if c.score_b is None else round(c.score_b, 3),
                    "top_signals": [[k, round(v, 3)] for k, v in c.reasons[:3]], "a": s.pair.a, "b": s.pair.b, "notes": s.pair.notes})
    return out


def analyze(scored: list[Scored]) -> dict:
    return {**evaluate(scored), "by_reason": by_reason(scored), "by_signal": by_signal(scored), "ablation": ablation(scored), "baselines": baselines(scored),
            "ablation_note": ABLATION_NOTE, "margin_sweep_info_only": margin_sweep(scored), "failures": failures(scored)}


# ---------------------------------------------------------------------------
# 리포트·게이트
# ---------------------------------------------------------------------------
def build_report(path: Path = PAIRS_PATH, split: str = "all") -> dict:
    pairs = load_pairs(path, "all")
    scored = score_pairs(pairs)
    parts = {"dev": [s for s in scored if s.pair.split == "dev"], "holdout": [s for s in scored if s.pair.split == "holdout"], "all": scored}
    wanted = ("dev", "holdout", "all") if split == "all" else (split,)
    counts = {sp: {"pairs": len(parts[sp]), "labels": {lb: sum(s.pair.better == lb for s in parts[sp]) for lb in LABELS}} for sp in ("dev", "holdout", "all")}
    reasons: dict[str, dict] = {}
    for s in scored:
        reasons.setdefault(s.pair.reason_code, {"dev": 0, "holdout": 0})[s.pair.split] += 1
    rep = {
        "schema": "quality_signals/1",
        "title": "소재 퀄리티 신호 — 작성자 선호 쌍과의 일치율",
        "reproduce": "python -m evals.quality_signals --split all --out evals/reports/quality_signals.json",
        "no_independent_labeler": "라벨은 이 프로젝트의 작성자(AI) 한 명이 달았다 — 독립 라벨러 없음. 문구·상품 정보는 전부 가상.",
        "not_measured": "실제 클릭률·전환·광고주 만족과의 상관은 측정하지 않았다 (실제 성과 데이터 없음). 이 일치율은 '작성자가 고른 쪽을 신호가 따라가는 정도'일 뿐이다.",
        "tie_handling": "라벨 tie 는 예측도 tie(종합 점수 차 < 무승부 기준)일 때만 맞다. 라벨 a/b 인 쌍에서 예측 tie 는 일치로 세지 않는다. '예측이 tie 가 아닌 쌍만 본 일치율'과 그 비율도 따로 적는다.",
        "dataset": {"path": "evals/datasets/quality_pairs.jsonl", "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "counts": counts, "reason_codes": dict(sorted(reasons.items()))},
        "signals": {"keys": list(cq.SIGNAL_KEYS), "weights": dict(cq.WEIGHTS), "tie_margin": cq.TIE_MARGIN, "grade_bands": [list(b) for b in cq.GRADE_BANDS],
                    "assumed_limits": asdict(cq.ASSUMED_LIMITS), "assumed_limits_note": "이 프로젝트가 가정한 값 — 실제 콘솔 규격과 다를 수 있다", "core_sha256": signal_code_sha256()},
        "procedure": {**{k: v for k, v in PROCEDURE.items()}, "checks": procedure_checks(path)},
        "splits": {sp: analyze(parts[sp]) for sp in wanted if parts[sp]},
        "limitations": list(LIMITATIONS),
    }
    rep["gate"] = {"failures": gate(rep), "rule": gate_rule()}
    return rep


def gate_rule() -> str:
    return (f"결정 쌍(라벨 a/b) {MIN_PAIRS_FOR_GATE}개 이상인 split 마다 결정 쌍 일치율이 우연 수준(늘 한쪽만 고르는 일치율과 0.5 중 큰 값)을 넘어야 한다 "
            f"— 우연 이하로 떨어지면 신호가 선호를 따라가지 못한다는 뜻이라 실패. 이 기준은 일부러 느슨하다(쌍 수십 개로는 몇 %p 차이를 우연과 가를 수 없다). "
            f"기준선 리포트를 주면 결정 쌍 일치율이 {MAX_DROP_VS_BASELINE:.0%}p 넘게 떨어질 때도 실패.")


def gate(report: dict, baseline: Optional[dict] = None) -> list[str]:
    """실패 사유 목록 (비어 있으면 통과)."""
    out: list[str] = []
    for sp, m in report.get("splits", {}).items():
        if sp == "all":
            continue
        n = m["decisive_agreement"]["n"]
        rate = m["decisive_agreement"]["rate"]
        level = m["chance"]["level"]
        if n >= MIN_PAIRS_FOR_GATE and rate is not None and level is not None and rate <= level:
            out.append(f"{sp}: 결정 쌍 일치율 {rate:.1%} 이(가) 우연 수준 {level:.1%} 이하 (n={n})")
        if baseline:
            prev = baseline.get("splits", {}).get(sp, {}).get("decisive_agreement", {}).get("rate")
            if prev is not None and rate is not None and prev - rate > MAX_DROP_VS_BASELINE:
                out.append(f"{sp}: 결정 쌍 일치율이 기준선 {prev:.1%} 에서 {rate:.1%} 로 {prev - rate:.1%}p 떨어짐 (허용 {MAX_DROP_VS_BASELINE:.0%}p)")
    return out


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.1%}"


def format_summary(rep: dict) -> str:
    lines = [f"== 소재 퀄리티 신호 평가 ({rep['dataset']['counts']['all']['pairs']}쌍, 가중치 합 1, 무승부 기준 {rep['signals']['tie_margin']})",
             "   라벨: 작성자(AI) 한 명 · 독립 라벨러 없음 · 문구 전부 가상 · 클릭률/전환과의 상관은 측정하지 않음"]
    for sp, m in rep["splits"].items():
        d, dd, t3 = m["decisive_agreement"], m["decisive_agreement_when_decided"], m["three_way_accuracy"]
        lines.append(f"[{sp}] 쌍 {m['n']} (결정 {m['n_decisive_labels']} · 무승부 {m['n_tie_labels']})  우연 수준 {_pct(m['chance']['level'])}")
        lines.append(f"   결정 쌍 일치율 {d['k']}/{d['n']} = {_pct(d['rate'])}  95% 구간 {d['wilson95']}   (예측 tie 를 뺀 일치율 {dd['k']}/{dd['n']} = {_pct(dd['rate'])}, 비율 {_pct(dd['coverage'])})")
        lines.append(f"   3분류 정확도 {t3['k']}/{t3['n']} = {_pct(t3['rate'])}   무승부 쌍 중 tie 로 예측 {m['tie_pairs']['predicted_tie']}/{m['tie_pairs']['n']}")
        b = m["baselines"]
        lines.append("   단순 방식 — " + " · ".join(f"{k} {_pct(v['rate'])}" for k, v in b.items()))
        lines.append("   신호 하나만(의견 낸 쌍 기준) — " + " · ".join(f"{v['name']} {_pct(v['agreement_when_decided']['rate'])}({v['decided']})" for v in m["by_signal"].values()))
        lines.append("   신호 하나만, 자기 이유 코드 쌍에서 — " + " · ".join(f"{v['name']} {v['on_own_reason']['k']}/{v['on_own_reason']['n']}" for v in m["by_signal"].values()))
        lines.append("   하나씩 뺐을 때 결정 일치율 변화 — " + " · ".join(f"{v['name']} {v['delta_decisive']:+.1%}" for v in m["ablation"].values()))
        lines.append("   이유 코드별 — " + " · ".join(f"{k} {v['k']}/{v['n']}" for k, v in m["by_reason"].items()))
        lines.append(f"   틀린 쌍 {len(m['failures'])}개: " + ", ".join(f['id'] for f in m["failures"]))
    chk = rep["procedure"]["checks"]
    if not all(v for k, v in chk.items() if k != "amendments"):
        lines.append("   ! 절차 점검: " + ", ".join(k for k, v in chk.items() if k != "amendments" and not v) + " — 동결 뒤 바뀐 것이 있다(리포트 procedure 참조)")
    lines.append("   게이트: " + ("통과" if not rep["gate"]["failures"] else "실패 — " + "; ".join(rep["gate"]["failures"])))
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="소재 퀄리티 신호 평가 (모델·네트워크 없음)")
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    ap.add_argument("--out", default=None, help="리포트를 저장할 경로 (예: evals/reports/quality_signals.json). 없으면 화면에만 낸다")
    ap.add_argument("--pairs", default=str(PAIRS_PATH), help="쌍 데이터 경로")
    ap.add_argument("--gate", action="store_true", help="게이트에 걸리면 종료 코드 1")
    ap.add_argument("--baseline", default=None, help="이전 리포트 경로 — 일치율이 5%%p 넘게 떨어지면 게이트 실패")
    args = ap.parse_args(argv)
    rep = build_report(Path(args.pairs), args.split)
    if args.baseline:
        base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        rep["gate"]["failures"] = gate(rep, base)
    print(format_summary(rep))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"리포트 저장: {out}")
    return 1 if (args.gate and rep["gate"]["failures"]) else 0


if __name__ == "__main__":
    sys.exit(main())
