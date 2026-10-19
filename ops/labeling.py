"""라벨(어노테이션) 데이터 모델 · 검증 · 일치도 · 조정 · 라벨러 품질.

사람이 광고 한 건에 남기는 판정을 `Annotation` 으로 받고, 코드가 검증한다. 모델 출력과 마찬가지로 사람 입력도 그대로 믿지 않는다:
- 인용한 구절은 광고 문구에 글자 그대로 있어야 한다 (없으면 거부).
- 규칙은 카탈로그 안의 것만, 판정과 모순되면 거부한다 (APPROVE 인데 게재 불가 규칙, 수정/게재 불가인데 규칙·인용 없음).
- 읽을 수 없을 만큼 빨리 제출한 라벨은 거부하지 않고 표시만 한다 (`too_fast`).

이 모듈의 숫자가 의미하지 않는 것
- Cohen/Fleiss kappa 는 '라벨러끼리 같은 가이드라인을 같은 뜻으로 읽었는가' 이지 '라벨이 옳은가' 가 아니다. 두 사람이 같은 오해를 공유해도 kappa 는 높다.
- 이 프로젝트의 라벨러는 한 명뿐이다. 다인 라벨 기능은 구현했고 합성 데이터로만 검증했다 — 독립 라벨러가 확보됐다는 근거가 아니다.
- 표본이 작으면 kappa 는 크게 흔들린다. 그래서 부트스트랩 신뢰구간(시드 고정)과 '표본 부족' 경고를 함께 낸다. Landis-Koch 해석 띠는 관례일 뿐 기준이 아니다.
- 검증용 항목의 정답은 가이드라인 작성자의 라벨이다. 라벨러 정확도는 '작성자의 기준과 얼마나 맞는가' 이지 '진실과 얼마나 맞는가' 가 아니다.
"""
from __future__ import annotations

import math
import random
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from itertools import combinations
from typing import Iterable, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from core.schemas import AdCopy, Layer, Severity, utcnow
from guardrails.policy import PolicyCatalog, get_catalog

GUIDELINE_VERSION = "1.0"   # docs/LABELING.md 의 가이드라인 버전. 라벨마다 어느 판본으로 달았는지 남긴다.


class LabelDecision(str, Enum):
    """라벨 선택지. '담당자 확인' 은 라벨이 아니다 — 애매하면 가장 보수적인 라벨을 고르고 이유를 적는다 (docs/LABELING.md)."""

    APPROVE = "APPROVE"
    REVISE = "REVISE"
    REJECT = "REJECT"


LABELS = [LabelDecision.APPROVE, LabelDecision.REVISE, LabelDecision.REJECT]
CONSERVATIVE_RANK = {LabelDecision.APPROVE: 0, LabelDecision.REVISE: 1, LabelDecision.REJECT: 2}   # 클수록 보수적(엄격)

# --- 입력 한도 -----------------------------------------------------------------------------
MAX_ANNOTATOR_LEN = 40
MAX_RATIONALE_LEN = 2000
MAX_SPAN_LEN = 200
MAX_SPANS = 20
MIN_SPAN_CHARS = 2          # 한 글자 인용은 어느 문구에나 우연히 들어 있어 검증이 되지 않는다
MIN_RATIONALE_OMISSION = 10  # 인용할 구절이 없는 '누락' 유형 라벨은 이유 글이 있어야 한다
RESERVED_ANNOTATORS = {"pipeline", "system", "preflight", "auto", "automatic", "model", "gpt", "claude", "openai", "anthropic", "prelabel", "bot", "mock",
                       "시스템", "자동", "파이프라인", "모델"}   # 사전 라벨이 자동으로 '사람 라벨' 이 되는 길을 막는다

# --- 가정한 값 (이 프로젝트가 정한 값이며 실측·외부 기준이 아니다) -------------------------------------
ASSUMED_READ_CHARS_PER_SEC = 20.0   # 광고 문구를 '훑어보는' 속도의 넉넉한 상한. 실제 읽기 속도를 재서 정한 값이 아니라, 이보다 빠르면 물리적으로 읽기 어렵다고 보고 표시만 하는 가정
ASSUMED_MIN_REVIEW_SECONDS = 3.0    # 아무리 짧은 광고도 판정까지 이만큼은 걸린다고 본 하한
ASSUMED_MIN_ITEMS_FOR_KAPPA = 30    # 이보다 적게 겹치면 '표본 부족' 경고 (kappa 신뢰구간이 넓어 해석하기 어렵다는 일반 관행)
ASSUMED_GOLD_ACCURACY_MIN = 0.80    # 검증용 항목 정확도가 이보다 낮으면 '주의'
ASSUMED_MIN_GOLD_FOR_JUDGMENT = 5   # 검증용 항목이 이보다 적으면 정확도로 판단하지 않는다
ASSUMED_DEVIATION_RATE_MAX = 0.30   # 최종 라벨과 다른 비율이 이보다 높으면 '주의'
ASSUMED_MIN_FINAL_FOR_DEVIATION = 5
KAPPA_TARGET = 0.60                 # docs/LABELING.md 의 가이드라인 개정 기준(가정). 이보다 낮으면 가이드라인을 고친다
BOOT_SEED = 20261019                # 부트스트랩 시드 — 같은 입력이면 같은 신뢰구간
BOOT_N = 1000
MAX_SKEW_SHARE = 0.9                # 한 가지 라벨이 이 비율 이상이면 '편중' 경고 (kappa 가 실제보다 낮게 나오기 쉽다)

# 인용할 문구가 없는 '누락' 유형 규칙: 필수 고지 누락(고지 계층)과 조건 생략. 없는 것을 인용할 수는 없으므로 이유 글로 대신한다.
OMISSION_RULE_IDS = {"TA-OMISSION-01", "GRND-COND-01"}


def omission_rule_ids(catalog: Optional[PolicyCatalog] = None) -> set[str]:
    catalog = catalog or get_catalog()
    ids = set(OMISSION_RULE_IDS) | {r.id for r in catalog.rules.values() if r.layer == Layer.DISCLOSURE}
    return {i for i in ids if catalog.get(i) is not None}


def clean_name(name: str) -> str:
    """이름 정규화: 앞뒤 공백 제거, 연속 공백 축소, 제어 문자 제거. 외부에서 온 문자열이므로 화면에 낼 때는 따로 이스케이프한다."""
    s = unicodedata.normalize("NFKC", name or "")
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", s).strip()


def _catalog_from(info: Optional[ValidationInfo]) -> PolicyCatalog:
    ctx = getattr(info, "context", None) if info is not None else None
    return (ctx or {}).get("catalog") or get_catalog()


# ---------------------------------------------------------------------------
# 판정과 규칙·인용의 일관성
# ---------------------------------------------------------------------------
def decision_consistency(decision: LabelDecision, rule_ids: Sequence[str], cited_spans: Sequence[str], rationale: str,
                         catalog: Optional[PolicyCatalog] = None) -> tuple[list[str], list[str]]:
    """(오류, 경고). 오류가 하나라도 있으면 라벨을 받지 않는다. 문구에 인용이 실제로 있는지는 여기서 보지 않는다(`check_annotation`)."""
    catalog = catalog or get_catalog()
    errors: list[str] = []
    warnings: list[str] = []
    rules = [catalog.get(r) for r in rule_ids]
    for rid, r in zip(rule_ids, rules):
        if r is None:
            errors.append(f"알 수 없는 규칙입니다: {rid}")
    known = [r for r in rules if r is not None]
    if decision == LabelDecision.APPROVE:
        blocks = [r for r in known if r.severity == Severity.BLOCK]
        if blocks:
            errors.append("‘점검 통과’ 로 라벨하면서 게재 불가 등급 규칙을 골랐습니다: " + ", ".join(r.title for r in blocks) + ". 규칙을 빼거나 판정을 바꿔 주세요.")
        fixes = [r for r in known if r.severity == Severity.FIX]
        if fixes:
            warnings.append("‘점검 통과’ 인데 수정 필요 등급 규칙을 골랐습니다: " + ", ".join(r.title for r in fixes) + ". 정말 통과가 맞는지 확인해 주세요.")
    else:
        if not rule_ids:
            errors.append("‘수정 필요’·‘게재 불가’ 로 라벨하려면 이유가 된 규칙을 하나 이상 골라야 합니다.")
        omission_only = bool(known) and all(r.id in omission_rule_ids(catalog) for r in known)
        if not cited_spans and not omission_only:
            errors.append("‘수정 필요’·‘게재 불가’ 로 라벨하려면 문제가 된 구절을 광고 문구에서 하나 이상 인용해야 합니다.")
        if not cited_spans and omission_only and len((rationale or "").strip()) < MIN_RATIONALE_OMISSION:
            errors.append(f"빠진 내용(고지·조건 누락)은 인용할 구절이 없으므로, 무엇이 빠졌는지 이유를 {MIN_RATIONALE_OMISSION}자 이상 적어야 합니다.")
        if decision == LabelDecision.REJECT and known and not any(r.severity == Severity.BLOCK for r in known):
            warnings.append("‘게재 불가’ 인데 게재 불가 등급 규칙이 하나도 없습니다. ‘수정 필요’ 가 맞는지 확인해 주세요. (더 엄격한 쪽으로 고른 것이라면 이유에 적어 두세요.)")
        if decision == LabelDecision.REVISE and any(r.severity == Severity.BLOCK for r in known):
            warnings.append("‘수정 필요’ 인데 게재 불가 등급 규칙이 들어 있습니다. ‘게재 불가’ 가 맞는지 확인해 주세요.")
    return errors, warnings


def _clean_rule_ids(v: Sequence[str]) -> list[str]:
    out: list[str] = []
    for x in v or []:
        x = (x or "").strip()
        if x and x not in out:
            out.append(x)
    return out


def _clean_spans(v: Sequence[str]) -> list[str]:
    out: list[str] = []
    for x in v or []:
        x = (x or "").strip()
        if not x:
            continue
        if len(x) < MIN_SPAN_CHARS:
            raise ValueError(f"인용 구절은 {MIN_SPAN_CHARS}자 이상이어야 합니다: ‘{x}’")
        if len(x) > MAX_SPAN_LEN:
            raise ValueError(f"인용 구절은 {MAX_SPAN_LEN}자 이하여야 합니다.")
        if x not in out:
            out.append(x)
    if len(out) > MAX_SPANS:
        raise ValueError(f"인용 구절은 {MAX_SPANS}개까지 받습니다.")
    return out


def validate_annotator_name(v: str) -> str:
    """사람 라벨러 이름 검사·정규화. 비어 있거나 길거나 자동 처리용으로 예약된 이름이면 ValueError."""
    name = clean_name(v)
    if not name:
        raise ValueError("라벨러 이름을 입력해 주세요.")
    if len(name) > MAX_ANNOTATOR_LEN:
        raise ValueError(f"라벨러 이름은 {MAX_ANNOTATOR_LEN}자 이하여야 합니다.")
    if name.casefold() in RESERVED_ANNOTATORS:
        raise ValueError("이 이름은 자동 처리용으로 예약돼 있어 사람 라벨러 이름으로 쓸 수 없습니다.")
    return name


class Annotation(BaseModel):
    """사람이 광고 한 건에 남긴 판정. 만들어지는 순간 자체 일관성(규칙·판정)을 검사하고, 문구 대조는 `check_annotation` 이 한다."""

    item_id: str
    annotator: str
    decision: LabelDecision
    rule_ids: list[str] = Field(default_factory=list)
    cited_spans: list[str] = Field(default_factory=list)
    rationale: str = ""
    seconds_spent: Optional[float] = Field(default=None, ge=0, le=24 * 3600)
    created_at: datetime = Field(default_factory=utcnow)
    guideline_version: str = GUIDELINE_VERSION
    # 사전 라벨(자동 검사의 제안)을 라벨러가 봤는지 — 앵커링(제안에 끌려가는 편향)을 나중에 재려고 숨김/표시 모두 남긴다
    prelabel_mode: Literal["shown", "hidden", "none"] = "none"
    prelabel_decision: Optional[LabelDecision] = None     # 그 항목에 있던 제안(숨겼어도 기록). 큐가 채운다 — 호출자 값은 믿지 않는다
    flags: list[str] = Field(default_factory=list)         # 큐가 채운다 (예: too_fast)

    @field_validator("item_id")
    @classmethod
    def _item(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or len(v) > 80:
            raise ValueError("항목 ID 가 올바르지 않습니다.")
        return v

    @field_validator("annotator")
    @classmethod
    def _annotator(cls, v: str) -> str:
        return validate_annotator_name(v)

    @field_validator("rule_ids")
    @classmethod
    def _rules(cls, v: list[str]) -> list[str]:
        return _clean_rule_ids(v)

    @field_validator("cited_spans")
    @classmethod
    def _spans(cls, v: list[str]) -> list[str]:
        return _clean_spans(v)

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > MAX_RATIONALE_LEN:
            raise ValueError(f"이유는 {MAX_RATIONALE_LEN}자 이하로 적어 주세요.")
        return v

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> "Annotation":
        errors, _ = decision_consistency(self.decision, self.rule_ids, self.cited_spans, self.rationale, _catalog_from(info))
        if errors:
            raise ValueError(" / ".join(errors))
        return self


class AnnotationRejected(ValueError):
    """라벨을 받지 않았다. `issues` 에 사람이 읽을 이유가 있다."""

    def __init__(self, issues: Sequence[str]):
        self.issues = list(issues)
        super().__init__(" / ".join(self.issues))


@dataclass
class AnnotationCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    located: dict[str, tuple[str, int, int]] = field(default_factory=dict)   # 인용 → (필드, 시작, 끝)

    @property
    def ok(self) -> bool:
        return not self.errors


def copy_fields(copy: AdCopy) -> list[tuple[str, str]]:
    """인용을 찾을 수 있는 문구 단위. 인용은 한 단위 안에 통째로 있어야 한다 (제목 끝 + 본문 앞을 이어 붙인 인용은 광고에 없는 문장이다)."""
    out = [("headline", copy.headline), ("body", copy.body), ("cta", copy.cta)]
    out += [(f"disc{i}", d) for i, d in enumerate(copy.disclosures)]
    return [(n, t) for n, t in out if t]


def locate_span(copy: AdCopy, span: str) -> Optional[tuple[str, int, int]]:
    """인용이 광고 문구 안에 글자 그대로 있으면 (필드, 시작, 끝). 없으면 None. 공백·표기를 맞춰 주지 않는다 — 보이지 않는 글자까지 같아야 한다."""
    if not span:
        return None
    for name, text in copy_fields(copy):
        i = text.find(span)
        if i >= 0:
            return name, i, i + len(span)
    return None


def fast_threshold_seconds(copy: AdCopy) -> float:
    """이보다 빨리 제출하면 `too_fast`. 문구 길이 / 가정한 훑어보기 속도, 하한은 가정한 최소 시간."""
    n = len(copy.full_text())
    return max(ASSUMED_MIN_REVIEW_SECONDS, n / ASSUMED_READ_CHARS_PER_SEC)


def check_annotation(ann: Annotation, copy: AdCopy, *, catalog: Optional[PolicyCatalog] = None) -> AnnotationCheck:
    """문구와 대조한 검사. 인용이 광고 문구에 글자 그대로 없으면 오류(거부)."""
    catalog = catalog or get_catalog()
    res = AnnotationCheck()
    errs, warns = decision_consistency(ann.decision, ann.rule_ids, ann.cited_spans, ann.rationale, catalog)
    res.errors.extend(errs)
    res.warnings.extend(warns)
    missing: list[str] = []
    for sp in ann.cited_spans:
        loc = locate_span(copy, sp)
        if loc is None:
            missing.append(sp)
        else:
            res.located[sp] = loc
    if missing:
        res.errors.append("인용한 구절이 광고 문구에 그대로 없어 저장하지 않았습니다: " + ", ".join(f"‘{m}’" for m in missing)
                          + ". 문구에서 그대로 복사하거나 아래 후보에서 골라 주세요.")
    if ann.seconds_spent is not None and ann.seconds_spent < fast_threshold_seconds(copy):
        res.flags.append("too_fast")
        res.warnings.append(f"제출이 매우 빨랐습니다({ann.seconds_spent:.1f}초). 광고를 끝까지 읽었는지 한 번 더 확인해 주세요.")
    return res


def ensure_annotation(ann: Annotation, copy: AdCopy, *, catalog: Optional[PolicyCatalog] = None) -> AnnotationCheck:
    res = check_annotation(ann, copy, catalog=catalog)
    if not res.ok:
        raise AnnotationRejected(res.errors)
    return res


# ---------------------------------------------------------------------------
# 조정 (adjudication)
# ---------------------------------------------------------------------------
MIN_ADJUDICATION_REASON = 10


class AdjudicationRecord(BaseModel):
    """라벨이 갈린 항목의 최종 라벨. 제3 라벨러/조정자가 정하고 이유를 남긴다. 이미 그 항목을 라벨한 사람은 조정할 수 없다(큐가 검사)."""

    item_id: str
    adjudicator: str
    decision: LabelDecision
    rule_ids: list[str] = Field(default_factory=list)
    cited_spans: list[str] = Field(default_factory=list)
    reason: str
    created_at: datetime = Field(default_factory=utcnow)
    guideline_version: str = GUIDELINE_VERSION

    @field_validator("adjudicator")
    @classmethod
    def _adj(cls, v: str) -> str:
        return validate_annotator_name(v)

    @field_validator("rule_ids")
    @classmethod
    def _rules(cls, v: list[str]) -> list[str]:
        return _clean_rule_ids(v)

    @field_validator("cited_spans")
    @classmethod
    def _spans(cls, v: list[str]) -> list[str]:
        return _clean_spans(v)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < MIN_ADJUDICATION_REASON:
            raise ValueError(f"조정 이유를 {MIN_ADJUDICATION_REASON}자 이상 적어 주세요. 왜 이 라벨로 정했는지가 가이드라인 개정의 근거가 됩니다.")
        if len(v) > MAX_RATIONALE_LEN:
            raise ValueError(f"이유는 {MAX_RATIONALE_LEN}자 이하로 적어 주세요.")
        return v

    @model_validator(mode="after")
    def _consistent(self, info: ValidationInfo) -> "AdjudicationRecord":
        errors, _ = decision_consistency(self.decision, self.rule_ids, self.cited_spans, self.reason, _catalog_from(info))
        if errors:
            raise ValueError(" / ".join(errors))
        return self


def ensure_adjudication(rec: AdjudicationRecord, copy: AdCopy) -> None:
    missing = [s for s in rec.cited_spans if locate_span(copy, s) is None]
    if missing:
        raise AnnotationRejected(["인용한 구절이 광고 문구에 그대로 없어 저장하지 않았습니다: " + ", ".join(f"‘{m}’" for m in missing)])


@dataclass(frozen=True)
class FinalLabel:
    """확정된 라벨 — 라벨러들이 일치했거나(agreed) 조정자가 정했거나(adjudicated)."""

    decision: LabelDecision
    rule_ids: list[str]
    cited_spans: list[str]
    labelers: list[str]
    adjudicated: bool
    adjudicator: str = ""
    agreement: Optional[float] = None         # 라벨러 쌍들의 판정 일치 비율(처음 라벨 기준). 한 명이면 None
    rule_agreement: Optional[float] = None    # 라벨러 쌍들의 규칙 집합 자카드 평균. 한 명이면 None
    rationale: str = ""
    rules_unresolved: bool = False            # 판정은 같지만 고른 규칙의 교집합이 없어 합집합을 쓴 경우

    @property
    def single_labeler(self) -> bool:
        return len(self.labelers) < 2 and not self.adjudicated


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def pairwise_stats(anns: Sequence[Annotation]) -> tuple[Optional[float], Optional[float]]:
    if len(anns) < 2:
        return None, None
    dec, rul = [], []
    for x, y in combinations(anns, 2):
        dec.append(1.0 if x.decision == y.decision else 0.0)
        rul.append(_jaccard(set(x.rule_ids), set(y.rule_ids)))
    return sum(dec) / len(dec), sum(rul) / len(rul)


def resolve_final(annotations: Sequence[Annotation], adjudication: Optional[AdjudicationRecord], required_labels: int) -> Optional[FinalLabel]:
    """라벨이 확정됐으면 FinalLabel, 아니면 None.
    - 조정 기록이 있으면 조정자의 라벨.
    - 필요한 수만큼 라벨이 모였고 모두 같은 판정이면 그 판정 (규칙은 공통 규칙, 공통이 없으면 합집합 + rules_unresolved).
    - 라벨이 갈렸는데 조정 기록이 없으면 None."""
    labelers = [a.annotator for a in annotations]
    agr, rag = pairwise_stats(annotations)
    if adjudication is not None:
        return FinalLabel(decision=adjudication.decision, rule_ids=list(adjudication.rule_ids), cited_spans=list(adjudication.cited_spans), labelers=labelers,
                          adjudicated=True, adjudicator=adjudication.adjudicator, agreement=agr, rule_agreement=rag, rationale=adjudication.reason)
    if len(annotations) < max(1, required_labels):
        return None
    if len({a.decision for a in annotations}) != 1:
        return None
    sets = [set(a.rule_ids) for a in annotations]
    common = set.intersection(*sets) if sets else set()
    union = set.union(*sets) if sets else set()
    first = annotations[0]
    rules = common or union
    order = {r: i for i, r in enumerate(r for a in annotations for r in a.rule_ids)}
    spans: list[str] = []
    for a in annotations:
        for s in a.cited_spans:
            if s not in spans:
                spans.append(s)
    return FinalLabel(decision=first.decision, rule_ids=sorted(rules, key=lambda r: order.get(r, 0)), cited_spans=spans, labelers=labelers, adjudicated=False,
                      agreement=agr, rule_agreement=rag, rationale=" / ".join(a.rationale for a in annotations if a.rationale),
                      rules_unresolved=bool(union) and not common)


# ---------------------------------------------------------------------------
# 일치도 — 순수 함수
# ---------------------------------------------------------------------------
def _same_length(a: Sequence, b: Sequence) -> None:
    if len(a) != len(b):
        raise ValueError(f"두 라벨러의 항목 수가 다릅니다 ({len(a)} vs {len(b)})")
    if not a:
        raise ValueError("비교할 항목이 없습니다")


def percent_agreement(a: Sequence, b: Sequence) -> float:
    """단순 일치율. 우연히 맞는 몫을 빼지 않으므로 라벨이 한쪽으로 쏠리면 부풀려진다 — kappa 와 함께 본다."""
    _same_length(a, b)
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def cohens_kappa(a: Sequence, b: Sequence) -> Optional[float]:
    """Cohen's kappa (두 명). (관측 일치 − 우연 일치) / (1 − 우연 일치). 두 사람이 한 가지 라벨만 쓰면 우연 일치가 1 이라 정의되지 않아 None."""
    _same_length(a, b)
    n = len(a)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[c] / n) * (cb[c] / n) for c in set(ca) | set(cb))
    if pe >= 1.0 - 1e-12:
        return None
    return (po - pe) / (1.0 - pe)


def fleiss_kappa(counts: Sequence[Sequence[int]]) -> Optional[float]:
    """Fleiss' kappa (세 명 이상). counts[i][j] = i 번째 항목에 j 번째 라벨을 준 사람 수. 모든 항목의 평가자 수가 같아야 한다."""
    if not counts:
        raise ValueError("비교할 항목이 없습니다")
    k = len(counts[0])
    n = sum(counts[0])
    if n < 2:
        raise ValueError("항목마다 평가자가 2명 이상이어야 합니다")
    for row in counts:
        if len(row) != k or sum(row) != n:
            raise ValueError("항목마다 평가자 수가 같아야 합니다")
    N = len(counts)
    p_j = [sum(row[j] for row in counts) / (N * n) for j in range(k)]
    p_i = [(sum(c * c for c in row) - n) / (n * (n - 1)) for row in counts]
    p_bar = sum(p_i) / N
    p_e = sum(p * p for p in p_j)
    if p_e >= 1.0 - 1e-12:
        return None
    return (p_bar - p_e) / (1.0 - p_e)


def counts_matrix(ratings: Sequence[Sequence], labels: Sequence) -> list[list[int]]:
    """항목별 라벨 목록 → Fleiss 입력 행렬."""
    idx = {lab: i for i, lab in enumerate(labels)}
    out = []
    for row in ratings:
        c = [0] * len(labels)
        for r in row:
            c[idx[r]] += 1
        out.append(c)
    return out


def fleiss_kappa_from_ratings(ratings: Sequence[Sequence], labels: Sequence = LABELS) -> Optional[float]:
    return fleiss_kappa(counts_matrix(ratings, labels))


def confusion_matrix(a: Sequence, b: Sequence, labels: Sequence = LABELS) -> dict[str, dict[str, int]]:
    """행 = 첫 번째 라벨러, 열 = 두 번째 라벨러. 키는 라벨 값 문자열."""
    _same_length(a, b)
    def key(x):
        return x.value if isinstance(x, Enum) else str(x)
    keys = [key(x) for x in labels]
    m = {r: {c: 0 for c in keys} for r in keys}
    for x, y in zip(a, b):
        m[key(x)][key(y)] += 1
    return m


LANDIS_KOCH = [(0.0, "poor", "우연보다 낮음"), (0.20, "slight", "거의 없음"), (0.40, "fair", "약간"), (0.60, "moderate", "보통"), (0.80, "substantial", "상당함"), (1.0001, "almost_perfect", "거의 완벽")]


def landis_koch(kappa: Optional[float]) -> tuple[str, str]:
    """(영문 키, 화면용 이름). 해석 띠는 관례일 뿐이며 이 프로젝트의 개정 기준은 KAPPA_TARGET 이다."""
    if kappa is None:
        return "undefined", "계산할 수 없음"
    if kappa < 0:
        return LANDIS_KOCH[0][1], LANDIS_KOCH[0][2]
    for upper, key, ko in LANDIS_KOCH[1:]:
        if kappa <= upper:
            return key, ko
    return LANDIS_KOCH[-1][1], LANDIS_KOCH[-1][2]


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """비율 k/n 의 Wilson 신뢰구간 — 표본이 작아도 0~1 안에 머문다."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _percentile_ci(vals: list[float], level: float) -> tuple[float, float]:
    vals = sorted(vals)
    m = len(vals)
    alpha = 1 - level
    lo = vals[min(m - 1, max(0, int(math.floor(alpha / 2 * m))))]
    hi = vals[min(m - 1, max(0, int(math.ceil((1 - alpha / 2) * m)) - 1))]
    return lo, hi


def bootstrap_kappa_ci(a: Sequence, b: Sequence, *, n_boot: int = BOOT_N, level: float = 0.95, seed: int = BOOT_SEED) -> tuple[Optional[float], Optional[float], int]:
    """항목을 복원 추출해 kappa 를 다시 계산한 백분위 신뢰구간. 시드 고정이라 같은 입력이면 같은 값. (하한, 상한, 유효한 재표집 수)
    재표집에서 kappa 가 정의되지 않은 경우(한 가지 라벨만 뽑힘)는 버린다. 유효한 재표집이 절반 미만이면 구간을 내지 않는다."""
    _same_length(a, b)
    rng = random.Random(seed)
    n = len(a)
    if n_boot > 200 and n > 2000:
        n_boot = 200   # 큰 표본은 구간이 좁고 계산이 오래 걸린다
    vals: list[float] = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        v = cohens_kappa([a[i] for i in idx], [b[i] for i in idx])
        if v is not None:
            vals.append(v)
    if len(vals) < max(20, n_boot // 2):
        return None, None, len(vals)
    lo, hi = _percentile_ci(vals, level)
    return lo, hi, len(vals)


def bootstrap_fleiss_ci(rows: Sequence[Sequence[int]], *, n_boot: int = BOOT_N, level: float = 0.95, seed: int = BOOT_SEED) -> tuple[Optional[float], Optional[float], int]:
    rng = random.Random(seed)
    n = len(rows)
    if n_boot > 200 and n > 2000:
        n_boot = 200
    vals: list[float] = []
    for _ in range(n_boot):
        v = fleiss_kappa([rows[rng.randrange(n)] for _ in range(n)])
        if v is not None:
            vals.append(v)
    if len(vals) < max(20, n_boot // 2):
        return None, None, len(vals)
    lo, hi = _percentile_ci(vals, level)
    return lo, hi, len(vals)


@dataclass
class KappaResult:
    method: str                      # "cohen" | "fleiss"
    n_items: int
    n_raters: int
    percent_agreement: float
    kappa: Optional[float]
    ci_low: Optional[float]
    ci_high: Optional[float]
    ci_level: float = 0.95
    boot_valid: int = 0
    band: str = "undefined"          # Landis-Koch 키 (참고용)
    band_ko: str = ""
    sufficient: bool = True          # 표본이 충분한가 (ASSUMED_MIN_ITEMS_FOR_KAPPA 이상)
    warnings: list[str] = field(default_factory=list)
    below_target: Optional[bool] = None   # kappa < KAPPA_TARGET (가이드라인 개정 검토 신호). 계산 불가면 None

    def to_dict(self) -> dict:
        return asdict(self)


def _skew_warning(labels: Sequence) -> Optional[str]:
    c = Counter(labels)
    if not c:
        return None
    top, share = c.most_common(1)[0][0], c.most_common(1)[0][1] / len(labels)
    if share >= MAX_SKEW_SHARE:
        name = top.value if isinstance(top, Enum) else str(top)
        return f"라벨이 한쪽으로 치우쳐 있습니다(‘{name}’ 가 {share:.0%}). 이런 경우 일치율은 높아도 kappa 는 낮게 나오기 쉽습니다."
    return None


def _finish(res: KappaResult, min_items: int, all_labels: Sequence) -> KappaResult:
    res.band, res.band_ko = landis_koch(res.kappa)
    res.sufficient = res.n_items >= min_items
    if not res.sufficient:
        res.warnings.append(f"표본 부족: 같이 라벨한 광고가 {res.n_items}건뿐입니다(권장 {min_items}건 이상). 일치도 값이 크게 흔들릴 수 있으니 신뢰구간을 함께 보세요.")
    if res.kappa is None:
        res.warnings.append("두 사람 모두 한 가지 라벨만 써서 kappa 를 계산할 수 없습니다.")
    elif res.ci_low is not None and res.ci_high is not None and res.ci_high - res.ci_low > 0.4:
        res.warnings.append(f"신뢰구간이 넓습니다({res.ci_low:.2f} ~ {res.ci_high:.2f}). 이 값만으로 라벨러 합의 수준을 단정할 수 없습니다.")
    sk = _skew_warning(all_labels)
    if sk:
        res.warnings.append(sk)
    if res.kappa is not None:
        res.below_target = res.kappa < KAPPA_TARGET
    return res


def cohen_report(a: Sequence, b: Sequence, *, n_boot: int = BOOT_N, seed: int = BOOT_SEED, min_items: int = ASSUMED_MIN_ITEMS_FOR_KAPPA) -> KappaResult:
    k = cohens_kappa(a, b)
    lo, hi, valid = bootstrap_kappa_ci(a, b, n_boot=n_boot, seed=seed)
    res = KappaResult(method="cohen", n_items=len(a), n_raters=2, percent_agreement=percent_agreement(a, b), kappa=k, ci_low=lo, ci_high=hi, boot_valid=valid)
    return _finish(res, min_items, list(a) + list(b))


def fleiss_report(ratings: Sequence[Sequence], labels: Sequence = LABELS, *, n_boot: int = BOOT_N, seed: int = BOOT_SEED,
                  min_items: int = ASSUMED_MIN_ITEMS_FOR_KAPPA) -> KappaResult:
    rows = counts_matrix(ratings, labels)
    k = fleiss_kappa(rows)
    lo, hi, valid = bootstrap_fleiss_ci(rows, n_boot=n_boot, seed=seed)
    n_raters = sum(rows[0])
    pa = sum(1 for row in ratings if len(set(row)) == 1) / len(ratings)   # 항목 안의 모든 평가자가 같은 라벨을 준 비율
    res = KappaResult(method="fleiss", n_items=len(rows), n_raters=n_raters, percent_agreement=pa, kappa=k, ci_low=lo, ci_high=hi, boot_valid=valid)
    return _finish(res, min_items, [x for row in ratings for x in row])


@dataclass
class RuleAgreement:
    rule_id: str
    n_items: int          # 비교한 (항목, 라벨러 쌍) 수
    both: int
    only_first: int
    only_second: int
    neither: int
    percent: float
    kappa: Optional[float]

    @property
    def support(self) -> int:
        return self.both + self.only_first + self.only_second


@dataclass
class PairAgreement:
    annotator_a: str
    annotator_b: str
    result: KappaResult
    confusion: dict[str, dict[str, int]]
    item_ids: list[str]


@dataclass
class AgreementReport:
    annotators: list[str]
    n_multi_labeled: int                       # 둘 이상이 라벨한 항목 수
    overall: Optional[KappaResult]
    pairs: list[PairAgreement]
    per_rule: list[RuleAgreement]
    confusion: dict[str, dict[str, int]]       # 주 비교 쌍(공통 항목이 가장 많은 쌍)의 혼동 행렬
    primary_pair: Optional[tuple[str, str]]
    warnings: list[str]
    note: str = ("일치도는 라벨러들이 같은 가이드라인을 같은 뜻으로 읽었는지를 보여 줄 뿐, 라벨이 옳다는 뜻이 아닙니다. "
                 "독립적으로 모집한 라벨러의 값이 아니라면 이 값을 독립 검증으로 말하지 않습니다.")

    @property
    def measurable(self) -> bool:
        return self.overall is not None


def collect_ratings(annotations: Iterable[Annotation]) -> dict[str, dict[str, LabelDecision]]:
    """항목 → {라벨러: 판정}. 같은 사람이 같은 항목에 두 번 라벨한 기록이 섞여 들어오면 오류 (큐는 이를 막는다)."""
    out: dict[str, dict[str, LabelDecision]] = defaultdict(dict)
    for a in annotations:
        if a.annotator in out[a.item_id]:
            raise ValueError(f"같은 라벨러가 같은 항목에 두 번 라벨했습니다: {a.annotator} / {a.item_id}")
        out[a.item_id][a.annotator] = a.decision
    return dict(out)


def per_rule_agreement(annotations: Sequence[Annotation]) -> list[RuleAgreement]:
    """규칙마다 '이 규칙을 골랐는가' 를 이진 라벨로 보고 라벨러 쌍별로 일치를 센다. 같은 항목을 둘 이상이 라벨한 경우만. 모든 쌍을 합쳐 계산한다."""
    by_item: dict[str, list[Annotation]] = defaultdict(list)
    for a in annotations:
        by_item[a.item_id].append(a)
    rules = sorted({r for a in annotations for r in a.rule_ids})
    out: list[RuleAgreement] = []
    for rid in rules:
        both = ofirst = osecond = neither = 0
        va: list[bool] = []
        vb: list[bool] = []
        for anns in by_item.values():
            for x, y in combinations(sorted(anns, key=lambda z: z.annotator), 2):
                fx, fy = rid in x.rule_ids, rid in y.rule_ids
                va.append(fx)
                vb.append(fy)
                both += fx and fy
                ofirst += fx and not fy
                osecond += fy and not fx
                neither += (not fx) and (not fy)
        n = len(va)
        if n == 0:
            continue
        out.append(RuleAgreement(rule_id=rid, n_items=n, both=both, only_first=ofirst, only_second=osecond, neither=neither,
                                 percent=(both + neither) / n, kappa=cohens_kappa(va, vb)))
    out.sort(key=lambda r: (-r.support, r.rule_id))
    return out


def agreement_report(annotations: Sequence[Annotation], *, n_boot: int = BOOT_N, seed: int = BOOT_SEED,
                     min_items: int = ASSUMED_MIN_ITEMS_FOR_KAPPA) -> AgreementReport:
    """라벨 모음에서 일치도를 계산한다. 같은 항목에 둘 이상이 라벨한 것만 쓴다. 라벨러가 한 명뿐이면 overall=None (잴 수 없다)."""
    ratings = collect_ratings(annotations)
    multi = {i: r for i, r in ratings.items() if len(r) >= 2}
    annotators = sorted({a.annotator for a in annotations})
    warnings: list[str] = []
    if len(annotators) < 2:
        return AgreementReport(annotators=annotators, n_multi_labeled=0, overall=None, pairs=[], per_rule=[], confusion={}, primary_pair=None,
                               warnings=["라벨러가 한 명이라 일치도를 잴 수 없습니다."])
    pairs: list[PairAgreement] = []
    for x, y in combinations(annotators, 2):
        ids = sorted(i for i, r in multi.items() if x in r and y in r)
        if not ids:
            continue
        a = [multi[i][x] for i in ids]
        b = [multi[i][y] for i in ids]
        pairs.append(PairAgreement(annotator_a=x, annotator_b=y, result=cohen_report(a, b, n_boot=n_boot, seed=seed, min_items=min_items),
                                   confusion=confusion_matrix(a, b), item_ids=ids))
    if not pairs:
        return AgreementReport(annotators=annotators, n_multi_labeled=0, overall=None, pairs=[], per_rule=[], confusion={}, primary_pair=None,
                               warnings=["라벨러는 여러 명이지만 같은 광고를 함께 라벨한 것이 없어 일치도를 잴 수 없습니다."])
    pairs.sort(key=lambda p: (-len(p.item_ids), p.annotator_a, p.annotator_b))
    primary = pairs[0]
    overall: KappaResult
    if len(annotators) == 2:
        overall = primary.result
    else:
        # 세 명 이상: 가장 많은 사람이 함께 라벨한 항목 묶음(사람 수가 같은 항목끼리)으로 Fleiss. 사람 수가 다른 항목은 쓰지 않는다.
        by_k: dict[int, list[str]] = defaultdict(list)
        for i, r in multi.items():
            by_k[len(r)].append(i)
        usable = {k: ids for k, ids in by_k.items() if k >= 3}
        if usable:
            k = max(usable, key=lambda kk: (len(usable[kk]), kk))
            ids = sorted(usable[k])
            overall = fleiss_report([[multi[i][r] for r in sorted(multi[i])] for i in ids], n_boot=n_boot, seed=seed, min_items=min_items)
            overall.warnings.append(f"{k}명이 모두 라벨한 {len(ids)}건으로 계산했습니다 (사람 수가 다른 항목은 제외).")
        else:
            overall = primary.result
            warnings.append("세 명이 함께 라벨한 광고가 없어 가장 많이 겹친 두 사람의 값을 대표로 보여 줍니다.")
    per_rule = per_rule_agreement([a for a in annotations if a.item_id in multi])
    return AgreementReport(annotators=annotators, n_multi_labeled=len(multi), overall=overall, pairs=pairs, per_rule=per_rule, confusion=primary.confusion,
                           primary_pair=(primary.annotator_a, primary.annotator_b), warnings=warnings)


# ---------------------------------------------------------------------------
# 라벨러 품질 — 검증용 항목(정답을 아는 항목), 처리 시간, 합의 이탈
# ---------------------------------------------------------------------------
@dataclass
class AnnotatorQuality:
    annotator: str
    n_labeled: int
    n_gold: int
    gold_correct: int
    gold_accuracy: Optional[float]
    gold_ci: Optional[tuple[float, float]]
    unsafe_misses: int            # 정답이 수정/게재 불가인데 ‘점검 통과’ 로 라벨
    over_flags: int               # 정답이 ‘점검 통과’ 인데 수정/게재 불가로 라벨
    mean_seconds: Optional[float]
    too_fast: int
    n_final: int                  # 최종 라벨이 확정된 항목 중 이 라벨러가 라벨한 수 (검증용 제외)
    n_deviating: int              # 그 중 최종 라벨과 다르게 라벨한 수
    deviation_rate: Optional[float]
    status: str                   # "insufficient" | "ok" | "watch"
    confirmed_low: bool           # 정확도 신뢰구간 상한까지 기준 미만이라 우연으로 보기 어려움
    notes: list[str] = field(default_factory=list)


def annotator_quality(annotations: Iterable[Annotation], gold: Mapping[str, LabelDecision], finals: Mapping[str, LabelDecision]) -> list[AnnotatorQuality]:
    """라벨러별 품질. gold: 검증용 항목 id → 정답, finals: 확정된 일반 항목 id → 최종 판정.
    - 검증용 정확도: 판정이 정답과 같은 비율 (표본이 ASSUMED_MIN_GOLD_FOR_JUDGMENT 미만이면 판단하지 않는다).
    - 합의 이탈: 확정된 항목에서 자기 판정이 최종 판정과 다른 비율. 항목이 어려워서 갈린 것일 수도 있어 단독으로 라벨러를 평가하지 않는다.
    '주의' 는 사람을 평가하는 것이 아니라 가이드라인·교육·표본 점검을 시작하라는 신호다."""
    per: dict[str, list[Annotation]] = defaultdict(list)
    for a in annotations:
        per[a.annotator].append(a)
    out: list[AnnotatorQuality] = []
    for name in sorted(per):
        anns = per[name]
        g = [a for a in anns if a.item_id in gold]
        correct = sum(1 for a in g if a.decision == gold[a.item_id])
        unsafe = sum(1 for a in g if a.decision == LabelDecision.APPROVE and gold[a.item_id] != LabelDecision.APPROVE)
        over = sum(1 for a in g if a.decision != LabelDecision.APPROVE and gold[a.item_id] == LabelDecision.APPROVE)
        acc = correct / len(g) if g else None
        ci = wilson_interval(correct, len(g)) if g else None
        f = [a for a in anns if a.item_id in finals and a.item_id not in gold]
        dev = sum(1 for a in f if a.decision != finals[a.item_id])
        secs = [a.seconds_spent for a in anns if a.seconds_spent is not None]
        notes: list[str] = []
        status = "insufficient"
        confirmed = False
        if len(g) >= ASSUMED_MIN_GOLD_FOR_JUDGMENT and acc is not None:
            status = "watch" if acc < ASSUMED_GOLD_ACCURACY_MIN else "ok"
            confirmed = bool(ci and ci[1] < ASSUMED_GOLD_ACCURACY_MIN)
            if status == "watch":
                notes.append(f"검증용 항목 정확도 {acc:.0%}({correct}/{len(g)})가 기준 {ASSUMED_GOLD_ACCURACY_MIN:.0%} 미만입니다.")
        else:
            notes.append(f"검증용 항목이 {len(g)}건뿐이라(최소 {ASSUMED_MIN_GOLD_FOR_JUDGMENT}건) 정확도로 판단하지 않습니다.")
        dev_rate = dev / len(f) if f else None
        if dev_rate is not None and len(f) >= ASSUMED_MIN_FINAL_FOR_DEVIATION and dev_rate > ASSUMED_DEVIATION_RATE_MAX:
            notes.append(f"확정된 {len(f)}건 중 {dev}건({dev_rate:.0%})이 최종 라벨과 달랐습니다.")
            if status == "ok":
                status = "watch"
        out.append(AnnotatorQuality(annotator=name, n_labeled=len(anns), n_gold=len(g), gold_correct=correct, gold_accuracy=acc, gold_ci=ci, unsafe_misses=unsafe,
                                    over_flags=over, mean_seconds=(sum(secs) / len(secs) if secs else None), too_fast=sum(1 for a in anns if "too_fast" in a.flags),
                                    n_final=len(f), n_deviating=dev, deviation_rate=dev_rate, status=status, confirmed_low=confirmed, notes=notes))
    return out
