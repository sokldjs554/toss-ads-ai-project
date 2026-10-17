"""도메인 스키마 (내부용).

LLM 에게 보여 주는 출력 스키마는 `core/llm_schemas.py` 에 따로 둔다.
이유: OpenAI Structured Outputs(strict) 는 기본값·범위 제약(ge/le)·pattern 을 허용하지 않으므로
LLM 출력 스키마는 '평평하고 전부 required' 여야 하고, 내부 스키마는 검증기(validator)를 가진다.
LLM 출력 → 내부 스키마 변환 지점이 곧 '모델 출력을 신뢰하지 않는' 검증 지점이다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------
class ProductCategory(str, Enum):
    LOAN = "loan"            # 대출 (신용·담보·대부)
    CARD = "card"            # 카드
    INSURANCE = "insurance"  # 보험
    INVESTMENT = "investment"  # 투자·증권·펀드
    SAVINGS = "savings"      # 예·적금
    CRYPTO = "crypto"        # 가상자산
    ECOMMERCE = "ecommerce"  # 커머스
    APP = "app"              # 앱·서비스
    EDUCATION = "education"
    HEALTH = "health"        # 건강기능식품·의료
    REALESTATE = "realestate"
    OTHER = "other"


FINANCIAL_CATEGORIES = {
    ProductCategory.LOAN,
    ProductCategory.CARD,
    ProductCategory.INSURANCE,
    ProductCategory.INVESTMENT,
    ProductCategory.SAVINGS,
    ProductCategory.CRYPTO,
}


class Fact(BaseModel):
    """광고주가 제공한 '사실'. 광고 문구의 모든 주장은 이 목록의 id 를 가리켜야 한다 (근거 결합)."""

    id: str
    text: str


class ProductFacts(BaseModel):
    name: str
    category: ProductCategory = ProductCategory.OTHER
    facts: list[Fact] = Field(default_factory=list)
    landing_url: Optional[str] = None
    # 상품 설명서·상세페이지 등 '신뢰할 수 없는' 자유 텍스트. 프롬프트 인젝션 1차 유입 경로.
    raw_description: str = ""

    def fact_ids(self) -> set[str]:
        return {f.id for f in self.facts}


class CampaignBrief(BaseModel):
    brief_id: str = Field(default_factory=lambda: new_id("brief"))
    advertiser_input: str  # 자연어 요청 ("20대 대상 신용대출, 월 300만원, 신청 전환")
    product: ProductFacts
    monthly_budget_krw: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# 1) 플래너
# ---------------------------------------------------------------------------
class Objective(str, Enum):
    AWARENESS = "awareness"
    TRAFFIC = "traffic"
    CONVERSION = "conversion"
    APP_INSTALL = "app_install"
    LEAD = "lead"


class AdSet(BaseModel):
    name: str
    audience: str
    placement: str
    budget_krw: int = Field(ge=0)
    bid_strategy: str
    rationale: str


class CampaignPlan(BaseModel):
    objective: Objective
    kpi: str
    total_budget_krw: int = Field(ge=0)
    ad_sets: list[AdSet] = Field(min_length=1)
    planner_reasoning: str
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    repaired: bool = False  # 1차 출력이 검증 실패해 수리된 경우 표시

    @model_validator(mode="after")
    def _budget_adds_up(self) -> "CampaignPlan":
        total = sum(a.budget_krw for a in self.ad_sets)
        if self.total_budget_krw and abs(total - self.total_budget_krw) > max(1, int(self.total_budget_krw * 0.01)):
            raise ValueError(f"ad set 예산 합({total}) 이 총 예산({self.total_budget_krw}) 과 1% 이상 다릅니다")
        return self


# ---------------------------------------------------------------------------
# 2) 소재
# ---------------------------------------------------------------------------
class Claim(BaseModel):
    text: str
    fact_id: Optional[str] = None  # None 이면 '근거 없는 주장' → grounding 가드레일이 잡는다


class AdCopy(BaseModel):
    variant_id: str = Field(default_factory=lambda: new_id("var"))
    headline: str
    body: str
    cta: str
    claims: list[Claim] = Field(default_factory=list)
    disclosures: list[str] = Field(default_factory=list)
    tone: str = ""

    def full_text(self) -> str:
        parts = [self.headline, self.body, self.cta, *self.disclosures]
        return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# 3) 가상 유저 패널
# ---------------------------------------------------------------------------
class PersonaFeedback(BaseModel):
    persona_id: str
    persona_name: str
    appeal: int = Field(ge=1, le=5)
    trust: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    would_click: bool
    concern: str
    quote: str


class AudienceReport(BaseModel):
    variant_id: str
    feedbacks: list[PersonaFeedback]
    mean_appeal: float
    mean_trust: float
    mean_clarity: float
    click_rate: float
    diversity: float  # 페르소나 간 점수 표준편차. 0 에 가까우면 '모두 같은 말' → 정보량 낮음(FM-15)
    sycophancy_flag: bool
    summary: str


# ---------------------------------------------------------------------------
# 4) 가드레일 / 심사
# ---------------------------------------------------------------------------
class Severity(str, Enum):
    BLOCK = "block"  # 반려
    FIX = "fix"      # 수정 후 재심사
    WARN = "warn"    # 주의 (승인 가능)


class Layer(str, Enum):
    INJECTION = "injection"    # 프롬프트 인젝션 탐지
    LEXICAL = "lexical"        # 결정론 규칙 (정규화 + 패턴)
    DISCLOSURE = "disclosure"  # 필수 고지 누락
    GROUNDING = "grounding"    # 근거 없는 주장·숫자
    SEMANTIC = "semantic"      # LLM 심사
    STRUCTURAL = "structural"  # 스키마·예산 등 구조 검증


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    layer: Layer
    quote: str = ""             # 원문 인용. 반드시 원문에 존재해야 한다(없으면 verified_quote=False)
    start: int = -1
    end: int = -1
    explanation: str = ""
    verified_quote: bool = True


class Decision(str, Enum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    REJECT = "REJECT"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ReviewVerdict(BaseModel):
    variant_id: str
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    findings: list[Finding] = Field(default_factory=list)
    rationale: str = ""
    proposed_rewrite: Optional[AdCopy] = None       # 모델이 '제안' 한 수정안. 재검사 전이므로 그 자체로는 신뢰하지 않는다 (PreflightReport.rewrite_status 참조)
    provider: str = ""
    model: str = ""
    unsupported_decision: bool = False           # 모델이 REVISE/REJECT 를 말했지만 남은 근거 finding 이 하나도 없음 → 자동 판정 금지
    second_opinion: Optional[Decision] = None   # 교차 검증(다른 제공자) 결과
    second_opinion_provider: str = ""
    second_opinion_independent: bool = True      # False 면 같은 제공자가 두 번 본 것 — 상관 오류를 줄이지 못한다 (PARTIAL 모드)
    disagreement: bool = False                   # 두 모델 계열의 판정 불일치 → HUMAN_REVIEW 근거


class InterceptKind(str, Enum):
    POLICY_INTENT = "policy_intent"          # 광고주 요청 자체가 정책 위반을 지시
    INJECTION = "injection"                  # 입력에 프롬프트 인젝션
    PROHIBITED_CATEGORY = "prohibited_category"  # 플랫폼 금지 업종


class InterceptNotice(BaseModel):
    """Safety Intercept 결과. 날것의 에러 대신 사용자에게 보여 줄 '가이드라인 준수 제안'."""

    triggered: bool = False
    kind: Optional[InterceptKind] = None
    hard_block: bool = False  # True 면 파이프라인 중단 (금지 업종·명백한 사행성)
    findings: list[Finding] = Field(default_factory=list)
    message: str = ""           # 부드러운 안내문
    suggested_input: str = ""   # 준수 방향으로 고쳐 쓴 요청
    removed_phrases: list[str] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    layer: Layer


class RewriteStatus(str, Enum):
    """자동 수정안의 상태. 원래 광고의 판정(final_decision)과는 별개로 관리한다."""

    NONE = "none"                      # 모델이 수정안을 내지 않음
    VERIFIED = "verified"              # 전체 가드레일 재검사를 통과 → 제시 가능
    FAILED_RECHECK = "failed_recheck"  # 재검사에서 다시 걸림 → 폐기, 제시하지 않음
    UNCHECKED = "unchecked"            # 재검사를 하지 못함 → 제시하지 않음


class PreflightReport(BaseModel):
    """소재 하나의 최종 사전점검 결과."""

    variant_id: str
    ad_copy: AdCopy
    audience: Optional[AudienceReport] = None
    verdict: ReviewVerdict
    final_decision: Decision
    checklist: list[ChecklistItem] = Field(default_factory=list)
    decision_reason: str = ""
    # 수정안은 재검사를 통과한 것만 verified_rewrite 에 들어간다. 어떤 final_decision 에서도 실패한 수정안은 여기 오지 않는다.
    rewrite_status: RewriteStatus = RewriteStatus.NONE
    verified_rewrite: Optional[AdCopy] = None
    rewrite_recheck_decision: Optional[Decision] = None
    rewrite_recheck_findings: list[Finding] = Field(default_factory=list)
    rewrite_note: str = ""


class FailureEvent(BaseModel):
    code: str           # FM-xx
    stage: str
    message: str
    fallback: str       # 어떤 대체 경로를 탔는지
    recoverable: bool = True
    at: datetime = Field(default_factory=utcnow)


class PipelineRun(BaseModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    mode: str
    brief: CampaignBrief
    effective_input: str = ""   # 인터셉트로 고쳐진 요청 (없으면 원문)
    intercept: InterceptNotice = Field(default_factory=InterceptNotice)
    plan: Optional[CampaignPlan] = None
    reports: list[PreflightReport] = Field(default_factory=list)
    failures: list[FailureEvent] = Field(default_factory=list)
    trace_id: str = ""
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    started_at: datetime = Field(default_factory=utcnow)
