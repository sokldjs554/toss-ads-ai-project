"""VISION 대역 — 이미지 속을 읽지 못한다.

대역은 모델 성능이 아니다. 이미지를 보지 않으므로 글자를 읽을 수도, 시각 요소를 찾을 수도 없다.
- 요청의 context 에 declared_text(사용자가 직접 적은 글)가 있으면 그 글을 '받아 적은 것처럼' 돌려준다.
  (agents/vision.py 는 제공자가 mock 이면 이 글의 출처를 '선언한 글' 로 기록한다 — 이미지에서 읽은 글이 아니다.)
- 없으면 readable=False. 읽지 못했으므로 통과가 아니라 담당자 확인으로 이어진다.
결정론: 같은 context 면 같은 응답.
"""
from __future__ import annotations

import re

from llm.base import LLMRequest

MAX_DECLARED_SEGMENTS = 40


def handle(req: LLMRequest) -> dict:
    declared = str((req.context or {}).get("declared_text") or "").strip()
    if not declared:
        return {"readable": False, "text_segments": [], "tags": [],
                "notes": "예시 실행(대역): 이미지를 읽지 못했습니다. 키가 있을 때만 모델이 이미지 속 글을 읽습니다."}
    parts = [p.strip() for p in re.split(r"[\n\r]+", declared) if p.strip()][:MAX_DECLARED_SEGMENTS]
    return {"readable": True, "text_segments": [{"text": p, "region": "unknown"} for p in parts], "tags": [],
            "notes": "예시 실행(대역): 이미지를 읽지 않고 사용자가 적은 글을 그대로 돌려줬습니다."}
