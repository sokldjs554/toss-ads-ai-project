"""실행 설정.

환경변수 하나로 모드가 정해진다.
- 둘 다 있으면 LIVE: 구조화 작업 → OpenAI, 창의 작업 → Anthropic, 서로가 서로의 fallback.
- 하나만 있으면 PARTIAL: 있는 쪽이 전부를 맡는다 (라우터가 fallback 체인으로 처리).
- 둘 다 없으면 DEMO: 결정론적 대역(mock provider)이 LLM 단계를 수행한다. 가드레일·관측·평가 코드는 그대로 돈다.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum

try:  # .env 는 있으면 읽고 없으면 조용히 넘어간다
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv 미설치 환경
    pass


# 브라우저(WebAssembly·stlite) 실행판에서는 네트워크 SDK 를 쓸 수 없으므로 항상 DEMO 모드로 고정한다.
IN_BROWSER = sys.platform == "emscripten"


class Mode(str, Enum):
    LIVE = "live"
    PARTIAL = "partial"
    DEMO = "demo"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    anthropic_api_key: str
    openai_model: str
    anthropic_model: str
    request_timeout_s: float
    max_retries: int
    run_budget_usd: float
    auto_decision_confidence: float
    force_demo: bool

    @classmethod
    def from_env(cls, **overrides: object) -> "Settings":
        base = dict(
            openai_api_key=_env("OPENAI_API_KEY"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            openai_model=_env("OPENAI_MODEL", "gpt-4o"),
            anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            request_timeout_s=float(_env("PREFLIGHT_REQUEST_TIMEOUT_S", "45")),
            max_retries=int(_env("PREFLIGHT_MAX_RETRIES", "2")),
            run_budget_usd=float(_env("PREFLIGHT_RUN_BUDGET_USD", "0.50")),
            auto_decision_confidence=float(_env("PREFLIGHT_AUTO_DECISION_CONFIDENCE", "0.80")),
            force_demo=_env("PREFLIGHT_FORCE_DEMO", "0") in {"1", "true", "yes"} or IN_BROWSER,
        )
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key) and not self.force_demo

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key) and not self.force_demo

    @property
    def mode(self) -> Mode:
        if self.has_openai and self.has_anthropic:
            return Mode.LIVE
        if self.has_openai or self.has_anthropic:
            return Mode.PARTIAL
        return Mode.DEMO
