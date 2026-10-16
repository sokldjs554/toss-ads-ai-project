"""토큰 → 비용 환산표 (USD / 1M tokens). 공개 가격표 기준 근사치이며 실제 청구와 다를 수 있다.

모르는 모델은 보수적으로(비싼 쪽) 추정해 예산 가드(FM-20)가 느슨해지지 않게 한다.
"""
from __future__ import annotations

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    # Anthropic
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    # 대역
    "mock": (0.0, 0.0),
}
_CONSERVATIVE = (5.00, 25.00)


def price_for(model: str) -> tuple[float, float]:
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    for known, p in PRICES_PER_MTOK.items():  # 날짜 접미사 등 변형 모델명
        if model.startswith(known):
            return p
    return _CONSERVATIVE


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = price_for(model)
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 6)
