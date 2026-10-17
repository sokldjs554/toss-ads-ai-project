"""화면 문구를 한곳에서 관리한다. 내부 식별자(APPROVE, GRND-NUM-01, coverage …)는 코드에 그대로 두고, 사람에게 보일 말만 여기서 바꾼다.

원칙
- 광고를 만드는 사람이 읽는 말: '오탐' 대신 '문제없는 광고에 수정이나 게재 제한을 요청한 비율'.
- '게재 불가'·'점검 통과' 는 이 프로젝트의 검사 기준에 따른 결과다. 실제 플랫폼 심사·집행 승인이 아니다 (DISCLAIMER).
- 비율은 건수와 함께 풀어 쓴다. 분모가 0 이면 '대상 없음'.
"""
from __future__ import annotations

from core.schemas import Decision, Layer, RewriteStatus, Severity

PROJECT_NAME = "Preflight"
DISCLAIMER = ("개인 프로젝트입니다. 이 화면의 '점검 통과'·'게재 불가' 는 이 프로젝트가 공개 자료로 재구성한 검사 기준에 따른 결과이며, "
              "실제 광고 플랫폼의 심사나 집행 승인이 아닙니다. 모든 상품·수치는 가상입니다.")

# --- 판정 ------------------------------------------------------------------
DECISION_LABEL: dict[Decision, str] = {
    Decision.APPROVE: "점검 통과",
    Decision.REVISE: "수정 필요",
    Decision.REJECT: "게재 불가",
    Decision.HUMAN_REVIEW: "담당자 확인 필요",
}
DECISION_SHORT: dict[Decision, str] = {Decision.APPROVE: "통과", Decision.REVISE: "수정", Decision.REJECT: "불가", Decision.HUMAN_REVIEW: "확인"}
DECISION_HELP: dict[Decision, str] = {
    Decision.APPROVE: "이 프로젝트의 검사 기준에서 문제가 발견되지 않았습니다. 실제 플랫폼 심사 결과가 아닙니다.",
    Decision.REVISE: "확인이 필요한 구절이 있어 고친 뒤 다시 검사해야 합니다.",
    Decision.REJECT: "검사 기준상 그대로는 게재할 수 없는 표현이 있습니다. 표현을 바꾼 뒤 다시 검사하세요.",
    Decision.HUMAN_REVIEW: "자동으로 확정하지 못했습니다. 담당자가 아래 내용을 확인해야 다음 단계로 갈 수 있습니다.",
}
# 색은 보조 신호다. 결과는 항상 문구로도 구분한다.
DECISION_STYLE: dict[Decision, tuple[str, str]] = {   # (글자색, 배경색) — 흰 배경 대비 4.5:1 이상
    Decision.APPROVE: ("#0B6B37", "#E6F4EC"),
    Decision.REVISE: ("#8A5A00", "#FFF3D6"),
    Decision.REJECT: ("#A32116", "#FCE8E6"),
    Decision.HUMAN_REVIEW: ("#334155", "#E8EDF3"),
}
NEXT_ACTION: dict[Decision, str] = {
    Decision.APPROVE: "이 광고안은 그대로 사용할 수 있습니다. 다른 광고안도 확인해 보세요.",
    Decision.REVISE: "아래 '수정 제안' 을 참고해 문구를 고치고 '수정한 문구 다시 검사' 를 누르세요.",
    Decision.REJECT: "표시된 표현을 빼거나 바꾼 뒤 다시 검사하세요. 같은 뜻의 다른 표현도 같은 기준으로 검사됩니다.",
    Decision.HUMAN_REVIEW: "담당자에게 아래 '확인 요청 내용' 을 전달하세요. 확인 전에는 게재할 수 없습니다.",
}

# --- 수정안 상태 -------------------------------------------------------------
REWRITE_STATUS_LABEL: dict[RewriteStatus, str] = {
    RewriteStatus.NONE: "자동 수정 제안 없음",
    RewriteStatus.VERIFIED: "검사를 통과한 수정 제안",
    RewriteStatus.FAILED_RECHECK: "자동 수정안이 재검사를 통과하지 못해 제시하지 않음",
    RewriteStatus.UNCHECKED: "자동 수정안을 재검사하지 못해 제시하지 않음",
}

# --- 검사 계층·심각도 -------------------------------------------------------
LAYER_LABEL: dict[Layer, str] = {
    Layer.INJECTION: "심사 조작 문장 검사",
    Layer.LEXICAL: "표현 규칙 검사",
    Layer.DISCLOSURE: "필수 안내 문구 검사",
    Layer.GROUNDING: "상품 정보와 일치하는지 확인",
    Layer.SEMANTIC: "검토 모델의 의미 검사",
    Layer.STRUCTURAL: "형식 검사",
}
SEVERITY_LABEL: dict[Severity, str] = {Severity.BLOCK: "게재 불가 사유", Severity.FIX: "수정 필요", Severity.WARN: "참고"}
SEVERITY_STYLE: dict[Severity, str] = {Severity.BLOCK: "#A32116", Severity.FIX: "#8A5A00", Severity.WARN: "#475467"}

# --- 실행 방식 ----------------------------------------------------------------
RUN_MODE_LABEL = {
    "demo": "예시 실행 · 실제 AI 호출 없음",
    "partial": "실제 AI 호출 · 한 제공자",
    "live": "실제 AI 호출 · 두 제공자",
    "stored": "저장된 실행 결과",
}
RUN_MODE_HELP = {
    "demo": "API 키가 없어 모델 단계(캠페인 구성·문구 작성·가상 반응·의미 검사)는 정해진 규칙대로 움직이는 대역이 맡습니다. 표현·안내·상품 정보 검사와 최종 판정은 실제 코드가 그대로 돕니다. 이 결과는 모델 성능이 아닙니다.",
    "partial": "한 제공자의 키만 있어 그 모델이 모든 단계를 맡습니다. 두 모델 계열의 교차 확인은 되지 않습니다.",
    "live": "구조화 작업은 OpenAI, 문구·가상 반응은 Anthropic 모델이 맡고 서로의 대체 경로가 됩니다.",
    "stored": "과거에 실제 모델로 실행해 저장한 기록입니다. 지금 다시 호출한 것이 아닙니다.",
}

# --- 체크리스트 이름(코드 내부 이름 → 화면) ----------------------------------
CHECK_NAME = {
    "인젝션·심사 조작 문구 없음": "심사 조작 문장 없음",
    "금지·주의 표현 (정규화 후 패턴)": "표현 규칙",
    "업종별 필수 고지": "필수 안내 문구",
    "숫자·주장의 근거 (사실 목록)": "숫자·주장이 상품 정보와 일치",
    "랜딩 페이지 표현 일치": "랜딩 페이지와 표현 일치",
    "길이·언어 형식": "글자 수·한글 표기",
    "반려 소재 유사 재등록 아님": "이전에 게재 불가였던 광고와 다름",
    "의미 심사 (LLM)": "검토 모델의 의미 검사",
    "교차 검증 (다른 모델 계열)": "다른 모델 계열의 교차 확인",
    "자동 수정안 재검사": "자동 수정안 재검사",
    "최종 판정": "최종 판정",
}

# --- 평가 지표 ----------------------------------------------------------------
METRIC = {
    "coverage": ("담당자 확인 없이 판단한 비율", "통과·수정 요청·게재 불가를 모두 포함합니다. '자동 승인율' 이 아닙니다."),
    "unsafe_approve": ("수정이나 게재 제한이 필요한 광고를 통과시킨 비율", "가장 중요한 안전 지표. 해당 평가에서 관측된 값이며 모든 광고에 대한 보장이 아닙니다."),
    "false_flag": ("문제없는 광고에 수정이나 게재 제한을 요청한 비율", "과한 요청은 광고주가 떠나는 이유가 됩니다."),
    "human_review": ("담당자 확인으로 넘긴 비율", "자동으로 확정하지 못한 비율."),
    "benign_correct_pass": ("문제없는 광고를 올바르게 통과시킨 비율", "담당자 확인으로 넘긴 것은 '올바른 통과' 로 세지 않습니다."),
    "benign_human_review": ("문제없는 광고를 담당자 확인으로 넘긴 비율", "수정 요청(오탐)은 아니지만 자동으로 통과시키지도 못한 경우."),
    "accuracy_decided": ("자동 판단 중 정답과 같은 등급인 비율", ""),
    "high_confidence_error": ("확신 0.8 이상인데 틀린 비율", "검토 모델이 말한 확신이 믿을 만한지 보는 값."),
}
TERM = {
    "grounding": "상품 정보와 일치하는지 확인",
    "guardrail": "검사 기준",
    "trace": "처리 기록",
    "planner": "캠페인 구성",
    "red_team": "우회 표현 검사",
    "hallucinated_quote": "원문에 없는 내용을 근거로 제시",
    "confidence": "검토 모델의 확신 정도",
    "second_opinion": "다른 모델 계열의 교차 확인",
    "audience": "가상 사용자 반응 (모델이 만든 가상 의견이며 실제 사용자 조사가 아닙니다)",
}


def decision_label(d: Decision) -> str:
    return DECISION_LABEL[d]


def fraction_sentence(k: int, n: int, what: str, of_what: str) -> str:
    """'전체 55건 중 52건은 담당자 확인 없이 판단했습니다.' 꼴. 분모 0 → '대상 없음'."""
    if n == 0:
        return f"{of_what}: 대상 없음"
    return f"{of_what} {n}건 중 {k}건{what}"


def pct(k: int, n: int) -> str:
    if n == 0:
        return "대상 없음"
    return f"{k / n:.0%} ({k}/{n})"


def check_name(internal: str) -> str:
    return CHECK_NAME.get(internal, internal)
