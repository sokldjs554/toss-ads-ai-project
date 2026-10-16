"""토스애즈 공개 가이드(캠페인 생성·입찰 방식·광고 세트)에서 읽은 '구조 제약' 을 코드로 옮긴 것.

플래너의 출력은 이 제약에 맞아야 한다. 모델이 그럴듯하게 제안한 '전환 최대화 + 도달률' 같은 조합은 여기서 걸린다 (FM-02).
가이드가 바뀌면 이 파일만 바꾼다. 값은 2026-10 기준 공개 문서이며 실제 콘솔과 다를 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.schemas import Objective

# 목표 → 허용 입찰 방식 (가이드 '입찰 방식 설정하기' 표)
ALLOWED_BIDS: dict[Objective, list[str]] = {
    Objective.TRAFFIC: ["자동 입찰 클릭 최대화", "직접 입찰 CPC", "직접 입찰 CPM"],
    Objective.AWARENESS: ["직접 입찰 CPM"],
    Objective.LEAD: ["자동 입찰 전환 최대화", "자동 입찰 tCPA", "직접 입찰 CPC", "직접 입찰 CPM"],
    Objective.CONVERSION: ["자동 입찰 전환 최대화", "직접 입찰 CPC"],
    Objective.APP_INSTALL: ["자동 입찰 전환 최대화", "직접 입찰 CPC"],
}
OBJECTIVE_LABEL: dict[Objective, str] = {
    Objective.TRAFFIC: "방문 유도하기", Objective.AWARENESS: "도달률 높이기", Objective.LEAD: "잠재고객 모으기",
    Objective.CONVERSION: "구매 유도하기", Objective.APP_INSTALL: "앱 설치 유도하기",
}
# 입찰 단가 범위 (원) 와 단위
BID_RANGES: dict[str, tuple[int, int, int]] = {
    "자동 입찰 tCPA": (4_000, 500_000, 100),
    "직접 입찰 CPC": (200, 10_000, 10),
    "직접 입찰 CPM": (1_500, 100_000, 100),
}
CPM_ONLY_PLACEMENT = "보드 배너"
PLACEMENTS = ["리스트 배너", "보드 배너", "페이지 배너"]
MIN_AUDIENCE = 50_000
MIN_AGE = 19
MAX_RESPONSE_TARGETS = 3
MAX_CONVERSION_TARGETS = 10
MAX_CUSTOMER_LISTS = 1


@dataclass(frozen=True)
class PlanIssue:
    field: str
    message: str
    repaired: bool


def allowed_bids(obj: Objective) -> list[str]:
    return ALLOWED_BIDS.get(obj, ["직접 입찰 CPC"])


def spec_prompt() -> str:
    lines = ["[플랫폼 구조 제약 — 반드시 지킬 것]"]
    for obj, bids in ALLOWED_BIDS.items():
        lines.append(f"- 목표 '{OBJECTIVE_LABEL[obj]}'({obj.value}) 허용 입찰: {', '.join(bids)}")
    lines.append(f"- '직접 입찰 CPM' 은 {CPM_ONLY_PLACEMENT} 지면에서만 가능")
    lines.append("- 단가 범위: tCPA 4,000~500,000원(100원 단위), CPC 200~10,000원(10원 단위), CPM 1,500~100,000원(100원 단위)")
    lines.append(f"- 타겟은 만 {MIN_AGE}세 이상, 예상 타겟 규모 {MIN_AUDIENCE:,}명 이상이어야 집행 가능 (너무 좁은 타겟은 금지)")
    lines.append(f"- 광고 반응 타겟 최대 {MAX_RESPONSE_TARGETS}개, 전환 추적 타겟 최대 {MAX_CONVERSION_TARGETS}개, 고객 목록 최대 {MAX_CUSTOMER_LISTS}개")
    lines.append("- 금융 업종(대출·카드·보험·투자·예적금)은 준법 심의·심의필이 있어야 심사 승인 — risks 에 반드시 적을 것")
    lines.append("- ad_sets 의 budget_krw 합은 total_budget_krw 와 같아야 함")
    return "\n".join(lines)
