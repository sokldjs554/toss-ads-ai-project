from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from pydantic import BaseModel


class Task(str, Enum):
    PLAN = "plan"            # 캠페인 구조 (구조화 → OpenAI)
    COPY = "copy"            # 광고 문구 (창의 → Claude)
    AUDIENCE = "audience"    # 가상 유저 피드백 (정성 → Claude)
    REVIEW = "review"        # 심사 (구조화 → OpenAI)
    REDIRECT = "redirect"    # 준수 제안문 (창의 → Claude)
    REPAIR = "repair"        # 텍스트 → 스키마 정규화 (구조화 → OpenAI)
    ATTACK = "attack"        # 레드팀 공격 소재 (창의 → Claude)
    JUDGE = "judge"          # 품질 쌍대 비교 (구조화 → OpenAI)
    SECOND_OPINION = "second_opinion"  # 교차 검증 심사 (다른 계열 → Claude)


# 작업별 제공자 우선순위. 앞이 1순위, 뒤가 fallback. 'mock' 은 키가 없을 때만 붙는다.
ROUTING: dict[Task, list[str]] = {
    Task.PLAN: ["openai", "anthropic"],
    Task.COPY: ["anthropic", "openai"],
    Task.AUDIENCE: ["anthropic", "openai"],
    Task.REVIEW: ["openai", "anthropic"],
    Task.REDIRECT: ["anthropic", "openai"],
    Task.REPAIR: ["openai", "anthropic"],
    Task.ATTACK: ["anthropic", "openai"],
    Task.JUDGE: ["openai", "anthropic"],
    Task.SECOND_OPINION: ["anthropic", "openai"],
}

# 창의 작업은 제약 디코딩 없이(자유 텍스트 → 사후 정규화) 돌린다. 형식 제약이 창의성·추론을 떨어뜨린다는
# 보고(“Let Me Speak Freely?”, Tam et al. 2024)를 따른 선택이며, 그 대가(FM-01)는 수리 경로가 진다.
UNCONSTRAINED_TASKS = {Task.COPY, Task.AUDIENCE, Task.REDIRECT, Task.ATTACK}


@dataclass
class LLMRequest:
    task: Task
    system: str
    user: str
    schema: Optional[type[BaseModel]] = None
    max_tokens: int = 2048
    temperature: float = 0.7
    context: dict[str, Any] = field(default_factory=dict)  # mock 제공자와 트레이스용 구조화 입력
    label: str = ""  # 트레이스에 보일 이름 (예: review:var_ab12)

    @property
    def constrained(self) -> bool:
        return self.schema is not None and self.task not in UNCONSTRAINED_TASKS


@dataclass
class LLMResponse:
    text: str
    parsed: Optional[BaseModel]
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    finish_reason: str = ""
    fallback_chain: list[str] = field(default_factory=list)
    repaired: bool = False


class Provider(Protocol):
    name: str
    model: str

    @property
    def available(self) -> bool: ...

    def complete(self, req: LLMRequest) -> LLMResponse: ...
