"""검수 대기열 — 자동으로 확정하지 못한 광고를 사람이 라벨하게 하고, 그 라벨을 평가셋 후보로 잇는 닫힌 고리의 앞부분.

    담당자 확인 건 → 대기열(queued) → 점유(claimed) → 라벨(labeled) → [갈리면 disagreed → 조정 adjudicated] → 내보내기(exported)   (건너뜀: skipped)

원칙
- 어떤 항목을 먼저 사람에게 보여 줄지는 **불확실성 표집** 으로 정한다. 확신이 낮을수록, 두 모델 계열이 갈릴수록, 금융처럼 주의가 필요한 업종일수록,
  코드와 모델의 결론이 다를수록 먼저 (`compute_priority`; 가중치는 상수로 두고 테스트한다).
- 정답을 아는 **검증용 항목** 을 섞어 라벨러 품질을 잰다. 라벨러가 받는 `AnnotatorView` 에는 검증용 여부·정답이 구조적으로 들어갈 수 없다.
- 상태 전이는 엄격하다. 허용되지 않은 전이는 `IllegalTransition`. 같은 사람이 같은 항목에 두 번 라벨할 수 없고, 한 사람이 동시에 잡을 수 있는 항목 수에 상한이 있으며, 오래 잡고 있으면 점유가 회수된다.
- 저장 형식은 JSONL (`to_jsonl`/`from_jsonl`). 화면은 방문자별 브라우저 저장소에 자동 저장한다(`ops.review_persistence`, `ui.review_queue_storage`). 불러올 때 형식 버전·항목·상태 일관성을 다시 검증한다.
- 시간은 `clock` 으로 주입한다 (기본은 현재 시각). 같은 입력·같은 시계면 같은 결과.

이 모듈의 값이 의미하지 않는 것
- `priority` 는 '사람이 먼저 봐야 할 정도' 를 가늠한 값이지 위반 확률이 아니다. 가중치·업종 위험도·SLA 시간은 이 프로젝트가 가정한 값이다(ASSUMED).
- 보고서에는 모델이 처음 말한 판정이 따로 저장되지 않는다(완화 불가 규칙으로 합쳐진 값만). 그래서 '코드와 모델의 결론이 다름' 은 '코드만의 판정 ≠ 합쳐진 판정' 으로 근사한다.
- 이 메모리 객체 자체를 여러 스레드에서 공유하지 않는다. 화면의 여러 탭은 저장소 트랜잭션과 버전 확인으로 동시 수정을 감지하고, 오래된 탭의 덮어쓰기를 거부한다. 여러 기기·직원 사이의 공용 서버 큐는 아니다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable, Mapping, Optional, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator

from core.schemas import AdCopy, Claim, Decision, Fact, PipelineRun, PreflightReport, ProductCategory, ProductFacts, Severity, utcnow
from ops.labeling import (
    GUIDELINE_VERSION,
    AdjudicationRecord,
    Annotation,
    AnnotationRejected,
    AnnotatorQuality,
    FinalLabel,
    LabelDecision,
    annotator_quality,
    ensure_adjudication,
    ensure_annotation,
    fast_threshold_seconds,
    resolve_final,
    validate_annotator_name,
)

FORMAT_NAME = "preflight.review_queue"
FORMAT_VERSION = 1
MAX_JSONL_BYTES = 30_000_000
MAX_ITEMS = 20_000
MAX_FIELD_CHARS = 3000          # 외부에서 온 문구 한 칸의 길이 상한 (모델·광고주 입력은 신뢰하지 않는다)
MAX_FACTS = 60
MAX_FINDINGS = 60
MAX_HISTORY = 60

# --- 우선순위 (불확실성 표집) — 가정한 값 ---------------------------------------------------------
# priority = 0.45*(1-확신) + 0.20*[교차 확인 불일치] + 0.20*업종 위험도 + 0.15*[코드 판정 ≠ 합쳐진 판정]   (가중치 합 1.0 → 0~1)
PRIORITY_WEIGHTS = {"uncertainty": 0.45, "disagreement": 0.20, "risk": 0.20, "conflict": 0.15}
# 업종 위험도: 금융(규제 문구가 많은 업종)을 높게 — 이 프로젝트의 정책 카탈로그가 금융 업종에 필수 고지를 요구한다는 점에 근거한 가정이며 실제 업계 통계가 아니다
ASSUMED_CATEGORY_RISK = {"loan": 1.0, "investment": 1.0, "crypto": 1.0, "insurance": 0.8, "health": 0.8, "card": 0.7, "savings": 0.6, "realestate": 0.5,
                         "education": 0.3, "ecommerce": 0.2, "app": 0.2, "other": 0.1}
DEFAULT_CATEGORY_RISK = 0.1
HIGH_RISK_MIN = 0.7             # 이 이상이면 '고위험 업종' 사유 태그
NEUTRAL_CONFIDENCE = 0.5        # 확신 정보가 없는 항목(코드 검사만 본 항목)은 중간으로 본다
# SLA: 우선순위 구간별 처리 기한(시간). 실제 운영 기준이 아니라 이 프로젝트가 가정한 값.
ASSUMED_SLA_HOURS = ((0.6, 4.0), (0.3, 24.0), (0.0, 72.0))
URGENT_WINDOW = timedelta(hours=1)          # 마감이 이 안에 들어오거나 이미 지난 항목은 우선순위보다 먼저 준다
DEFAULT_CLAIM_TTL = timedelta(minutes=30)
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_GOLD_RATE = 0.10
CATEGORY_NAME_KO = {"loan": "대출", "card": "카드", "insurance": "보험", "investment": "투자", "savings": "예·적금", "crypto": "가상자산", "ecommerce": "커머스", "app": "앱·서비스",
                    "education": "교육", "health": "건강", "realestate": "부동산", "other": "기타"}
DEFAULT_THRESHOLD = 0.80        # 자동 판정 확신 기준(core.config 의 기본값과 같다). 낮은 확신 사유 판단에 쓴다


class ItemState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    LABELED = "labeled"
    DISAGREED = "disagreed"
    ADJUDICATED = "adjudicated"
    EXPORTED = "exported"
    SKIPPED = "skipped"


# 허용되는 전이만 적는다. 여기 없는 전이는 IllegalTransition.
TRANSITIONS: dict[ItemState, set[ItemState]] = {
    ItemState.QUEUED: {ItemState.CLAIMED, ItemState.SKIPPED},
    ItemState.CLAIMED: {ItemState.CLAIMED, ItemState.QUEUED, ItemState.LABELED, ItemState.DISAGREED, ItemState.SKIPPED},
    ItemState.LABELED: {ItemState.EXPORTED},
    ItemState.DISAGREED: {ItemState.ADJUDICATED},
    ItemState.ADJUDICATED: {ItemState.EXPORTED},
    ItemState.EXPORTED: set(),
    ItemState.SKIPPED: {ItemState.QUEUED},
}
FINAL_STATES = {ItemState.LABELED, ItemState.ADJUDICATED, ItemState.EXPORTED}
OPEN_STATES = {ItemState.QUEUED, ItemState.CLAIMED}


class QueueReason(str, Enum):
    HUMAN_REVIEW = "human_review"                  # 자동 검사가 확정하지 못해 담당자 확인으로 판정됨
    CROSSCHECK_DISAGREE = "crosscheck_disagree"    # 다른 모델 계열의 교차 확인과 판정이 갈림
    LOW_CONFIDENCE = "low_confidence"              # 검토 모델의 확신이 자동 판정 기준보다 낮음
    HIGH_RISK_CATEGORY = "high_risk_category"      # 금융 등 주의가 필요한 업종
    RANDOM_QC = "random_qc"                        # 무작위 품질 점검 표본
    GOLD = "gold"                                  # 검증용 항목 (라벨러에게는 RANDOM_QC 로 보인다)


# 대기열 목록(점유 전 항목)에 쓰는 쉬운 말. 검증용 항목은 점유 전에는 목록에 없고, 무작위 품질 점검과 같은 말을 쓴다.
REASON_LABEL = {
    QueueReason.HUMAN_REVIEW: "자동 검사가 확정하지 못함",
    QueueReason.CROSSCHECK_DISAGREE: "두 번의 검토 판단이 서로 다름",
    QueueReason.LOW_CONFIDENCE: "검토 모델의 확신이 낮음",
    QueueReason.HIGH_RISK_CATEGORY: "금융 등 주의가 필요한 업종",
    QueueReason.RANDOM_QC: "무작위 품질 점검",
    QueueReason.GOLD: "무작위 품질 점검",
}
# 대표 사유의 순서: 대기열에 들어온 직접적인 이유(불일치·낮은 확신·담당자 확인·무작위 점검)가 먼저, 업종은 그 뒤 — 업종은 다른 이유가 없을 때만 대표 사유가 된다
# 라벨러가 점유한 항목의 화면에는 사유를 항목마다 다르게 쓰지 않고 모든 항목에 같은 말을 쓴다 — 사유가 검증용 항목과 일반 항목을 가르는 단서가 되지 않게.
VIEW_REASON_LABEL = "사람의 확인이 필요한 광고"
_PRIMARY_ORDER = [QueueReason.CROSSCHECK_DISAGREE, QueueReason.LOW_CONFIDENCE, QueueReason.HUMAN_REVIEW, QueueReason.RANDOM_QC, QueueReason.HIGH_RISK_CATEGORY]


# ---------------------------------------------------------------------------
# 오류
# ---------------------------------------------------------------------------
class QueueError(Exception):
    """대기열 오류의 뿌리."""


class IllegalTransition(QueueError):
    """허용되지 않은 상태 전이."""


class UnknownItem(QueueError):
    pass


class DuplicateItem(QueueError):
    pass


class ClaimLimitReached(QueueError):
    """한 사람이 동시에 잡을 수 있는 항목 수 상한."""


class NotClaimed(QueueError):
    """점유하지 않았거나 점유가 만료된 항목에 라벨을 내려 했다."""


class DuplicateAnnotation(QueueError):
    """같은 사람이 같은 항목에 두 번 라벨하려 했다."""


class NotAllowed(QueueError):
    """역할 규칙 위반 (예: 자기가 라벨한 항목을 스스로 조정)."""


class QueueFormatError(QueueError):
    """JSONL 형식·내용 오류."""


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------
def _cap(s: str, n: int = MAX_FIELD_CHARS) -> str:
    s = s if isinstance(s, str) else str(s)
    return s if len(s) <= n else s[:n]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _hash01(s: str) -> float:
    """문자열 → [0,1) 의 고정된 값 (난수 대신 — 같은 입력이면 같은 선택)."""
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:8], 16) / 2 ** 32


def make_id(*parts: str) -> str:
    return "rq_" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]


class ItemSource(BaseModel):
    run_id: str = ""
    variant_id: str = ""
    dataset_id: str = ""


class FindingBrief(BaseModel):
    rule_id: str
    severity: str
    layer: str
    quote: str = ""
    verified_quote: bool = True
    explanation: str = ""

    @field_validator("quote", "explanation", mode="before")
    @classmethod
    def _c(cls, v):
        return _cap(v or "", 300)


class VerdictSnapshot(BaseModel):
    """대기열에 넣을 때의 자동 판정 요약. 나중에 파이프라인이 바뀌어도 '그때 무엇을 보고 사람에게 넘겼는지' 가 남는다."""

    final_decision: str = ""              # Decision 값 (코드만 본 항목은 빈 문자열)
    model_decision: str = ""              # 코드 검사와 검토 모델을 합친 판정 (HUMAN_REVIEW 일 수 있음)
    code_decision: str = ""               # 코드 검사(결정론 계층)만의 판정
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    second_opinion: Optional[str] = None
    disagreement: bool = False
    unverified_quotes: int = 0
    findings: list[FindingBrief] = Field(default_factory=list)
    decision_reason: str = ""
    run_mode: str = ""                    # demo | partial | live | code_only …

    @field_validator("decision_reason", mode="before")
    @classmethod
    def _c(cls, v):
        return _cap(v or "", 500)

    @field_validator("findings")
    @classmethod
    def _f(cls, v):
        return v[:MAX_FINDINGS]

    def suggested_label(self) -> Optional[LabelDecision]:
        """사전 라벨(제안). 합쳐진 판정이 라벨 선택지에 있으면 그것, 아니면(담당자 확인 등) 코드 판정. 둘 다 없으면 None. 확정이 아니다."""
        for d in (self.model_decision, self.code_decision):
            if d in {x.value for x in LabelDecision}:
                return LabelDecision(d)
        return None

    def suggested_rules(self) -> list[str]:
        out: list[str] = []
        for f in self.findings:
            if f.verified_quote and f.severity in (Severity.BLOCK.value, Severity.FIX.value) and f.rule_id not in out:
                out.append(f.rule_id)
        return out

    def suggested_spans(self) -> list[str]:
        out: list[str] = []
        for f in self.findings:
            if f.verified_quote and f.quote and f.severity in (Severity.BLOCK.value, Severity.FIX.value) and 2 <= len(f.quote) <= 200 and f.quote not in out:
                out.append(f.quote)
        return out


class GoldAnswer(BaseModel):
    decision: LabelDecision
    rule_ids: list[str] = Field(default_factory=list)


class ClaimInfo(BaseModel):
    annotator: str
    claimed_at: datetime
    expires_at: datetime


class StateChange(BaseModel):
    at: datetime
    from_state: str
    to_state: str
    actor: str = ""
    note: str = ""


class ReviewItem(BaseModel):
    id: str
    source: ItemSource = Field(default_factory=ItemSource)
    ad_copy: AdCopy
    product_name: str = ""
    category: str = "other"                              # 광고주가 선언한 업종
    categories: list[str] = Field(default_factory=list)  # 선언 업종 + 문구 단서로 넓힌 업종
    facts: list[Fact] = Field(default_factory=list)
    landing_text: str = ""
    verdict: Optional[VerdictSnapshot] = None
    queued_reason: QueueReason = QueueReason.HUMAN_REVIEW
    reason_tags: list[QueueReason] = Field(default_factory=list)
    priority: float = Field(default=0.0, ge=0.0, le=1.0)
    priority_parts: dict[str, float] = Field(default_factory=dict)
    created_at: datetime
    due_at: datetime
    state: ItemState = ItemState.QUEUED
    claims: list[ClaimInfo] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    adjudication: Optional[AdjudicationRecord] = None
    gold: Optional[GoldAnswer] = None                    # 있으면 검증용 항목. 라벨러에게 절대 보이지 않는다
    synthetic: bool = False                              # 합성 예시 (실제 라벨로 세지 않는다)
    first_claimed_at: Optional[datetime] = None
    labeled_at: Optional[datetime] = None
    exported_at: Optional[datetime] = None
    skip_reason: str = ""
    history: list[StateChange] = Field(default_factory=list)
    notes: str = ""                                      # 데이터셋에서 온 항목의 원래 메모 (내보낼 때 합친다)

    @field_validator("landing_text", mode="before")
    @classmethod
    def _l(cls, v):
        return _cap(v or "", 5000)

    @field_validator("product_name", mode="before")
    @classmethod
    def _p(cls, v):
        return _cap(v or "", 200)

    @field_validator("facts")
    @classmethod
    def _facts(cls, v):
        return [Fact(id=_cap(f.id, 40), text=_cap(f.text, 500)) for f in v[:MAX_FACTS]]

    @property
    def is_gold(self) -> bool:
        return self.gold is not None

    def labelers(self) -> list[str]:
        return [a.annotator for a in self.annotations]


class PrelabelView(BaseModel):
    """사전 라벨(자동 검사의 제안). 확정이 아니며 라벨러가 직접 확인해야 라벨이 된다."""

    decision: LabelDecision
    rule_ids: list[str] = Field(default_factory=list)
    cited_spans: list[str] = Field(default_factory=list)
    confidence: Optional[float] = None
    note: str = "자동 검사의 제안일 뿐 확정이 아닙니다. 직접 읽고 판단해 주세요."


class AnnotatorView(BaseModel):
    """라벨러가 받는 항목. 검증용 여부·정답·내부 사유 코드·다른 라벨러의 라벨이 구조적으로 들어 있지 않다 (검증용 항목도 같은 모양)."""

    item_id: str
    ad_copy: AdCopy
    product_name: str
    category: str
    facts: list[Fact]
    landing_text: str
    reason_label: str
    priority: float
    due_at: datetime
    prelabel: Optional[PrelabelView] = None
    prelabel_mode: str = "none"        # shown | hidden | none — 제출할 때 그대로 라벨에 기록한다


@dataclass
class ListRow:
    item_id: str
    priority: float
    reason_label: str
    due_at: datetime
    overdue: bool
    labels: int
    needed: int
    state: str
    headline: str
    category: str
    claimed_by: list[str]
    synthetic: bool


@dataclass
class EnqueueResult:
    added: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)       # 이미 대기열에 있어 다시 넣지 않은 항목
    not_eligible: int = 0                                      # 담당자 확인이 아니어서 담지 않은 보고서 수


@dataclass
class SubmitResult:
    item_id: str
    state: ItemState
    warnings: list[str]
    flags: list[str]
    final: Optional[FinalLabel]


@dataclass
class QueueStats:
    total: int
    by_state: dict[str, int]
    waiting: int                      # 아직 라벨이 모두 모이지 않은 항목 (queued + claimed)
    awaiting_adjudication: int
    sla_breached: int                 # 끝나지 않았는데 마감이 지난 항목
    labels_submitted: int
    throughput_24h: int               # 최근 24시간에 낸 라벨 수
    mean_wait_s: Optional[float]      # 대기열에 들어온 뒤 처음 점유되기까지 평균
    mean_label_s: Optional[float]     # 라벨 한 건에 쓴 평균 시간 (제출한 값)
    oldest_wait_s: Optional[float]    # 아직 점유되지 않은 항목 중 가장 오래 기다린 시간

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 우선순위·SLA·사유
# ---------------------------------------------------------------------------
def category_risk(categories: Iterable[str]) -> float:
    return max((ASSUMED_CATEGORY_RISK.get(c, DEFAULT_CATEGORY_RISK) for c in categories), default=DEFAULT_CATEGORY_RISK)


def compute_priority(*, confidence: Optional[float], disagreement: bool, categories: Iterable[str], conflict: bool) -> tuple[float, dict[str, float]]:
    """불확실성 표집 우선순위(0~1). 확신이 낮을수록·불일치·고위험 업종·코드와 모델의 결론이 다를수록 높다. 다른 조건이 같으면 확신이 낮을수록 같거나 높다(단조)."""
    c = NEUTRAL_CONFIDENCE if confidence is None else min(1.0, max(0.0, float(confidence)))
    w = PRIORITY_WEIGHTS
    parts = {"uncertainty": w["uncertainty"] * (1.0 - c), "disagreement": w["disagreement"] * (1.0 if disagreement else 0.0),
             "risk": w["risk"] * category_risk(categories), "conflict": w["conflict"] * (1.0 if conflict else 0.0)}
    return round(min(1.0, sum(parts.values())), 4), {k: round(v, 4) for k, v in parts.items()}


def sla_hours(priority: float) -> float:
    for lo, hours in ASSUMED_SLA_HOURS:
        if priority >= lo:
            return hours
    return ASSUMED_SLA_HOURS[-1][1]


def reasons_for(verdict: Optional[VerdictSnapshot], categories: Iterable[str], *, threshold: float = DEFAULT_THRESHOLD,
                base: Optional[QueueReason] = None) -> list[QueueReason]:
    """대기열에 들어온 사유(여러 개일 수 있음). 우선순위 순서로 정렬해 돌려준다 — 첫 번째가 대표 사유."""
    tags: set[QueueReason] = set()
    if base is not None:
        tags.add(base)
    if verdict is not None:
        if verdict.disagreement or (verdict.second_opinion and verdict.model_decision and verdict.second_opinion != verdict.model_decision):
            tags.add(QueueReason.CROSSCHECK_DISAGREE)
        # '확신이 기준보다 낮아서' 담당자 확인이 된 경우만. 판정 함수가 남긴 사유에 임계값이 있으면 그것을 따르고(실행마다 기준이 다를 수 있다),
        # 사유가 없으면 확신 < threshold 로 본다. 모델 호출이 실패해 확신이 0 인 경우는 낮은 확신이 아니라 처리 실패다.
        if verdict.final_decision == Decision.HUMAN_REVIEW.value and ("임계값" in verdict.decision_reason or (verdict.confidence is not None and 0 < verdict.confidence < threshold)):
            tags.add(QueueReason.LOW_CONFIDENCE)
        if verdict.final_decision == Decision.HUMAN_REVIEW.value:
            tags.add(QueueReason.HUMAN_REVIEW)
    if category_risk(categories) >= HIGH_RISK_MIN:
        tags.add(QueueReason.HIGH_RISK_CATEGORY)
    if not tags:
        tags.add(QueueReason.RANDOM_QC)
    return [r for r in _PRIMARY_ORDER if r in tags] + [r for r in tags if r not in _PRIMARY_ORDER]


def _code_decision(findings: Sequence) -> str:
    sev = {f.severity for f in findings}
    if Severity.BLOCK in sev:
        return Decision.REJECT.value
    if Severity.FIX in sev:
        return Decision.REVISE.value
    return Decision.APPROVE.value


def snapshot_from_report(report: PreflightReport, run: PipelineRun) -> VerdictSnapshot:
    """보고서 → 판정 요약. 코드 판정은 의미 검사(semantic) 계층이 아닌 finding 만으로 다시 계산한다."""
    v = report.verdict
    code = [f for f in v.findings if f.layer.value != "semantic"]
    return VerdictSnapshot(
        final_decision=report.final_decision.value, model_decision=v.decision.value, code_decision=_code_decision(code), confidence=float(v.confidence),
        second_opinion=v.second_opinion.value if v.second_opinion else None, disagreement=bool(v.disagreement),
        unverified_quotes=sum(1 for f in v.findings if not f.verified_quote),
        findings=[FindingBrief(rule_id=f.rule_id, severity=f.severity.value, layer=f.layer.value, quote=f.quote, verified_quote=f.verified_quote, explanation=f.explanation)
                  for f in v.findings[:MAX_FINDINGS]],
        decision_reason=report.decision_reason, run_mode=run.mode)


def code_only_snapshot(copy: AdCopy, product: ProductFacts, categories: Iterable[str], landing_text: str = "", catalog=None) -> VerdictSnapshot:
    """모델 없이 코드 검사(결정론 가드레일)만 돌린 판정 요약 — 데이터셋에서 온 항목과 검증용 항목에 쓴다. 네트워크를 쓰지 않는다."""
    from guardrails import output_guard   # 지연 import: 대기열 모듈 자체는 가볍게 유지

    det = output_guard.check(copy, product, set(categories) or {product.category.value}, landing_text, [], catalog)
    return VerdictSnapshot(
        final_decision="", model_decision="", code_decision=det.decision.value, confidence=None, second_opinion=None, disagreement=False,
        findings=[FindingBrief(rule_id=f.rule_id, severity=f.severity.value, layer=f.layer.value, quote=f.quote, verified_quote=f.verified_quote, explanation=f.explanation)
                  for f in det.findings[:MAX_FINDINGS]],
        decision_reason="코드 검사만 수행(검토 모델 없음)", run_mode="code_only")


def _safe_category(value: str) -> ProductCategory:
    return ProductCategory(value) if value in {c.value for c in ProductCategory} else ProductCategory.OTHER


def _copy_snapshot(copy: AdCopy) -> AdCopy:
    c = copy.model_copy(deep=True)
    c.headline, c.body, c.cta = _cap(c.headline), _cap(c.body), _cap(c.cta)
    c.disclosures = [_cap(d) for d in c.disclosures[:20]]
    c.claims = [Claim(text=_cap(x.text, 500), fact_id=x.fact_id) for x in c.claims[:30]]
    return c


# ---------------------------------------------------------------------------
# 대기열
# ---------------------------------------------------------------------------
class ReviewQueue:
    def __init__(self, *, required_labels: int = 2, max_concurrent_claims: int = DEFAULT_MAX_CONCURRENT, claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
                 gold_rate: float = DEFAULT_GOLD_RATE, seed: str = "rq", clock: Optional[Callable[[], datetime]] = None):
        if required_labels < 1:
            raise ValueError("필요한 라벨 수는 1 이상이어야 합니다")
        if max_concurrent_claims < 1:
            raise ValueError("동시 점유 상한은 1 이상이어야 합니다")
        if not 0.0 <= gold_rate <= 1.0:
            raise ValueError("검증용 항목 비율은 0~1 이어야 합니다")
        self.required_labels = required_labels
        self.max_concurrent_claims = max_concurrent_claims
        self.claim_ttl = claim_ttl
        self.gold_rate = gold_rate
        self.seed = seed
        self._clock = clock or utcnow
        self._items: dict[str, ReviewItem] = {}
        self._gold: dict[str, ReviewItem] = {}      # 검증용 항목 틀 (라벨러마다 따로 복제해 낸다)

    # --- 기본 ---------------------------------------------------------------------------
    def _now(self) -> datetime:
        return _aware(self._clock())

    def now(self) -> datetime:
        """대기열이 쓰는 현재 시각 (주입한 시계)."""
        return self._now()

    def get(self, item_id: str) -> ReviewItem:
        it = self._items.get(item_id)
        if it is None:
            raise UnknownItem(f"대기열에 없는 항목입니다: {item_id}")
        return it

    def __len__(self) -> int:
        return len(self._items)

    def items(self, state: Optional[ItemState] = None, *, include_gold: bool = False, include_synthetic: bool = True) -> list[ReviewItem]:
        return [it for it in self._items.values() if (state is None or it.state == state) and (include_gold or not it.is_gold) and (include_synthetic or not it.synthetic)]

    def _set_state(self, it: ReviewItem, new: ItemState, *, actor: str = "", note: str = "") -> None:
        if new not in TRANSITIONS[it.state]:
            raise IllegalTransition(f"{it.id}: {it.state.value} → {new.value} 는 허용되지 않는 전이입니다")
        it.history.append(StateChange(at=self._now(), from_state=it.state.value, to_state=new.value, actor=actor, note=note))
        del it.history[:-MAX_HISTORY]
        it.state = new

    # --- 적재 ---------------------------------------------------------------------------
    def add(self, item: ReviewItem) -> ReviewItem:
        if item.id in self._items:
            raise DuplicateItem(f"이미 대기열에 있는 항목입니다: {item.id}")
        if len(self._items) >= MAX_ITEMS:
            raise QueueError(f"대기열은 {MAX_ITEMS}건까지 담습니다")
        self._items[item.id] = item
        return item

    def _build_item(self, *, item_id: str, source: ItemSource, copy: AdCopy, product_name: str, category: str, categories: Sequence[str], facts: Sequence[Fact],
                    landing_text: str, verdict: Optional[VerdictSnapshot], threshold: float, base_reason: Optional[QueueReason], synthetic: bool = False,
                    gold: Optional[GoldAnswer] = None, notes: str = "") -> ReviewItem:
        now = self._now()
        cats = sorted(set(categories) | {category})
        tags = reasons_for(verdict, cats, threshold=threshold, base=base_reason)
        conflict = bool(verdict and verdict.code_decision and verdict.model_decision in {d.value for d in LabelDecision} and verdict.code_decision != verdict.model_decision)
        prio, parts = compute_priority(confidence=verdict.confidence if verdict else None, disagreement=bool(verdict and verdict.disagreement), categories=cats, conflict=conflict)
        return ReviewItem(id=item_id, source=source, ad_copy=_copy_snapshot(copy), product_name=product_name, category=category, categories=cats, facts=list(facts),
                          landing_text=landing_text, verdict=verdict, queued_reason=tags[0], reason_tags=tags, priority=prio, priority_parts=parts, created_at=now,
                          due_at=now + timedelta(hours=sla_hours(prio)), gold=gold, synthetic=synthetic, notes=_cap(notes, 500))

    def enqueue_from_run(self, run: PipelineRun, *, threshold: float = DEFAULT_THRESHOLD, landing_text: str = "", qc_rate: float = 0.0, qc_seed: str = "qc") -> EnqueueResult:
        """실행 결과에서 '담당자 확인 필요' 보고서만 담는다. 같은 (실행, 광고안) 은 두 번 담지 않는다.
        qc_rate > 0 이면 담당자 확인이 아닌 보고서도 해시로 정한 비율만큼 '무작위 품질 점검' 표본으로 담는다 (기본 0 — 끔)."""
        res = EnqueueResult()
        product = run.brief.product
        categories = list(run.categories) or [product.category.value]
        for rep in run.reports:
            iid = make_id("run", run.run_id, rep.variant_id)
            base: Optional[QueueReason]
            if rep.final_decision == Decision.HUMAN_REVIEW:
                base = QueueReason.HUMAN_REVIEW
            elif qc_rate > 0 and _hash01(f"{qc_seed}|{run.run_id}|{rep.variant_id}") < qc_rate:
                base = QueueReason.RANDOM_QC
            else:
                res.not_eligible += 1
                continue
            if iid in self._items:
                res.duplicates.append(iid)
                continue
            item = self._build_item(item_id=iid, source=ItemSource(run_id=run.run_id, variant_id=rep.variant_id), copy=rep.ad_copy, product_name=product.name,
                                    category=product.category.value, categories=categories, facts=product.facts, landing_text=landing_text,
                                    verdict=snapshot_from_report(rep, run), threshold=threshold, base_reason=base)
            self.add(item)
            res.added.append(iid)
        return res

    def enqueue_ad(self, copy: AdCopy, *, dataset_id: str, category: str, facts: Sequence[Fact] = (), landing_text: str = "", product_name: str = "",
                   reason: QueueReason = QueueReason.RANDOM_QC, synthetic: bool = False, notes: str = "", catalog=None,
                   verdict: Optional[VerdictSnapshot] = None) -> ReviewItem:
        """데이터셋·직접 입력 광고를 대기열에 넣는다. verdict 를 주지 않으면 모델 없이 코드 검사만 돌려 사전 라벨 근거를 만든다."""
        cat = _safe_category(category)
        name = product_name or f"{CATEGORY_NAME_KO.get(cat.value, '기타')} 상품"    # 데이터셋 id 를 상품명으로 쓰지 않는다 — 라벨러 화면에 출처가 새지 않게
        product = ProductFacts(name=name, category=cat, facts=list(facts))
        snap = verdict if verdict is not None else code_only_snapshot(copy, product, {cat.value}, landing_text, catalog)
        item = self._build_item(item_id=make_id("dataset", dataset_id), source=ItemSource(dataset_id=dataset_id), copy=copy, product_name=name,
                                category=cat.value, categories=[cat.value], facts=facts, landing_text=landing_text, verdict=snap, threshold=DEFAULT_THRESHOLD,
                                base_reason=reason, synthetic=synthetic, notes=notes)
        return self.add(item)

    def enqueue_gold(self, rows: Iterable[Mapping]) -> int:
        """정답을 아는 검증용 항목을 풀에 넣는다 (골든셋 형식의 행: id·category·headline·body·cta·label·rule_ids·disclosures·facts·claims·landing_text).
        풀에 넣을 뿐 바로 대기열에 보이지 않는다 — `claim` 이 라벨러마다 따로 복제해 일반 항목과 같은 모양으로 내보낸다.
        정답은 가이드라인 작성자의 라벨이며 독립 검증이 아니다. 이미 있는 id 는 건너뛴다. 새로 넣은 개수를 돌려준다."""
        added = 0
        for row in rows:
            rid = str(row.get("id", "")).strip()
            tid = make_id("gold", rid)
            if not rid or tid in self._gold:
                continue
            label = str(row.get("label", "")).upper()
            if label not in {d.value for d in LabelDecision}:
                continue
            copy = AdCopy(variant_id=rid, headline=str(row.get("headline", "")), body=str(row.get("body", "")), cta=str(row.get("cta", "")),
                          disclosures=[str(d) for d in row.get("disclosures", [])], claims=[Claim(**c) for c in row.get("claims", [])])
            facts = [Fact(**f) for f in row.get("facts", [])]
            cat = _safe_category(str(row.get("category", "other")))
            name = f"{CATEGORY_NAME_KO.get(cat.value, '기타')} 상품"
            product = ProductFacts(name=name, category=cat, facts=facts)
            snap = code_only_snapshot(copy, product, {cat.value}, str(row.get("landing_text", "")))
            tmpl = self._build_item(item_id=tid, source=ItemSource(dataset_id=rid), copy=copy, product_name=name, category=cat.value, categories=[cat.value], facts=facts,
                                    landing_text=str(row.get("landing_text", "")), verdict=snap, threshold=DEFAULT_THRESHOLD, base_reason=QueueReason.GOLD,
                                    gold=GoldAnswer(decision=LabelDecision(label), rule_ids=[str(r) for r in row.get("rule_ids", [])]))
            self._gold[tid] = tmpl
            added += 1
        return added

    @property
    def gold_pool_size(self) -> int:
        return len(self._gold)

    # --- 점유 ---------------------------------------------------------------------------
    def _active_claims_of(self, name: str) -> list[ReviewItem]:
        return [it for it in self._items.values() if any(c.annotator == name for c in it.claims)]

    def _touched_count(self, name: str) -> int:
        return sum(1 for it in self._items.values() if name in it.labelers() or any(c.annotator == name for c in it.claims))

    def _eligible(self, it: ReviewItem, name: str) -> bool:
        return (not it.is_gold and it.state in OPEN_STATES and name not in it.labelers() and not any(c.annotator == name for c in it.claims)
                and len(it.annotations) + len(it.claims) < self.required_labels)

    def _order_key(self, it: ReviewItem, now: datetime):
        urgent = it.due_at - now <= URGENT_WINDOW
        return (not urgent, -it.priority, it.due_at, it.created_at, it.id)

    def _take(self, it: ReviewItem, name: str, now: datetime) -> None:
        self._set_state(it, ItemState.CLAIMED, actor=name, note="점유")
        it.claims.append(ClaimInfo(annotator=name, claimed_at=now, expires_at=now + self.claim_ttl))
        if it.first_claimed_at is None:
            it.first_claimed_at = now

    def reap_expired(self) -> list[tuple[str, str]]:
        """오래 잡고 있던 점유를 회수한다 → [(항목 id, 라벨러)]. 라벨을 낸 적 없는 항목은 대기 상태로 돌아간다."""
        now = self._now()
        out: list[tuple[str, str]] = []
        for it in self._items.values():
            gone = [c for c in it.claims if c.expires_at <= now]
            if not gone:
                continue
            it.claims = [c for c in it.claims if c.expires_at > now]
            out.extend((it.id, c.annotator) for c in gone)
            if it.state == ItemState.CLAIMED:
                self._set_state(it, ItemState.CLAIMED if it.claims else ItemState.QUEUED, actor="system", note="점유 만료")
        return out

    def claim(self, annotator: str, *, hide_prelabel: bool = True) -> Optional[AnnotatorView]:
        """다음에 라벨할 항목을 받는다. 우선순위 순(마감이 임박했거나 지난 항목이 먼저), 이미 이 사람이 라벨한 항목은 주지 않는다.
        가끔(검증용 비율만큼, 해시로 정한 자리에) 검증용 항목이 섞여 나온다 — 받는 쪽은 구분할 수 없다. 줄 항목이 없으면 None."""
        name = validate_annotator_name(annotator)
        self.reap_expired()
        now = self._now()
        if len(self._active_claims_of(name)) >= self.max_concurrent_claims:
            raise ClaimLimitReached(f"동시에 잡을 수 있는 항목은 {self.max_concurrent_claims}건까지입니다. 먼저 라벨을 내거나 반납해 주세요.")
        cand = [it for it in self._items.values() if self._eligible(it, name)]
        if not cand:
            return None
        k = self._touched_count(name)
        if self._gold and _hash01(f"{self.seed}|gold|{name}|{k}") < self.gold_rate:
            g = self._gold_for(name, now)
            if g is not None:
                return self.view(g.id, name, hide_prelabel=hide_prelabel)
        it = min(cand, key=lambda x: self._order_key(x, now))
        self._take(it, name, now)
        return self.view(it.id, name, hide_prelabel=hide_prelabel)

    def claim_item(self, annotator: str, item_id: str, *, hide_prelabel: bool = True) -> AnnotatorView:
        """특정 항목을 직접 잡는다 (조회·테스트·합성 예시용). 점유 규칙은 `claim` 과 같다."""
        name = validate_annotator_name(annotator)
        self.reap_expired()
        it = self.get(item_id)
        if len(self._active_claims_of(name)) >= self.max_concurrent_claims:
            raise ClaimLimitReached(f"동시에 잡을 수 있는 항목은 {self.max_concurrent_claims}건까지입니다.")
        if name in it.labelers():
            raise DuplicateAnnotation(f"{name} 님은 이미 이 항목에 라벨했습니다. 같은 사람이 같은 광고를 두 번 라벨할 수 없습니다.")
        if not self._eligible(it, name):
            raise IllegalTransition(f"{it.id} 는 지금 {name} 님이 잡을 수 없는 상태입니다 ({it.state.value})")
        self._take(it, name, self._now())
        return self.view(it.id, name, hide_prelabel=hide_prelabel)

    def _gold_for(self, name: str, now: datetime) -> Optional[ReviewItem]:
        """이 라벨러가 아직 받지 않은 검증용 항목을 복제해 점유시킨다. 전에 반납한 검증용 항목이 있으면 그것부터 다시 준다."""
        mine = [it for it in self._items.values() if it.is_gold and it.gold is not None and it.id in self._instances_of(name)]
        for it in mine:
            if it.state == ItemState.QUEUED and not it.annotations:
                self._take(it, name, now)
                return it
        seen = {it.source.dataset_id for it in mine}
        pool = [t for t in self._gold.values() if t.source.dataset_id not in seen]
        if not pool:
            return None
        pool.sort(key=lambda t: _hash01(f"{self.seed}|pick|{name}|{t.id}"))
        t = pool[0]
        inst = t.model_copy(deep=True)
        inst.id = make_id("gold", t.source.dataset_id, name)
        inst.created_at = now
        inst.due_at = now + timedelta(hours=sla_hours(inst.priority))
        inst.state = ItemState.QUEUED
        self.add(inst)
        self._take(inst, name, now)
        return inst

    def _instances_of(self, name: str) -> set[str]:
        return {make_id("gold", t.source.dataset_id, name) for t in self._gold.values()}

    def held_by(self, annotator: str) -> list[str]:
        """이 사람이 지금 점유하고 있는 항목 id (만료된 점유는 먼저 회수한다). 화면이 '내가 잡은 광고' 를 따로 기억하지 않고 큐에서 바로 읽게 한다."""
        name = validate_annotator_name(annotator)
        self.reap_expired()
        return [it.id for it in self._active_claims_of(name)]

    def release(self, annotator: str, item_id: str) -> None:
        """점유를 반납한다 (라벨을 내지 않고)."""
        name = validate_annotator_name(annotator)
        it = self.get(item_id)
        mine = [c for c in it.claims if c.annotator == name]
        if not mine:
            raise NotClaimed(f"{name} 님이 잡고 있는 항목이 아닙니다: {item_id}")
        it.claims = [c for c in it.claims if c.annotator != name]
        self._set_state(it, ItemState.CLAIMED if it.claims else ItemState.QUEUED, actor=name, note="반납")

    # --- 라벨 -----------------------------------------------------------------------------
    def view(self, item_id: str, annotator: str, *, hide_prelabel: bool = True) -> AnnotatorView:
        """라벨러에게 보여 줄 모양. 검증용 항목과 일반 항목이 같은 필드 구성을 가진다."""
        it = self.get(item_id)
        pre: Optional[PrelabelView] = None
        mode = "none"
        sug = it.verdict.suggested_label() if it.verdict else None
        if sug is not None:
            mode = "hidden" if hide_prelabel else "shown"
            if not hide_prelabel and it.verdict is not None:
                pre = PrelabelView(decision=sug, rule_ids=it.verdict.suggested_rules(), cited_spans=it.verdict.suggested_spans(), confidence=it.verdict.confidence)
        # 광고안 id·어조·이어받은 사실 id 는 출처(데이터셋 id 등)를 드러낼 수 있어 모두 같은 모양으로 지운 사본을 준다
        shown = it.ad_copy.model_copy(deep=True, update={"variant_id": "var_" + it.id[3:], "tone": "", "carried_fact_ids": []})
        # 사실 id 도 출처를 담을 수 있다. 화면용 별칭으로 바꾸되 주장→사실 연결은 함께 옮기고, 없는 사실은 근거 없음으로 둔다.
        fact_ids = {f.id: f"F{i}" for i, f in enumerate(it.facts, 1)}
        facts = [f.model_copy(update={"id": fact_ids[f.id]}) for f in it.facts]
        for claim in shown.claims:
            claim.fact_id = fact_ids.get(claim.fact_id)
        return AnnotatorView(item_id=it.id, ad_copy=shown, product_name=it.product_name, category=it.category,
                             facts=facts, landing_text=it.landing_text, reason_label=VIEW_REASON_LABEL,
                             priority=it.priority, due_at=it.due_at, prelabel=pre, prelabel_mode=mode)

    def submit(self, annotation: Annotation) -> SubmitResult:
        """라벨을 받는다. 점유한 사람만 낼 수 있고, 같은 사람이 같은 항목에 두 번 낼 수 없다. 인용·규칙 검사에 걸리면 `AnnotationRejected`
        (점유는 유지되어 고쳐서 다시 낼 수 있다). 사전 라벨 기록과 `too_fast` 표시는 호출자 값이 아니라 큐가 채운다."""
        if not isinstance(annotation, Annotation):
            raise TypeError("라벨은 Annotation 이어야 합니다 (사전 라벨 같은 제안은 라벨이 될 수 없습니다)")
        self.reap_expired()
        it = self.get(annotation.item_id)
        name = annotation.annotator
        if name in it.labelers():
            raise DuplicateAnnotation(f"{name} 님은 이미 이 항목에 라벨했습니다. 같은 사람이 같은 광고를 두 번 라벨할 수 없습니다.")
        mine = [c for c in it.claims if c.annotator == name]
        if it.state != ItemState.CLAIMED or not mine:
            raise NotClaimed("이 항목을 점유하고 있지 않거나 점유 시간이 지났습니다. 다시 받아 주세요.")
        check = ensure_annotation(annotation, it.ad_copy)          # 인용이 문구에 없으면 여기서 거부된다
        now = self._now()
        # 시간을 안 적었으면 큐가 점유 시각부터 잰다. 적었으면 그 값을 쓴다 (check_annotation 이 이미 too_fast 를 판단했다)
        measured = annotation.seconds_spent is None
        seconds = max(0.0, (now - mine[0].claimed_at).total_seconds()) if measured else annotation.seconds_spent
        flags = list(check.flags)
        if measured and seconds < fast_threshold_seconds(it.ad_copy) and "too_fast" not in flags:
            flags.append("too_fast")
        sug = it.verdict.suggested_label() if it.verdict else None
        mode = annotation.prelabel_mode if sug is not None else "none"
        stored = annotation.model_copy(update={"seconds_spent": seconds, "created_at": now, "flags": flags, "prelabel_decision": sug, "prelabel_mode": mode})
        it.claims = [c for c in it.claims if c.annotator != name]
        it.annotations.append(stored)
        n = len(it.annotations)
        required = 1 if it.is_gold else self.required_labels
        if n >= required:
            same = len({a.decision for a in it.annotations}) == 1
            self._set_state(it, ItemState.LABELED if same else ItemState.DISAGREED, actor=name, note="라벨 제출")
            it.labeled_at = now
        else:
            self._set_state(it, ItemState.CLAIMED if it.claims else ItemState.QUEUED, actor=name, note="라벨 제출(더 필요)")
        return SubmitResult(item_id=it.id, state=it.state, warnings=check.warnings, flags=flags,
                            final=resolve_final(it.annotations, it.adjudication, required) if it.state in FINAL_STATES else None)

    def adjudicate(self, record: AdjudicationRecord) -> FinalLabel:
        """라벨이 갈린 항목의 최종 라벨을 정한다. 그 항목을 라벨한 사람은 조정할 수 없다."""
        it = self.get(record.item_id)
        if it.state != ItemState.DISAGREED:
            raise IllegalTransition(f"{it.id}: 라벨이 갈린(disagreed) 항목만 조정할 수 있습니다 (지금 {it.state.value})")
        if record.adjudicator in it.labelers():
            raise NotAllowed("자기가 라벨한 항목은 스스로 조정할 수 없습니다. 제3의 조정자가 필요합니다.")
        ensure_adjudication(record, it.ad_copy)
        it.adjudication = record.model_copy(update={"created_at": self._now()})
        self._set_state(it, ItemState.ADJUDICATED, actor=record.adjudicator, note="조정")
        final = resolve_final(it.annotations, it.adjudication, self.required_labels)
        assert final is not None
        return final

    def skip(self, item_id: str, *, actor: str = "", reason: str = "") -> None:
        it = self.get(item_id)
        self._set_state(it, ItemState.SKIPPED, actor=actor, note=reason)
        it.claims = []
        it.skip_reason = _cap(reason, 300)

    def reopen(self, item_id: str, *, actor: str = "") -> None:
        it = self.get(item_id)
        self._set_state(it, ItemState.QUEUED, actor=actor, note="다시 열기")
        it.skip_reason = ""

    def mark_exported(self, item_ids: Iterable[str]) -> int:
        """내보낸 항목을 exported 로 바꾼다. 검증용 항목과 확정되지 않은 항목은 거부(IllegalTransition)."""
        n = 0
        now = self._now()
        for iid in item_ids:
            it = self.get(iid)
            if it.is_gold:
                raise IllegalTransition("검증용 항목은 내보낼 수 없습니다")
            self._set_state(it, ItemState.EXPORTED, actor="export", note="내보냄")
            it.exported_at = now
            n += 1
        return n

    # --- 조회 ---------------------------------------------------------------------------
    def list_open(self, *, include_synthetic: bool = True, include_claimed: bool = False) -> list[ListRow]:
        """대기열 목록 — 지금 점유되지 않은 광고, 배정되는 순서대로. 검증용 항목은 없다. 점유된 광고는 기본으로 뺀다: 점유한 사람의 화면에서
        '목록에 있는데 내가 받은 광고' 와 '목록에 없는 광고(검증용)' 가 갈리지 않게 하려는 것이다. 관리용으로는 include_claimed=True."""
        now = self._now()
        rows = []
        for it in sorted([x for x in self._items.values() if not x.is_gold and x.state in OPEN_STATES and (include_claimed or not x.claims) and (include_synthetic or not x.synthetic)],
                         key=lambda x: self._order_key(x, now)):
            rows.append(ListRow(item_id=it.id, priority=it.priority, reason_label=REASON_LABEL[it.queued_reason], due_at=it.due_at, overdue=it.due_at < now,
                                labels=len(it.annotations), needed=self.required_labels, state=it.state.value, headline=it.ad_copy.headline, category=it.category,
                                claimed_by=[c.annotator for c in it.claims], synthetic=it.synthetic))
        return rows

    def disagreements(self) -> list[ReviewItem]:
        return [it for it in self._items.values() if it.state == ItemState.DISAGREED and not it.is_gold]

    def annotations(self, *, include_gold: bool = False) -> list[Annotation]:
        return [a for it in self._items.values() if include_gold or not it.is_gold for a in it.annotations]

    def final_labels(self, *, include_gold: bool = False) -> dict[str, FinalLabel]:
        out: dict[str, FinalLabel] = {}
        for it in self._items.values():
            if it.state in FINAL_STATES and (include_gold or not it.is_gold):
                f = resolve_final(it.annotations, it.adjudication, 1 if it.is_gold else self.required_labels)
                if f is not None:
                    out[it.id] = f
        return out

    def quality(self) -> list[AnnotatorQuality]:
        """라벨러별 품질 — 검증용 항목 정확도, 평균 처리 시간, 합의 이탈 빈도."""
        gold = {it.id: it.gold.decision for it in self._items.values() if it.gold is not None}
        finals = {i: f.decision for i, f in self.final_labels().items()}
        return annotator_quality(self.annotations(include_gold=True), gold, finals)

    def stats(self, *, include_gold: bool = False) -> QueueStats:
        now = self._now()
        its = [it for it in self._items.values() if include_gold or not it.is_gold]
        by_state: dict[str, int] = {s.value: 0 for s in ItemState}
        for it in its:
            by_state[it.state.value] += 1
        unfinished = [it for it in its if it.state in OPEN_STATES or it.state == ItemState.DISAGREED]
        anns = [a for it in its for a in it.annotations]
        waits = [(it.first_claimed_at - it.created_at).total_seconds() for it in its if it.first_claimed_at is not None]
        secs = [a.seconds_spent for a in anns if a.seconds_spent is not None]
        unclaimed = [it for it in its if it.state == ItemState.QUEUED and it.first_claimed_at is None]
        return QueueStats(
            total=len(its), by_state=by_state, waiting=by_state["queued"] + by_state["claimed"], awaiting_adjudication=by_state["disagreed"],
            sla_breached=sum(1 for it in unfinished if it.due_at < now), labels_submitted=len(anns),
            throughput_24h=sum(1 for a in anns if now - _aware(a.created_at) <= timedelta(hours=24)),
            mean_wait_s=(sum(waits) / len(waits) if waits else None), mean_label_s=(sum(secs) / len(secs) if secs else None),
            oldest_wait_s=(max((now - it.created_at).total_seconds() for it in unclaimed) if unclaimed else None))

    # --- 저장 ---------------------------------------------------------------------------
    def to_jsonl(self, *, include_gold: bool = True) -> str:
        """대기열을 JSONL 문자열로. 첫 줄은 형식 머리글. 파일에 쓰는 것은 호출자 몫이다.
        include_gold=False 면 검증용 항목과 정답을 빼고 내보낸다 (라벨러에게 줄 사본용 — 이 사본을 다시 불러오면 검증용 품질은 잴 수 없다)."""
        cfg = {"required_labels": self.required_labels, "max_concurrent_claims": self.max_concurrent_claims, "claim_ttl_s": self.claim_ttl.total_seconds(),
               "gold_rate": self.gold_rate, "seed": self.seed}
        lines = [json.dumps({"format": FORMAT_NAME, "version": FORMAT_VERSION, "config": cfg, "guideline_version": GUIDELINE_VERSION}, ensure_ascii=False)]
        for it in self._items.values():
            if it.is_gold and not include_gold:
                continue
            lines.append(json.dumps({"kind": "item", "data": it.model_dump(mode="json")}, ensure_ascii=False))
        if include_gold:
            for t in self._gold.values():
                lines.append(json.dumps({"kind": "gold_template", "data": t.model_dump(mode="json")}, ensure_ascii=False))
        return "\n".join(lines) + "\n"

    @classmethod
    def from_jsonl(cls, text: str, *, clock: Optional[Callable[[], datetime]] = None) -> "ReviewQueue":
        """JSONL 을 불러온다. 형식 이름·버전, 각 항목의 스키마, 상태와 라벨의 일관성, 라벨의 인용을 다시 검증하고, 어긋나면 `QueueFormatError`."""
        if len(text.encode("utf-8")) > MAX_JSONL_BYTES:
            raise QueueFormatError(f"파일이 너무 큽니다 (상한 {MAX_JSONL_BYTES // 1_000_000}MB)")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise QueueFormatError("빈 파일입니다")
        try:
            head = json.loads(lines[0])
        except json.JSONDecodeError as e:
            raise QueueFormatError(f"1번째 줄을 읽을 수 없습니다: {e}") from e
        if not isinstance(head, dict) or head.get("format") != FORMAT_NAME:
            raise QueueFormatError("검수 대기열 파일이 아닙니다 (형식 이름이 다릅니다)")
        if head.get("version") != FORMAT_VERSION:
            raise QueueFormatError(f"지원하지 않는 형식 버전입니다: {head.get('version')} (지원: {FORMAT_VERSION})")
        cfg = head.get("config") or {}
        try:
            q = cls(required_labels=int(cfg.get("required_labels", 2)), max_concurrent_claims=int(cfg.get("max_concurrent_claims", DEFAULT_MAX_CONCURRENT)),
                    claim_ttl=timedelta(seconds=float(cfg.get("claim_ttl_s", DEFAULT_CLAIM_TTL.total_seconds()))), gold_rate=float(cfg.get("gold_rate", DEFAULT_GOLD_RATE)),
                    seed=str(cfg.get("seed", "rq")), clock=clock)
        except (ValueError, TypeError) as e:
            raise QueueFormatError(f"머리글 설정이 올바르지 않습니다: {e}") from e
        if len(lines) - 1 > MAX_ITEMS * 2:
            raise QueueFormatError("항목이 너무 많습니다")
        for no, ln in enumerate(lines[1:], start=2):
            try:
                rec = json.loads(ln)
                kind, data = rec["kind"], rec["data"]
                item = ReviewItem.model_validate(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as e:
                raise QueueFormatError(f"{no}번째 줄을 읽을 수 없습니다: {str(e)[:200]}") from e
            if kind == "gold_template":
                if item.gold is None:
                    raise QueueFormatError(f"{no}번째 줄: 검증용 틀에 정답이 없습니다")
                q._gold[item.id] = item
            elif kind == "item":
                if item.id in q._items:
                    raise QueueFormatError(f"{no}번째 줄: 항목 id 가 중복입니다 ({item.id})")
                q._validate_loaded(item, no)
                q._items[item.id] = item
            else:
                raise QueueFormatError(f"{no}번째 줄: 알 수 없는 종류입니다 ({kind})")
        return q

    def _validate_loaded(self, it: ReviewItem, line_no: int) -> None:
        def bad(msg: str):
            raise QueueFormatError(f"{line_no}번째 줄({it.id}): {msg}")

        names = [a.annotator for a in it.annotations]
        if len(set(names)) != len(names):
            bad("같은 사람이 같은 항목에 두 번 라벨한 기록이 있습니다")
        for a in it.annotations:
            if a.item_id != it.id:
                bad("라벨의 항목 id 가 항목과 다릅니다")
            res = ensure_annotation_quiet(a, it.ad_copy)
            if res:
                bad("라벨 검증 실패 — " + res)
        required = 1 if it.is_gold else self.required_labels
        n = len(it.annotations)
        decisions = {a.decision for a in it.annotations}
        if it.state in OPEN_STATES and n >= required:
            bad(f"라벨이 {n}건 모였는데 상태가 {it.state.value} 입니다")
        if it.state == ItemState.LABELED and (n < required or len(decisions) != 1):
            bad("labeled 인데 라벨이 부족하거나 판정이 갈려 있습니다")
        if it.state == ItemState.DISAGREED and (n < 2 or len(decisions) < 2):
            bad("disagreed 인데 판정이 갈린 라벨이 없습니다")
        if it.state == ItemState.ADJUDICATED and it.adjudication is None:
            bad("adjudicated 인데 조정 기록이 없습니다")
        if it.adjudication is not None:
            if it.adjudication.adjudicator in names:
                bad("조정자가 그 항목의 라벨러입니다")
            if it.adjudication.item_id != it.id:
                bad("조정 기록의 항목 id 가 다릅니다")
        if it.state == ItemState.EXPORTED and resolve_final(it.annotations, it.adjudication, required) is None:
            bad("exported 인데 확정된 라벨이 없습니다")
        if it.claims and it.state not in (ItemState.CLAIMED,):
            bad("점유 기록이 있는데 점유 상태가 아닙니다")


def ensure_annotation_quiet(a: Annotation, copy: AdCopy) -> str:
    """불러오기 검증용: 오류가 있으면 문장, 없으면 빈 문자열."""
    try:
        ensure_annotation(a, copy)
    except AnnotationRejected as e:
        return str(e)
    return ""


# ---------------------------------------------------------------------------
# 합성 예시 (두 명의 가상 라벨러) — 실제 라벨로 세지 않는다
# ---------------------------------------------------------------------------
def build_demo_queue(records: Sequence[Mapping], *, clock: Optional[Callable[[], datetime]] = None) -> ReviewQueue:
    """`evals/datasets/labeling_demo.jsonl` 의 합성 기록으로 대기열을 만든다. 모든 항목은 synthetic=True.
    기록은 실제 점유·제출 경로(`claim_item`·`submit`·`adjudicate`)를 거쳐 들어가므로 인용·규칙 검사와 상태 전이가 그대로 적용된다."""
    q = ReviewQueue(required_labels=2, max_concurrent_claims=5, gold_rate=0.0, seed="demo", clock=clock)
    for rec in records:
        copy = AdCopy(variant_id=rec["id"], headline=rec["headline"], body=rec["body"], cta=rec["cta"], disclosures=list(rec.get("disclosures", [])),
                      claims=[Claim(**c) for c in rec.get("claims", [])])
        item = q.enqueue_ad(copy, dataset_id=rec["id"], category=rec.get("category", "other"), facts=[Fact(**f) for f in rec.get("facts", [])],
                            landing_text=rec.get("landing_text", ""), synthetic=True, notes=rec.get("notes", ""))
        for a in rec.get("annotations", []):
            q.claim_item(a["annotator"], item.id, hide_prelabel=True)
            q.submit(Annotation(item_id=item.id, annotator=a["annotator"], decision=LabelDecision(a["decision"]), rule_ids=a.get("rule_ids", []),
                                cited_spans=a.get("cited_spans", []), rationale=a.get("rationale", ""), seconds_spent=a.get("seconds_spent"),
                                prelabel_mode="hidden"))
        adj = rec.get("adjudication")
        if adj:
            q.adjudicate(AdjudicationRecord(item_id=item.id, adjudicator=adj["adjudicator"], decision=LabelDecision(adj["decision"]), rule_ids=adj.get("rule_ids", []),
                                            cited_spans=adj.get("cited_spans", []), reason=adj["reason"]))
    return q
