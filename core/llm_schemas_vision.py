"""이미지 읽기(Task.VISION) 모델 출력 스키마.

규칙은 core/llm_schemas.py 와 같다 (OpenAI Structured Outputs strict 호환): 모든 필드 required, 기본값·범위·pattern 제약 없음,
닫힌 어휘는 Literal 로 고정 — 모델이 어휘 밖의 태그를 만들어 낼 수 없다. 값 수준 검증(글자 수 상한·중복 제거·인젝션 검사)은 agents/vision.py 가 한다.

모델은 이 스키마로 '이미지에서 본 것' 을 받아 적기만 한다. 판정·평가·위반 여부 판단을 요구하지 않는다.
"""
from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel

# 시각 요소 태그의 닫힌 어휘. 모델이 '봤다' 고 말하는 것이며 코드가 검증할 수 없다 — 그래서 이 태그만으로 게재 불가를 내지 않는다 (guardrails/image_checks.IMAGE_TAG_RULES).
VisionTag = Literal[
    "money_cash", "celebrity_face", "before_after", "minor_person", "competitor_logo",
    "government_or_bank_logo", "qr_code", "countdown_timer", "medical_imagery", "excessive_text",
]
VISION_TAGS: tuple = get_args(VisionTag)

Region = Literal["top_left", "top", "top_right", "left", "center", "right", "bottom_left", "bottom", "bottom_right", "unknown"]
REGIONS: tuple = get_args(Region)


class VisionSegmentOut(BaseModel):
    text: str        # 이미지에 보이는 글자를 그대로 받아 적은 것 (고치거나 요약하지 않는다)
    region: Region   # 이미지 안의 대략적인 위치


class VisionOut(BaseModel):
    readable: bool                          # 이미지를 읽을 수 있었는가 (너무 작거나 흐리거나 열 수 없으면 false)
    text_segments: list[VisionSegmentOut]   # 글자 덩어리들 (읽을 글이 없으면 빈 목록)
    tags: list[VisionTag]                   # 보이는 시각 요소 (없으면 빈 목록)
    notes: str                              # 읽기에 관한 짧은 메모 (예: 일부가 흐려서 못 읽음)
