"""사전 라벨 — 사람이 라벨하기 전에 자동으로 채워 두는 제안. 확정이 아니다.

    python -m ops.prelabel --eval          # 업종 분류기를 기존 평가 데이터셋의 category 라벨로 채점 (정확도·혼동)

두 가지를 한다.
1. `classify_category(text, facts)`: 결정론 키워드 분류기. 금융 업종 단서는 `guardrails/policy.yaml` 의 category_cues(= 입력 검사가 쓰는 추론)를 그대로 재사용하고,
   그 밖의 업종(카드·커머스·앱·교육·건강·부동산)은 이 모듈이 단서를 더한다. 모델을 부르지 않는다.
2. `prelabel(item)`: 파이프라인의 판정을 '제안 라벨' 로 미리 채운다. **자동 확정은 없다** — `Prelabel` 은 `Annotation` 이 아니고, 큐는 `Annotation` 만 받는다.
   사람이 제안을 보고 판단하면 제안에 끌려가는 편향(앵커링)이 생길 수 있어 '사전 라벨 숨김' 을 설정으로 둔다. 라벨마다 숨김/표시를 기록하고
   `anchoring_report` 가 두 집단이 제안과 같은 판정을 낸 비율을 비교한다.

이 모듈의 숫자가 의미하지 않는 것
- 업종 분류 정확도는 '이 데이터셋의 category 칸(저자가 단 선언 업종)을 얼마나 맞히는가' 다. 데이터셋 대부분이 한 저자의 가상 사례이고 커머스 사례가 많아(conditions_extra 174건 중 166건)
  전체 정확도가 분포에 크게 좌우된다 — 데이터셋별 값과 최빈 업종만 찍는 기준선을 함께 본다. 실제 광고주 데이터의 정확도가 아니다.
- 업종 단서는 개발용 데이터셋을 보며 한 번 손봤다(`LEXICON_VERSION` 참고). 개발용 정확도는 그만큼 낙관적이고, 최종 확인용(holdout)은 단서를 고정한 뒤 한 번만 쟀다.
- 사전 라벨의 판정은 파이프라인(코드 + 모델)의 결론이다. 라벨러가 이를 따라 했다는 것은 '파이프라인과 같은 생각' 이지 '옳다' 가 아니다.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from core.schemas import ProductCategory
from guardrails.normalize import normalize
from guardrails.policy import get_catalog
from ops.dataset_tools import DATASETS, copy_key, load_jsonl_rows
from ops.labeling import Annotation, LabelDecision, wilson_interval
from ops.review_queue import ReviewItem

LEXICON_VERSION = "1.1"   # 1.0: 도메인 지식으로 처음 쓴 단서(데이터를 보기 전). 1.1: 개발용 오류를 보고 두 군데만 손봄 — ① 금융 업종은 약한 단서 하나(가중치 1)만으로는 정하지 않는다(MIN_FINANCIAL_SCORE) ② 건강에 '감량' 단서 추가
DEV_DATASETS = ["golden", "contrast_g069", "adversarial", "conditions", "conditions_extra", "redteam_seeds"]
HOLDOUT_DATASETS = ["holdout"]
# 동점일 때의 우선 순서 (금융 업종이 앞 — 오분류의 비용이 더 크다는 가정)
TIE_ORDER = ["loan", "card", "insurance", "investment", "savings", "crypto", "realestate", "health", "education", "app", "ecommerce"]
OTHER_CONFIDENCE = 0.2          # 단서가 없거나 약해서 'other' 로 둘 때의 확신 (모른다는 뜻)
MIN_FINANCIAL_SCORE = 2         # 금융 업종은 이 점수 미만(약한 단서 하나뿐)이면 후보에서 뺀다 — 금융으로 잘못 보면 더 엄격한 규칙이 적용돼 비용이 크다는 가정. 비금융은 약한 단서도 허용

# (패턴, 가중치). 가중치 3 = 그 업종에서만 거의 쓰이는 낱말, 2 = 주로 그 업종, 1 = 다른 업종에도 흔함.
# 금융 5개 업종(loan·investment·crypto·insurance·savings)의 기본 단서는 policy.yaml 에서 가져오고(가중치 3), 아래는 이 모듈이 더하는 단서다.
_EXTRA: dict[str, list[tuple[str, int]]] = {
    "loan": [(r"대출", 3), (r"대환", 3), (r"신용\s*불량", 3), (r"무직자", 2), (r"무\s*심사", 3), (r"급전", 3), (r"상환", 2), (r"원리금", 2), (r"금리", 1), (r"이자", 1), (r"한도", 1),
             (r"당일\s*(입금|승인)", 2), (r"입금", 1), (r"승인", 1), (r"심사", 1), (r"연체", 1)],
    "card": [(r"신용\s*카드", 3), (r"체크\s*카드", 3), (r"카드", 2), (r"연회비", 3), (r"리볼빙", 3), (r"카드론", 2)],
    "insurance": [(r"보험", 3), (r"특약", 2), (r"면책", 2), (r"실손", 3), (r"진단\s*시", 1), (r"가입\s*후\s*\d+\s*일", 1)],
    "investment": [(r"투자", 3), (r"수익률?", 2), (r"펀드", 3), (r"ETF", 3), (r"주식", 3), (r"채권", 2), (r"배당", 2), (r"종목", 2), (r"증권", 2), (r"원금\s*(손실|보장)", 2)],
    "savings": [(r"예금", 3), (r"적금", 3), (r"파킹\s*통장", 3), (r"예치", 2), (r"이율", 2), (r"만기", 1), (r"예금자\s*보호", 2)],
    "crypto": [(r"코인", 3), (r"가상\s*자산", 3), (r"암호\s*화폐", 3), (r"비트코인", 3), (r"토큰", 2), (r"에어드랍|에어드롭", 2)],
    "realestate": [(r"분양", 3), (r"아파트", 3), (r"부동산", 3), (r"임대", 2), (r"청약", 3), (r"전세", 3), (r"월세", 3), (r"매매", 2), (r"오피스텔", 3), (r"입주", 2), (r"평형", 2)],
    "health": [(r"건강\s*기능\s*식품", 3), (r"건강", 2), (r"영양제?", 2), (r"비타민", 3), (r"다이어트", 3), (r"감량", 3), (r"체지방", 3), (r"면역", 3), (r"효능", 2), (r"치료", 2), (r"병원", 2),
               (r"의료", 2), (r"혈압|혈당", 2), (r"질환", 2), (r"임상", 2)],
    "education": [(r"강의", 3), (r"수강", 3), (r"학원", 3), (r"교육", 2), (r"자격증", 3), (r"클래스", 2), (r"커리큘럼", 3), (r"학습", 2), (r"강사", 2), (r"합격", 1), (r"취업", 1), (r"부업", 2), (r"재테크", 2)],
    "app": [(r"앱", 2), (r"어플", 2), (r"애플리케이션", 2), (r"설치", 2), (r"다운로드", 2), (r"구독", 2), (r"무료\s*체험", 2), (r"로그인", 2), (r"업데이트", 1), (r"서비스", 1), (r"이용권", 1), (r"자동\s*결제", 1)],
    "ecommerce": [(r"쿠폰", 2), (r"할인", 1), (r"구매", 1), (r"구입", 1), (r"주문", 2), (r"배송", 2), (r"장바구니", 2), (r"쇼핑", 2), (r"적립", 1), (r"특가", 1), (r"세일", 1), (r"반품", 2), (r"판매", 1), (r"상품", 1)],
}
_FINANCIAL = {"loan", "card", "insurance", "investment", "savings", "crypto"}
_COMPILED: Optional[dict[str, list[tuple[re.Pattern, int, str]]]] = None


def _lexicon() -> dict[str, list[tuple[re.Pattern, int, str]]]:
    """업종 → [(패턴, 가중치, 출처)]. policy.yaml 의 업종 단서(입력 검사와 같은 추론)가 먼저, 이 모듈의 단서가 뒤. 처음 부를 때 한 번 만든다(import 시점에는 만들지 않는다)."""
    global _COMPILED
    if _COMPILED is not None:
        return _COMPILED
    lex: dict[str, list[tuple[re.Pattern, int, str]]] = defaultdict(list)
    for cat, pats in get_catalog().category_cues.items():
        for p in pats:
            lex[cat].append((p, 3, "policy"))
    for cat, items in _EXTRA.items():
        for pat, w in items:
            lex[cat].append((re.compile(pat, re.IGNORECASE), w, "prelabel"))
    _COMPILED = dict(lex)
    return _COMPILED


def classify_category(text: str, facts: Sequence[str | Mapping] = ()) -> tuple[ProductCategory, float, list[str]]:
    """광고 문구와 사실 목록으로 업종을 추정한다 → (업종, 확신 0~1, 근거 단서 목록). 결정론적이다.

    업종마다 서로 다른 단서가 맞은 개수 × 가중치를 더해 가장 높은 업종을 고른다. 확신 = 1등 점수 / (1등 + 2등 + 1) — 점수가 높고 2등과 차이가 클수록 1 에 가깝다.
    단서가 없으면(금융 업종은 약한 단서 하나뿐인 경우 포함) ('other', 0.2, []). 이 확신은 보정된 확률이 아니라 단서의 우세 정도다."""
    parts = [text or ""] + [(f.get("text", "") if isinstance(f, Mapping) else str(f)) for f in facts]
    n = normalize("\n".join(parts))
    surfaces = [n.loose.text, n.tight.text]      # 띄어 쓴 우회('대 출')도 같이 본다
    scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for cat, entries in _lexicon().items():
        seen_ev: list[str] = []
        total = 0
        for pat, w, _src in entries:
            hit = None
            for surf in surfaces:
                m = pat.search(surf)
                if m:
                    hit = m.group(0)
                    break
            if hit is not None and hit not in seen_ev:
                seen_ev.append(hit)
                total += w
        if total and not (cat in _FINANCIAL and total < MIN_FINANCIAL_SCORE):
            scores[cat], evidence[cat] = total, seen_ev
    if not scores:
        return ProductCategory.OTHER, OTHER_CONFIDENCE, []
    ranked = sorted(scores, key=lambda c: (-scores[c], TIE_ORDER.index(c) if c in TIE_ORDER else 99))
    top = ranked[0]
    second = scores[ranked[1]] if len(ranked) > 1 else 0
    conf = scores[top] / (scores[top] + second + 1.0)
    return ProductCategory(top), round(conf, 4), [f"{top}: {hit}" for hit in evidence[top][:6]]


def classify_row(row: Mapping) -> tuple[ProductCategory, float, list[str]]:
    """평가셋 행(headline·body·cta·disclosures·facts)을 분류한다."""
    text = "\n".join([str(row.get("headline", "")), str(row.get("body", "")), str(row.get("cta", "")), *[str(d) for d in row.get("disclosures", [])]])
    return classify_category(text, [f for f in row.get("facts", []) if isinstance(f, Mapping)])


# ---------------------------------------------------------------------------
# 업종 분류기 평가
# ---------------------------------------------------------------------------
@dataclass
class DatasetScore:
    name: str
    n: int
    correct: int
    baseline_correct: int                     # 가장 흔한 업종만 찍었을 때 맞는 수
    confusion: dict[str, dict[str, int]]      # 정답 → 예측 → 건수
    per_category: dict[str, tuple[int, int]]  # 업종 → (맞음, 전체)
    confident_n: int = 0                      # 확신 ≥ 0.7 로 낸 예측 수
    confident_correct: int = 0
    unique_n: int = 0                         # 같은 문구·같은 정답을 한 번만 센 건수
    unique_correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def baseline(self) -> float:
        return self.baseline_correct / self.n if self.n else 0.0

    @property
    def macro_recall(self) -> float:
        rs = [c / t for c, t in self.per_category.values() if t]
        return sum(rs) / len(rs) if rs else 0.0


@dataclass
class CategoryEval:
    scores: list[DatasetScore]
    lexicon_version: str = LEXICON_VERSION

    def pooled(self, names: Iterable[str]) -> DatasetScore:
        names = set(names)
        sel = [s for s in self.scores if s.name in names]
        conf: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for s in sel:
            for t, row in s.confusion.items():
                for p, k in row.items():
                    conf[t][p] += k
            for c, (a, b) in s.per_category.items():
                per[c][0] += a
                per[c][1] += b
        true_counts = {t: sum(r.values()) for t, r in conf.items()}
        base = max(true_counts.values()) if true_counts else 0
        return DatasetScore(name="+".join(sorted(names)), n=sum(s.n for s in sel), correct=sum(s.correct for s in sel), baseline_correct=base,
                            confusion={t: dict(r) for t, r in conf.items()}, per_category={c: (a, b) for c, (a, b) in per.items()},
                            confident_n=sum(s.confident_n for s in sel), confident_correct=sum(s.confident_correct for s in sel),
                            unique_n=sum(s.unique_n for s in sel), unique_correct=sum(s.unique_correct for s in sel))


def score_rows(name: str, rows: Sequence[Mapping]) -> DatasetScore:
    conf: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    correct = confident_n = confident_correct = 0
    uniq: dict[tuple[str, str], bool] = {}
    for r in rows:
        truth = str(r.get("category", "other"))
        pred, c, _ = classify_row(r)
        ok = pred.value == truth
        correct += ok
        conf[truth][pred.value] += 1
        per[truth][1] += 1
        per[truth][0] += ok
        if c >= 0.7:
            confident_n += 1
            confident_correct += ok
        uniq[(copy_key(str(r.get("headline", "")), str(r.get("body", "")), str(r.get("cta", ""))), truth)] = ok
    true_counts = Counter(str(r.get("category", "other")) for r in rows)
    return DatasetScore(name=name, n=len(rows), correct=correct, baseline_correct=max(true_counts.values()) if true_counts else 0,
                        confusion={t: dict(v) for t, v in conf.items()}, per_category={c: (a, b) for c, (a, b) in per.items()},
                        confident_n=confident_n, confident_correct=confident_correct, unique_n=len(uniq), unique_correct=sum(uniq.values()))


def evaluate_categories(names: Optional[Sequence[str]] = None, *, datasets_dir: Path = DATASETS) -> CategoryEval:
    """기존 평가 데이터셋의 category 칸을 정답으로 분류기를 채점한다 (읽기만 한다 — 데이터셋을 바꾸지 않는다)."""
    names = list(names) if names else DEV_DATASETS + HOLDOUT_DATASETS
    scores = []
    for n in names:
        p = Path(n) if Path(n).exists() else datasets_dir / f"{n}.jsonl"
        scores.append(score_rows(p.stem, load_jsonl_rows(p)))
    return CategoryEval(scores=scores)


def format_eval(ev: CategoryEval) -> str:
    def line(s: DatasetScore) -> str:
        conf = f"{s.confident_correct}/{s.confident_n}" if s.confident_n else "-"
        return (f"{s.name:<18} n={s.n:<4} 정확도 {s.accuracy:.3f} ({s.correct}/{s.n}) · 최빈 업종만 찍는 기준선 {s.baseline:.3f} · 업종별 재현율 평균 {s.macro_recall:.3f} · "
                f"문구 중복 제거 {s.unique_correct}/{s.unique_n} · 확신≥0.7 예측 {conf}")

    out = [f"업종 분류기(단서 v{ev.lexicon_version}) — 기존 평가 데이터셋의 category 칸 대비"]
    out += [line(s) for s in ev.scores]
    dev = [s.name for s in ev.scores if s.name in DEV_DATASETS]
    if dev:
        out += ["개발용 합계: " + line(ev.pooled(dev))]
    ho = [s.name for s in ev.scores if s.name in HOLDOUT_DATASETS]
    if ho:
        out += ["최종 확인용: " + line(ev.pooled(ho))]
    allp = ev.pooled([s.name for s in ev.scores])
    out.append("혼동(정답 → 예측, 틀린 것만 많은 순):")
    wrong = Counter({(t, p): k for t, row in allp.confusion.items() for p, k in row.items() if t != p})
    out += [f"  {t} → {p}: {k}" for (t, p), k in wrong.most_common(12)]
    out.append("업종별 재현율(전체): " + ", ".join(f"{c} {a}/{b}" for c, (a, b) in sorted(allp.per_category.items(), key=lambda kv: -kv[1][1])))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 사전 라벨
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PrelabelSettings:
    hide: bool = True      # 기본은 숨김 — 라벨러가 먼저 스스로 판단하게 한다 (앵커링 완화)


@dataclass(frozen=True)
class Prelabel:
    """파이프라인 판정에서 만든 제안. 라벨이 아니다 — 큐에는 낼 수 없고(Annotation 만 받는다), 사람이 직접 확인해 Annotation 을 만들어야 한다."""

    item_id: str
    available: bool                              # 제안이 있었는가 (숨겼어도 있었으면 True)
    hidden: bool                                 # 라벨러에게 숨겼는가
    decision: Optional[LabelDecision] = None     # 숨겼거나 제안이 없으면 None
    rule_ids: tuple[str, ...] = ()
    cited_spans: tuple[str, ...] = ()
    confidence: Optional[float] = None
    source: str = ""
    note: str = "자동 검사의 제안일 뿐 확정이 아닙니다. 직접 읽고 판단해 주세요."
    auto_confirmed: bool = False                 # 항상 False — 자동 확정은 없다

    @property
    def mode(self) -> str:
        """라벨에 기록할 값: shown | hidden | none"""
        if not self.available:
            return "none"
        return "hidden" if self.hidden else "shown"


def prelabel(item: ReviewItem, settings: Optional[PrelabelSettings] = None, *, hide: Optional[bool] = None) -> Prelabel:
    """항목의 자동 판정 요약에서 제안 라벨을 만든다. hide(또는 settings.hide) 가 True 면 내용을 감춘다(그래도 '제안이 있었음' 은 남긴다)."""
    hide_it = hide if hide is not None else (settings or PrelabelSettings()).hide
    v = item.verdict
    sug = v.suggested_label() if v else None
    if v is None or sug is None:
        return Prelabel(item_id=item.id, available=False, hidden=False)
    src = {"code_only": "코드 검사만"}.get(v.run_mode, f"자동 검사({v.run_mode or '실행 방식 미상'})")
    if hide_it:
        return Prelabel(item_id=item.id, available=True, hidden=True, source=src)
    return Prelabel(item_id=item.id, available=True, hidden=False, decision=sug, rule_ids=tuple(v.suggested_rules()), cited_spans=tuple(v.suggested_spans()),
                    confidence=v.confidence, source=src)


def form_defaults(p: Prelabel) -> dict:
    """라벨 입력 폼의 미리 채울 값. 숨겼거나 제안이 없으면 빈 폼. 이 값은 폼 기본값일 뿐이며 사람이 제출해야 라벨이 된다."""
    if p.decision is None:
        return {"decision": None, "rule_ids": [], "cited_spans": []}
    return {"decision": p.decision, "rule_ids": list(p.rule_ids), "cited_spans": list(p.cited_spans)}


@dataclass
class AnchoringReport:
    n_shown: int
    n_hidden: int
    agree_shown: int
    agree_hidden: int
    rate_shown: Optional[float]
    rate_hidden: Optional[float]
    ci_shown: Optional[tuple[float, float]]
    ci_hidden: Optional[tuple[float, float]]
    difference: Optional[float]               # 표시 집단 − 숨김 집단 (제안과 같은 판정을 낸 비율의 차)
    sufficient: bool
    warnings: list[str] = field(default_factory=list)
    note: str = ("제안을 보여 준 라벨과 숨긴 라벨의 '제안과 같은 판정' 비율 차이입니다. 두 집단의 광고·라벨러 구성이 같지 않으면 앵커링의 증거가 아니고, "
                 "표본이 작으면 우연일 수 있습니다.")


MIN_PER_GROUP = 20     # 집단마다 이만큼은 있어야 비교한다 (가정한 값)


def anchoring_report(annotations: Iterable[Annotation]) -> AnchoringReport:
    """제안(사전 라벨)이 있던 항목의 라벨을 표시/숨김으로 나눠 제안과 같은 판정을 낸 비율을 비교한다."""
    shown = [a for a in annotations if a.prelabel_mode == "shown" and a.prelabel_decision is not None]
    hidden = [a for a in annotations if a.prelabel_mode == "hidden" and a.prelabel_decision is not None]
    ag_s = sum(1 for a in shown if a.decision == a.prelabel_decision)
    ag_h = sum(1 for a in hidden if a.decision == a.prelabel_decision)
    rs = ag_s / len(shown) if shown else None
    rh = ag_h / len(hidden) if hidden else None
    warns: list[str] = []
    sufficient = len(shown) >= MIN_PER_GROUP and len(hidden) >= MIN_PER_GROUP
    if not shown or not hidden:
        warns.append("제안을 보여 준 라벨과 숨긴 라벨이 모두 있어야 비교할 수 있습니다.")
    elif not sufficient:
        warns.append(f"표본 부족: 집단마다 {MIN_PER_GROUP}건 이상 있어야 비교합니다 (표시 {len(shown)}건, 숨김 {len(hidden)}건).")
    return AnchoringReport(n_shown=len(shown), n_hidden=len(hidden), agree_shown=ag_s, agree_hidden=ag_h, rate_shown=rs, rate_hidden=rh,
                           ci_shown=wilson_interval(ag_s, len(shown)) if shown else None, ci_hidden=wilson_interval(ag_h, len(hidden)) if hidden else None,
                           difference=(rs - rh) if rs is not None and rh is not None else None, sufficient=sufficient, warnings=warns)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="업종 분류기 평가")
    ap.add_argument("--eval", action="store_true", help="기존 평가 데이터셋의 category 라벨 대비 정확도·혼동")
    ap.add_argument("--datasets", nargs="*", default=None)
    args = ap.parse_args(argv)
    if args.eval:
        print(format_eval(evaluate_categories(args.datasets)))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
